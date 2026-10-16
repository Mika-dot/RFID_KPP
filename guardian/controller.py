from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

from guardian.policy import Observation, Policy
from guardian.probes import get_json


class Controller:
    def __init__(self, cfg, store, telemetry, stop):
        self.cfg, self.store, self.telemetry, self.stop = cfg, store, telemetry, stop
        self.policy = Policy(cfg["nodes"], cfg.get("failback_stable_sec", 120), cfg.get("startup_sec", 120),
                             auto_failback=cfg.get("auto_failback", False))
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

    def _drain_pending_fences(self):
        if not self.cfg.get("hardware_fencing_required", False):
            return
        from guardian.hardware_fence import fence_previous
        for node_id, epoch in self.store.pending_fences():
            fence_previous(self.cfg, node_id, epoch)
            self.store.confirm_fence(node_id, epoch)

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
            # A failed SQL call may have committed or may have left the old
            # lease untouched. Quarantine first and fence against the actual
            # authority, never an invented next epoch. The adapter allows an
            # emergency DENY of a faulted owner only for the live controller.
            if assigned_epoch is not None:
                try:
                    self.store.fault(chosen)
                    current = self.store.lease()
                    epoch = current.get("epoch")
                    if type(epoch) is not int or epoch < assigned_epoch:
                        raise RuntimeError("HardwareFenceLeaseHistoryChanged")
                    emergency = current.get("valid") is True and current.get("owner") == chosen
                    if emergency:
                        fence_previous(self.cfg, chosen, epoch, emergency=True, controller_store=self.store)
                    else:
                        fence_previous(self.cfg, chosen, epoch)
                    rollback_epoch = epoch
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
        # Retry durable obligations even with owner=None and no ready reserve.
        self._drain_pending_fences()
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
        # Automatic main rollouts must NEVER elect a fresh candidate while
        # only one replica receiver has the candidate.  Qualify both passive
        # receivers at the same exact SHA, in the same SQL epoch, before the
        # planned HA handoff.  Ordinary fault failover remains independent.
        updating = False
        if (self.cfg.get("auto_update", False) is True
                and chosen == previous and previous and observations[previous].healthy):
            old = snapshots[previous].get("release_sha")
            target = snapshots[previous].get("main_sha")
            peers = sorted(
                (node for node in self.cfg["nodes"] if node["id"] != previous),
                key=lambda node: node["priority"],
            )
            def receiver_ready(node):
                row = snapshots[node["id"]]
                preflight = row.get("preflight") or {}
                update = row.get("update") or {}
                age = row.get("preflight_sample_age")
                return (
                    row.get("release_sha") == target
                    and row.get("active") is False
                    and row.get("prepared") is True
                    and row.get("faulted") is False
                    and row.get("operator_maintenance") is False
                    and row.get("replication_enabled") is True
                    and row.get("fallback_enabled") is True
                    and row.get("epoch") == lease["epoch"]
                    and preflight.get("ok") is True
                    and type(age) in (int, float) and 0 <= age < 90
                    and update.get("qualification") == "passed"
                    and time.monotonic() - self.policy.stable.get(
                        node["id"], time.monotonic()) >= self.policy.recovery_seconds
                )
            if (target and target != old and len(peers) == 2
                    and all(receiver_ready(node) for node in peers)):
                chosen, updating = peers[0]["id"], True
                self.telemetry.event("rolling_update_admitted",
                                     previous=previous, target=target,
                                     receivers=[node["id"] for node in peers])
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
            self._drain_pending_fences()
            if previous:
                node = next(n for n in self.cfg["nodes"] if n["id"] == previous)
                try:
                    get_json(node["url"].rstrip("/") + "/demote", os.environ["PERIMETER_HA_TOKEN"],
                             timeout=8, body={})
                except Exception:
                    pass
            if chosen:
                hardware_required = self.cfg.get("hardware_fencing_required", False)
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
