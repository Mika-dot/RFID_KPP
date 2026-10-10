"""Build anonymized, bounded recorded-source regression fixtures from existing CSV.

No hardware or SQL connections. Expected behavior is captured from an explicit
stable checkout, not labelled physical truth. Video is absent unless separately
supplied; missing evidence is never synthesized. Personal SKUD fields are ignored.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from common.kpp_core_v3 import RfidRead, StrictSessionizer, infer_rfid_direction
from common.warehouse_identity import IdentityRecord, normalize_tag, resolve_warehouse_identity
from guardian.qualification import offline_env


def csv_rows(folder, table, inventory):
    paths = list(Path(folder).glob(table + "_*.csv"))
    if len(paths) != 1:
        raise ValueError("ExactlyOneSourceRequired:" + table)
    path = paths[0]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    item = {"sha256": digest.hexdigest(), "bytes": path.stat().st_size, "rows": 0}
    inventory[table] = item
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if None in row or any(value is None for value in row.values()):
                raise ValueError("IncompleteSourceRow:" + table)
            item["rows"] += 1
            yield row


def parse_time(value):
    return datetime.fromisoformat(value) if value and value.strip() else None


def build(folder, start, end, count=12, key=None):
    if start >= end or type(count) is not int or not 1 <= count <= 50:
        raise ValueError("RecordedTraceRangeInvalid")
    key = key or os.urandom(32)
    inventory = {}
    reads = []
    for row in csv_rows(folder, "RFID_Tags", inventory):
        stamp = parse_time(row["SourceReaderTime"]) or parse_time(row["RecordTime"])
        if stamp is not None and start <= stamp < end:
            reads.append(RfidRead(int(row["Id"]), stamp, int(row["Antenna"]),
                                  float(row["RSSI"]), row["EPC"], row["TID"]))
    reads.sort(key=lambda row: row.id)
    sessionizer = StrictSessionizer(35, 900, 2)
    sessions = []
    for read in reads:
        sessions.extend(sessionizer.process(read))
    sessions.extend(sessionizer.drain())
    tasks = []
    for row in csv_rows(folder, "RfidTags", inventory):
        stamp = parse_time(row["Dt"])
        if stamp is not None and start - timedelta(days=1) <= stamp <= end + timedelta(days=1):
            tasks.append(dict(row, Dt=stamp, Tag=normalize_tag(row["Tag"])))
    warehouse = []
    for row in csv_rows(folder, "Warehouse", inventory):
        stamp = parse_time(row["Dt"])
        if stamp is not None and start - timedelta(days=1) <= stamp <= end + timedelta(days=1):
            warehouse.append(dict(row, Dt=stamp, Tag=normalize_tag(row["Tag"])))
    skud = []
    for row in csv_rows(folder, "RusGuardLogs", inventory):
        stamp = parse_time(row["CreatedAt"])
        if stamp is not None and start <= stamp < end and row["Direction"] in {"IN", "OUT"}:
            skud.append((stamp, row["Direction"]))
    known = {row["Tag"] for row in tasks if row["Tag"]}
    identities = [IdentityRecord.from_values(row["Id"], row["Dt"], row["Tag"], row["Ids"], row["SeriesNumber"])
                  for row in tasks]
    for row in warehouse:
        resolution = resolve_warehouse_identity(row["Tag"], row["Ids"], row["SeriesNumber"], row["Dt"], identities)
        row["candidate_tags"] = {candidate.tag for candidate in resolution.candidates}
    warehouse_tags = set().union(*(row["candidate_tags"] for row in warehouse)) if warehouse else set()
    eligible = [session for session in sessions if 2 <= len(session.reads) <= 100
                and session.first_seen > start + timedelta(seconds=35)
                and session.last_seen < end - timedelta(seconds=35)]
    # Diverse source cases first; fill remaining slots deterministically.
    chosen, seen = [], set()
    for session in eligible:
        signature = (session.full_tag in known,
                     session.full_tag in warehouse_tags,
                     infer_rfid_direction(session, {2, 3}, {1, 4}).value,
                     len({row.antenna for row in session.reads}))
        if signature not in seen:
            chosen.append(session); seen.add(signature)
        if len(chosen) >= count:
            break
    selected_keys = {session.event_key for session in chosen}
    chosen += [session for session in eligible if session.event_key not in selected_keys][:count - len(chosen)]
    if not chosen:
        raise ValueError("NoCompleteRecordedSession")
    def alias(value, length=24):
        return hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest().upper()[:length] if value else ""
    def tag(value):
        return alias(value[:24]) + alias(value[24:]) if value else ""
    traces = []
    selected_read_ids = set()
    for index, session in enumerate(chosen, 1):
        at = session.first_seen
        # Include adjacent raw tags too, preserving real passage grouping.
        first, last = session.first_seen - timedelta(seconds=2), session.last_seen + timedelta(seconds=2)
        raw = [row for row in reads if first <= row.record_time <= last]
        if len(raw) > 500:
            continue
        selected_read_ids.update(row.id for row in raw)
        raw_tags = {row.full_tag for row in raw}
        matching_tasks = [row for row in tasks if row["Tag"] in raw_tags
                          and abs((row["Dt"] - at).total_seconds()) <= 86400]
        trace = {"name": "recorded_csv_" + str(index).zfill(2), "at": at.isoformat(),
                 "provenance": "anonymized_recorded_sources_stable_behavior_not_physical_truth",
                 "reads": [{"id": i, "offset": (row.record_time - at).total_seconds(),
                            "antenna": row.antenna, "rssi": row.rssi,
                            "epc": alias(row.epc), "tid": alias(row.tid)} for i, row in enumerate(raw, 1)],
                 "tasks": [{"Id": i, "Dt": row["Dt"].isoformat(), "Tag": tag(row["Tag"]),
                            "Ids": alias(row["Ids"]), "SeriesNumber": alias(row["SeriesNumber"])}
                           for i, row in enumerate(matching_tasks, 1)],
                 "warehouse": [{"tag": tag(row["Tag"]), "ids": alias(row["Ids"]),
                                "series": alias(row["SeriesNumber"]), "offset": (row["Dt"] - at).total_seconds()}
                               for row in warehouse if row["candidate_tags"] & raw_tags],
                 "skud": [{"id": i, "offset": (stamp - at).total_seconds(), "direction": direction}
                          for i, (stamp, direction) in enumerate(skud, 1)
                          if first - timedelta(seconds=60) <= stamp <= last + timedelta(seconds=60)],
                 "video": [], "unavailable_sources": ["video_recording_not_supplied"]}
        traces.append(trace)
    return {"version": 1, "provenance": {"kind": "anonymized_csv_recordings", "source_inventory": inventory,
            "source_range": [start.isoformat(), end.isoformat()], "selected_traces": len(traces),
            "selected_fragment_read_rows": sum(len(trace["reads"]) for trace in traces),
            "distinct_source_reads": len(selected_read_ids),
            "pseudonymization": "HMAC_SHA256_private_key_not_in_corpus",
            "expected_kind": "pinned_stable_behavior_reference_not_physical_acceptance"}, "traces": traces}


def baseline(corpus, stable):
    stable = Path(stable).resolve()
    sha = subprocess.check_output(["git", "-C", str(stable), "rev-parse", "HEAD"], text=True).strip()
    if subprocess.check_output(["git", "-C", str(stable), "status", "--porcelain"], text=True).strip():
        raise ValueError("CleanStableReferenceRequired")
    with tempfile.TemporaryDirectory(prefix="recorded-perimeter-") as folder:
        source, output = Path(folder) / "input.json", Path(folder) / "reference.json"
        source.write_text(json.dumps(corpus), encoding="utf-8")
        subprocess.run([sys.executable, "-I", str(ROOT / "guardian/replay.py"), "--root", str(stable),
                        "--traces", str(source), "--output", str(output)], env=offline_env(),
                       check=True, timeout=60, cwd=folder)
        expected = json.loads(output.read_text())
    for trace in corpus["traces"]:
        trace["expected"] = expected[trace["name"]]
    corpus["provenance"]["stable_reference_sha"] = sha
    return corpus


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--source-dir", type=Path, required=True)
    p.add_argument("--start", type=datetime.fromisoformat, required=True)
    p.add_argument("--end", type=datetime.fromisoformat, required=True)
    p.add_argument("--stable", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--count", type=int, default=12)
    a = p.parse_args(argv)
    corpus = baseline(build(a.source_dir, a.start, a.end, a.count), a.stable)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("RECORDED_TRACES_WRITTEN", len(corpus["traces"]))


if __name__ == "__main__":
    main()
