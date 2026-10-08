"""Observed handoff timings. These measure readiness, not physical RFID acceptance."""
from __future__ import annotations
from guardian.config import atomic_json


class RecoveryTimings:
    def __init__(self, path=None):
        self.path, self.pending, self.last = path, None, None
        if path is not None and path.exists():
            import json
            value = json.loads(path.read_text(encoding="utf-8"))
            self.pending, self.last = value.get("pending"), value.get("last")

    def record(self, event):
        kind, at = event["kind"], event["time"]
        if kind == "handoff_detected":
            self.pending = {"detected_at": at, "previous": event.get("previous"), "target": event.get("target")}
        elif self.pending and kind in {"handoff_fenced", "handoff_granted", "handoff_ready"}:
            if event.get("target") != self.pending["target"] or at < self.pending["detected_at"]:
                return  # Clock reversal or a different transition cannot produce an RTO.
            self.pending[kind.removeprefix("handoff_")+"_at"] = at
            if kind == "handoff_granted":
                self.pending["epoch"] = event.get("epoch")
            if kind == "handoff_ready" and event.get("epoch") == self.pending.get("epoch"):
                self.last = dict(self.pending, readiness_rto_sec=at-self.pending["detected_at"],
                                 measurement="observed_readiness_not_physical_passage")
                self.pending = None
        if self.path is not None:
            atomic_json(self.path, {"pending": self.pending, "last": self.last})
