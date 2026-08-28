from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine_contract import EngineInput
from app.engine_registry import descriptor
from app.optimizer_v35_replay import solve_v35_from_rows
from app.stochastic_engine import (
    CVAR_ALPHA,
    RISK_AVERSION,
    SCENARIO_SPECS,
    StochasticDeterministicV1Engine,
    build_scenarios,
    solve_stochastic_from_rows,
    weighted_cvar,
)


def _cfg():
    return {
        "policy": {
            "battery": {
                "capacity_kwh": 20.0,
                "hard_min_soc_pct": 5.0,
                "hard_max_soc_pct": 100.0,
                "preferred_min_soc_pct": 5.0,
                "preferred_max_soc_pct": 100.0,
                "normal_reserve_soc_pct": 5.0,
                "high_uncertainty_reserve_soc_pct": 5.0,
            },
            "economics": {
                "import_overhead_ore_kwh": 0.0,
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
            "physical_grid_import_limit_kw": 20.0,
            "grid_export_limit_kw": 20.0,
            "soc_grid_step_kwh": 0.5,
            "terminal_soc_tolerance_pct": 3.0,
            "terminal_soc_tiebreak_ore_per_kwh": 0.0,
            "reserve_uncertainty_full_scale_kw": 3.0,
            "reserve_critical_soc_pct": 5.0,
            "reserve_critical_penalty_ore_per_kwh_hour": 0.0,
            "reserve_preferred_penalty_ore_per_kwh_hour": 0.0,
            "reserve_target_penalty_ore_per_kwh_hour": 0.0,
            "preferred_max_excess_penalty_ore_per_kwh_hour": 0.0,
            "unknown_price_energy_coverage_fraction": 0.35,
            "unknown_price_risk_premium_ore_kwh": 0.0,
            "unknown_price_default_continuation_value_ore_kwh": 0.0,
        },
        "tariffs": {"enabled": False},
    }


def _rows(*, uncertainty=0.0):
    start = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    prices = [60.0, 240.0, 20.0, 160.0]
    rows = []
    for i, price in enumerate(prices):
        rows.append(
            {
                "start": (start + timedelta(minutes=15 * i)).isoformat(),
                "load_kw": 3.0,
                "pv_kw": 1.0,
                "load_uncertainty_kw": float(uncertainty),
                "pv_uncertainty_kw": float(uncertainty) * 0.8,
                "price_known": True,
                "price_ore_kwh": price,
            }
        )
    return rows


def test_registry_exposes_stochastic_challenger():
    item = descriptor("stochastic_deterministic_v1")
    assert item.available is True
    assert item.baseline is False
    assert item.trainable is False
    assert item.family == "deterministic"


def test_scenario_distribution_is_probability_normalized_and_unbiased():
    assert abs(sum(spec.weight for spec in SCENARIO_SPECS) - 1.0) < 1e-12
    assert abs(sum(spec.weight * spec.load_sigma for spec in SCENARIO_SPECS)) < 1e-12
    assert abs(sum(spec.weight * spec.pv_sigma for spec in SCENARIO_SPECS)) < 1e-12
    scenarios = build_scenarios(_rows(uncertainty=1.0))
    assert len(scenarios) == 5
    assert {s.spec.name for s in scenarios} >= {"nominal", "high_load_low_pv", "low_load_high_pv"}


def test_weighted_cvar_uses_upper_cost_tail():
    value = weighted_cvar([(0.8, 10.0), (0.2, 30.0)], alpha=0.8)
    assert abs(value - 30.0) < 1e-9


def test_zero_uncertainty_collapses_exactly_to_frozen_v35():
    rows = _rows(uncertainty=0.0)
    baseline = solve_v35_from_rows(_cfg(), rows, 50.0)
    stochastic = solve_stochastic_from_rows(_cfg(), rows, 50.0)
    assert stochastic["collapsed_to_deterministic"] is True
    assert stochastic["first_action_kw"] == baseline["first_action_kw"]
    assert stochastic["first_expected_soc_pct"] == baseline["first_expected_soc_pct"]


def test_stochastic_engine_produces_common_vintage_safe_first_action():
    rows = _rows(uncertainty=1.2)
    engine_input = EngineInput(
        generated_at="2026-08-28T11:59:30+00:00",
        decision_start=rows[0]["start"],
        initial_soc_pct=50.0,
        interval_minutes=15,
        horizon_rows=tuple(rows),
        constraints={},
        objective={},
        source={"kind": "test"},
    )
    decision = StochasticDeterministicV1Engine(_cfg()).decide(engine_input)
    assert decision.engine_id == "stochastic_deterministic_v1"
    assert decision.information_vintage_id == engine_input.information_vintage_id
    assert decision.decision_start == engine_input.decision_start
    assert -8.0 <= decision.requested_action_kw <= 8.0
    assert 5.0 <= decision.expected_soc_pct <= 100.0
    assert decision.model["scenario_count"] == 5
    assert decision.model["cvar_alpha"] == CVAR_ALPHA
    assert decision.model["risk_aversion"] == RISK_AVERSION
    assert decision.diagnostics["nonanticipativity"] == "same_first_action_across_all_scenarios"
    assert len(decision.diagnostics["scenario_costs"]) == 5
    assert decision.model["qualification_required"] == "robust10_v1"
