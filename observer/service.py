from __future__ import annotations
import argparse
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from guardian.config import atomic_json
from common.replicated_ingest import validate_peers
from observer.behavior import BehaviorObserver, cross_source, hypotheses
from observer.collector import cluster_metrics, sql_metrics


class Service:
    def __init__(self, cfg):
        validate_peers(cfg["nodes"])
        self.cfg, self.lock, self.stop = cfg, threading.RLock(), threading.Event()
        self.engine = BehaviorObserver(Path(cfg["state_dir"])/"behavior.sqlite", cfg.get("baseline_days", 7))
        self.snapshot = {"status": "starting", "metrics": {}, "at": 0}
        from observer.notifications import AlertOutbox
        self.alerts = AlertOutbox(Path(cfg["state_dir"])/"alerts.sqlite")

    def tick(self):
        values, nodes = cluster_metrics(self.cfg["nodes"], os.environ["PERIMETER_HA_TOKEN"])
        failures = []
        if os.getenv("PERIMETER_OBSERVER_SQL"):
            try:
                values.update(sql_metrics())
            except Exception as exc:
                failures.append("sql:"+type(exc).__name__)
        else:
            failures.append("sql:unconfigured")
        now = time.time()
        results = self.engine.observe(now, cross_source(values))
        critical = values["ha_active_nodes"] != 1 or values["ha_healthy_executors"] != 1
        critical |= any(values.get(k, 0)>limit for k,limit in self.cfg.get("critical_limits", {}).items())
        warning = values["ha_ready_reserves"] < 2 or bool(failures)
        severe = any(v["status"] == "critical" for v in results.values())
        status = "critical" if critical or severe else "warning" if warning or any(v["status"] == "warning" for v in results.values()) else "collecting_baseline" if any(not v["baseline_ready"] for v in results.values()) else "normal"
        snapshot = {"at": now, "status": status, "metrics": results, "unavailable_sources": failures,
                    "hypotheses": hypotheses(results), "baseline_days_required": self.cfg.get("baseline_days", 7)}
        with self.lock:
            self.snapshot = snapshot
        atomic_json(Path(self.cfg["state_dir"])/"latest.json", snapshot)
        self.alerts.transition("cluster_behavior", status, now,self.cfg.get("alert_persistence_samples",3))
        self.alerts.deliver(os.getenv("PERIMETER_ALERT_WEBHOOK"), os.getenv("PERIMETER_ALERT_TOKEN"))

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                with self.lock:
                    self.snapshot = dict(self.snapshot, status="collector_error")
                try:
                    self.alerts.transition("cluster_behavior","collector_error",time.time(),self.cfg.get("alert_persistence_samples",3))
                except Exception:
                    pass  # A failed alert store must not kill the collector loop.
            self.stop.wait(self.cfg.get("poll_sec", 60))

    def server(self):
        service = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with service.lock:
                    data = dict(service.snapshot)
                age = time.time()-data["at"]
                stale = not 0 <= age <= service.cfg.get("poll_sec", 60)*2+10
                if self.path == "/health/ready":
                    healthy = not stale and data["status"] not in {"critical", "collector_error", "starting"}
                    code, raw, mime = (200 if healthy else 503), json.dumps({"status": data["status"], "stale": stale}).encode(), "application/json"
                else:
                    if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer "+os.environ["PERIMETER_HA_TOKEN"]):
                        code, raw, mime = 401, b'{"error":"Unauthorized"}', "application/json"
                    elif self.path == "/status":
                        code, raw, mime = 200, json.dumps(dict(data, stale=stale), allow_nan=False).encode(), "application/json"
                    elif self.path == "/metrics":
                        lines = ["perimeter_behavior_stale "+str(int(stale)), "perimeter_behavior_sample_age_seconds "+str(max(0, age))]
                        for key, m in data["metrics"].items():
                            for field in ("value", "drift_score", "anomaly_score", "divergence_score", "residual", "degradation_velocity_per_hour", "forecast_15min", "forecast_1hour"):
                                if m.get(field) is not None:
                                    lines.append('perimeter_behavior_'+field+'{metric="'+key+'"} '+str(m[field]))
                            lines.append('perimeter_behavior_baseline_ready{metric="'+key+'"} '+str(int(m["baseline_ready"])))
                        code, raw, mime = 200, ("\n".join(lines)+"\n").encode(), "text/plain; version=0.0.4"
                    else:
                        code, raw, mime = 404, b'{}', "application/json"
                self.send_response(code)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        return ThreadingHTTPServer((self.cfg.get("listen", "127.0.0.1"), self.cfg.get("port", 19153)), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    service = Service(json.loads(args.config.read_text(encoding="utf-8-sig")))
    threading.Thread(target=service.run, daemon=True).start()
    server = service.server()
    try:
        server.serve_forever()
    finally:
        service.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
