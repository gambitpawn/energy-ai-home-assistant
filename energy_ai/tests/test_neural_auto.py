from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import neural_auto


class NeuralAutoPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.meta = Path(self.tmp.name) / "neural_v1.json"
        self.path_patch = patch("app.neural_auto.Path", side_effect=lambda value: self.meta if value == "/data/models/neural_v1.json" else Path(value))
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()

    def _write_meta(self, samples: int, trained_at: datetime, revision: int = 1):
        self.meta.write_text(json.dumps({
            "samples": samples,
            "trained_at": trained_at.isoformat(),
            "model_id": f"neural_v1-r{revision:04d}",
            "model_revision": revision,
        }), encoding="utf-8")

    @patch("app.neural_auto.model_status")
    @patch("app.neural_auto.sample_count")
    def test_first_model_due_at_minimum_samples(self, sample_count, model_status):
        sample_count.return_value = 64
        model_status.return_value = {"model_exists": False}
        status = neural_auto.retraining_policy_status(datetime(2026, 8, 28, tzinfo=timezone.utc))
        self.assertTrue(status["retraining_due"])
        self.assertEqual(status["reason"], "first_model_ready_to_train")
        self.assertEqual(status["cadence"], "daily")

    @patch("app.neural_auto.model_status")
    @patch("app.neural_auto.sample_count")
    def test_growing_dataset_waits_24_hours(self, sample_count, model_status):
        now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
        self._write_meta(100, now - timedelta(hours=12))
        sample_count.return_value = 120
        model_status.return_value = {"model_exists": True}
        status = neural_auto.retraining_policy_status(now)
        self.assertFalse(status["retraining_due"])
        self.assertEqual(status["minimum_interval_hours"], 24)

    @patch("app.neural_auto.model_status")
    @patch("app.neural_auto.sample_count")
    def test_large_dataset_waits_seven_days(self, sample_count, model_status):
        now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        self._write_meta(1000, now - timedelta(days=6))
        sample_count.return_value = 1100
        model_status.return_value = {"model_exists": True}
        status = neural_auto.retraining_policy_status(now)
        self.assertFalse(status["retraining_due"])
        self.assertEqual(status["cadence"], "weekly")
        self.assertEqual(status["minimum_interval_hours"], 168)


if __name__ == "__main__":
    unittest.main()
