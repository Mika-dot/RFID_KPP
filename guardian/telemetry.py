from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path


class Telemetry:
    def __init__(self, cfg):
        self.cfg = cfg
        self.queue = queue.Queue(maxsize=1000)
        self.events = 0
        self.dropped = 0
        threading.Thread(target=self._writer, daemon=True).start()

    def event(self, kind, **fields):
        record = dict(time=time.time(), node=self.cfg["node_id"], kind=kind, **fields)
        self.events += 1
        logging.info("HA %s %s", kind, json.dumps(fields, ensure_ascii=False))
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def _writer(self):
        sentry = False
        try:
            from common.observability import init_sentry
            sentry = init_sentry("Perimeter.Aggregator", "perimeter-ha-1.0")
        except Exception:
            pass
        path = Path(self.cfg["state_dir"]) / "audit.jsonl"
        while True:
            record = self.queue.get()
            try:
                if path.exists() and path.stat().st_size > 10*1024*1024:
                    path.replace(path.with_suffix(".previous.jsonl"))
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                if sentry and record["kind"] in ("failover", "repair_failed", "controller_error", "update_rejected"):
                    import sentry_sdk
                    with sentry_sdk.push_scope() as scope:
                        scope.set_tag("ha_node", self.cfg["node_id"])
                        scope.set_context("ha_event", record)
                        sentry_sdk.capture_message("Perimeter HA: " + record["kind"], level="warning")
            except Exception as e:
                logging.error("Audit writer: %s", type(e).__name__)
            finally:
                self.queue.task_done()

    def metrics(self, status):
        node = json.dumps(self.cfg["node_id"])
        rows = {
            "perimeter_ha_active": int(status.get("active", False)),
            "perimeter_ha_ready": int(status.get("healthy", False)),
            "perimeter_ha_prepared": int(status.get("prepared", False)),
            "perimeter_ha_faulted": int(status.get("faulted", False)),
            "perimeter_ha_epoch": status.get("epoch", 0),
            "perimeter_ha_events_total": self.events,
            "perimeter_ha_audit_dropped_total": self.dropped,
        }
        return "\n".join('%s{node=%s} %s' % (key,node,value) for key,value in rows.items()) + "\n"
