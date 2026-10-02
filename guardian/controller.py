from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

from guardian.policy import Observation, Policy
from guardian.probes import get_json


class Controller:
    def __init__(self, cfg, store, telemetry, stop):
        self.cfg, self.store, self.telemetry, self.stop = cfg, store, telemetry, stop
        self.policy = Policy(cfg["nodes"], cfg.get("failback_stable_sec", 120), cfg.get("startup_sec", 120))
        self.is_owner = False

    def poll(self, node):
        try:
            code, data = get_json(node["url"].rstrip("/") + "/status", os.environ["PERIMETER_HA_TOKEN"])
            if code != 200 or data.get("node") != node["id"] or data.get("sample_age", 999) > 10:
                raise ValueError("Invalid or stale node identity/sample")
            return node["id"], data
        except Exception:
            return node["id"], {}

    def run(self):
        while not self.stop.is_set():
            try:
                owned = self.store.claim_controller()
                if owned != self.is_owner:
                    self.policy.stable.clear()
                    self.policy.proven.clear()
                    self.telemetry.event("controller_role", active=owned)
                    self.is_owner = owned
                if owned:
                    self.tick()
            except Exception as e:
                self.telemetry.event("controller_error", error=type(e).__name__)
            self.stop.wait(2)

    def tick(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            snapshots = dict(pool.map(self.poll, self.cfg["nodes"]))
        lease = self.store.lease()
        if not lease["enabled"]:
            return
        observations = {}
        for node in self.cfg["nodes"]:
            nid = node["id"]
            s = snapshots[nid]
            state = self.store.node_state(nid)
            healthy = (s.get("healthy") and s.get("active") and lease["valid"]
                       and lease["owner"] == nid and s.get("epoch") == lease["epoch"])
            observations[nid] = Observation(bool(s.get("prepared")), bool(healthy),
                                             state["faulted"], bool(s))
        chosen = self.policy.choose(lease, observations, time.monotonic())
        previous = lease["owner"] if lease["valid"] else None
        # Roll an automatic main update through a validated reserve first.
        updating = False
        if chosen == previous and previous and observations[previous].healthy:
            old = snapshots[previous].get("release_sha")
            target = snapshots[previous].get("main_sha")
            if target and old != target:
                for node in sorted(self.cfg["nodes"], key=lambda n: n["priority"]):
                    nid = node["id"]
                    if (nid != previous and snapshots[nid].get("release_sha") == target
                        and observations[nid].prepared and not observations[nid].faulted
                        and time.monotonic()-self.policy.stable.get(nid, time.monotonic()) >= self.policy.recovery_seconds):
                        chosen, updating = nid, True
                        break
        if chosen != previous:
            failure = previous and not observations[previous].healthy
            if failure and not updating:
                self.store.fault(previous)
            # Fence first. A missing demotion ACK cannot leave the old epoch writable.
            self.store.grant(None)
            if previous:
                node = next(n for n in self.cfg["nodes"] if n["id"] == previous)
                try:
                    get_json(node["url"].rstrip("/") + "/demote", os.environ["PERIMETER_HA_TOKEN"],
                             timeout=8, body={})
                except Exception:
                    pass
            if chosen:
                if not self.store.claim_controller():
                    return
                self.store.grant(chosen)
                if hasattr(self.policy, "proven"):
                    self.policy.proven.discard(chosen)
            event = "rolling_update" if updating else ("failover" if failure or not previous else "failback")
            self.telemetry.event(event, previous=previous, target=chosen)
        elif chosen:
            self.store.grant(chosen)
