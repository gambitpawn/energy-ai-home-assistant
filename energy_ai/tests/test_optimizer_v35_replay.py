from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.optimizer_v35_replay import solve_v35_from_rows

CFG = {
    "policy": {
        "battery": {
            "capacity_kwh": 19.6,
            "hard_min_soc_pct": 5.0,
            "hard_max_soc_pct": 100.0,
            "preferred_min_soc_pct": 15.0,
            "preferred_max_soc_pct": 90.0,
            "normal_reserve_soc_pct": 20.0,
            "high_uncertainty_reserve_soc_pct": 28.0,
        },
        "economics": {
            "import_overhead_ore_kwh": 0.0,
            "export_overhead_ore_kwh": 0.0,
            "minimum_arbitrage_margin_ore_kwh": 20.0,
        },
    },
    "optimizer": {
        "battery_max_charge_kw": 8.0,
        "battery_max_discharge_kw": 8.0,
        "battery_charge_efficiency": 0.95,
        "battery_discharge_efficiency": 0.95,
        "battery_degradation_ore_kwh": 5.0,
        "physical_grid_import_limit_kw": 13.8,
        "grid_export_limit_kw": 10.0,
        "soc_grid_step_kwh": 0.5,
        "reserve_critical_soc_pct": 10.0,
        "reserve_critical_penalty_ore_per_kwh_hour": 300.0,
        "reserve_preferred_penalty_ore_per_kwh_hour": 100.0,
        "reserve_target_penalty_ore_per_kwh_hour": 10.0,
        "preferred_max_excess_penalty_ore_per_kwh_hour": 2.0,
        "reserve_uncertainty_full_scale_kw": 3.0,
        "terminal_soc_tolerance_pct": 3.0,
        "terminal_soc_tiebreak_ore_per_kwh": 5.0,
        "unknown_price_energy_coverage_fraction": 0.35,
        "unknown_price_risk_premium_ore_kwh": 40.0,
        "unknown_price_default_continuation_value_ore_kwh": 150.0,
    },
}


def rows(prices):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "start": (start + timedelta(minutes=15 * i)).isoformat(),
            "load_kw": 1.0,
            "pv_kw": 0.0,
            "load_uncertainty_kw": 0.0,
            "pv_uncertainty_kw": 0.0,
            "price_known": True,
            "price_ore_kwh": float(price),
        }
        for i, price in enumerate(prices)
    ]


class PureReplayTests(unittest.TestCase):
    def test_fully_priced_horizon_finishes_near_initial_soc(self):
        result = solve_v35_from_rows(CFG, rows([100.0] * 16), 50.0)
        self.assertTrue(result["terminal_soc_constraint_applied"])
        self.assertLessEqual(abs(result["terminal_soc_pct"] - 50.0), 3.01)

    def test_clear_future_spread_changes_first_action(self):
        flat = solve_v35_from_rows(CFG, rows([100.0] * 16), 50.0)
        spread = solve_v35_from_rows(CFG, rows([10.0] * 4 + [400.0] * 12), 50.0)
        self.assertLessEqual(spread["first_action_kw"], flat["first_action_kw"] + 1e-9)

    def test_measured_soc_below_hard_min_is_clamped_for_planning(self):
        result = solve_v35_from_rows(CFG, rows([100.0] * 4), 4.9)
        self.assertAlmostEqual(4.9, result["measured_initial_soc_pct"], places=6)
        self.assertAlmostEqual(5.0, result["planning_initial_soc_pct"], places=6)


if __name__ == "__main__":
    unittest.main()
