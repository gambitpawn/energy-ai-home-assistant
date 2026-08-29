from __future__ import annotations

import math

from app.battery_health_routes import battery_health_test_payload


def _cfg(capacity_kwh: float = 19.6):
    return {"policy": {"battery": {"capacity_kwh": capacity_kwh}}}


def test_defaults_use_installed_capacity_and_are_diagnostic_only():
    payload = battery_health_test_payload(
        _cfg(),
        soc_start_pct=100.0,
        soc_end_pct=100.0,
        interval_hours=1.0,
    )
    assert payload["diagnostic_only"] is True
    assert payload["planner_integration_enabled"] is False
    assert payload["physical_write_performed"] is False
    assert payload["input"]["capacity_kwh"] == 19.6
    assert math.isclose(payload["result"]["high_soc_occupancy_cost_ore"], 33.32, abs_tol=1e-9)
    assert math.isclose(payload["result"]["total_battery_health_cost_sek"], 0.3332, abs_tol=1e-9)


def test_custom_rates_are_applied_without_mutating_defaults():
    custom = battery_health_test_payload(
        _cfg(),
        soc_start_pct=100.0,
        soc_end_pct=100.0,
        interval_hours=1.0,
        zone_1_ore_per_kwh_hour=10.0,
        zone_2_ore_per_kwh_hour=30.0,
        zone_3_ore_per_kwh_hour=100.0,
    )
    default_again = battery_health_test_payload(
        _cfg(),
        soc_start_pct=100.0,
        soc_end_pct=100.0,
        interval_hours=1.0,
    )
    assert custom["result"]["high_soc_occupancy_cost_ore"] > default_again["result"]["high_soc_occupancy_cost_ore"]
    assert default_again["result"]["parameters"]["high_soc_zone_3_cost_ore_per_kwh_hour"] == 50.0


def test_90_to_100_transition_reports_cycle_and_high_soc_cost_separately():
    payload = battery_health_test_payload(
        _cfg(),
        soc_start_pct=90.0,
        soc_end_pct=100.0,
        interval_hours=1.0,
    )
    result = payload["result"]
    assert math.isclose(result["internal_throughput_kwh"], 1.96, abs_tol=1e-9)
    assert math.isclose(result["cycle_wear_cost_ore"], 9.8, abs_tol=1e-9)
    assert result["high_soc_occupancy_cost_ore"] > 0.0
    assert math.isclose(
        result["total_battery_health_cost_ore"],
        result["cycle_wear_cost_ore"] + result["high_soc_occupancy_cost_ore"],
        abs_tol=1e-9,
    )
