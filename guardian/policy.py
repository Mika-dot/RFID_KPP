from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    prepared: bool = False
    healthy: bool = False
    faulted: bool = False
    reachable: bool = False


class Policy:
    """No LLM input is accepted by the election state machine."""

    def __init__(self, nodes, recovery_seconds=120, startup_seconds=120):
        self.nodes = sorted(nodes, key=lambda n: n["priority"])
        self.recovery_seconds = recovery_seconds
        self.startup_seconds = startup_seconds
        self.stable = {}

    def choose(self, lease, observations, now):
        for n in self.nodes:
            nid = n["id"]
            obs = observations.get(nid, Observation())
            if obs.prepared and obs.reachable and not obs.faulted:
                self.stable.setdefault(nid, now)
            else:
                self.stable.pop(nid, None)
        current = lease.get("owner") if lease.get("valid") else None
        if current:
            obs = observations.get(current, Observation())
            if not obs.reachable or obs.faulted:
                current = None
            elif not obs.healthy and lease["age"] >= self.startup_seconds:
                current = None
            # Once an active stack has been healthy, ANY degraded sample faults it.
            elif not obs.healthy and current in getattr(self, "proven", set()):
                current = None
            if obs.healthy:
                if not hasattr(self, "proven"):
                    self.proven = set()
                self.proven.add(lease["owner"])
        candidates = [n["id"] for n in self.nodes if n["id"] in self.stable]
        if current is None:
            # Do not immediately choose the failed owner again in this cycle.
            candidates = [n for n in candidates if n != lease.get("owner")]
            if not lease.get("valid"):
                candidates = [n["id"] for n in self.nodes if n["id"] in self.stable]
            return candidates[0] if candidates else None
        rank = {n["id"]: n["priority"] for n in self.nodes}
        for nid in candidates:
            if rank[nid] < rank[current] and now - self.stable[nid] >= self.recovery_seconds:
                if hasattr(self, "proven"):
                    self.proven.discard(nid)
                return nid
        return current
