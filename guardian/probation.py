"""Bounded business checks during a candidate trial; quiet traffic is allowed."""
from __future__ import annotations


class BusinessProbation:
    def __init__(self, stall_sec=120):
        self.stall_sec, self.streams = stall_sec, {}

    def assess(self, metrics, now):
        for stream in ("rfid", "video"):
            pending, sent = metrics.get(stream+"_pending"), metrics.get(stream+"_sent")
            if pending is None or sent is None:
                continue  # Missing sample is not evidence of business success.
            state = self.streams.get(stream)
            if state is None or sent != state["sent"] or pending == 0:
                self.streams[stream] = {"since":now, "sent":sent, "pending":pending}
                continue
            if pending >= state["pending"] and pending>0 and now-state["since"] >= self.stall_sec:
                return "candidate_"+stream+"_delivery_stalled"
        return None
