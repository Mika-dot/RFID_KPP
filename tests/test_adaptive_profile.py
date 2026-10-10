import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from deploy.ha.tune_adaptive_windows import load_accepted_profile, train


def labelled_rows():
    rows = []
    start = datetime(2026, 9, 1, 9)
    for i in range(80):
        confirmed = i % 2 == 0
        warehouse = start + timedelta(days=i // 2, seconds=i % 2)
        seconds = 300 + (i % 4) * 5 if confirmed else 2400 + i
        rows.append({"event_id": i + 1, "warehouse_id": i + 1000,
                     "kpp_at": (warehouse - timedelta(seconds=seconds)).isoformat(),
                     "warehouse_at": warehouse.isoformat(), "transport": "FORKLIFT",
                     "label": "confirmed" if confirmed else "rejected"})
    return rows


class ProfileTests(unittest.TestCase):
    def test_chronological_holdout_accepts_a_separating_profile_and_loads_it(self):
        report = train(labelled_rows())
        self.assertEqual("accepted", report["gate"]["status"])
        self.assertEqual(1, report["candidate"]["recall"])
        self.assertEqual(0, report["candidate"]["false_positive_rate"])
        self.assertTrue(report["optimized_buckets"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps(report))
            model = load_accepted_profile(path)
        self.assertLess(model.bounds_for(datetime(2026, 10, 5, 9), "FORKLIFT").upper_sec, 2400)

    def test_hash_change_or_missing_validation_cannot_be_activated(self):
        report = train(labelled_rows())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            report["model"]["defaults"]["margin_sec"] = 9999
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "HashMismatch"):
                load_accepted_profile(path)
            report["gate"]["status"] = "rejected"
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "NotAccepted"):
                load_accepted_profile(path)

    def test_unlabelled_and_duplicate_pairs_are_not_training_data(self):
        rows = labelled_rows()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            train(rows + [rows[0]])
        rows[0]["label"] = "guessed"
        with self.assertRaisesRegex(ValueError, "OperatorLabels"):
            train(rows)

    def test_confusable_positives_and_negatives_reject_the_profile(self):
        rows = labelled_rows()
        for row in rows:
            warehouse = datetime.fromisoformat(row["warehouse_at"])
            row["kpp_at"] = (warehouse - timedelta(seconds=300)).isoformat()
        report = train(rows)
        self.assertEqual("rejected", report["gate"]["status"])

    def test_transport_specific_success_cannot_qualify_unknown_runtime_path(self):
        rows = labelled_rows()
        for i, row in enumerate(rows):
            row["transport"] = "FORKLIFT" if i % 4 < 2 else "TRUCK"
            short = (row["transport"] == "FORKLIFT") == (row["label"] == "confirmed")
            warehouse = datetime.fromisoformat(row["warehouse_at"])
            row["kpp_at"] = (warehouse - timedelta(seconds=300 if short else 2400)).isoformat()
        report = train(rows)
        self.assertEqual("UNKNOWN", report["matching_transport"])
        self.assertEqual("rejected", report["gate"]["status"])

    def test_baseline_uses_actual_legacy_window_and_activation_rejects_different_config(self):
        rows = labelled_rows()
        for row in rows:
            if row["label"] == "confirmed":
                warehouse = datetime.fromisoformat(row["warehouse_at"])
                row["kpp_at"] = (warehouse - timedelta(hours=48)).isoformat()
        report = train(rows, legacy_window_hours=72)
        self.assertEqual(72, report["legacy_window_hours"])
        self.assertEqual(1, report["baseline"]["recall"])
        accepted = train(labelled_rows(), legacy_window_hours=72)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps(accepted))
            load_accepted_profile(path, legacy_window_hours=72)
            with self.assertRaisesRegex(ValueError, "RuntimeMismatch"):
                load_accepted_profile(path, legacy_window_hours=24)

    def test_old_profile_must_be_requalified_for_runtime_matching_path(self):
        report = train(labelled_rows())
        report["version"] = 1
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "NotAccepted"):
                load_accepted_profile(path)


if __name__ == "__main__":
    unittest.main()
