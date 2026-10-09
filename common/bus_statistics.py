"""Small, thread-safe runtime samples; telemetry must not interrupt delivery."""
from __future__ import annotations
from collections import deque
import math
import re
import threading
import time


class RuntimeStatistics:
    def __init__(self, window=300, limit=12000, clock=time.monotonic):
        self.window, self.limit, self.clock = window, limit, clock
        self.samples = {}
        self.lock = threading.RLock()
        self.started = clock()

    def add(self, name, value=1):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", name) or not math.isfinite(float(value)):
            return
        now = self.clock()
        with self.lock:
            if name not in self.samples and len(self.samples) >= 128:
                return
            queue = self.samples.setdefault(name, deque(maxlen=self.limit + 1))
            queue.append((now, float(value)))

    def snapshot(self):
        from observer.detailed import percentile
        now = self.clock()
        elapsed = max(1, min(self.window, now - self.started))
        out = {}
        with self.lock:
            for key, queue in self.samples.items():
                while queue and queue[0][0] < now - self.window:
                    queue.popleft()
                if len(queue) > self.limit:
                    continue  # Truncated populations are unknown, not understated rates.
                values = [v for _, v in queue]
                if key.endswith("_rate") or key.endswith("_fps"):
                    out[key] = sum(values) / elapsed
                elif key.endswith("_per_min"):
                    out[key] = sum(values) * 60 / elapsed
                elif values:
                    out[key] = percentile(values, .95)
            # Ratio denominator is processed camera reads, not repeated idle polls.
            for cid in (0, 1):
                frames = self.samples.get(f"camera_{cid}_fresh_fps")
                stale = self.samples.get(f"camera_{cid}_stale_rate")
                if frames is not None and len(frames) <= self.limit and (stale is None or len(stale) <= self.limit):
                    total = len(frames) + len(stale or ())
                    if total:
                        out[f"camera_{cid}_stale_ratio"] = len(stale or ()) / total
        return out


STATISTICS = RuntimeStatistics()


def record(name, value=1):
    try:
        STATISTICS.add(name, value)
    except Exception:
        pass


def snapshot():
    try:
        return STATISTICS.snapshot()
    except Exception:
        return {}
