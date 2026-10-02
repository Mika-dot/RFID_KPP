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
from guardian.update import Updates


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
        self.restart_requested = False
        self.last_sample = 0
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

    def snapshot(self):
        with self.lock:
            result = dict(self.status)
            result["sample_age"] = time.monotonic()-self.last_sample
            result["release_sha"] = self.updates.state["current"]["sha"]
            result["main_sha"] = self.updates.main_sha
            if self.stop.is_set() or self.maintenance:
                result.update(prepared=False, healthy=False)
            return result

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
            if not owned or self.maintenance:
                if self.processes.children:
                    self.processes.stop()
                    self.telemetry.event("demoted", epoch=lease["epoch"])
                trial_failed = self.updates.state.get("trial_started") and state["faulted"]
                preparation_failed = (not self.preflight_result["ok"] and
                    time.time()-self.updates.state.get("activated_at", time.time()) > 120)
                if self.updates.state.get("pending") and (trial_failed or preparation_failed):
                    self.updates.rollback()
                    self.restart_requested = True
                    self.stop.set()
                elif self.updates.activate():
                    self.restart_requested = True
                    self.stop.set()
            elif self.processes.epoch != lease["epoch"]:
                self.processes.start(lease["epoch"])
                self.updates.begin_trial()
                self.healthy_since = None
                self.telemetry.event("promoted", epoch=lease["epoch"])
        health = services_health() if owned and self.processes.children else {}
        healthy = owned and self.processes.alive() and len(health) == 5 and all(h["ok"] for h in health.values())
        now = time.monotonic()
        if healthy:
            if self.healthy_since is None:
                self.healthy_since = now
            if now-self.healthy_since >= self.cfg.get("verify_sec", 60):
                self.updates.confirm()
        else:
            self.healthy_since = None
        with self.lock:
            prepared = (self.preflight_result["ok"] and now-self.preflight_at < 90
                        and not self.maintenance and (healthy if owned else True))
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
            def send(self, code, data, content_type="application/json"):
                raw = (json.dumps(data, ensure_ascii=False) if content_type == "application/json" else data).encode()
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
                self.send(404, {"error": "NotFound"})

            def do_POST(self):
                if not self.authenticated():
                    return self.send(401, {"error": "Unauthorized"})
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 4096:
                        raise ValueError("InvalidBodySize")
                    data = json.loads(self.rfile.read(size))
                    if self.path == "/repair" and set(data) == {"action", "service"}:
                        return self.send(200, node.repair(data["action"], data["service"]))
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
