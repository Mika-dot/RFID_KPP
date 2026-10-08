from __future__ import annotations

import unittest

from common.warehouse_direction import project_warehouse_direction, warehouse_direction_fields


class WarehouseDirectionTests(unittest.TestCase):
    def test_export_event_71746_keeps_passage_in_and_exposes_warehouse_out(self):
        # Sanitized metadata from the 07.10 export. No tag, person or image.
        event = {"EventId": 71746, "WarehouseId": 4411, "FinalDirection": "IN",
                 "RfidDirection": "UNKNOWN", "VideoDirection": "IN", "SkudDirection": "OUT",
                 "ConfidencePct": 70, "ConsensusCode": "WEIGHTED_MAJORITY",
                 "WarningFlags": "WAREHOUSE_CONFIRMED | OUT_CONFIRMED_BY_WAREHOUSE",
                 "VideoEventId": 920649, "RfidReadCount": 9}
        result = project_warehouse_direction(event)
        self.assertEqual(result["FinalDirection"], "IN")
        self.assertEqual(result["ConfidencePct"], 70)
        self.assertEqual(result["WarehouseDirection"], "OUT")
        self.assertTrue(result["WarehouseDirectionConflict"])
        self.assertIn("WAREHOUSE_DIRECTION_CONFLICT", result["WarningFlags"])
        self.assertNotIn("OUT_CONFIRMED_BY_WAREHOUSE", result["WarningFlags"])
        self.assertEqual(result["VideoEventId"], 920649)
        self.assertEqual(result["RfidReadCount"], 9)
        self.assertIn("OUT_CONFIRMED_BY_WAREHOUSE", event["WarningFlags"])

    def test_warehouse_cannot_resolve_explicit_fusion_conflict(self):
        result = warehouse_direction_fields({"FinalDirection": "UNKNOWN", "ConsensusCode": "CONFLICT",
                                             "ConfidencePct": 0, "WarningFlags": "DIRECTION_CONFLICT"})
        self.assertEqual(result["FinalDirection"], "UNKNOWN")
        self.assertEqual(result["ConfidencePct"], 0)
        self.assertTrue(result["WarehouseDirectionConflict"])
        self.assertIn("DIRECTION_CONFLICT", result["WarningFlags"])
        self.assertNotIn("OUT_CONFIRMED_BY_WAREHOUSE", result["WarningFlags"])

    def test_missing_direction_keeps_documented_warehouse_fallback(self):
        result = warehouse_direction_fields({"FinalDirection": None, "ConsensusCode": "NO_DATA",
                                             "ConfidencePct": 0})
        self.assertEqual(result["FinalDirection"], "OUT")
        self.assertEqual(result["ConfidencePct"], 100)
        self.assertIn("WAREHOUSE_DIRECTION_INFERRED", result["WarningFlags"])
        self.assertFalse(result["WarehouseDirectionConflict"])
        self.assertEqual(warehouse_direction_fields(result), result)

    def test_observed_out_is_not_given_artificial_100_percent_confidence(self):
        result = warehouse_direction_fields({"FinalDirection": "OUT", "ConfidencePct": 55})
        self.assertEqual(result["ConfidencePct"], 55)
        self.assertIn("OUT_CONFIRMED_BY_WAREHOUSE", result["WarningFlags"])
        self.assertNotIn("WAREHOUSE_DIRECTION_INFERRED", result["WarningFlags"])

    def test_later_in_removes_prior_inference_but_keeps_source_warnings(self):
        result = warehouse_direction_fields({"FinalDirection": "IN", "ConfidencePct": 90,
            "WarningFlags": "VIDEO_NOT_MATCHED | WAREHOUSE_DIRECTION_INFERRED | OUT_CONFIRMED_BY_WAREHOUSE"})
        self.assertNotIn("WAREHOUSE_DIRECTION_INFERRED", result["WarningFlags"])
        self.assertNotIn("OUT_CONFIRMED_BY_WAREHOUSE", result["WarningFlags"])
        self.assertIn("VIDEO_NOT_MATCHED", result["WarningFlags"])
        self.assertEqual(warehouse_direction_fields(result), result)

    def test_kpp_only_event_does_not_gain_warehouse_evidence(self):
        event = {"WarehouseId": None, "FinalDirection": "UNKNOWN", "ConfidencePct": 0}
        self.assertEqual(project_warehouse_direction(event), event)


if __name__ == "__main__":
    unittest.main()
