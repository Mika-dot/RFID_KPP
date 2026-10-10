from __future__ import annotations

import csv
import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.audit_reconciliation_export import VIDEO_COLUMNS, VideoExport, csv_records, video_metadata


AT = "2026-10-07 07:54:00.555"
UUID = "11111111-2222-3333-4444-555555555555"


def csv_fields(values):
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow(values)
    return out.getvalue()


def video_line(image="", captured=AT):
    head = csv_fields([920649, AT, "0>1", 0, 1, "forklift", 5.82, 1, "jpg", 2, "v3 durable queue"])
    middle = csv_fields([UUID, captured, AT, AT])
    tail = csv_fields([2, '["C0-8>C1-4", "C0-7>C1-5"]', "CAPTURE_TIMESTAMP"])
    return head + "," + image + "," + middle + ",," + tail + "\n"


class VideoMetadataTests(unittest.TestCase):
    def test_corrupt_unquoted_image_does_not_shift_uuid_or_track_count(self):
        row = video_metadata(video_line('damaged,\ufffd,bytes,,,,,', captured=""))
        self.assertEqual(row["id"], 920649)
        self.assertEqual(row["uuid"], UUID)
        self.assertIsNone(row["captured_at"])
        self.assertEqual(row["reel_count"], 2)
        self.assertTrue(row["lossy_image_text"])
        self.assertNotIn("image", row)

    def test_ambiguous_metadata_inside_corrupt_image_is_rejected(self):
        image = "," + csv_fields([UUID, AT, AT, AT]) + ",binary"
        with self.assertRaisesRegex(ValueError, "AmbiguousVideoMetadata"):
            video_metadata(video_line(image))

    def test_truncated_record_is_rejected(self):
        with self.assertRaises(ValueError):
            video_metadata(video_line()[:100])

    def test_quoted_legacy_uuid_is_validated_like_unquoted_current_uuid(self):
        line = video_line().replace(UUID, '"' + UUID + '"')
        row = video_metadata(line)
        self.assertEqual(row["uuid"], UUID)
        self.assertEqual(row["reel_count"], 2)


class VideoArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.text = csv_fields(VIDEO_COLUMNS) + "\n" + video_line()

    def zip_bytes(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ReelTransitions.csv", self.text)
        return stream.getvalue()

    def split(self, corrupt_crc=False):
        data = bytearray(b"PK\x07\x08" + self.zip_bytes())
        if corrupt_crc:
            data[18] ^= 1  # CRC32 in the local header.
        footer = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, footer + 4, 3, 3)
        cuts = [0, 90, 120, 150, len(data)]
        for index, ext in enumerate(("z01", "z02", "z03", "zip")):
            (self.folder / ("video." + ext)).write_bytes(data[cuts[index]:cuts[index + 1]])
        return self.folder / "video.zip"

    def test_single_zip_checks_crc_and_reads_metadata(self):
        path = self.folder / "video.zip"
        path.write_bytes(self.zip_bytes())
        source = VideoExport(path)
        self.assertEqual([row["id"] for row in source.rows()], [920649])
        self.assertTrue(source.inventory["archive_crc_verified"])

    def test_split_member_crosses_all_three_volume_boundaries(self):
        source = VideoExport(self.split())
        self.assertEqual([row["id"] for row in source.rows()], [920649])
        self.assertTrue(source.inventory["archive_crc_verified"])
        self.assertEqual(source.inventory["csv_bytes"], len(self.text.encode()))

    def test_missing_volume_cannot_produce_successful_audit(self):
        source = VideoExport(self.split())
        (self.folder / "video.z02").unlink()
        with self.assertRaisesRegex(ValueError, "SplitZipPartMissing"):
            list(source.rows())
        self.assertNotIn("archive_crc_verified", source.inventory)

    def test_wrong_split_crc_cannot_produce_successful_audit(self):
        source = VideoExport(self.split(corrupt_crc=True))
        with self.assertRaisesRegex(ValueError, "ZipCrcOrSizeMismatch"):
            list(source.rows())
        self.assertNotIn("archive_crc_verified", source.inventory)

    def test_quoted_multiline_notes_and_images_across_single_byte_chunks(self):
        values = [920649, AT, "0>1", 0, 1, "forklift", 5.82, 1, "jpg", 2,
                  'notes with "quotes"\nand newline', "base64\nwrapped", UUID, AT, AT, AT,
                  "binary\r\nimage", 2, '["track"]', "CAPTURE_TIMESTAMP"]
        raw = (csv_fields(VIDEO_COLUMNS) + "\r\n" + csv_fields(values) + "\r\n").encode()
        records = list(csv_records(bytes([byte]) for byte in raw))
        self.assertEqual(2, len(records))
        self.assertEqual(UUID, video_metadata(records[1].decode())["uuid"])
        path = self.folder / "multiline.csv"
        path.write_bytes(raw)
        source = VideoExport(path)
        self.assertEqual([920649], [row["id"] for row in source.rows()])
        self.assertEqual(len(raw), source.inventory["csv_bytes"])

    def test_unterminated_quoted_record_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unterminated"):
            list(csv_records([b'1,"never closed\nnext physical line']))


if __name__ == "__main__":
    unittest.main()
