"""Train and validate a travel-window candidate from an operator-labelled export.

No SQL connection, no production writes. Train/holdout split is chronological;
negative labels must be rejected identity links, never guessed missed reads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from common.adaptive_windows import AdaptiveWindowModel, TravelObservation, bucket_keys


def model_hash(model):
    return hashlib.sha256(model.dumps().encode()).hexdigest()


def metrics(model, rows, baseline=False, legacy_window_hours=24):
    tp = fp = positives = negatives = 0
    for row in rows:
        seconds = row["observation"].travel_seconds
        # The matcher has no trusted transport before selecting a passage.
        # Qualify the UNKNOWN hierarchy it actually uses, never labelled transport.
        bounds = model.bounds_for(row["observation"].warehouse_at, "UNKNOWN")
        accepted = seconds <= legacy_window_hours * 3600 if baseline or bounds.confidence == "cold_start" else bounds.lower_sec <= seconds <= bounds.upper_sec
        if row["label"] == "confirmed":
            positives += 1
            tp += int(accepted)
        else:
            negatives += 1
            fp += int(accepted)
    return {"recall": tp / positives if positives else None,
            "false_positive_rate": fp / negatives if negatives else None,
            "true_positive": tp, "false_positive": fp,
            "positives": positives, "negatives": negatives}


def train(rows, min_samples=8, margin_sec=30, legacy_window_hours=24):
    if not math.isfinite(legacy_window_hours) or legacy_window_hours <= 0:
        raise ValueError("LegacyWindowInvalid")
    prepared, seen = [], set()
    for row in rows:
        if row.get("label") not in {"confirmed", "rejected"}:
            raise ValueError("OperatorLabelsRequired")
        observation = TravelObservation(
            datetime.fromisoformat(row["kpp_at"]), datetime.fromisoformat(row["warehouse_at"]),
            str(row.get("transport", "UNKNOWN")), int(row["event_id"]), int(row["warehouse_id"]),
            confirmed=row["label"] == "confirmed")
        # A pair cannot occur in both train and holdout, even with two labels.
        if observation.key in seen:
            raise ValueError("DuplicateTrainingIdentity")
        seen.add(observation.key)
        if observation.travel_seconds is None or observation.travel_seconds > 604800:
            raise ValueError("TrainingTimeOutOfBounds")
        prepared.append({"label": row["label"], "observation": observation})
    prepared.sort(key=lambda item: (item["observation"].warehouse_at, item["observation"].key))
    split = int(len(prepared) * .7)
    training, validation = prepared[:split], prepared[split:]
    if any(sum(row["label"] == label for row in group) < min_samples
           for group in (training, validation) for label in ("confirmed", "rejected")):
        raise ValueError("IndependentTrainAndHoldoutLabelsRequired")
    model = AdaptiveWindowModel(min_samples=min_samples, margin_sec=margin_sec)
    examples = defaultdict(lambda: {"confirmed": [], "rejected": []})
    for row in training:
        observation = row["observation"]
        if row["label"] == "confirmed":
            model.observe(observation)
        for key in bucket_keys(observation.warehouse_at, observation.transport):
            examples[key][row["label"]].append(observation.travel_seconds)
    optimized = []
    for key, labels in examples.items():
        if all(len(labels[label]) >= min_samples for label in ("confirmed", "rejected")):
            result = model.fit_gradient_descent(labels["confirmed"], labels["rejected"],
                                                bucket=key, steps=1000, learning_rate=1)
            optimized.append({"bucket": key, **result})
    baseline = metrics(model, validation, baseline=True, legacy_window_hours=legacy_window_hours)
    candidate = metrics(model, validation, legacy_window_hours=legacy_window_hours)
    accepted = candidate["recall"] >= max(.95, baseline["recall"]) and candidate["false_positive_rate"] <= min(.05, baseline["false_positive_rate"])
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"version": 2, "model": model.to_dict(), "model_sha256": model_hash(model),
            "legacy_window_hours": legacy_window_hours, "matching_transport": "UNKNOWN",
            "source_sha256": digest, "split": "chronological_70_30",
            "training_samples": len(training), "validation_samples": len(validation),
            "optimized_buckets": optimized, "baseline": baseline, "candidate": candidate,
            "gate": {"status": "accepted" if accepted else "rejected",
                     "minimum_recall": .95, "maximum_false_positive_rate": .05,
                     "no_recall_regression": True}}


def load_accepted_profile(path, legacy_window_hours=24):
    if not path:
        raise ValueError("ActiveAdaptiveProfileRequired")
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("version") != 2 or value.get("gate", {}).get("status") != "accepted":
        raise ValueError("AdaptiveProfileNotAccepted")
    if (value.get("legacy_window_hours") != legacy_window_hours
            or value.get("matching_transport") != "UNKNOWN"):
        raise ValueError("AdaptiveProfileRuntimeMismatch")
    model = AdaptiveWindowModel.from_dict(value["model"])
    if value.get("model_sha256") != model_hash(model):
        raise ValueError("AdaptiveProfileHashMismatch")
    baseline, candidate = value.get("baseline", {}), value.get("candidate", {})
    try:
        valid = (candidate["recall"] >= max(.95, baseline["recall"])
                 and candidate["false_positive_rate"] <= min(.05, baseline["false_positive_rate"])
                 and candidate["positives"] >= model.min_samples
                 and candidate["negatives"] >= model.min_samples)
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError("AdaptiveProfileValidationMissing")
    return model


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-samples", type=int, default=8)
    parser.add_argument("--margin-sec", type=float, default=30)
    parser.add_argument("--legacy-window-hours", type=float,
                        default=float(os.getenv("KPP_TASK_MATCH_WINDOW_HOURS", "24")))
    args = parser.parse_args(argv)
    if args.output.resolve() == args.input.resolve():
        raise ValueError("SeparateOutputRequired")
    report = train(json.loads(args.input.read_text(encoding="utf-8")), args.min_samples, args.margin_sec,
                   args.legacy_window_hours)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("training_samples", "validation_samples", "gate", "candidate")}, ensure_ascii=False))
    return 0 if report["gate"]["status"] == "accepted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
