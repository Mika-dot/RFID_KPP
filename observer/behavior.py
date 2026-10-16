from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager


def slope(points):
    if len(points) < 3:
        return 0.0
    origin = points[0][0]
    xs = [(t-origin)/3600 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    variance = sum((x-mx)**2 for x in xs)
    return sum((x-mx)*(y-my) for x, y in zip(xs, ys))/variance if variance else 0.0


def bucket(ts):
    dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=3)))
    return dt.weekday(), dt.hour, dt.hour//8


class BehaviorObserver:
    """Median/MAD baseline, EWMA/CUSUM, persistence and bounded forecasts.

    Baseline needs elapsed history, not only a high sample count. Abnormal
    observations do not immediately train the baseline. Unknown/missing metrics
    are absent, never fabricated as zero. No repair/election API exists here.
    """
    def __init__(self, path, min_history_days=7, min_samples=30, retention_days=21):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.min_span = min_history_days*86400
        self.min_samples, self.retention = min_samples, max(retention_days, min_history_days+1)*86400
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sample(ts REAL, metric TEXT, value REAL, eligible INTEGER, PRIMARY KEY(ts,metric))")
            db.execute("CREATE INDEX IF NOT EXISTS sample_metric ON sample(metric,ts)")
            db.execute("CREATE TABLE IF NOT EXISTS model(metric TEXT PRIMARY KEY, state TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def observe(self, ts, values):
        if not math.isfinite(ts) or any(not isinstance(k, str) or not k.replace("_", "").replace("-", "").isalnum() for k in values):
            raise ValueError("InvalidBehaviorSample")
        output = {}
        with self.connect() as db:
            for metric, value in values.items():
                if value is None:
                    continue
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("InvalidBehaviorValue")
                found = db.execute("SELECT state FROM model WHERE metric=?", (metric,)).fetchone()
                state = json.loads(found[0]) if found else {}
                if ts <= state.get("at", -1):
                    raise ValueError("NonMonotonicBehaviorSample")
                history = db.execute("SELECT ts,value FROM sample WHERE metric=? AND ts>=? AND eligible=1 ORDER BY ts",
                                     (metric, ts-self.retention)).fetchall()
                ready = (len(history) >= self.min_samples and history[-1][0]-history[0][0] >= self.min_span) if history else False
                selected = [p for p in history if bucket(p[0])[:2] == bucket(ts)[:2]]
                label = "weekday_hour"
                if len(selected) < self.min_samples:
                    selected = [p for p in history if bucket(p[0])[2] == bucket(ts)[2]]
                    label = "shift"
                if len(selected) < self.min_samples:
                    selected, label = history, "global"
                median = statistics.median(v for _, v in selected) if selected else None
                mad = statistics.median(abs(v-median) for _, v in selected) if selected else None
                scale = max(1e-6, (mad or 0)*1.4826, abs(median or 0)*.05)
                z = (value-median)/scale if ready else 0
                persistent = state.get("persistent", 0)+1 if ready and abs(z) >= 3 else 0
                ewma = .2*value+.8*state.get("ewma", value)
                pos = max(0, state.get("cusum_pos", 0)+z-.5) if ready else 0
                neg = min(0, state.get("cusum_neg", 0)+z+.5) if ready else 0
                # A gap breaks persistence; it cannot manufacture a continuous trend.
                if state and ts-state["at"] > 300:
                    persistent, pos, neg = int(ready and abs(z) >= 3), 0, 0
                recent = db.execute("SELECT ts,value FROM sample WHERE metric=? AND ts>=? ORDER BY ts", (metric, ts-3600)).fetchall()
                trend = slope(recent+[(ts, value)])
                drift = min(100, max(abs(pos), abs(neg))*5) if persistent >= 3 else 0
                anomaly = min(100, abs(z)*10) if ready else None
                status = "collecting_baseline" if not ready else "critical" if drift >= 80 else "warning" if drift >= 30 else "normal"
                def forecast(hours):
                    result = value+trend*hours
                    return min(1,max(0,result)) if metric.endswith("_ratio") else max(0,result) if value>=0 else result
                output[metric] = {"value": value, "baseline": median, "mad": mad, "baseline_ready": ready,
                    "baseline_scope": label, "anomaly_score": anomaly, "drift_score": drift,
                    "degradation_velocity_per_hour": trend, "ewma": ewma,
                    "cusum_score": max(abs(pos), abs(neg)), "persistent_samples": persistent,
                    "residual": value-median if ready else None,
                    "divergence_score": anomaly if "_per_" in metric or "_match_ratio" in metric else None,
                    "status": status, "forecast_15min": forecast(.25), "forecast_1hour": forecast(1),
                    "forecast_shift": forecast(8), "forecast_kind": "linear_trend_not_probability"}
                db.execute("INSERT INTO sample VALUES(?,?,?,?)", (ts, metric, value, int(not ready or abs(z) < 3)))
                state = dict(at=ts, ewma=ewma, persistent=persistent, cusum_pos=pos, cusum_neg=neg)
                db.execute("INSERT OR REPLACE INTO model VALUES(?,?)", (metric, json.dumps(state)))
            db.execute("DELETE FROM sample WHERE ts<?", (ts-self.retention,))
        return output


def cross_source(values):
    result = dict(values)
    groups = values.get("rfid_groups_5min")
    if groups is not None and groups >= 3:
        for source in ("video", "skud"):
            v = values.get(source+"_events_5min")
            if v is not None:
                result[source+"_per_rfid_group"] = v/groups
    for numerator, denominator, key in (
        ("need_recheck_5min", "final_events_5min", "need_recheck_ratio"),
        ("unknown_direction_5min", "final_events_5min", "unknown_direction_ratio"),
        ("video_matched_5min", "rfid_reels_5min", "video_match_ratio"),
        ("skud_matched_5min", "rfid_reels_5min", "skud_match_ratio"),
        ("warehouse_only_5min", "final_events_5min", "warehouse_only_ratio")):
        n, d = values.get(numerator), values.get(denominator)
        if n is not None and d is not None and d >= 3:
            result[key] = n/d
    return result


def hypotheses(metrics):
    bad = {k for k, v in metrics.items() if v["status"] in {"warning", "critical"}}
    result = []
    # These are hypotheses from aggregate evidence, never asserted diagnoses.
    if "cursor_lag" in bad:
        result.append({"zone": "Aggregator", "evidence": ["cursor_lag"], "kind": "hypothesis"})
    if "video_per_rfid_group" in bad or "video_match_ratio" in bad:
        result.append({"zone": "Video_or_correlation", "evidence": sorted(bad & {"video_per_rfid_group", "video_match_ratio"}), "kind": "hypothesis"})
    if "spool_pending" in bad or "replica_pending" in bad:
        result.append({"zone": "SQL_delivery_or_replication", "evidence": sorted(bad & {"spool_pending", "replica_pending"}), "kind": "hypothesis"})
    if "skud_per_rfid_group" in bad or "skud_match_ratio" in bad:
        result.append({"zone": "RusGuard_or_correlation", "evidence": sorted(bad & {"skud_per_rfid_group", "skud_match_ratio"}), "kind": "hypothesis"})
    return result
