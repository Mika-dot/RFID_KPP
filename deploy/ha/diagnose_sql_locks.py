"""Bounded SELECT-only lock diagnosis; no SQL text, reader socket or repairs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from deploy.ha.release_update import load_environment
from deploy.ha.diagnose_cluster import diagnose
from deploy.ha.diagnose_fault import clean, sanitize
from guardian.net import json_request
from observer.collector import table
from observer.mirror import json_value

QUERIES = {
    "database": """SELECT DB_NAME() AS database_name,
        is_read_committed_snapshot_on, snapshot_isolation_state_desc
        FROM sys.databases WHERE database_id=DB_ID()""",
    "requests": """SELECT TOP (20) r.session_id, r.blocking_session_id,
        r.status, r.command, r.wait_type, r.wait_time, r.wait_resource,
        r.total_elapsed_time, r.open_transaction_count, s.host_name, s.program_name
        FROM sys.dm_exec_requests r
        LEFT JOIN sys.dm_exec_sessions s ON s.session_id=r.session_id
        WHERE r.session_id<>@@SPID AND (r.database_id=DB_ID()
            OR r.session_id IN (SELECT blocking_session_id FROM sys.dm_exec_requests
                WHERE database_id=DB_ID() AND blocking_session_id>0))
        ORDER BY r.wait_time DESC, r.total_elapsed_time DESC""",
    "sessions": """SELECT TOP (20) s.session_id, s.status, s.host_name,
        s.program_name, s.open_transaction_count,
        s.last_request_start_time, s.last_request_end_time
        FROM sys.dm_exec_sessions s
        WHERE s.is_user_process=1 AND s.session_id<>@@SPID
        AND ((s.database_id=DB_ID() AND s.open_transaction_count>0)
            OR s.session_id IN (SELECT blocking_session_id FROM sys.dm_exec_requests
                WHERE database_id=DB_ID() AND blocking_session_id>0))
        ORDER BY s.open_transaction_count DESC, s.session_id""",
    "event_locks": """SELECT TOP (30) t.request_session_id, t.resource_type,
        t.request_mode, t.request_status, COUNT_BIG(*) AS lock_count
        FROM sys.dm_tran_locks t
        LEFT JOIN sys.partitions p ON t.resource_type IN ('KEY','PAGE','RID','HOBT')
            AND t.resource_associated_entity_id=p.hobt_id
        WHERE t.resource_database_id=DB_ID()
        AND ((t.resource_type='OBJECT' AND t.resource_associated_entity_id=OBJECT_ID(?))
            OR p.object_id=OBJECT_ID(?))
        GROUP BY t.request_session_id, t.resource_type, t.request_mode, t.request_status
        ORDER BY t.request_session_id, t.resource_type, t.request_mode""",
}


def collect(cfg, env, connect=None, request=json_request):
    result = {"read_only": True, "node": cfg["node_id"],
              "cluster": diagnose(cfg["nodes"], env["PERIMETER_HA_TOKEN"], request),
              "sql": {}, "sql_errors": {}}
    source = table(env.get("OBSERVER_EVENTS_TABLE", "dbo.KPP_ReelEvents"))
    if connect is None:
        import pyodbc
        connect = pyodbc.connect
    db = None
    try:
        destination = env.get("PERIMETER_OBSERVER_SQL") or env["PERIMETER_HA_SQL"]
        db = connect(destination, timeout=3, autocommit=True, readonly=True)
        db.timeout = 3
        for name, sql in QUERIES.items():
            cursor = None
            try:
                params = (source, source) if name == "event_locks" else ()
                cursor = db.execute(sql, *params)
                columns = [field[0] for field in cursor.description]
                result["sql"][name] = [dict(zip(columns, (json_value(v) for v in row)))
                                       for row in cursor.fetchall()]
            except Exception as exc:
                result["sql_errors"][name] = type(exc).__name__ + ":" + clean(str(exc), env)[:1200]
            finally:
                if cursor is not None:
                    cursor.close()
    except Exception as exc:
        result["sql_errors"]["connection"] = type(exc).__name__ + ":" + clean(str(exc), env)[:1200]
    finally:
        if db is not None:
            db.close()
    return sanitize(result, env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/perimeter/node.json"))
    parser.add_argument("--environment", type=Path, default=Path("/etc/perimeter/environment"))
    args = parser.parse_args(argv)
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    env = load_environment(args, cfg)
    print(json.dumps(collect(cfg, env), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("SQL_LOCK_DIAGNOSIS_FAILED", type(exc).__name__, file=sys.stderr)
        raise SystemExit(2)
