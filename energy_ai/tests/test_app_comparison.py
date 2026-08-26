from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.app_comparison import _actual_interval, _direction, resolve_window
from app.app_comparison_v2 import _aligned_quarter


CFG = {
    "policy": {
        "economics": {
            "import_overhead_ore_kwh": 0.0,
            "export_overhead_ore_kwh": 0.0,
        }
    },
    "optimizer": {"battery_degradation_ore_kwh": 5.0},
}


class AppComparisonTests(unittest.TestCase):
    def test_actual_interval_uses_measured_grid_and_battery(self):
        row = {
            "grid_power_kw": 4.0,
            "battery_power_kw": 2.0,
            "price_ore_kwh": 100.0,
        }
        result = _actual_interval(row, CFG)
        self.assertAlmostEqual(1.0, result["grid_import_kw"] * 0.25)
        self.assertAlmostEqual(0.5, result["throughput_kwh"])
        self.assertAlmostEqual(102.5, result["cash_cost_ore"])

    def test_export_is_revenue_and_degradation_remains_cost(self):
        row = {
            "grid_power_kw": -2.0,
            "battery_power_kw": 1.0,
            "price_ore_kwh": 100.0,
        }
        result = _actual_interval(row, CFG)
        self.assertAlmostEqual(0.0, result["grid_import_kw"])
        self.assertAlmostEqual(2.0, result["grid_export_kw"])
        self.assertAlmostEqual(-48.75, result["cash_cost_ore"])

    def test_direction_threshold(self):
        self.assertEqual(0, _direction(0.2))
        self.assertEqual(1, _direction(0.3))
        self.assertEqual(-1, _direction(-0.3))

    def test_explicit_window_is_stockholm_when_naive(self):
        start, end = resolve_window(start="2026-08-26T00:00:00", end="2026-08-27T00:00:00")
        self.assertEqual("2026-08-25T22:00:00+00:00", start.isoformat())
        self.assertEqual("2026-08-26T22:00:00+00:00", end.isoformat())

    def test_quarter_alignment(self):
        self.assertTrue(_aligned_quarter(datetime(2026, 8, 26, 8, 15, tzinfo=timezone.utc)))
        self.assertFalse(_aligned_quarter(datetime(2026, 8, 26, 8, 16, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
