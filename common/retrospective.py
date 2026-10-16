"""Re-sessionize a bounded real raw slice for review; never write a passage."""
from __future__ import annotations

from common.kpp_core_v3 import RfidRead, StrictSessionizer, infer_rfid_direction


def session_candidates(tag, warehouse_at, rows, expected_start, expected_end,
                       legacy_start, legacy_end, outer, inner, page_limit=200):
    sessionizer = StrictSessionizer(35, 900, 2)
    sessions = []
    # SQL ranks by time distance. Restore source-id order for the same
    # late/reordered-read semantics used by the production sessionizer.
    for row in sorted(rows, key=lambda row: int(row[0])):
        read = RfidRead(int(row[0]), row[1], int(row[2]), float(row[3] or 0), tag[:24], tag[24:])
        sessions.extend(sessionizer.process(read))
    sessions.extend(sessionizer.drain())
    output = []
    center = expected_start + (expected_end - expected_start) / 2
    scale = max(30, (expected_end - expected_start).total_seconds() / 2)
    for session in sessions:
        delta = (warehouse_at - session.midpoint).total_seconds()
        if delta < 0:
            continue
        output.append({"first_seen": session.first_seen.isoformat(), "last_seen": session.last_seen.isoformat(),
                       "raw_id_min": session.raw_id_min, "raw_id_max": session.raw_id_max,
                       "read_count": len(session.reads), "antenna_count": len({r.antenna for r in session.reads}),
                       "rfid_direction_candidate": infer_rfid_direction(session, outer, inner).value,
                       "delay_sec": delta, "in_expected_window": expected_start <= session.midpoint <= expected_end,
                       "outside_legacy_window": not legacy_start <= session.midpoint <= legacy_end,
                       "time_fit_score": round(max(0, 1 - abs((session.midpoint - center).total_seconds()) / scale), 4),
                       "source_context": "bounded_raw_slice", "page_may_be_truncated": len(rows) >= page_limit,
                       "learning_eligible": False, "physical_passage_confirmed": False})
    return sorted(output, key=lambda row: (-row["time_fit_score"], row["raw_id_min"]))
