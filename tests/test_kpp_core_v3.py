from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from common.kpp_core_v3 import (
    Direction,
    ObjectType,
    RegistryRecord,
    RfidRead,
    StrictSessionizer,
    TagSession,
    TimedExternalEvent,
    assign_events_one_to_one,
    classify_reel,
    group_reel_sessions,
)


def read(idx: int, sec: float, tag: str = "E" * 24 + "T" * 8, batch: str | None = None) -> RfidRead:
    return RfidRead(
        idx,
        datetime(2026, 7, 1, 12, 0, 0) + timedelta(seconds=sec),
        1,
        -45.0,
        tag[:24],
        tag[24:],
        ingest_batch_id=batch,
    )


def session(*reads: RfidRead) -> TagSession:
    s = TagSession(reads[0].full_tag, reads[0].epc, reads[0].tid, reads[0].record_time, reads[0].record_time)
    for item in reads:
        s.add(item)
    return s


class SessionizerTests(unittest.TestCase):
    def test_gap_36_seconds_creates_two_sessions(self):
        z = StrictSessionizer(gap_sec=35, max_duration_sec=900)
        self.assertEqual(z.process(read(1, 0)), [])
        closed = z.process(read(2, 36))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].raw_id_min, 1)
        self.assertEqual(z.active[read(2, 36).full_tag].raw_id_min, 2)

    def test_max_duration_checked_before_add(self):
        z = StrictSessionizer(gap_sec=1000, max_duration_sec=900)
        z.process(read(1, 0))
        closed = z.process(read(2, 901))
        self.assertEqual(len(closed), 1)
        self.assertLessEqual(closed[0].duration_sec, 900)
        self.assertEqual(z.active[read(2, 901).full_tag].duration_sec, 0)

    def test_replayed_raw_id_is_idempotent(self):
        z = StrictSessionizer()
        r = read(1, 0)
        z.process(r)
        z.process(r)
        self.assertEqual(len(z.active[r.full_tag].reads), 1)

    def test_very_late_read_is_not_merged_backwards(self):
        z = StrictSessionizer(late_tolerance_sec=2)
        z.process(read(10, 100))
        closed = z.process(read(9, 0))
        self.assertEqual(closed[0].close_reason, "LATE_DATA")
        self.assertEqual(z.active[read(10, 100).full_tag].raw_id_min, 10)


    def test_reader_epoch_change_does_not_double_count_same_passage(self):
        z = StrictSessionizer(gap_sec=35, max_duration_sec=900)
        z.process(read(1, 0, batch="epoch-a"))
        closed = z.process(read(2, 1, batch="epoch-b"))
        self.assertEqual(closed, [])
        active = z.active[read(2, 1, batch="epoch-b").full_tag]
        self.assertEqual([r.id for r in active.reads], [1, 2])
        self.assertIn("RFID_READER_CONNECTION_EPOCH_CHANGED_WITHIN_SESSION", active.warning_flags)

    def test_active_session_roundtrip(self):
        s = session(read(1, 0), read(2, 1))
        restored = TagSession.from_json(s.to_json())
        self.assertEqual(restored.event_key, s.event_key)
        self.assertEqual([r.id for r in restored.reads], [1, 2])


class ReelClassificationTests(unittest.TestCase):
    def setUp(self):
        self.s = session(read(1, 0))
        self.task = RegistryRecord(5, self.s.first_seen + timedelta(hours=23), self.s.full_tag, "DOC")

    def test_full_tag_inside_24h_is_reel(self):
        d = classify_reel(self.s, {self.s.full_tag: [self.task]}, {}, {}, {}, 24)
        self.assertTrue(d.is_reel)
        self.assertEqual(d.object_type, ObjectType.REEL)

    def test_full_tag_outside_24h_is_not_reel(self):
        old = RegistryRecord(6, self.s.first_seen - timedelta(hours=25), self.s.full_tag)
        d = classify_reel(self.s, {self.s.full_tag: [old]}, {}, {}, {}, 24)
        self.assertFalse(d.is_reel)

    def test_unknown_rfid_does_not_count_as_reel(self):
        d = classify_reel(self.s, {}, {}, {}, {}, 24)
        self.assertFalse(d.is_reel)
        self.assertEqual(d.object_type, ObjectType.UNKNOWN_RFID)

    def test_epc_only_disabled_by_default(self):
        other_tag = self.s.epc + "DIFFERENT"
        rec = RegistryRecord(7, self.s.first_seen, other_tag)
        d = classify_reel(self.s, {}, {}, {self.s.epc: [rec]}, {}, 24, allow_unique_epc=False)
        self.assertFalse(d.is_reel)

    def test_ambiguous_epc_never_confirms(self):
        recs = [
            RegistryRecord(7, self.s.first_seen, self.s.epc + "A"),
            RegistryRecord(8, self.s.first_seen, self.s.epc + "B"),
        ]
        d = classify_reel(self.s, {}, {}, {self.s.epc: recs}, {}, 24, allow_unique_epc=True)
        self.assertFalse(d.is_reel)


class AssignmentTests(unittest.TestCase):
    def test_external_event_is_not_reused_between_groups(self):
        s1 = session(read(1, 0, "A" * 24 + "1"))
        s2 = session(read(2, 20, "B" * 24 + "2"))
        groups = group_reel_sessions([s1, s2], {s1.event_key: Direction.IN, s2.event_key: Direction.IN}, window_sec=1)
        event = TimedExternalEvent(10, s1.midpoint, Direction.IN)
        result = assign_events_one_to_one(groups, [event], {Direction.IN: 0, Direction.UNKNOWN: 0}, 60, 60)
        self.assertEqual(len(result), 1)


    def test_video_with_strongly_wrong_reel_count_is_rejected(self):
        sessions = [session(read(i, i * 0.1, chr(64+i) * 24 + str(i))) for i in range(1, 6)]
        directions = {item.event_key: Direction.IN for item in sessions}
        groups = group_reel_sessions(sessions, directions, window_sec=2)
        self.assertEqual(groups[0].reel_count, 5)
        event = TimedExternalEvent(99, groups[0].anchor_time, Direction.IN, reel_count=1)
        result = assign_events_one_to_one(groups, [event], {Direction.IN: 0, Direction.UNKNOWN: 0}, 60, 60)
        self.assertEqual(result, {})

    def test_simultaneous_reels_form_explicit_group(self):
        s1 = session(read(1, 0, "A" * 24 + "1"))
        s2 = session(read(2, 1, "B" * 24 + "2"))
        groups = group_reel_sessions([s1, s2], {s1.event_key: Direction.IN, s2.event_key: Direction.IN}, window_sec=2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].reel_count, 2)

    def test_global_assignment_avoids_greedy_dead_end(self):
        g1s = session(read(101, 0, "G" * 24 + "1"))
        g2s = session(read(102, 2, "H" * 24 + "2"))
        groups = group_reel_sessions(
            [g1s, g2s],
            {g1s.event_key: Direction.IN, g2s.event_key: Direction.IN},
            window_sec=0.1,
        )
        # e1 подходит обеим группам, e2 только первой. Жадный выбор g1->e1
        # оставил бы g2 без связи; min-cost max-cardinality даёт две связи.
        e1 = TimedExternalEvent(201, g1s.midpoint + timedelta(seconds=1), Direction.IN)
        e2 = TimedExternalEvent(202, g1s.midpoint - timedelta(seconds=2), Direction.IN)
        result = assign_events_one_to_one(
            groups, [e1, e2], {Direction.IN: 0, Direction.UNKNOWN: 0}, before_sec=3, after_sec=1
        )
        self.assertEqual(len(result), 2)
        self.assertEqual({e.id for e in result.values()}, {201, 202})


if __name__ == "__main__":
    unittest.main()
