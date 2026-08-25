from __future__ import annotations

import unittest

from app.tariff_scenarios import run_edge_cases


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
        "reserve_critical_soc_pct": 10.0,
        "reserve_critical_penalty_ore_per_kwh_hour": 300.0,
        "reserve_preferred_penalty_ore_per_kwh_hour": 100.0,
        "reserve_target_penalty_ore_per_kwh_hour": 10.0,
        "preferred_max_excess_penalty_ore_per_kwh_hour": 2.0,
        "reserve_uncertainty_full_scale_kw": 3.0,
    },
    "tariffs": {"enabled": False, "test_only": True},
}


class TariffScenarioTests(unittest.TestCase):
    def test_edge_cases(self):
        result = run_edge_cases(CFG)
        failures = {name: item for name, item in result["tests"].items() if not item["pass"]}
        self.assertEqual({}, failures, failures)


if __name__ == "__main__":
    unittest.main()
