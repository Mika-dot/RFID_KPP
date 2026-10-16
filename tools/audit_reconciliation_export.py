#!/usr/bin/env python3
"""Read-only replay of event/video CSV exports, including the supplied split ZIP.

Only counts, event IDs and source metadata leave the process. Images, RFID tags
and personal fields are never included in the result. Lossy binary CSV text is
used only for unambiguous metadata recovery, never to reconstruct an image.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import struct
import sys
import uuid
import zipfile
import zlib
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.kpp_core_v3 import normalize_video_direction
from common.warehouse_direction import project_warehouse_direction

VIDEO_COLUMNS = ["Id", "Timestamp", "Direction", "FromCamera", "ToCamera", "TransportMode",
                 "TimeDiffSec", "ImageSourceCamera", "ImageFormat", "DetectionCount", "Notes",
                 "ImageBase64", "ClientEventUuid", "CapturedAt", "ProcessedAt", "ReceivedAt",
                 "ImageData", "ReelCount", "SourceTrackIds", "TimeQuality"]
TS = r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?"
UUID = r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
UUID_FIELD = r'(?:"' + UUID + r'"|' + UUID + r'|"")?'
ANCHOR = re.compile(r",(?!,,,,)(" + UUID_FIELD + r"),(" + TS + r"|),(" + TS + r"|),(" + TS + r"|),")
TAIL = re.compile(r',([0-9]+),("(?:[^"\r\n]|"")*"|[^,\r\n]*),([A-Z_]+)\r?\n?$')


def video_metadata(line):
    head = next(csv.reader(io.StringIO(line, newline="")))[:11]
    anchors = [m for m in ANCHOR.finditer(line) if m.group(1).strip('"') or any(m.group(i) for i in (2, 3, 4))]
    tail = TAIL.search(line)
    if len(head) != 11 or not head[0].isdigit() or len(anchors) != 1 or tail is None:
        raise ValueError("AmbiguousVideoMetadata")
    middle = anchors[0]
    if middle.start() < len(",".join(head)) or middle.end() > tail.start():
        raise ValueError("InvalidVideoMetadataBounds")
    uuid_text = middle.group(1).strip('"')
    source_uuid = str(uuid.UUID(uuid_text)) if uuid_text else ""
    times = [datetime.fromisoformat(value) if value else None for value in middle.group(2, 3, 4)]
    at = datetime.fromisoformat(head[1])
    from_camera, to_camera = int(head[3]), int(head[4])
    float(head[6])
    int(head[7]); int(head[9])
    tracks = next(csv.reader([tail.group(2)]))[0] if tail.group(2) else ""
    if tracks and not isinstance(json.loads(tracks), list):
        raise ValueError("InvalidSourceTracks")
    return {"id": int(head[0]), "timestamp": at, "direction": head[2],
            "from_camera": from_camera, "to_camera": to_camera,
            "uuid": source_uuid, "captured_at": times[0], "received_at": times[2],
            "reel_count": int(tail.group(1)), "time_quality": tail.group(3),
            "lossy_image_text": "\ufffd" in line}


def csv_records(chunks):
    """Frame bounded logical records; quoted fields may span chunks/newlines.

    Quotes inside unquoted corrupt legacy image text do not open CSV fields.
    Metadata recovery remains responsible for rejecting ambiguous records.
    """
    record = bytearray()
    quoted = closing_quote = False
    field_start = True
    for chunk in chunks:
        for byte in chunk:
            record.append(byte)
            if len(record) > 16 * 1024 * 1024:
                raise ValueError("VideoRecordTooLarge")
            if quoted:
                if closing_quote:
                    closing_quote = False
                    if byte == 34:  # doubled quote inside a field
                        continue
                    quoted = False
                else:
                    closing_quote = byte == 34
                    continue
            if byte == 34 and field_start:
                quoted = True
                field_start = False
            elif byte == 44:
                field_start = True
            elif byte == 10:
                yield bytes(record)
                record.clear()
                field_start = True
            elif byte != 13:
                field_start = False
    if quoted and not closing_quote:
        raise ValueError("UnterminatedVideoCsvField")
    if record:
        yield bytes(record)


class VideoExport:
    def __init__(self, path):
        self.path = Path(path)
        self.inventory = {}

    def chunks(self):
        if self.path.suffix.lower() == ".csv":
            with self.path.open("rb") as stream:
                yield from iter(lambda: stream.read(65536), b"")
            self.inventory["archive_crc_verified"] = False
            return
        with self.path.open("rb") as stream:
            stream.seek(max(0, self.path.stat().st_size - 65557))
            footer = stream.read()
        pos = footer.rfind(b"PK\x05\x06")
        if pos < 0:
            raise ValueError("ZipFooterMissing")
        disk = struct.unpack_from("<H", footer, pos + 4)[0]
        if disk == 0:
            with zipfile.ZipFile(self.path) as archive:
                if len(archive.infolist()) != 1:
                    raise ValueError("ExpectedOneVideoCsv")
                with archive.open(archive.infolist()[0]) as stream:
                    yield from iter(lambda: stream.read(65536), b"")
            self.inventory["archive_crc_verified"] = True
            return
        parts = [self.path.with_suffix(f".z{i:02d}") for i in range(1, disk + 1)] + [self.path]
        if not all(part.is_file() for part in parts):
            raise ValueError("SplitZipPartMissing")
        with parts[0].open("rb") as stream:
            if stream.read(4) != b"PK\x07\x08":
                raise ValueError("SplitZipMarkerMissing")
            header = struct.unpack("<4s5H3L2H", stream.read(30))
            if header[0] != b"PK\x03\x04" or header[2] != 0 or header[3] != 8:
                raise ValueError("UnsupportedSplitZipEncoding")
            name = stream.read(header[-2]).decode("utf-8")
            if not name.lower().endswith(".csv"):
                raise ValueError("ExpectedVideoCsv")
            extra = stream.read(header[-1])
            size, compressed = header[8], header[7]
            offset = stream.tell()
        if size == 0xffffffff or compressed == 0xffffffff:
            extras, cursor = {}, 0
            while cursor < len(extra):
                key, length = struct.unpack_from("<HH", extra, cursor)
                extras[key] = extra[cursor + 4:cursor + 4 + length]
                cursor += 4 + length
            values = extras.get(1, b"")
            if size == 0xffffffff:
                size, = struct.unpack_from("<Q", values); values = values[8:]
            if compressed == 0xffffffff:
                compressed, = struct.unpack_from("<Q", values)
        inflater, left, crc, total = zlib.decompressobj(-15), compressed, 0, 0
        for index, part in enumerate(parts):
            with part.open("rb") as stream:
                if index == 0:
                    stream.seek(offset)
                while left:
                    chunk = stream.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
                    while chunk:
                        decoded = inflater.decompress(chunk, 1024 * 1024)
                        chunk = inflater.unconsumed_tail
                        crc = zlib.crc32(decoded, crc); total += len(decoded)
                        if total > size:
                            raise ValueError("ZipSizeMismatch")
                        yield decoded
        if left or not inflater.eof or total != size or crc != header[6]:
            raise ValueError("ZipCrcOrSizeMismatch")
        self.inventory["archive_crc_verified"] = True
        self.inventory["archive_crc32"] = f"{crc:08x}"

    def rows(self):
        digest, total, header_seen = hashlib.sha256(), 0, False
        def measured_chunks():
            nonlocal total
            for chunk in self.chunks():
                digest.update(chunk)
                total += len(chunk)
                yield chunk
        for line in csv_records(measured_chunks()):
            text = line.decode("utf-8-sig")
            if not header_seen:
                if next(csv.reader(io.StringIO(text, newline=""))) != VIDEO_COLUMNS:
                    raise ValueError("UnexpectedVideoColumns")
                header_seen = True
            else:
                yield video_metadata(text)
        if not header_seen:
            raise ValueError("VideoHeaderMissing")
        self.inventory.update(csv_bytes=total, csv_sha256=digest.hexdigest())


def audit(events_csv, video_path, cutoff, video_links_csv=None):
    csv.field_size_limit(16 * 1024 * 1024)
    video_source, videos = VideoExport(video_path), {}
    counts = Counter()
    for row in video_source.rows():
        counts["video_rows"] += 1
        counts["lossy_image_text_rows"] += row["lossy_image_text"]
        if row["id"] in videos:
            raise ValueError("DuplicateVideoId")
        videos[row["id"]] = row
    refs, event_ids, event_keys, recent = set(), set(), set(), []
    with Path(events_csv).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if None in row or any(value is None for value in row.values()):
                raise ValueError("IncompleteEventRecord")
            counts["event_rows"] += 1
            counts["duplicate_event_ids"] += row["EventId"] in event_ids
            counts["duplicate_event_keys"] += row["EventKey"] in event_keys
            event_ids.add(row["EventId"]); event_keys.add(row["EventKey"])
            event = {**row, "WarehouseId": int(row["WarehouseId"]) if row["WarehouseId"] else None,
                     "ConfidencePct": int(row["ConfidencePct"]) if row["ConfidencePct"] else None}
            projected = project_warehouse_direction(event)
            counts["warehouse_direction_conflicts"] += bool(projected.get("WarehouseDirectionConflict"))
            counts["reel_in_with_out_warehouse_flag"] += (row["IsReel"] == "1" and row["FinalDirection"] == "IN"
                                                         and "OUT_CONFIRMED_BY_WAREHOUSE" in row["WarningFlags"])
            if row["FinalDirection"] in {"IN", "OUT"}:
                counts["changed_known_passage_directions"] += projected["FinalDirection"] != row["FinalDirection"]
            video_id = int(row["VideoEventId"]) if row["VideoEventId"] else None
            video = videos.get(video_id)
            if video_id is not None:
                refs.add(video_id)
                if video and row.get("VideoClientEventUuid"):
                    counts["video_uuid_comparisons"] += 1
                    counts["video_uuid_mismatches"] += str(uuid.UUID(row["VideoClientEventUuid"])) != video["uuid"]
            if (datetime.fromisoformat(row["FirstSeen"]) >= cutoff and row["IsReel"] == "1"
                    and int(row["RfidReadCount"] or 0) > 0 and row["SessionCloseReason"] != "WAREHOUSE_ONLY"):
                counts["recent_rfid_reels"] += 1
                counts["recent_need_recheck"] += row["NeedRecheck"] == "1"
                if video:
                    counts["recent_video_direction_mismatches"] += normalize_video_direction(video["direction"]).value != row["VideoDirection"]
                recent.append({"event_id": int(row["EventId"]), "video_id": video_id,
                    "video_found": video is not None, "video_captured_at": video["captured_at"].isoformat() if video and video["captured_at"] else None,
                    "final_direction": projected["FinalDirection"], "warehouse_id": event["WarehouseId"],
                    "warehouse_direction_conflict": bool(projected.get("WarehouseDirectionConflict")),
                    "need_recheck": row["NeedRecheck"] == "1"})
    if video_links_csv:
        with Path(video_links_csv).open(encoding="utf-8-sig", newline="") as stream:
            refs.update(int(row["VideoEventId"]) for row in csv.DictReader(stream))
    counts["distinct_video_refs"] = len(refs)
    counts["missing_video_refs"] = len(refs - videos.keys())
    def inventory(path):
        path, digest = Path(path), hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"filename": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}

    return {"cutoff_local_export_time": cutoff.isoformat(), "video_inventory": video_source.inventory,
            "event_inventory": inventory(events_csv),
            "video_links_inventory": inventory(video_links_csv) if video_links_csv else None,
            "counts": dict(sorted(counts.items())), "recent_events": sorted(recent, key=lambda row: row["event_id"]),
            "limits": ["offline exports only; no live HTTP, spool or hardware acceptance",
                       "lossy image text is not evidence of damaged SQL VARBINARY",
                       "historical 1C/warehouse tag correctness requires source registry exports",
                       "export filters and source snapshot atomicity are not established",
                       "camera orientation is compared only after the supplied cutoff"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-csv", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--video-links-csv", type=Path)
    parser.add_argument("--cutoff", default="2026-10-06T18:54:00")
    args = parser.parse_args()
    try:
        result = audit(args.events_csv, args.video, datetime.fromisoformat(args.cutoff), args.video_links_csv)
    except Exception as exc:
        code = str(exc) if re.fullmatch(r"[A-Z][A-Za-z]+", str(exc)) else type(exc).__name__
        print(json.dumps({"error": code, "audit_complete": False}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
