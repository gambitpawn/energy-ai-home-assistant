from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.engine_contract import EngineInput
from app.neural_features import FEATURE_NAMES, FEATURE_SCHEMA, feature_metadata, vectorize
from app.neural_teacher_v2 import perfect_information_teacher_v2


class NeuralFeaturesV3Tests(unittest.TestCase):
    def _input(self, row_count: int = 8, varying_load: bool = False):
        rows = []
        start = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
        for i in range(row_count):
            rows.append({
                "start": (start + timedelta(minutes=15 * i)).isoformat(),
                "load_kw": float(i) if varying_load else 3.0,
                "pv_kw": 1.0,
                "load_uncertainty_kw": 0.3,
                "pv_uncertainty_kw": 0.2,
                "price_known": True,
                "price_ore_kwh": 150.0,
            })
        return EngineInput(
            generated_at="2026-11-03T07:59:00+00:00",
            decision_start="2026-11-03T08:00:00+00:00",
            initial_soc_pct=55.0,
            interval_minutes=15,
            horizon_rows=tuple(rows),
            constraints={
                "battery_capacity_kwh": 24.0,
                "hard_min_soc_pct": 7.0,
                "hard_max_soc_pct": 100.0,
                "preferred_min_soc_pct": 18.0,
                "preferred_max_soc_pct": 92.0,
                "normal_reserve_soc_pct": 22.0,
                "high_uncertainty_reserve_soc_pct": 30.0,
                "battery_max_charge_kw": 9.0,
                "battery_max_discharge_kw": 9.0,
                "physical_grid_import_limit_kw": 17.0,
                "grid_export_limit_kw": 12.0,
                "charge_efficiency": 0.96,
                "discharge_efficiency": 0.94,
            },
            objective={
                "installation": {
                    "battery_capacity_kwh": 24.0,
                    "pv_capacity_kw": 14.0,
                    "ev_max_power_kw": 22.0,
                    "battery_max_charge_kw": 9.0,
                    "battery_max_discharge_kw": 9.0,
                    "physical_grid_import_limit_kw": 17.0,
                    "grid_export_limit_kw": 12.0,
                    "charge_efficiency": 0.96,
                    "discharge_efficiency": 0.94,
                    "unknown_price_energy_coverage_fraction": 0.4,
                    "unknown_price_risk_premium_ore_kwh": 45.0,
                    "unknown_price_default_continuation_value_ore_kwh": 160.0,
                },
                "economics": {
                    "import_overhead_ore_kwh": 12.0,
                    "export_overhead_ore_kwh": 3.0,
                    "minimum_arbitrage_margin_ore_kwh": 25.0,
                    "battery_degradation_ore_kwh": 7.0,
                },
                "tariffs": {
                    "enabled": True,
                    "consumption_demand": {
                        "enabled": True,
                        "kind": "import_top3_mean",
                        "rate_sek_per_kw": 105.0,
                        "start_hour": 7,
                        "end_hour": 19,
                        "active_months": [1, 2, 11, 12],
                        "day_rule": "workdays",
                        "top_n": 3,
                    },
                    "production_demand": {"enabled": False},
                },
                "tariff_state": {
                    "consumption_demand": {
                        "active_month_at_decision": True,
                        "active_day_at_decision": True,
                        "active_at_decision": True,
                        "historical_metric_kw": 8.0,
                        "historical_top_values_kw": [9.0, 8.0, 7.0],
                        "current_clock_hour_average_kw_so_far": 6.5,
                        "current_clock_hour_quarters_elapsed": 2,
                    },
                    "production_demand": {},
                },
            },
        )

    def test_generalized_feature_schema_contains_installation_and_tariff(self):
        self.assertEqual(FEATURE_SCHEMA, "neural_v1_features_v3")
        x = vectorize(self._input())
        self.assertEqual(len(x), len(FEATURE_NAMES))
        values = dict(zip(FEATURE_NAMES, x))
        self.assertEqual(values["battery_capacity_kwh"], 24.0)
        self.assertEqual(values["pv_capacity_kw"], 14.0)
        self.assertEqual(values["ev_max_power_kw"], 22.0)
        self.assertEqual(values["consumption_demand_enabled"], 1.0)
        self.assertEqual(values["consumption_historical_peak1_kw"], 9.0)
        self.assertEqual(values["consumption_historical_peak3_kw"], 7.0)
        self.assertEqual(values["q00_consumption_tariff_active_fraction"], 1.0)

    def test_multiresolution_horizon_preserves_near_term_structure(self):
        x = vectorize(self._input(row_count=144, varying_load=True))
        values = dict(zip(FEATURE_NAMES, x))
        self.assertEqual(values["q00_load_mean_kw"], 0.0)
        self.assertEqual(values["q23_load_mean_kw"], 23.0)
        self.assertEqual(values["h00_load_mean_kw"], 25.5)
        self.assertEqual(values["h11_load_mean_kw"], 69.5)
        self.assertEqual(values["b00_load_mean_kw"], 75.5)
        self.assertEqual(values["b08_load_mean_kw"], 139.5)
        meta = feature_metadata()
        self.assertEqual(meta["maximum_horizon_hours"], 36.0)
        self.assertEqual([item["block_hours"] for item in meta["horizon_layout"]], [0.25, 1.0, 2.0])

    @patch("app.neural_teacher_v2._solve_rows")
    @patch("app.neural_teacher_v2._actual_rows")
    def test_active_consumption_tariff_uses_tariff_teacher(self, actual_rows, solve_rows):
        engine_input = self._input()
        actual_rows.return_value = ([
            {"start": r["start"], "load_kw": 3.0, "pv_kw": 1.0, "price_ore_kwh": 150.0}
            for r in engine_input.horizon_rows
        ], {"actual_coverage_fraction": 1.0})
        solve_rows.return_value = {
            "engine": "tariff_shadow_milp_v1",
            "terminal_soc_pct": 50.0,
            "tariff": {"metric_kw": 7.5},
            "rows": [{"charge_kw": 1.0, "discharge_kw": 4.0}],
        }
        result = perfect_information_teacher_v2({}, engine_input)
        self.assertIsNotNone(result)
        action, diag = result
        self.assertEqual(action, 3.0)
        self.assertEqual(diag["teacher_mode"], "tariff_aware_perfect_information")
        self.assertEqual(diag["teacher_tariff"], "consumption_demand")
        self.assertEqual(diag["historical_peaks_kw"], [9.0, 8.0, 7.0])


if __name__ == "__main__":
    unittest.main()
