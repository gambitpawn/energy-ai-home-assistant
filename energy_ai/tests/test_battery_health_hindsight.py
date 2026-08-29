from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.battery_health_hindsight import (
    HEALTH_PROFILES,
    compare_profiles_for_rows,
    profile_parameters,
    solve_hindsight_rows,
)


def _cfg():
    return {
        "policy": {
            "battery": {
                "capacity_kwh": 19.6,
                "hard_min_soc_pct": 5.0,
                "hard_max_soc_pct": 100.0,
                "preferred_min_soc_pct": 15.0,
                "normal_reserve_soc_pct": 20.0,
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
            "physical_grid_import_limit_kw": 13.8,
            "grid_export_limit_kw": 10.0,
            "reserve_critical_soc_pct": 10.0,
            "reserve_critical_penalty_ore_per_kwh_hour": 300.0,
            "reserve_preferred_penalty_ore_per_kwh_hour": 100.0,
            "reserve_target_penalty_ore_per_kwh_hour": 10.0,
        },
        "tariffs": {"enabled": False},
    }


def _flat_rows(count: int = 12, price: float = 100.0):
    start = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    return [
        {
            "start": (start + timedelta(minutes=15 * i)).isoformat(),
            "load_kw": 0.0,
            "pv_kw": 0.0,
            "price_ore_kwh": price,
        }
        for i in range(count)
    ]


def test_profile_rates_match_mild_default_strong_contract():
    assert set(HEALTH_PROFILES) == {"mild", "default", "strong"}
    assert profile_parameters("mild").high_soc_zone_3_cost_ore_per_kwh_hour == 25.0
    assert profile_parameters("default").high_soc_zone_3_cost_ore_per_kwh_hour == 50.0
    assert profile_parameters("strong").high_soc_zone_3_cost_ore_per_kwh_hour == 100.0


def test_flat_price_oracle_delays_full_charge_until_end_of_horizon():
    rows = _flat_rows()
    result = solve_hindsight_rows(
        _cfg(),
        rows,
        initial_soc_pct=90.0,
        terminal_soc_pct=100.0,
        parameters=profile_parameters("default"),
        grid_step_kwh=0.1,
        include_actions=True,
    )

    assert result["status"] == "optimal"
    assert result["terminal_soc_pct"] == 100.0
    assert result["max_soc_pct"] == 100.0
    assert result["last_charge_start"] == rows[-1]["start"]
    assert result["first_reach_99_5_soc"] == (
        datetime.fromisoformat(rows[-1]["start"]) + timedelta(minutes=15)
    ).isoformat()
    assert result["hours_above_98_soc"] < 0.25
    assert len(result["actions"]) == len(rows)


def test_profile_comparison_uses_same_terminal_soc_and_stronger_health_price():
    comparison = compare_profiles_for_rows(
        _cfg(),
        _flat_rows(),
        initial_soc_pct=90.0,
        terminal_soc_pct=100.0,
        grid_step_kwh=0.1,
        include_actions=False,
    )

    assert comparison["diagnostic_only"] is True
    assert comparison["planner_integration_enabled"] is False
    assert comparison["physical_write_performed"] is False
    mild = comparison["profiles"]["mild"]
    default = comparison["profiles"]["default"]
    strong = comparison["profiles"]["strong"]
    assert mild["terminal_soc_pct"] == default["terminal_soc_pct"] == strong["terminal_soc_pct"] == 100.0
    assert mild["battery_health_cost_ore"] < default["battery_health_cost_ore"] < strong["battery_health_cost_ore"]
    assert mild["canonical_objective_cost_ore"] < default["canonical_objective_cost_ore"] < strong["canonical_objective_cost_ore"]
