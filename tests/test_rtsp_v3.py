from __future__ import annotations

import importlib.util
import tempfile
import unittest
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

MODULE_PATH = Path(__file__).resolve().parents[1] / "RTSP" / "RTSP_yolo_DB_v3.py"
spec = importlib.util.spec_from_file_location("rtsp_v3", MODULE_PATH)
rtsp = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules[spec.name] = rtsp
spec.loader.exec_module(rtsp)


class SpoolTests(unittest.TestCase):
    def test_enqueue_is_durable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = rtsp.DurableEventSpool(str(Path(tmp) / "spool.sqlite"))
            event = rtsp.GroupedTransition(
                event_uuid="11111111-1111-1111-1111-111111111111",
                direction="0>1", from_camera=0, to_camera=1,
                captured_at=datetime.now(), processed_at=datetime.now(), time_diff_sec=2.0,
                transport="forklift", reel_count=2, source_track_ids=["a>b", "c>d"], image_bytes=b"jpeg",
            )
            calls = []
            original_append = rtsp.append_csv
            rtsp.append_csv = lambda item: calls.append(item.event_uuid)
            try:
                self.assertTrue(spool.enqueue(event))
                self.assertTrue(spool.enqueue(event))
            finally:
                rtsp.append_csv = original_append
            self.assertEqual(calls, [event.event_uuid])
            pending = spool.next_pending()
            self.assertIsNotNone(pending)
            self.assertEqual(pending[1]["reel_count"], 2)
            spool.mark_sent(event.event_uuid)
            self.assertIsNone(spool.next_pending())

    def test_maintenance_never_deletes_pending_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = rtsp.DurableEventSpool(str(Path(tmp) / "spool.sqlite"))
            now = datetime.now()
            sent = rtsp.GroupedTransition(
                event_uuid="22222222-2222-2222-2222-222222222222",
                direction="0>1", from_camera=0, to_camera=1,
                captured_at=now, processed_at=now, time_diff_sec=2.0,
                transport="forklift", reel_count=1, source_track_ids=["a>b"], image_bytes=None,
            )
            pending = rtsp.GroupedTransition(
                event_uuid="33333333-3333-3333-3333-333333333333",
                direction="0>1", from_camera=0, to_camera=1,
                captured_at=now, processed_at=now, time_diff_sec=2.0,
                transport="forklift", reel_count=1, source_track_ids=["c>d"], image_bytes=None,
            )
            original_append = rtsp.append_csv
            rtsp.append_csv = lambda item: None
            try:
                spool.enqueue(sent)
                spool.enqueue(pending)
            finally:
                rtsp.append_csv = original_append
            spool.mark_sent(sent.event_uuid)
            with spool._connect() as conn:
                conn.execute("UPDATE events SET sent_at='2000-01-01T00:00:00' WHERE event_uuid=?", (sent.event_uuid,))
                conn.execute("UPDATE events SET created_at='2000-01-01T00:00:00' WHERE event_uuid=?", (pending.event_uuid,))
                conn.commit()
            spool.maintenance()
            with spool._connect() as conn:
                rows = dict(conn.execute("SELECT event_uuid,state FROM events").fetchall())
            self.assertNotIn(sent.event_uuid, rows)
            self.assertEqual(rows.get(pending.event_uuid), "PENDING")


class TrackerTests(unittest.TestCase):
    def test_two_reels_get_two_track_ids(self):
        tracker = rtsp.CentroidTracker(0)
        now = datetime.now()
        dets = [
            rtsp.Detection("cable_reel", .9, (0, 0, 50, 50)),
            rtsp.Detection("cable_reel", .9, (300, 0, 350, 50)),
        ]
        tracker.update(dets, now, (480, 640))
        tracks = tracker.update(dets, now + timedelta(milliseconds=100), (480, 640))
        self.assertEqual(len(tracks), 2)
        self.assertEqual(len({t.track_id for t in tracks}), 2)


    def test_pending_track_is_updated_without_duplicate_track(self):
        tracker = rtsp.CentroidTracker(0)
        now = datetime.now()
        det = [rtsp.Detection("cable_reel", .9, (0, 0, 50, 50))]
        tracker.update(det, now, (480, 640))
        tracks = tracker.update(det, now + timedelta(milliseconds=100), (480, 640))
        self.assertEqual(len(tracks), 1)
        track = tracks[0]
        track.pending = True
        tracker.update(det, now + timedelta(milliseconds=200), (480, 640))
        self.assertEqual(len(tracker.tracks), 1)
        self.assertEqual(next(iter(tracker.tracks.values())).hits, 3)

    def test_cross_camera_matching_is_one_to_one(self):
        now = datetime.now()
        a1 = rtsp.Track("a1", 0, now, now, (0, 0, 50, 50), hits=2, frame_shape=(480, 640))
        a2 = rtsp.Track("a2", 0, now, now, (300, 0, 350, 50), hits=2, frame_shape=(480, 640))
        b1 = rtsp.Track("b1", 1, now + timedelta(seconds=2), now + timedelta(seconds=2), (0, 0, 50, 50), hits=2, frame_shape=(480, 640))
        b2 = rtsp.Track("b2", 1, now + timedelta(seconds=2), now + timedelta(seconds=2), (300, 0, 350, 50), hits=2, frame_shape=(480, 640))
        matcher = rtsp.CrossCameraMatcher([0, 1])
        matcher.update_camera_tracks(0, [a1, a2])
        transitions = matcher.match(1, [b1, b2], np.zeros((10, 10, 3), dtype=np.uint8))
        self.assertEqual(len(transitions), 2)
        self.assertEqual(len({t.source_track_id for t in transitions}), 2)
        self.assertTrue(all(t.source_track.pending and t.target_track.pending for t in transitions))

    def test_batcher_counts_multiple_reels(self):
        now = datetime.now()
        dummy1 = rtsp.Track("a", 0, now, now, (0,0,1,1))
        dummy2 = rtsp.Track("b", 1, now, now, (0,0,1,1))
        transitions = [
            rtsp.RawTransition("0>1", 0, 1, now, now, now, 2.0, "forklift", "a1", "b1", None, dummy1, dummy2),
            rtsp.RawTransition("0>1", 0, 1, now, now + timedelta(milliseconds=200), now, 2.2, "human", "a2", "b2", None, dummy1, dummy2),
        ]
        batcher = rtsp.TransitionBatcher(1.0)
        batcher.add(transitions)
        grouped = batcher.flush_ready(now + timedelta(seconds=2))
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].reel_count, 2)
        self.assertEqual(grouped[0].transport, "forklift+human")

    def test_batcher_waits_for_window_after_latest_reel(self):
        now = datetime.now()
        a = rtsp.Track("a", 0, now, now, (0,0,1,1))
        b = rtsp.Track("b", 1, now, now, (0,0,1,1))
        batcher = rtsp.TransitionBatcher(1.0)
        first = rtsp.RawTransition("0>1", 0, 1, now, now, now, 2.0, "forklift", "a1", "b1", None, a, b)
        second = rtsp.RawTransition("0>1", 0, 1, now, now + timedelta(milliseconds=800), now, 2.0, "forklift", "a2", "b2", None, a, b)
        batcher.add([first, second])
        self.assertEqual(batcher.flush_ready(now + timedelta(seconds=1.1)), [])
        grouped = batcher.flush_ready(now + timedelta(seconds=1.9))
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].reel_count, 2)


if __name__ == "__main__":
    unittest.main()
