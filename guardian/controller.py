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
        self.handoff = None

    def poll(self, node):
        try:
            code, data = get_json(node["url"].rstrip("/") + "/status", os.environ["PERIMETER_HA_TOKEN"])
            if code != 200 or data.get("node") != node["id"] or data.get("sample_age", 999) > 10:
                raise ValueError("Invalid or stale node identity/sample")
            return node["id"], data
        except Exception:
            return node["id"], {}

    def _rollback_hardware_handoff(self, chosen, assigned_epoch=None):
        """Fail closed if the new owner cannot be given reader access.

        The SQL lease is committed before the external unfence call so the
        receipt is bound to the actual owner epoch.  If that call fails, clear
        the lease, persist a fence obligation for the candidate, and make a
        best-effort immediate fence.  The durable obligation is retried on the
        next controller cycle or after restart; the candidate is quarantined
        so it cannot be selected again before repair.
        """
        from guardian.hardware_fence import fence_previous

        rollback_epoch = None
        try:
            self.store.grant(None, fence_previous=chosen)
            for node_id, fence_epoch in self.store.pending_fences():
                if node_id != chosen:
                    continue
                rollback_epoch = fence_epoch
                try:
                    fence_previous(self.cfg, node_id, fence_epoch)
                    self.store.confirm_fence(node_id, fence_epoch)
                except Exception as exc:
                    self.telemetry.event("hardware_fence_pending", target=node_id,
                                         epoch=fence_epoch, error=type(exc).__name__)
        except Exception as exc:
            self.telemetry.event("hardware_handoff_rollback_error", target=chosen,
                                 error=type(exc).__name__)
            # If SQL rollback itself failed, still try the external fence with
            # the next epoch.  This is best effort; SQL retry remains the
            # authority when the controller regains its lease.
            if assigned_epoch is not None:
                try:
                    fence_previous(self.cfg, chosen, int(assigned_epoch) + 1)
                except Exception as fence_exc:
                    self.telemetry.event("hardware_fence_pending", target=chosen,
                                         epoch=int(assigned_epoch) + 1,
                                         error=type(fence_exc).__name__)
        try:
            self.store.fault(chosen)
        except Exception as exc:
            self.telemetry.event("hardware_handoff_quarantine_error", target=chosen,
                                 error=type(exc).__name__)
        try:
            node = next(n for n in self.cfg["nodes"] if n["id"] == chosen)
            get_json(node["url"].rstrip("/") + "/demote", os.environ["PERIMETER_HA_TOKEN"],
                     timeout=8, body={})
        except Exception:
            pass
        self.telemetry.event("hardware_handoff_rolled_back", target=chosen,
                             epoch=rollback_epoch if rollback_epoch is not None else assigned_epoch)

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
        # Expiry fences writes but does not erase the previous node's identity.
        # Retain it so an unreachable former owner is quarantined and repaired,
        # including when no reserve is currently prepared.
        previous = lease["owner"]
        if (self.handoff and lease["valid"] and lease["owner"] == self.handoff[0]
                and lease["epoch"] == self.handoff[1] and observations[lease["owner"]].healthy):
            self.telemetry.event("handoff_ready", target=lease["owner"], epoch=lease["epoch"])
            self.handoff = None
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
            self.telemetry.event("handoff_detected", previous=previous, target=chosen)
            failure = previous and not observations[previous].healthy
            if failure and not updating:
                self.store.fault(previous)
            # Fence first. A missing demotion ACK cannot leave the old epoch writable.
            if previous and self.cfg.get("hardware_fencing_required", False):
                self.store.grant(None, fence_previous=previous)
            else:
                self.store.grant(None)
            self.telemetry.event("handoff_fenced", target=chosen)
            if previous:
                node = next(n for n in self.cfg["nodes"] if n["id"] == previous)
                try:
                    get_json(node["url"].rstrip("/") + "/demote", os.environ["PERIMETER_HA_TOKEN"],
                             timeout=8, body={})
                except Exception:
                    pass
            if chosen:
                hardware_required = self.cfg.get("hardware_fencing_required", False)
                if self.cfg.get("hardware_fencing_required", False):
                    from guardian.hardware_fence import fence_previous
                    # A failed fence survives controller restart and owner=None.
                    for nid, fence_epoch in self.store.pending_fences():
                        fence_previous(self.cfg, nid, fence_epoch)
                        self.store.confirm_fence(nid, fence_epoch)
                if not self.store.claim_controller():
                    return
                self.store.grant(chosen)
                assigned = self.store.lease()
                if hardware_required:
                    from guardian.hardware_fence import allow_target
                    try:
                        if assigned.get("owner") != chosen or not assigned.get("valid"):
                            raise RuntimeError("HardwareFenceLeaseMismatch")
                        # Authorize only after SQL has committed the new owner;
                        # the receipt must carry that committed epoch.
                        allow_target(self.cfg, chosen, assigned["epoch"])
                    except Exception:
                        self._rollback_hardware_handoff(chosen, assigned.get("epoch"))
                        raise
                self.handoff = (chosen, assigned["epoch"])
                self.telemetry.event("handoff_granted", target=chosen, epoch=assigned["epoch"])
                if hasattr(self.policy, "proven"):
                    self.policy.proven.discard(chosen)
            event = "rolling_update" if updating else ("failover" if failure or not previous else "failback")
            self.telemetry.event(event, previous=previous, target=chosen)
        elif chosen:
            self.store.grant(chosen)
