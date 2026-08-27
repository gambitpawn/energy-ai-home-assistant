from __future__ import annotations

import unittest
from unittest.mock import patch

from app.engine_contract import EngineInput, input_from_optimizer_plan
from app.engine_registry import BASELINE_ENGINE_ID, DeterministicV35Adapter, registry_status


class EngineContractTests(unittest.TestCase):
    def _rows(self):
        return (
            {
                "start": "2026-08-27T08:00:00+00:00",
                "load_kw": 2.0,
                "pv_kw": 0.5,
                "load_uncertainty_kw": 0.2,
                "pv_uncertainty_kw": 0.1,
                "price_known": True,
                "price_ore_kwh": 100.0,
            },
            {
                "start": "2026-08-27T08:15:00+00:00",
                "load_kw": 2.2,
                "pv_kw": 0.8,
                "load_uncertainty_kw": 0.2,
                "pv_uncertainty_kw": 0.1,
                "price_known": False,
                "price_ore_kwh": None,
            },
        )

    def test_registry_has_one_immutable_deterministic_baseline(self):
        status = registry_status()
        self.assertEqual(status["baseline_engine_id"], BASELINE_ENGINE_ID)
        baselines = [e for e in status["engines"] if e["baseline"]]
        self.assertEqual(len(baselines), 1)
        self.assertEqual(baselines[0]["engine_id"], "deterministic_v35")
        self.assertEqual(baselines[0]["family"], "deterministic")
        self.assertFalse(status["selection"]["active_control_enabled"])

    def test_information_vintage_id_is_stable_and_sensitive_to_soc(self):
        kwargs = dict(
            generated_at="2026-08-27T07:59:20+00:00",
            decision_start="2026-08-27T08:00:00+00:00",
            interval_minutes=15,
            horizon_rows=self._rows(),
            constraints={"battery_capacity_kwh": 19.6},
            objective={"battery_degradation_cost": True},
            source={"kind": "test"},
        )
        a = EngineInput(initial_soc_pct=50.0, **kwargs)
        b = EngineInput(initial_soc_pct=50.0, **kwargs)
        c = EngineInput(initial_soc_pct=51.0, **kwargs)
        self.assertEqual(a.information_vintage_id, b.information_vintage_id)
        self.assertNotEqual(a.information_vintage_id, c.information_vintage_id)
        self.assertEqual(a.price_known_intervals, 1)

    def test_plan_adapter_creates_shared_input_contract(self):
        rows = [dict(r) for r in self._rows()]
        for row in rows:
            row.update({
                "battery_action_kw": 0.0,
                "expected_soc_pct": 50.0,
                "grid_import_kw": 0.0,
                "grid_export_kw": 0.0,
            })
        plan = {
            "generated_at": "2026-08-27T07:59:20+00:00",
            "planner": "deterministic_battery_dp_v3_5",
            "mode": "shadow_read_only",
            "interval_minutes": 15,
            "initial_soc_pct": 50.0,
            "constraints": {"battery_capacity_kwh": 19.6},
            "objective": {"battery_degradation_cost": True},
            "rows": rows,
        }
        engine_input = input_from_optimizer_plan(plan)
        self.assertEqual(engine_input.decision_start, rows[0]["start"])
        self.assertEqual(len(engine_input.horizon_rows), 2)
        self.assertEqual(engine_input.source["source_planner"], "deterministic_battery_dp_v3_5")

    @patch("app.engine_registry.solve_v35_from_rows")
    def test_baseline_adapter_returns_pre_safety_decision(self, solve):
        solve.return_value = {
            "engine": "pure_v35_replay_v1",
            "first_action_kw": -3.25,
            "first_expected_soc_pct": 54.0,
            "terminal_soc_pct": 50.0,
            "terminal_soc_constraint_applied": True,
            "objective_cost_ore": 12.3,
            "continuation": {"enabled": False},
            "rows": [
                {"action_kw": -3.25, "soc_end_pct": 54.0, "reserve_soc_pct": 20.0},
                {"action_kw": 3.0, "soc_end_pct": 50.0, "reserve_soc_pct": 20.0},
            ],
        }
        engine_input = EngineInput(
            generated_at="2026-08-27T07:59:20+00:00",
            decision_start="2026-08-27T08:00:00+00:00",
            initial_soc_pct=50.0,
            interval_minutes=15,
            horizon_rows=self._rows(),
        )
        decision = DeterministicV35Adapter({}).decide(engine_input)
        payload = decision.as_dict()
        self.assertEqual(payload["engine_id"], "deterministic_v35")
        self.assertEqual(payload["requested_action_kw"], -3.25)
        self.assertTrue(payload["safety_semantics"]["requested_action_is_pre_safety"])
        self.assertFalse(payload["safety_semantics"]["physical_authority"])
        self.assertEqual(payload["information_vintage_id"], engine_input.information_vintage_id)


if __name__ == "__main__":
    unittest.main()
