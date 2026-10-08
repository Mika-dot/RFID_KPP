from __future__ import annotations

import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from guardian.config import SERVICES, atomic_json
from guardian.probes import preflight, services_health
from guardian.processes import Processes
from guardian.repair import redact
from guardian.repair import ACTIONS
from guardian.sql import FENCING_PROTOCOL
from guardian.update import Updates


def advance_replica_cursor(cursors, peer_id, page):
    """Keep a peer's high-water cursor across empty replica pages.

    A peer may legitimately return an empty page at the current cursor. A
    cursor reset would rescan the journal from zero on every poll and can
    prevent recovery from reaching new records. Reject regressions while
    preserving the last known cursor when a stale/invalid response is seen.
    """
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise ValueError("InvalidReplicaPage")
    cursor = page.get("cursor")
    current = cursors.get(peer_id, 0)
    if type(cursor) is not int or cursor < current:
        raise ValueError("ReplicaCursorRegression")
    cursors[peer_id] = cursor


class Node:
    def __init__(self, cfg, store, telemetry, stop):
        self.cfg, self.store, self.telemetry, self.stop = cfg, store, telemetry, stop
        Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
        self.processes = Processes(cfg)
        self.processes.reap_orphans()
        self.updates = Updates(cfg, store, telemetry)
        self.mutation = threading.RLock()
        self.lock = threading.RLock()
        self.maintenance = False
        self.operator_path = Path(cfg["state_dir"]) / "operator-maintenance.json"
        self.operator_maintenance = (json.loads(self.operator_path.read_text()).get("enabled", False)
                                     if self.operator_path.exists() else False)
        self.restart_requested = False
        self.last_sample = 0
        self.controller_status = {"owner": None, "valid": False, "at": 0}
        self.resources = {}
        self.preflight_at = 0
        self.preflight_result = {"ok": False, "checks": {"starting": False}}
        self.verified_since = None
        self.healthy_since = None
        self.repair_path = Path(cfg["state_dir"]) / "repair-verification.json"
        self.repair_attempted = (json.loads(self.repair_path.read_text()).get("required", False)
                                 if self.repair_path.exists() else False)
        self.status = {"node": cfg["node_id"], "active": False, "healthy": False,
                       "prepared": False, "epoch": 0, "faulted": False}
        self.rate_path = Path(cfg["state_dir"]) / "repair-rate.json"
        self.replica = None
        self.fallback = None
        self.mirror = None
        self.mirror_status = {"enabled": False}
        self.last_mirror_sync = 0
        if cfg.get("fallback_enabled", False):
            from common.fallback_store import FallbackStore
            from common.metadata_mirror import MetadataMirror
            self.fallback = FallbackStore(Path(cfg["state_dir"]) / "fallback.sqlite",
                                          cfg.get("fallback_retention_days", 93))
            self.mirror = MetadataMirror(Path(cfg["state_dir"]) / "metadata.sqlite",
                                         cfg.get("fallback_retention_days", 93))
            self.mirror_status = {"enabled": True, **self.mirror.stats()}
        self.bus_metrics = {}
        self.bus_metrics_at = 0
        from guardian.probation import BusinessProbation
        self.business_probation = BusinessProbation(cfg.get("probation_stall_sec", 120))
        if cfg.get("replication_enabled", False):
            from common.replicated_ingest import ReplicaJournal, validate_peers
            validate_peers(cfg["nodes"], cfg["node_id"])
            self.replica = ReplicaJournal(Path(cfg["state_dir"])/"replica.sqlite", cfg["node_id"])

    def snapshot(self):
        with self.lock:
            result = dict(self.status)
            result["sample_age"] = time.monotonic()-self.last_sample
            result["release_sha"] = self.updates.state["current"]["sha"]
            result["main_sha"] = self.updates.main_sha
            result["update"] = {"pending": self.updates.state.get("pending", False),
                "trial_started": self.updates.state.get("trial_started", False),
                "quarantined": len(self.updates.quarantined) if isinstance(getattr(self.updates, "quarantined", None), (set, list, tuple)) else 0,
                "qualification": self.updates.state.get("qualification", {}).get("status")}
            result["resources"] = dict(getattr(self, "resources", {}))
            result["fencing_protocol"] = FENCING_PROTOCOL
            result["operator_maintenance"] = getattr(self, "operator_maintenance", False)
            verified_since = getattr(self, "verified_since", None)
            result["repair"] = {"verification_required": bool(getattr(self, "repair_attempted", False)),
                                "verified_sec": max(0, time.monotonic() - verified_since)
                                if verified_since is not None else 0}
            result["replication_enabled"] = getattr(self, "replica", None) is not None
            result["fallback_enabled"] = getattr(self, "fallback", None) is not None
            result["metadata_mirror"] = dict(getattr(self, "mirror_status", {"enabled": False}))
            result["controller"] = dict(getattr(self, "controller_status", {"owner": None, "valid": False, "at": 0}))
            env = dict(os.environ, **getattr(self, "cfg", {}).get("env", {}))
            result["correlation"] = {"mode": env.get("KPP_ADAPTIVE_WINDOWS_MODE", "shadow")
                                     if env.get("KPP_ADAPTIVE_WINDOWS_ENABLED", "0") == "1" else "off"}
            result["bus_metrics"] = dict(getattr(self, "bus_metrics", {}))
            result["bus_sample_age"] = time.monotonic()-getattr(self, "bus_metrics_at", 0)
            counts=getattr(self.telemetry,"kind_counts",{})
            result["event_counts"] = dict(counts) if isinstance(counts,dict) else {}
            timing=getattr(getattr(self.telemetry,"recovery",None),"last",None)
            result["recovery_timing"] = dict(timing) if isinstance(timing,dict) else None
            if self.stop.is_set() or self.maintenance or result["operator_maintenance"]:
                result.update(prepared=False, healthy=False)
            return result

    def archive_replica(self, record, committed=False):
        """Keep peer metadata for 93 days without carrying images into the cache."""
        fallback = getattr(self, "fallback", None)
        if fallback is not None:
            saved = fallback.put(record["stream"], record["uuid"], record["payload"])
            if committed:
                fallback.mark_committed(record["stream"], record["uuid"], saved["digest"])

    def sync_mirror(self):
        mirror = getattr(self, "mirror", None)
        if mirror is None or time.monotonic() - getattr(self, "last_mirror_sync", 0) < 60:
            return
        self.last_mirror_sync = time.monotonic()
        try:
            status = self.store.controller_status()
            if isinstance(status, dict):
                with self.lock:
                    self.controller_status = {**status, "at": time.time()}
        except Exception:
            with self.lock:
                self.controller_status = {"owner": None, "valid": False, "at": time.time()}
        from observer.mirror import sync_metadata
        error = None
        try:
            sync_metadata(mirror, batch_size=self.cfg.get("mirror_batch_size", 500))
        except Exception as exc:
            error = type(exc).__name__
            self.telemetry.event("mirror_sync_error", error=error)
        with self.lock:
            self.mirror_status = {"enabled": True, **mirror.stats(), "error": error}

    def recent_events(self):
        mirror = getattr(self, "mirror", None)
        if mirror is None:
            return {"configured": False, "source": "unconfigured", "stale": True, "events": []}
        stats = mirror.stats()
        info = stats["streams"].get("events", {})
        events = mirror.recent("events", 20)
        for event in events:
            warehouse = mirror.lookup("warehouse", event.get("WarehouseId")) if event.get("WarehouseId") else None
            if warehouse:
                event["SeriesNumber"] = warehouse.get("SeriesNumber")
        return {"configured": True, "source": "local_metadata_mirror", "node": self.cfg["node_id"],
                "at": info.get("synced_at"), "stale": info.get("age_sec", 999999) > 130 or bool(self.mirror_status.get("error")),
                "caught_up": info.get("caught_up", False), "events": events,
                "counts": mirror.counts_24h() if info.get("caught_up") else {}}

    def diagnostics(self):
        # A read-only endpoint must remain available on the active executor.
        # In particular, diagnosing it must not set maintenance or stop workers.
        with self.mutation:
            workers = {name: {"running": p.poll() is None, "exit_code": p.poll()}
                       for name, p in self.processes.children.items()}
        logs = {}
        for name in SERVICES:
            path = Path(self.cfg["state_dir"]) / "logs" / (name + ".log")
            try:
                with path.open("rb") as f:
                    f.seek(max(0, path.stat().st_size - 8192))
                    tail = f.read(8192).decode("utf-8", "replace")
                for key, value in os.environ.items():
                    if any(word in key.upper() for word in ("PASSWORD", "TOKEN", "CONNECTION", "CONN_STR")) and len(value) >= 4:
                        tail = tail.replace(value, "REDACTED")
                logs[name] = redact(tail)
            except FileNotFoundError:
                logs[name] = ""
        return {"status": self.snapshot(), "workers": workers, "logs": logs}

    def replication_loop(self):
        from common.replicated_ingest import MAX_BODY, replay_local
        from guardian.net import json_request
        cursors = {}
        while not self.stop.is_set():
            import sqlite3
            from datetime import datetime
            env = dict(os.environ, **self.cfg.get("env", {}))
            paths = {"rfid": env.get("RFID_SPOOL_PATH", str(Path(self.cfg["root"])/"rfid_spool_v4.sqlite")),
                     "video": env.get("RFID_VIDEO_SPOOL", str(Path(self.cfg["root"])/"recordings/video_spool_v3.sqlite"))}
            metrics = {}
            for stream, path in paths.items():
                try:
                    source = Path(path).resolve()
                    if not source.is_file():
                        continue
                    db = sqlite3.connect(source.as_uri()+"?mode=ro", uri=True, timeout=1)
                    try:
                        table = "reads" if stream == "rfid" else "events"
                        pending, oldest = db.execute("SELECT COUNT(*),MIN(created_at) FROM "+table+" WHERE state='PENDING'").fetchone()
                        sent = db.execute("SELECT COUNT(*) FROM "+table+" WHERE state='SENT'").fetchone()[0]
                    finally:
                        db.close()
                    metrics[stream+"_pending"] = pending
                    metrics[stream+"_sent"] = sent
                    metrics[stream+"_oldest_pending_sec"] = max(0, (datetime.now()-datetime.fromisoformat(oldest)).total_seconds()) if oldest else 0
                except Exception:
                    continue
            fallback = getattr(self, "fallback", None)
            if fallback is not None:
                try:
                    metrics.update({"fallback_" + key: value for key, value in fallback.stats().items()})
                    fallback.maintenance()
                except Exception as exc:
                    self.telemetry.event("fallback_error", error=type(exc).__name__)
            self.sync_mirror()
            with self.lock:
                self.bus_metrics = metrics
                self.bus_metrics_at = time.monotonic()
            if self.replica is not None:
                try:
                    lease = self.store.lease()
                    owned = lease["valid"] and lease["owner"] == self.cfg["node_id"]
                    if owned:
                        for peer in self.cfg["nodes"]:
                            if peer["id"] == self.cfg["node_id"]:
                                continue
                            try:
                                code, data = json_request(peer["url"].rstrip("/")+"/replica/page?after="+str(cursors.get(peer["id"], 0)),
                                    os.environ["PERIMETER_HA_TOKEN"], timeout=2, max_bytes=MAX_BODY)
                                if code != 200 or not isinstance(data.get("items"), list):
                                    continue
                                for item in data["items"]:
                                    if item["state"] not in {"PENDING", "SENT"}:
                                        raise ValueError("InvalidReplicaState")
                                    self.replica.put(item["record"])
                                    self.archive_replica(item["record"], item["state"] == "SENT")
                                    if item["state"] == "SENT":
                                        r = item["record"]
                                        self.replica.sent(r["stream"], r["uuid"], r["digest"])
                                advance_replica_cursor(cursors, peer["id"], data)
                            except Exception:
                                continue
                        # SQL/role may have changed during peer I/O; recheck.
                        current = self.store.lease()
                        if current["valid"] and current["owner"] == self.cfg["node_id"] and current["epoch"] == lease["epoch"]:
                            replay_local(self.replica, paths)
                    self.replica.maintenance()
                except Exception as exc:
                    self.telemetry.event("replication_error", error=type(exc).__name__)
            self.stop.wait(5)

    def set_operator_maintenance(self, enabled, release):
        if not isinstance(enabled, bool) or release != self.updates.state["current"]["sha"]:
            raise ValueError("UnexpectedMaintenanceRelease")
        with self.mutation:
            lease = self.store.lease()
            if lease["valid"] and lease["owner"] == self.cfg["node_id"]:
                raise RuntimeError("ActiveNodeRepairForbidden")
            if enabled:
                self.store.begin_repair(self.cfg["node_id"])
                self.processes.stop()
            atomic_json(self.operator_path, {"enabled": enabled})
            self.operator_maintenance = enabled
            self.verified_since = None
            if not enabled:
                # Release is not a claim of recovery. The node must still pass
                # independent preflight continuously for verify_sec.
                self.repair_attempted = True
                atomic_json(self.repair_path, {"required": True})
            return {"operator_maintenance": enabled, "stopped": not bool(self.processes.children)}

    def check(self):
        with self.mutation:
            cfg = dict(self.cfg)
            active = bool(self.processes.children)
        result = preflight(cfg, self.store, active)
        with self.mutation:
            # A slow DLL probe must not certify a different release/interpreter
            # or overwrite readiness after a role change.
            if cfg != self.cfg or active != bool(self.processes.children):
                return {"ok":False, "checks":{"configuration_changed":False}}
            with self.lock:
                self.preflight_result = result
                self.preflight_at = time.monotonic()
        return result

    def probe_loop(self):
        while not self.stop.is_set():
            self.check()
            # Also clean a timed-out passive DLL probe without stopping a live bridge.
            self.processes.reap_bridges(orphaned_only=True)
            self.stop.wait(30)

    def tick(self):
        lease = self.store.lease()
        state = self.store.node_state(self.cfg["node_id"])
        owned = lease["valid"] and lease["owner"] == self.cfg["node_id"]
        with self.mutation:
            if not owned or self.maintenance or getattr(self, "operator_maintenance", False):
                if self.processes.children:
                    self.processes.stop()
                    self.telemetry.event("demoted", epoch=lease["epoch"])
                trial_failed = self.updates.state.get("trial_started") and state["faulted"]
                preparation_failed = (not self.preflight_result["ok"] and
                    time.time()-self.updates.state.get("activated_at", time.time()) > 120)
                if not getattr(self, "operator_maintenance", False) and self.updates.state.get("pending") and (trial_failed or preparation_failed):
                    self.updates.rollback()
                    self.restart_requested = True
                    self.stop.set()
                elif not getattr(self, "operator_maintenance", False) and self.updates.activate():
                    self.restart_requested = True
                    self.stop.set()
            elif self.processes.epoch != lease["epoch"]:
                self.processes.start(lease["epoch"])
                self.updates.begin_trial()
                self.healthy_since = None
                if hasattr(self, "business_probation"):
                    self.business_probation.streams.clear()
                    self.bus_metrics = {}
                    self.bus_metrics_at = 0
                self.telemetry.event("promoted", epoch=lease["epoch"])
        health = services_health() if owned and self.processes.children else {}
        healthy = owned and self.processes.alive() and len(health) == 5 and all(h["ok"] for h in health.values())
        now = time.monotonic()
        complete = True
        if self.updates.state.get("pending") and self.updates.state.get("trial_started") and hasattr(self, "business_probation"):
            complete = (now-getattr(self,"bus_metrics_at",0) <= 15 and all(k in self.bus_metrics for k in
                ("rfid_pending", "rfid_sent", "video_pending", "video_sent")))
            problem = self.business_probation.assess(getattr(self, "bus_metrics", {}), now)
            if not complete and time.time()-self.updates.state.get("trial_started_at",time.time()) > 240:
                problem = "candidate_business_metrics_unavailable"
            if problem:
                # Fence/demote via the ordinary unhealthy path. Rollback occurs
                # after ownership is removed, never by swapping an active tree.
                healthy = False
                self.telemetry.event("candidate_business_fault", reason=problem)
        if healthy:
            if self.healthy_since is None:
                self.healthy_since = now
            duration = max(self.cfg.get("verify_sec", 60), self.cfg.get("probation_sec", 180)) if self.updates.state.get("pending") else self.cfg.get("verify_sec", 60)
            if complete and now-self.healthy_since >= duration:
                self.updates.confirm()
        else:
            self.healthy_since = None
        with self.lock:
            prepared = (self.preflight_result["ok"] and now-self.preflight_at < 90
                        and not self.maintenance and not getattr(self, "operator_maintenance", False)
                        and (healthy if owned else True))
        if not owned and state["faulted"]:
            # A repair claim is ignored; independently observe post-repair preflight.
            if prepared and self.repair_attempted:
                if self.verified_since is None:
                    self.verified_since = now
                if now-self.verified_since >= self.cfg.get("verify_sec", 60):
                    self.store.recovered(self.cfg["node_id"])
                    self.repair_attempted = False
                    atomic_json(self.repair_path, {"required":False})
                    self.telemetry.event("repair_verified")
            else:
                self.verified_since = None
        with self.lock:
            self.status = {"node": self.cfg["node_id"], "active": bool(owned and self.processes.children),
                           "healthy": bool(healthy), "prepared": bool(prepared), "epoch": lease["epoch"],
                           "faulted": state["faulted"], "services": health,
                           "preflight": self.preflight_result}
            self.last_sample = now

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:
                # SQL unavailable, identity mismatch or process startup error: fail closed.
                with self.mutation:
                    self.processes.stop()
                with self.lock:
                    self.status.update(active=False, healthy=False, prepared=False)
                    self.last_sample = time.monotonic()
                self.telemetry.event("node_error", error=type(e).__name__)
            self.stop.wait(2)
        with self.mutation:
            self.processes.stop()

    def repair(self, action, service):
        if action not in ACTIONS:
            raise ValueError("Unknown repair action")
        if service not in ["all"] + list(SERVICES):
            raise ValueError("Unknown service")
        with self.mutation:
            if getattr(self, "operator_maintenance", False):
                raise RuntimeError("OperatorMaintenance")
            lease = self.store.lease()
            if lease["valid"] and lease["owner"] == self.cfg["node_id"]:
                raise RuntimeError("ActiveNodeRepairForbidden")
            self.maintenance = True
            try:
                if action in {"restart_service", "rollback_release", "repair_dependencies", "restore_release"}:
                    times = json.loads(self.rate_path.read_text()) if self.rate_path.exists() else []
                    times = [t for t in times if time.time()-t < 600]
                    if len(times) >= 3:
                        raise RuntimeError("RepairRateLimited")
                    self.store.begin_repair(self.cfg["node_id"])
                    atomic_json(self.rate_path, times + [time.time()])
                    self.processes.stop()
                    self.verified_since = None
                    self.repair_attempted = True
                    atomic_json(self.repair_path, {"required":True})
                if action == "rollback_release":
                    self.updates.rollback()
                    self.restart_requested = True
                    self.stop.set()
                if action in {"repair_dependencies", "restore_release"}:
                    getattr(self.updates, action)()
                    self.restart_requested = True
                    self.stop.set()
                if action == "diagnose":
                    logs = {}
                    for name in SERVICES if service == "all" else [service]:
                        path = Path(self.cfg["state_dir"]) / "logs" / (name + ".log")
                        if path.exists():
                            with path.open("rb") as f:
                                f.seek(max(0, path.stat().st_size-8192))
                                tail = f.read().decode("utf-8", "replace")
                            for key, value in os.environ.items():
                                if any(word in key for word in ("PASSWORD", "TOKEN", "CONNECTION", "CONN_STR")) and len(value) >= 4:
                                    tail = tail.replace(value, "REDACTED")
                            logs[name] = redact(tail)
                    return {"status": self.snapshot(), "logs": logs}
                if action == "wait":
                    return {"waiting": True}
                result = self.check()
                return {"preflight": result, "verified": False}
            finally:
                self.maintenance = False

    def server(self):
        node = self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def send(self, code, data, content_type="application/json"):
                raw = (json.dumps(data, ensure_ascii=False,separators=(",",":")) if content_type == "application/json" else data).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def authenticated(self):
                return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + os.environ["PERIMETER_HA_TOKEN"])

            def do_GET(self):
                if self.path == "/metrics":
                    return self.send(200, node.telemetry.metrics(node.snapshot()), "text/plain; version=0.0.4")
                if self.path in ("/health", "/health/ready"):
                    s = node.snapshot()
                    ready = s["healthy"] if s["active"] else s["prepared"] and not s["faulted"]
                    return self.send(200 if self.path == "/health" or ready else 503,
                                     {"status": "ok" if ready else "degraded",
                                      **{k:s[k] for k in ("node", "active", "healthy", "prepared", "faulted", "epoch")}})
                if not self.authenticated():
                    return self.send(401, {"error": "Unauthorized"})
                if self.path == "/status":
                    return self.send(200, node.snapshot())
                if self.path == "/diagnostics":
                    return self.send(200, node.diagnostics())
                if self.path == "/replica/stats" and getattr(node, "replica", None) is not None:
                    return self.send(200, node.replica.stats())
                if self.path == "/fallback/stats" and getattr(node, "fallback", None) is not None:
                    return self.send(200, node.fallback.stats())
                if self.path == "/events/recent":
                    return self.send(200, node.recent_events())
                if self.path.startswith("/replica/page?") and getattr(node, "replica", None) is not None:
                    from urllib.parse import parse_qs, urlsplit
                    try:
                        query = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                        if set(query) != {"after"} or len(query["after"]) != 1:
                            raise ValueError("InvalidReplicaPage")
                        return self.send(200, node.replica.page(int(query["after"][0])))
                    except (ValueError, TypeError):
                        return self.send(400, {"error": "InvalidReplicaPage"})
                self.send(404, {"error": "NotFound"})

            def do_POST(self):
                if not self.authenticated():
                    return self.send(401, {"error": "Unauthorized"})
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    from common.replicated_ingest import MAX_BODY
                    replica_request = self.path in {"/replica/put", "/replica/commit"}
                    if not 0 < size <= (MAX_BODY if replica_request else 4096):
                        raise ValueError("InvalidBodySize")
                    data = json.loads(self.rfile.read(size))
                    if replica_request and getattr(node, "replica", None) is not None:
                        if self.path == "/replica/put":
                            import shutil
                            if shutil.disk_usage(node.cfg["state_dir"]).free < node.cfg.get("replica_min_free_bytes", 128*1024*1024):
                                raise RuntimeError("ReplicaDiskFull")
                            receipt = node.replica.put(data)
                            node.archive_replica(data)
                            return self.send(200, receipt)
                        if set(data) != {"stream", "uuid", "digest"}:
                            raise ValueError("InvalidReplicaCommit")
                        node.replica.sent(data["stream"], data["uuid"], data["digest"])
                        record = node.replica.get(data["stream"], data["uuid"])
                        node.archive_replica(record, committed=True)
                        return self.send(200, {"committed": True})
                    if self.path == "/repair" and set(data) == {"action", "service"}:
                        return self.send(200, node.repair(data["action"], data["service"]))
                    if self.path == "/maintenance" and set(data) == {"enabled", "release"}:
                        return self.send(200, node.set_operator_maintenance(data["enabled"], data["release"]))
                    if self.path == "/demote" and data == {}:
                        with node.mutation:
                            lease = node.store.lease()
                            if lease["valid"] and lease["owner"] == node.cfg["node_id"]:
                                raise RuntimeError("LeaseStillOwned")
                            node.processes.stop()
                        return self.send(200, {"stopped": True})
                    raise ValueError("InvalidRequest")
                except Exception as e:
                    self.send(409, {"error": type(e).__name__})

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer((self.cfg.get("listen", "0.0.0.0"), self.cfg.get("port", 18200)), Handler)
        server.daemon_threads = True
        return server
