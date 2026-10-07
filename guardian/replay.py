"""Replay recorded source contracts through production pure logic and SQL builder.

Executed by the INSTALLED qualifier with an explicit candidate import path. The
ODBC module is a local recording adapter; no real connection can be opened.
Fixtures are deterministic models, not evidence of a physical passage.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import re
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path


class RecordingCursor:
    def __init__(self):
        self.rows = []
    def execute(self, sql, *args):
        if sql.startswith("INSERT INTO"):
            columns = re.search(r"\((.*?)\) VALUES", sql, re.S).group(1).split(",")[:-2]
            self.rows.append(dict(zip(columns, args[0])))
        return self
    def fetchone(self):
        return None
    def cursor(self):
        return self


def replay(root, traces):
    # Import isolation is essential: never compare the installed module twice.
    sys.path.insert(0, str(root))
    sys.modules["pyodbc"] = types.SimpleNamespace(Connection=object, Cursor=object,
        connect=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("OfflineReplayConnectionForbidden")))
    from common.kpp_core_v3 import (RfidRead, RegistryRecord, StrictSessionizer, Direction,
        TimedExternalEvent, classify_reel, infer_rfid_direction, group_reel_sessions, assign_events_one_to_one)
    from common.warehouse_report import build_report_records
    path = Path(root)/"KPP/kpp_aggregator_v3.py"
    spec = importlib.util.spec_from_file_location("qualified_aggregator", path)
    agg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = agg
    spec.loader.exec_module(agg)
    agg.Config.OUTER_ANTENNAS, agg.Config.INNER_ANTENNAS = {2, 3}, {1, 4}
    output = {}
    for trace in traces:
        at = datetime.fromisoformat(trace["at"])
        sessions = StrictSessionizer(35, 900, 2)
        closed = []
        for row in trace["reads"]:
            closed.extend(sessions.process(RfidRead(row["id"], at+timedelta(seconds=row["offset"]),
                row["antenna"], -50, row["epc"], row["tid"])))
        closed.extend(sessions.drain())
        tasks = {}
        task_rows = []
        for i, tag in enumerate(trace.get("known_tags", []), 1):
            tasks[tag] = [RegistryRecord(i, at, tag, "DOC"+str(i), "SERIES"+str(i))]
            task_rows.append(dict(Id=i, Dt=at, Tag=tag, Ids="DOC"+str(i), SeriesNumber="SERIES"+str(i)))
        decisions = {s.event_key: classify_reel(s, tasks, {}, {}, {}) for s in closed}
        directions = {s.event_key: infer_rfid_direction(s, {2, 3}, {1, 4}) for s in closed}
        confirmed = [s for s in closed if decisions[s.event_key].is_reel]
        groups = group_reel_sessions(confirmed, directions)
        for source in ("video", "skud"):
            events = [TimedExternalEvent(e["id"], at+timedelta(seconds=e.get("offset", 1)),
                Direction(e["direction"]), reel_count=e.get("count")) for e in trace.get(source, [])]
            assigned = assign_events_one_to_one(groups, events, {}, 60, 60)
            for group in groups:
                setattr(group, source, assigned.get(group.group_key))
        group_by_event = {s.event_key:g for g in groups for s in g.sessions}
        recorded = RecordingCursor()
        obj = agg.Aggregator.__new__(agg.Aggregator)
        for s in closed:
            agg.Aggregator.upsert_event(obj, recorded, agg.SessionContext(s, decisions[s.event_key], directions[s.event_key]), group_by_event.get(s.event_key))
        rows = [dict(r, EventId=i+1) for i, r in enumerate(recorded.rows)]
        warehouse = []
        for i, wh in enumerate(trace.get("warehouse", []), 1):
            warehouse.append(dict(WarehouseId=i, WarehouseDt=at, WarehouseTag=wh.get("tag"),
                WarehouseDocIds=wh.get("ids", "DOC1"), WarehouseSeriesNumber=wh.get("series", "SERIES1")))
        # The production Web SELECT accepts confirmed physical RFID rows only.
        report = build_report_records(warehouse, task_rows,
            [r for r in rows if r["IsReel"] and r["RfidReadCount"]>0], at-timedelta(days=1), at+timedelta(days=1))
        output[trace["name"]] = {
            "sessions": len(closed), "reels": sum(int(r["IsReel"]) for r in rows),
            "directions": sorted(r["FinalDirection"] for r in rows),
            "read_counts": sorted(r["RfidReadCount"] for r in rows),
            "groups": len(groups), "group_counts": sorted(g.reel_count for g in groups),
            "video_links": sorted(g.video.id for g in groups if g.video),
            "skud_links": sorted(g.skud.id for g in groups if g.skud),
            "need_recheck": sum(r["NeedRecheck"] for r in rows),
            "report_rows": len(report), "report_directions": sorted(r["FinalDirection"] for r in report),
            "report_warehouse_conflicts": sum(bool(r.get("WarehouseDirectionConflict")) for r in report)}
    return output


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--traces", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args(argv)
    traces = json.loads(args.traces.read_text(encoding="utf-8"))["traces"]
    result = replay(args.root.resolve(), traces)
    args.output.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
