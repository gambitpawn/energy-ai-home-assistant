from __future__ import annotations

import unittest

from app.deterministic_refined import ENGINE_ID, solve_refined_from_rows
from app.engine_registry import BASELINE_ENGINE_ID, REFINED_ENGINE_ID, registry_status


class DeterministicRefinedTests(unittest.TestCase):
    def _cfg(self):
        return {
            "policy": {
                "battery": {
                    "capacity_kwh": 10.0,
                    "hard_min_soc_pct": 5.0,
                    "hard_max_soc_pct": 100.0,
                    "preferred_min_soc_pct": 5.0,
                    "preferred_max_soc_pct": 100.0,
                    "normal_reserve_soc_pct": 5.0,
                    "high_uncertainty_reserve_soc_pct": 5.0,
                },
                "economics": {
                    "import_overhead_ore_kwh": 41.0,
                    "export_overhead_ore_kwh": 0.0,
                    "minimum_arbitrage_margin_ore_kwh": 0.0,
                },
            },
            "optimizer": {
                "battery_max_charge_kw": 8.0,
                "battery_max_discharge_kw": 8.0,
                "battery_charge_efficiency": 0.95,
                "battery_discharge_efficiency": 0.95,
                "battery_degradation_ore_kwh": 0.0,
                "physical_grid_import_limit_kw": 0.0,
                "grid_export_limit_kw": 10.0,
                "soc_grid_step_kwh": 0.5,
                "refined_soc_grid_step_kwh": 0.1,
                "reserve_critical_soc_pct": 5.0,
                "reserve_critical_penalty_ore_per_kwh_hour": 0.0,
                "reserve_preferred_penalty_ore_per_kwh_hour": 0.0,
                "reserve_target_penalty_ore_per_kwh_hour": 0.0,
                "preferred_max_excess_penalty_ore_per_kwh_hour": 0.0,
                "reserve_uncertainty_full_scale_kw": 3.0,
                "terminal_soc_tolerance_pct": 0.0,
                "terminal_soc_tiebreak_ore_per_kwh": 0.0,
            },
        }

    def test_registry_keeps_v35_as_only_baseline_and_adds_refined_challenger(self):
        status = registry_status()
        baselines = [row for row in status["engines"] if row["baseline"]]
        self.assertEqual([row["engine_id"] for row in baselines], [BASELINE_ENGINE_ID])
        ids = {row["engine_id"] for row in status["engines"]}
        self.assertIn(REFINED_ENGINE_ID, ids)
        self.assertEqual(REFINED_ENGINE_ID, ENGINE_ID)

    def test_exact_pv_following_transition_can_absorb_sub_grid_surplus(self):
        rows = [
            {
                "start": "2026-08-29T08:00:00+00:00",
                "load_kw": 1.0,
                "pv_kw": 1.5,
                "load_uncertainty_kw": 0.0,
                "pv_uncertainty_kw": 0.0,
                "price_known": True,
                "price_ore_kwh": 2.0,
            },
            {
                "start": "2026-08-29T08:15:00+00:00",
                "load_kw": 0.45125,
                "pv_kw": 0.0,
                "load_uncertainty_kw": 0.0,
                "pv_uncertainty_kw": 0.0,
                "price_known": True,
                "price_ore_kwh": 200.0,
            },
        ]

        solved = solve_refined_from_rows(self._cfg(), rows, 50.0)

        self.assertEqual(solved["soc_grid_requested_step_kwh"], 0.1)
        self.assertTrue(solved["rows"][0]["pv_following_transition"])
        self.assertAlmostEqual(solved["first_action_kw"], -0.5, places=6)
        self.assertAlmostEqual(solved["first_expected_soc_pct"], 51.1875, places=4)
        self.assertGreaterEqual(solved["pv_following_transitions_selected"], 1)


if __name__ == "__main__":
    unittest.main()
