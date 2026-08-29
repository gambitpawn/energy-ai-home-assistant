from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.battery_health_scan import (
    compare_profiles_with_off_for_rows,
    economic_value_above_soc_threshold_for_rows,
    off_profile_parameters,
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


def _arbitrage_rows():
    start = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(16):
        cheap = i < 8
        rows.append(
            {
                "start": (start + timedelta(minutes=15 * i)).isoformat(),
                "load_kw": 0.0 if cheap else 5.0,
                "pv_kw": 0.0,
                "price_ore_kwh": 0.0 if cheap else 300.0,
            }
        )
    return rows


def test_off_profile_disables_high_soc_cost_but_keeps_cycle_wear():
    params = off_profile_parameters()
    assert params.high_soc_enabled is False
    assert params.cycle_wear_ore_per_kwh == 5.0
    assert params.high_soc_zone_1_cost_ore_per_kwh_hour == 0.0
    assert params.high_soc_zone_2_cost_ore_per_kwh_hour == 0.0
    assert params.high_soc_zone_3_cost_ore_per_kwh_hour == 0.0


def test_profile_comparison_adds_off_without_changing_existing_profiles():
    result = compare_profiles_with_off_for_rows(
        _cfg(),
        _arbitrage_rows(),
        initial_soc_pct=50.0,
        terminal_soc_pct=50.0,
        grid_step_kwh=0.2,
    )
    assert list(result["profiles"])[0] == "off"
    assert set(result["profiles"]) == {"off", "mild", "default", "strong"}
    off = result["profiles"]["off"]
    assert off["status"] == "optimal"
    assert off["high_soc_occupancy_cost_ore"] == 0.0


def test_capacity_above_90_has_positive_value_on_large_price_spread():
    result = economic_value_above_soc_threshold_for_rows(
        _cfg(),
        _arbitrage_rows(),
        initial_soc_pct=50.0,
        terminal_soc_pct=50.0,
        threshold_pct=90.0,
        grid_step_kwh=0.2,
    )
    assert result["status"] == "ok"
    assert result["economic_value_ore"] > 0.0
    assert result["capped_max_soc_pct"] <= 90.0 + 1e-9


def test_capacity_value_is_not_comparable_when_boundary_soc_exceeds_threshold():
    result = economic_value_above_soc_threshold_for_rows(
        _cfg(),
        _arbitrage_rows(),
        initial_soc_pct=95.0,
        terminal_soc_pct=50.0,
        threshold_pct=90.0,
        grid_step_kwh=0.2,
    )
    assert result["status"] == "boundary_soc_above_threshold"
    assert result["economic_value_ore"] is None
