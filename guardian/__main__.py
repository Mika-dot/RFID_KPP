from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
from pathlib import Path

from guardian.config import read_config
from guardian.sql import SqlStore, control_odbc


def supervise(stop, node, threads, telemetry, resource_guard):
    while not stop.wait(1):
        resources = resource_guard.sample()
        resources["restart_required"] |= telemetry.resource_exhausted.is_set()
        with node.lock:
            node.resources = resources
        if resources["restart_required"]:
            telemetry.event("agent_resource_exhausted", **resources)
            stop.set()
            return True
        # A live HTTP loop can still be unable to accept sockets. This check
        # runs independently of both HTTP requests and SQL connections.
        if any(not thread.is_alive() for thread in threads):
            stop.set()
            return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("doctor")
    migration = sub.add_parser("migrate")
    migration.add_argument("--enable", action="store_true")
    repair = sub.add_parser("repair-local")
    from guardian.repair import ACTIONS
    repair.add_argument("--action", choices=ACTIONS, default="verify")
    args = parser.parse_args(argv)
    cfg = read_config(args.config)
    Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
    # Pooling is a process-wide ODBC environment setting; configure it before
    # any agent thread or readiness check can create the first connection.
    control_odbc()
    store = SqlStore(cfg["node_id"])
    if args.command == "migrate":
        sql = (Path(cfg["root"]) / "migrations/003_perimeter_ha.sql").read_text()
        with store.connect() as conn:
            for batch in re.split(r"(?im)^GO\s*$", sql):
                if batch.strip():
                    conn.execute(batch)
            if args.enable:
                conn.execute("UPDATE dbo.KPP_HA_Lease SET Enabled=1 WHERE Id=1")
            conn.commit()
        print(json.dumps({"installed": True, "enabled": args.enable}))
        return 0
    if args.command == "doctor":
        from guardian.probes import preflight
        report = preflight(cfg, store)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 2
    from common.single_instance import SingleInstanceLock
    instance = SingleInstanceLock(str(Path(cfg["state_dir"]) / "guardian.lock"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from guardian.telemetry import Telemetry
    from guardian.node import Node
    stop = threading.Event()
    telemetry = Telemetry(cfg)
    node = Node(cfg, store, telemetry, stop)
    if args.command == "repair-local":
        print(json.dumps(node.repair(args.action, "all"), ensure_ascii=False))
        return 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    server = node.server()
    functions = [server.serve_forever, node.run, node.probe_loop,
                 lambda: node.updates.run(stop)]
    if cfg.get("controller_enabled"):
        from guardian.controller import Controller
        from guardian.repair import LmRepair
        functions += [Controller(cfg, store, telemetry, stop).run,
                      LmRepair(cfg, store, telemetry, stop).run]
    threads = [threading.Thread(target=f, daemon=True) for f in functions]
    for thread in threads:
        thread.start()
    from guardian.resources import ResourceGuard
    critical_failed = supervise(stop, node, threads, telemetry, ResourceGuard())
    if threads[0].is_alive():
        server.shutdown()
    server.server_close()
    threads[1].join(timeout=15)
    node.processes.stop()
    instance.close()
    return 75 if node.restart_requested else (2 if critical_failed else 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        sys.exit(2)
