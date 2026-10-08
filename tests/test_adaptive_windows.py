import unittest
from datetime import datetime, timedelta

from common.adaptive_windows import AdaptiveWindowModel, TravelObservation


class AdaptiveWindowTests(unittest.TestCase):
    def observations(self, count=12):
        base = datetime(2026, 10, 8, 9, 0, 0)
        return [
            TravelObservation(
                base + timedelta(days=i, seconds=i % 3 * 5),
                base + timedelta(days=i, seconds=300 + i % 4 * 10),
                transport="FORKLIFT-1",
                event_id=i + 1,
                warehouse_id=1000 + i,
            )
            for i in range(count)
        ]

    def test_confirmed_distribution_returns_one_sided_backward_window(self):
        model = AdaptiveWindowModel(min_samples=8, margin_sec=15)
        for observation in self.observations():
            self.assertTrue(model.observe(observation))
        warehouse_at = datetime(2026, 10, 15, 9, 0, 0)
        start, end, bounds = model.backward_interval(warehouse_at, "FORKLIFT-1")
        self.assertEqual("quantile", bounds.confidence)
        self.assertGreaterEqual(bounds.sample_count, 8)
        self.assertLess(start, end)
        self.assertGreaterEqual((warehouse_at - end).total_seconds(), 0)
        self.assertLess((warehouse_at - start).total_seconds(), 900)

    def test_invalid_or_unconfirmed_observations_do_not_change_model(self):
        model = AdaptiveWindowModel(min_samples=1)
        base = datetime(2026, 10, 8, 9, 0, 0)
        self.assertFalse(model.observe(TravelObservation(base, base - timedelta(seconds=1))))
        self.assertFalse(model.observe(TravelObservation(base, base + timedelta(seconds=1), confirmed=False)))
        self.assertEqual(0, len(model.buckets))

    def test_duplicate_confirmed_pair_is_idempotent(self):
        model = AdaptiveWindowModel(min_samples=1)
        observation = self.observations(1)[0]
        self.assertTrue(model.observe(observation))
        self.assertFalse(model.observe(observation))
        self.assertEqual(1, model.buckets["global"].count)

    def test_gradient_descent_separates_confirmed_and_rejected_deltas(self):
        model = AdaptiveWindowModel(hard_max_sec=24 * 3600, min_samples=1)
        result = model.fit_gradient_descent(
            [280, 300, 320, 340, 360, 380],
            [1800, 2400, 3600, 5400],
            steps=500,
            learning_rate=0.1,
            bucket="global",
        )
        self.assertLess(result["final_loss"], result["initial_loss"])
        self.assertGreater(result["center_sec"], 100)
        self.assertLess(result["upper_sec"], 1800)
        self.assertEqual("optimized", model.bounds_for(datetime.now()).confidence)

    def test_round_trip_preserves_model_and_optimized_profile(self):
        model = AdaptiveWindowModel(min_samples=1)
        for observation in self.observations(2):
            model.observe(observation)
        model.fit_gradient_descent([300, 320], [2400, 3600], bucket="global")
        restored = AdaptiveWindowModel.loads(model.dumps())
        self.assertEqual(model.to_dict(), restored.to_dict())


if __name__ == "__main__":
    unittest.main()
