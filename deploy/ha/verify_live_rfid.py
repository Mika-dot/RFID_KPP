"""Read-only physical RFID acceptance: baseline, real tag passage, check.

No SDK import/inventory, synthetic rows, service/lease changes or queue resets.
Only --baseline writes a new evidence file. --check correlates fresh SQL reads
with SENT UUIDs in the physical SQLite spool and the Aggregator cursor.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RELEASE = "1c7930912ad84e8f205cf16fbdd715c76c9eb74e"
SERVICES = {"RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard"}
NODES = {"physical", "perimetr", "comparator"}


class Abort(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise Abort(reason)


def healthy_cluster(lease, controller, peers, release=RELEASE):
    if (lease.get("enabled") is not True or lease.get("valid") is not True
            or lease.get("owner") != "physical" or controller != {"owner": "comparator", "valid": True}
            or set(peers) != NODES):
        return False
    for name, status in peers.items():
        if (status.get("node") != name or status.get("release_sha") != release
                or status.get("fencing_protocol") != 2 or status.get("sample_age", 999) >= 10
                or status.get("operator_maintenance") is not False or status.get("faulted") is not False
                or status.get("prepared") is not True or status.get("epoch") != lease["epoch"]
                or status.get("resources", {}).get("restart_required", False)):
            return False
        if name == "physical":
            health = status.get("services", {})
            if (status.get("active") is not True or status.get("healthy") is not True
                    or set(health) != SERVICES or not all(h.get("ok") is True for h in health.values())):
                return False
        elif status.get("active") is not False or status.get("services") != {}:
            return False
    return True


def sql_snapshot(conn, baseline=None):
    row = conn.execute("SELECT Enabled,Owner,Epoch,CASE WHEN Enabled=1 AND ExpiresAt>SYSUTCDATETIME() "
                       "THEN 1 ELSE 0 END FROM dbo.KPP_HA_Lease WHERE Id=1").fetchone()
    require(row is not None, "Lease record unavailable")
    lease = {"enabled": bool(row[0]), "owner": row[1], "epoch": int(row[2]), "valid": bool(row[3])}
    row = conn.execute("SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                       "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
    require(row is not None, "Controller record unavailable")
    controller = {"owner": row[0], "valid": bool(row[1])}
    maximum = int(conn.execute("SELECT ISNULL(MAX(Id),0) FROM dbo.RFID_Tags").fetchone()[0])
    row = conn.execute("SELECT StateValue FROM dbo.KPP_RuntimeState WHERE StateKey='LAST_RFID_ID_V3'").fetchone()
    require(row is not None, "Aggregator cursor unavailable")
    cursor = int(row[0])
    rows = []
    if baseline:
        # Bound evidence to the first 200 fresh, modern reads. Source time must
        # be new, not just a delayed delivery of an old queue item. Writer uses
        # datetime.now() on this same physical host for both SQLite and SQL.
        start = datetime.fromisoformat(baseline["local_time"])
        end = datetime.now() + timedelta(seconds=5)
        for r in conn.execute("SELECT TOP(200) Id,CONVERT(varchar(36),ClientReadUuid),SourceReaderTime,ReceivedAt "
                              "FROM dbo.RFID_Tags WHERE Id>? AND ClientReadUuid IS NOT NULL "
                              "AND SourceReaderTime>=? AND SourceReaderTime<=? "
                              "AND ReceivedAt>=? AND ReceivedAt<=? ORDER BY Id",
                              baseline["max_id"], start, end, start, end).fetchall():
            rows.append({"id": int(r[0]), "uuid": str(r[1]).lower(), "source_time": r[2].isoformat()})
    return {"lease": lease, "controller": controller, "max_id": maximum, "cursor": cursor, "fresh_rows": rows}


def spool_evidence(path, rows):
    require(path.is_file(), "Physical RFID spool unavailable")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        conn.execute("PRAGMA query_only=ON")
        counts = dict(conn.execute("SELECT state,COUNT(*) FROM reads GROUP BY state").fetchall())
        matched = []
        for r in rows:
            item = conn.execute("SELECT state,source_time FROM reads WHERE client_uuid=?", (r["uuid"],)).fetchone()
            if item and item[0] == "SENT":
                source = datetime.fromisoformat(item[1])
                sql_source = datetime.fromisoformat(r["source_time"])
                # SQL DATETIME2(3) rounds SQLite microseconds to milliseconds.
                if abs((source - sql_source).total_seconds()) <= 0.002:
                    matched.append(r["id"])
        return {"counts": counts, "matched_ids": matched}
    finally:
        conn.close()


def check_evidence(baseline, snapshot, spool, now):
    require(snapshot["lease"] == baseline["lease"], "Physical executor epoch changed; capture a new baseline")
    start = datetime.fromisoformat(baseline["local_time"])
    require(0 <= (now - start).total_seconds() <= 900, "Baseline expired or local clock changed; capture a new baseline")
    require(snapshot["max_id"] >= baseline["max_id"] and snapshot["cursor"] >= baseline["cursor"],
            "RFID source or Aggregator cursor regressed")
    require(bool(snapshot["fresh_rows"]), "NO_FRESH_RFID_READS; keep baseline and inspect the real reader")
    require(bool(spool["matched_ids"]), "FRESH_SQL_READ_NOT_CONFIRMED_SENT_IN_PHYSICAL_SPOOL; retry after delivery")
    require(spool["counts"].get("PENDING", 0) == 0, "RFID_DELIVERY_PENDING; wait and retry the same check")
    require(snapshot["cursor"] >= max(spool["matched_ids"]), "AGGREGATOR_NOT_CAUGHT_UP; wait and retry the same check")
    return {"fresh_sql_reads_sampled": len(snapshot["fresh_rows"]), "physical_sent_reads": len(spool["matched_ids"]),
            "first_matched_id": min(spool["matched_ids"]), "last_matched_id": max(spool["matched_ids"]),
            "aggregator_cursor": snapshot["cursor"], "spool_pending": 0, "epoch": snapshot["lease"]["epoch"]}


def main(argv=None):
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", type=Path, metavar="NEW_JSON")
    mode.add_argument("--check", type=Path, metavar="BASELINE_JSON")
    parser.add_argument("--release", default=RELEASE, help="Exact independently accepted runtime SHA")
    args = parser.parse_args(argv)
    release = args.release
    require(bool(re.fullmatch(r"[0-9a-f]{40}", release)), "Expected release must be an exact SHA")
    require(os.name == "nt", "Run on physical Windows with its native Python")
    cfg = json.loads(Path("D:/PerimeterHA/node.json").read_text(encoding="utf-8-sig"))
    root = Path(cfg["root"])
    record = json.loads((Path(cfg["state_dir"]) / "release.json").read_text(encoding="utf-8-sig"))
    require(cfg["node_id"] == "physical" and cfg.get("controller_enabled") is False
            and record["current"]["sha"] == release and Path(record["current"]["root"]).resolve() == root.resolve()
            and record.get("fencing_protocol_min") == 2 and record.get("pending") is False
            and record.get("previous") is None, "Stable exact physical hotfix release required")
    path = (args.baseline or args.check).resolve()
    require(path.parent == Path("D:/PerimeterHA").resolve() and path.name.startswith("rfid-baseline-")
            and path.suffix == ".json", "Unexpected RFID evidence path")
    baseline = None
    if args.check:
        baseline = json.loads(path.read_text(encoding="utf-8-sig"))
        require(baseline.get("kind") == "physical-live-rfid" and baseline.get("version") == 1
                and baseline.get("release") == release and isinstance(baseline.get("lease"), dict)
                and type(baseline.get("max_id")) is int and type(baseline.get("cursor")) is int,
                "A matching read-only baseline is required")
    sys.path.insert(0, str(root / "deploy/ha"))
    from windows_tool import read_bundle, windows_environment
    from inspect_rollout import request_json
    env = windows_environment(read_bundle("D:/PerimeterHA/transfer-private/environment.local.json"))
    env.update(cfg.get("env", {}))
    import pyodbc
    pyodbc.pooling = False
    conn = pyodbc.connect(env["PERIMETER_HA_SQL"], timeout=5, autocommit=True)
    try:
        conn.timeout = 5
        before = sql_snapshot(conn, baseline)
        peers = {}
        for peer in cfg["nodes"]:
            response = request_json(peer["url"].rstrip("/") + "/status", env["PERIMETER_HA_TOKEN"])
            require(response["http"] == 200, "Agent status unavailable: " + peer["id"])
            peers[peer["id"]] = response["body"]
        after = sql_snapshot(conn)
        require(before["lease"] == after["lease"], "Executor changed during snapshot")
        require(healthy_cluster(after["lease"], after["controller"], peers, release), "Stable physical full stack and both reserves required")
        spool_path = Path(env["RFID_SPOOL_PATH"])
        if not spool_path.is_absolute():
            spool_path = root / spool_path
        spool = spool_evidence(spool_path, before["fresh_rows"])
    finally:
        conn.close()
    if args.baseline:
        require(spool["counts"].get("PENDING", 0) == 0, "Drain pending RFID delivery before baseline")
        evidence = {"kind": "physical-live-rfid", "version": 1, "release": release,
                    "local_time": datetime.now().isoformat(), "utc": datetime.now(timezone.utc).isoformat(),
                    "lease": after["lease"], "max_id": after["max_id"], "cursor": after["cursor"],
                    "spool_counts": spool["counts"]}
        with path.open("x", encoding="utf-8") as f:
            json.dump(evidence, f, ensure_ascii=False, indent=2)
        print("RFID_BASELINE_SAVED", str(path), "max_id=" + str(evidence["max_id"]),
              "cursor=" + str(evidence["cursor"]), "epoch=" + str(evidence["lease"]["epoch"]), flush=True)
        print("PRESENT_REAL_TAG_THEN_REMOVE_IT; WAIT_20_SECONDS_AND_RUN_CHECK", flush=True)
    else:
        before["cursor"] = after["cursor"]
        proof = check_evidence(baseline, before, spool, datetime.now())
        print("LIVE_RFID_SQL_AND_AGGREGATOR_PROVEN", json.dumps(proof), flush=True)
        print("COIL_IDENTITY_DIRECTION_VIDEO_AND_WAREHOUSE_ACCEPTANCE_NOT_PROVEN", flush=True)
    return 0


def cli():
    try:
        return main()
    except Exception as exc:
        # SQL/HTTP/driver messages may contain credentials. Only controlled
        # reasons above can be emitted; unknown errors expose class only.
        print("RFID_LIVE_CHECK_STOPPED", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
