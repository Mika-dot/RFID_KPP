"""Bounded adaptive windows for KPP-to-Warehouse reconciliation.

The model is deliberately a *candidate generator*.  It learns only from
confirmed physical KPP events that already have Warehouse evidence and emits a
one-sided interval in which a missed KPP event may have happened.  It never
turns a Warehouse row into proof that an RFID reader saw the reel and it never
rewrites business rows by itself.

The first layer is robust distribution fitting (quantiles with a safety
margin).  An optional, deterministic gradient-descent step can tune a bucket
when both positive and negative labelled examples are available in an offline
audit.  Production use should promote an optimized profile only after the
replay/shadow gates accept it.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable, Mapping, Optional


MODEL_VERSION = 1
DEFAULT_LOWER_SEC = 0.0
DEFAULT_UPPER_SEC = 24.0 * 3600.0
DEFAULT_HARD_MAX_SEC = 7.0 * 24.0 * 3600.0
DEFAULT_MARGIN_SEC = 30.0
DEFAULT_MIN_SAMPLES = 8


def _finite_nonnegative(value: object) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result


def normalize_feature(value: object, default: str = "UNKNOWN") -> str:
    text = str(value or "").strip().upper()
    if not text:
        return default
    # Keep the model key compact and prevent a source value from creating a
    # path-like or delimiter-containing key.
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in text)[:64] or default


def season_for(at: datetime) -> str:
    if at.month in (12, 1, 2):
        return "WINTER"
    if at.month in (3, 4, 5):
        return "SPRING"
    if at.month in (6, 7, 8):
        return "SUMMER"
    return "AUTUMN"


def daypart_for(at: datetime) -> str:
    if at.hour < 6:
        return "NIGHT"
    if at.hour < 12:
        return "MORNING"
    if at.hour < 18:
        return "DAY"
    return "EVENING"


def _bucket_hierarchy(at: datetime, transport: object) -> list[str]:
    """Return most-specific to broadest comparable distribution buckets."""
    forklift = normalize_feature(transport)
    season = season_for(at)
    daypart = daypart_for(at)
    weekday = at.weekday()
    return [
        f"transport={forklift}|season={season}|daypart={daypart}|weekday={weekday}",
        f"transport={forklift}|season={season}|daypart={daypart}",
        f"season={season}|daypart={daypart}",
        "global",
    ]


def bucket_keys(at: datetime, transport: object = "UNKNOWN") -> list[str]:
    return _bucket_hierarchy(at, transport)


@dataclass(frozen=True)
class TravelObservation:
    """One already-confirmed KPP -> Warehouse pair."""

    kpp_at: datetime
    warehouse_at: datetime
    transport: str = "UNKNOWN"
    event_id: Optional[int] = None
    warehouse_id: Optional[int] = None
    confirmed: bool = True

    @property
    def travel_seconds(self) -> Optional[float]:
        value = _finite_nonnegative((self.warehouse_at - self.kpp_at).total_seconds())
        return value

    @property
    def key(self) -> str:
        if self.event_id is not None and self.warehouse_id is not None:
            return f"event:{int(self.event_id)}|warehouse:{int(self.warehouse_id)}"
        raw = "|".join(
            (
                self.kpp_at.isoformat(),
                self.warehouse_at.isoformat(),
                normalize_feature(self.transport),
            )
        )
        return "sample:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class WindowBounds:
    lower_sec: float
    upper_sec: float
    center_sec: float
    sample_count: int
    bucket: str
    confidence: str

    def backward_interval(self, warehouse_at: datetime) -> tuple[datetime, datetime]:
        """Return [earliest, latest] possible KPP timestamps."""
        return (
            warehouse_at - timedelta(seconds=self.upper_sec),
            warehouse_at - timedelta(seconds=self.lower_sec),
        )


@dataclass
class _Distribution:
    samples: list[float] = field(default_factory=list)

    def add(self, value: float, limit: int = 2048) -> None:
        self.samples.append(float(value))
        if len(self.samples) > limit:
            # Keep recent history while retaining a deterministic bounded store.
            self.samples = self.samples[-limit:]

    def quantile(self, q: float) -> float:
        if not self.samples:
            raise ValueError("EmptyDistribution")
        values = sorted(self.samples)
        position = (len(values) - 1) * min(1.0, max(0.0, q))
        left = int(math.floor(position))
        right = int(math.ceil(position))
        if left == right:
            return values[left]
        weight = position - left
        return values[left] * (1.0 - weight) + values[right] * weight

    @property
    def count(self) -> int:
        return len(self.samples)

    def to_dict(self) -> dict[str, Any]:
        return {"samples": list(self.samples)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "_Distribution":
        samples = []
        for raw in value.get("samples", []):
            item = _finite_nonnegative(raw)
            if item is not None:
                samples.append(item)
        return cls(samples[-2048:])


class AdaptiveWindowModel:
    """A bounded, serializable travel-time model.

    ``min_samples`` prevents a single fast or slow trip from changing a
    production window.  ``hard_max_sec`` prevents a corrupted timestamp from
    expanding the search indefinitely.  ``seen`` makes repeated bootstrap
    scans idempotent.
    """

    def __init__(
        self,
        *,
        default_lower_sec: float = DEFAULT_LOWER_SEC,
        default_upper_sec: float = DEFAULT_UPPER_SEC,
        hard_max_sec: float = DEFAULT_HARD_MAX_SEC,
        margin_sec: float = DEFAULT_MARGIN_SEC,
        min_samples: int = DEFAULT_MIN_SAMPLES,
    ) -> None:
        values = [float(x) for x in (default_lower_sec, default_upper_sec, hard_max_sec, margin_sec)]
        if not all(math.isfinite(x) and x >= 0 for x in values):
            raise ValueError("AdaptiveWindowConfigurationInvalid")
        self.default_lower_sec, self.default_upper_sec, self.hard_max_sec, self.margin_sec = values
        if not 0 <= self.default_lower_sec < self.default_upper_sec <= self.hard_max_sec:
            raise ValueError("AdaptiveWindowConfigurationInvalid")
        if int(min_samples) < 1:
            raise ValueError("AdaptiveWindowConfigurationInvalid")
        self.min_samples = int(min_samples)
        self.buckets: dict[str, _Distribution] = {}
        self.seen: list[str] = []
        self.optimized: dict[str, dict[str, float]] = {}

    def _remember(self, key: str) -> bool:
        if key in self.seen:
            return False
        self.seen.append(key)
        if len(self.seen) > 10000:
            self.seen = self.seen[-10000:]
        return True

    def observe(self, observation: TravelObservation) -> bool:
        """Add a confirmed pair; return False for invalid/unconfirmed data."""
        if not observation.confirmed:
            return False
        travel = observation.travel_seconds
        if travel is None or travel > self.hard_max_sec or not self._remember(observation.key):
            return False
        for bucket in _bucket_hierarchy(observation.warehouse_at, observation.transport):
            self.buckets.setdefault(bucket, _Distribution()).add(travel)
        return True

    def _choose(self, at: datetime, transport: object) -> tuple[str, _Distribution] | None:
        for bucket in _bucket_hierarchy(at, transport):
            distribution = self.buckets.get(bucket)
            optimized = self.optimized.get(bucket)
            optimized_count = int(optimized.get("sample_count", 0)) if optimized else 0
            if (
                distribution is not None
                and (distribution.count >= self.min_samples or optimized_count >= self.min_samples)
            ):
                return bucket, distribution
            if distribution is None and optimized_count >= self.min_samples:
                return bucket, _Distribution()
        return None

    def bounds_for(self, warehouse_at: datetime, transport: object = "UNKNOWN") -> WindowBounds:
        selected = self._choose(warehouse_at, transport)
        if selected is None:
            return WindowBounds(
                self.default_lower_sec,
                self.default_upper_sec,
                (self.default_lower_sec + self.default_upper_sec) / 2.0,
                0,
                "default",
                "cold_start",
            )
        bucket, distribution = selected
        optimized = self.optimized.get(bucket)
        if optimized and optimized.get("sample_count", 0) >= self.min_samples:
            center = float(optimized["center_sec"])
            half = float(optimized["half_width_sec"])
            lower = max(0.0, center - half - self.margin_sec)
            upper = min(self.hard_max_sec, center + half + self.margin_sec)
            confidence = "optimized"
        else:
            lower = max(0.0, distribution.quantile(0.05) - self.margin_sec)
            upper = min(self.hard_max_sec, distribution.quantile(0.95) + self.margin_sec)
            center = distribution.quantile(0.50)
            confidence = "quantile"
        if upper <= lower:
            upper = min(self.hard_max_sec, lower + max(60.0, self.margin_sec * 2.0))
        count = int(optimized["sample_count"]) if confidence == "optimized" else distribution.count
        return WindowBounds(lower, upper, center, count, bucket, confidence)

    def backward_interval(
        self, warehouse_at: datetime, transport: object = "UNKNOWN"
    ) -> tuple[datetime, datetime, WindowBounds]:
        bounds = self.bounds_for(warehouse_at, transport)
        start, end = bounds.backward_interval(warehouse_at)
        return start, end, bounds

    def fit_gradient_descent(
        self,
        positives: Iterable[float],
        negatives: Iterable[float],
        *,
        bucket: str = "global",
        steps: int = 250,
        learning_rate: float = 0.03,
        temperature_sec: float = 30.0,
        regularization: float = 0.001,
    ) -> dict[str, float]:
        """Tune a centre/half-width using labelled deltas.

        The optimizer is intentionally small and auditable.  Positive examples
        are confirmed KPP→Warehouse links; negatives are candidate pairs
        rejected by a stronger identity or a versioned replay.  It is not run
        automatically on unlabelled Warehouse-only rows.
        """
        pos = [x for x in (_finite_nonnegative(v) for v in positives) if x is not None and x <= self.hard_max_sec]
        neg = [x for x in (_finite_nonnegative(v) for v in negatives) if x is not None and x <= self.hard_max_sec]
        if not pos or not neg:
            raise ValueError("GradientDescentNeedsPositiveAndNegativeExamples")
        if not bucket or any(not math.isfinite(float(x)) for x in (learning_rate, temperature_sec, regularization)):
            raise ValueError("GradientDescentConfigurationInvalid")
        if learning_rate <= 0 or temperature_sec <= 0 or regularization < 0 or not 1 <= int(steps) <= 10000:
            raise ValueError("GradientDescentConfigurationInvalid")
        center = float(median(pos))
        p05 = sorted(pos)[max(0, int(len(pos) * 0.05) - 1)]
        p95 = sorted(pos)[min(len(pos) - 1, int(len(pos) * 0.95))]
        half = max(30.0, (p95 - p05) / 2.0)
        prior_center, prior_half = center, half
        temperature = max(1.0, float(temperature_sec))
        examples = [(x, 1.0) for x in pos] + [(x, 0.0) for x in neg]

        def loss(c: float, h: float) -> float:
            value = 0.0
            for x, target in examples:
                z = (h - abs(x - c)) / temperature
                probability = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))
                value -= target * math.log(max(probability, 1e-12))
                value -= (1.0 - target) * math.log(max(1.0 - probability, 1e-12))
            value /= len(examples)
            value += regularization * ((c - prior_center) ** 2 + (h - prior_half) ** 2) / max(self.hard_max_sec, 1.0)
            return value

        initial_loss = loss(center, half)
        for _ in range(max(1, int(steps))):
            grad_c = 0.0
            grad_h = 0.0
            for x, target in examples:
                difference = x - center
                sign = 1.0 if difference > 0 else -1.0 if difference < 0 else 0.0
                z = (half - abs(difference)) / temperature
                probability = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))
                residual = (probability - target) / temperature
                grad_c += residual * sign
                grad_h += residual
            grad_c = grad_c / len(examples) + regularization * 2.0 * (center - prior_center) / max(self.hard_max_sec, 1.0)
            grad_h = grad_h / len(examples) + regularization * 2.0 * (half - prior_half) / max(self.hard_max_sec, 1.0)
            center = min(self.hard_max_sec, max(0.0, center - learning_rate * grad_c * temperature))
            half = min(self.hard_max_sec, max(30.0, half - learning_rate * grad_h * temperature))

        result = {
            "center_sec": center,
            "half_width_sec": half,
            "lower_sec": max(0.0, center - half),
            "upper_sec": min(self.hard_max_sec, center + half),
            "sample_count": float(len(pos)),
            "negative_count": float(len(neg)),
            "initial_loss": initial_loss,
            "final_loss": loss(center, half),
        }
        self.optimized[bucket] = result
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MODEL_VERSION,
            "defaults": {
                "lower_sec": self.default_lower_sec,
                "upper_sec": self.default_upper_sec,
                "hard_max_sec": self.hard_max_sec,
                "margin_sec": self.margin_sec,
                "min_samples": self.min_samples,
            },
            "buckets": {key: value.to_dict() for key, value in self.buckets.items()},
            "seen": list(self.seen),
            "optimized": self.optimized,
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdaptiveWindowModel":
        if int(value.get("version", MODEL_VERSION)) != MODEL_VERSION:
            raise ValueError("AdaptiveWindowModelVersion")
        defaults = value.get("defaults", {})
        model = cls(
            default_lower_sec=float(defaults.get("lower_sec", DEFAULT_LOWER_SEC)),
            default_upper_sec=float(defaults.get("upper_sec", DEFAULT_UPPER_SEC)),
            hard_max_sec=float(defaults.get("hard_max_sec", DEFAULT_HARD_MAX_SEC)),
            margin_sec=float(defaults.get("margin_sec", DEFAULT_MARGIN_SEC)),
            min_samples=int(defaults.get("min_samples", DEFAULT_MIN_SAMPLES)),
        )
        for key, raw in dict(value.get("buckets", {})).items():
            if isinstance(raw, Mapping):
                distribution = _Distribution.from_dict(raw)
                if any(sample > model.hard_max_sec for sample in distribution.samples):
                    raise ValueError("AdaptiveWindowSampleOutOfBounds")
                model.buckets[str(key)] = distribution
        model.seen = [str(item) for item in list(value.get("seen", []))[-10000:]]
        optimized = value.get("optimized", {})
        if isinstance(optimized, Mapping):
            model.optimized = {str(k): dict(v) for k, v in optimized.items() if isinstance(v, Mapping)}
            for profile in model.optimized.values():
                for name in ("center_sec", "half_width_sec", "sample_count", "negative_count", "initial_loss", "final_loss"):
                    number = _finite_nonnegative(profile.get(name))
                    if number is None:
                        raise ValueError("AdaptiveWindowProfileInvalid")
                if (profile["center_sec"] > model.hard_max_sec or profile["half_width_sec"] > model.hard_max_sec
                        or profile["sample_count"] < 1 or profile["negative_count"] < 1):
                    raise ValueError("AdaptiveWindowProfileInvalid")
        return model

    @classmethod
    def loads(cls, raw: str) -> "AdaptiveWindowModel":
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            raise ValueError("AdaptiveWindowModelPayload")
        return cls.from_dict(value)
