from __future__ import annotations

import math

import pytest

from app.battery_health_cost import (
    BATTERY_HEALTH_COST_VERSION,
    BatteryHealthParameters,
    battery_health_cost,
)


CAP = 19.6


def _energy(soc_pct: float) -> float:
    return CAP * soc_pct / 100.0


def test_defaults_match_initial_high_soc_policy():
    p = BatteryHealthParameters()
    assert p.cycle_wear_ore_per_kwh == 5.0
    assert (p.high_soc_threshold_1_pct, p.high_soc_threshold_2_pct, p.high_soc_threshold_3_pct) == (90.0, 95.0, 98.0)
    assert (
        p.high_soc_zone_1_cost_ore_per_kwh_hour,
        p.high_soc_zone_2_cost_ore_per_kwh_hour,
        p.high_soc_zone_3_cost_ore_per_kwh_hour,
    ) == (5.0, 15.0, 50.0)


def test_no_high_soc_cost_below_first_threshold():
    result = battery_health_cost(
        energy_start_kwh=_energy(80.0),
        energy_end_kwh=_energy(80.0),
        capacity_kwh=CAP,
        interval_hours=1.0,
    )
    assert result["version"] == BATTERY_HEALTH_COST_VERSION
    assert result["high_soc_occupancy_cost_ore"] == pytest.approx(0.0)
    assert result["cycle_wear_cost_ore"] == pytest.approx(0.0)
    assert result["total_battery_health_cost_ore"] == pytest.approx(0.0)


def test_holding_95_percent_for_one_hour_costs_zone_1_only():
    result = battery_health_cost(
        energy_start_kwh=_energy(95.0),
        energy_end_kwh=_energy(95.0),
        capacity_kwh=CAP,
        interval_hours=1.0,
    )
    assert result["high_soc_zone_1_energy_hours"] == pytest.approx(0.98)
    assert result["high_soc_zone_1_cost_ore"] == pytest.approx(4.9)
    assert result["high_soc_zone_2_cost_ore"] == pytest.approx(0.0)
    assert result["high_soc_zone_3_cost_ore"] == pytest.approx(0.0)
    assert result["high_soc_occupancy_cost_ore"] == pytest.approx(4.9)


def test_holding_98_percent_for_one_hour_accumulates_lower_zones():
    result = battery_health_cost(
        energy_start_kwh=_energy(98.0),
        energy_end_kwh=_energy(98.0),
        capacity_kwh=CAP,
        interval_hours=1.0,
    )
    assert result["high_soc_zone_1_energy_hours"] == pytest.approx(0.98)
    assert result["high_soc_zone_2_energy_hours"] == pytest.approx(0.588)
    assert result["high_soc_zone_1_cost_ore"] == pytest.approx(4.9)
    assert result["high_soc_zone_2_cost_ore"] == pytest.approx(8.82)
    assert result["high_soc_zone_3_cost_ore"] == pytest.approx(0.0)
    assert result["high_soc_occupancy_cost_ore"] == pytest.approx(13.72)


def test_holding_100_percent_for_one_hour_matches_proposed_default_cost():
    result = battery_health_cost(
        energy_start_kwh=CAP,
        energy_end_kwh=CAP,
        capacity_kwh=CAP,
        interval_hours=1.0,
    )
    assert result["high_soc_zone_1_cost_ore"] == pytest.approx(4.9)
    assert result["high_soc_zone_2_cost_ore"] == pytest.approx(8.82)
    assert result["high_soc_zone_3_cost_ore"] == pytest.approx(19.6)
    assert result["high_soc_occupancy_cost_ore"] == pytest.approx(33.32)


def test_holding_100_percent_for_eight_hours_is_about_2_67_sek():
    result = battery_health_cost(
        energy_start_kwh=CAP,
        energy_end_kwh=CAP,
        capacity_kwh=CAP,
        interval_hours=8.0,
    )
    assert result["high_soc_occupancy_cost_ore"] == pytest.approx(266.56)


def test_crossing_all_high_soc_zones_is_integrated_exactly():
    result = battery_health_cost(
        energy_start_kwh=_energy(90.0),
        energy_end_kwh=_energy(100.0),
        capacity_kwh=CAP,
        interval_hours=1.0,
    )

    # Linear 90 -> 100% transition. Exact occupied-energy integrals are:
    # zone 90-95: 0.735 kWh*h; zone 95-98: 0.2058; zone 98-100: 0.0392.
    assert result["high_soc_zone_1_energy_hours"] == pytest.approx(0.735)
    assert result["high_soc_zone_2_energy_hours"] == pytest.approx(0.2058)
    assert result["high_soc_zone_3_energy_hours"] == pytest.approx(0.0392)
    assert result["high_soc_occupancy_cost_ore"] == pytest.approx(8.722)
    assert result["internal_throughput_kwh"] == pytest.approx(1.96)
    assert result["cycle_wear_cost_ore"] == pytest.approx(9.8)
    assert result["total_battery_health_cost_ore"] == pytest.approx(18.522)


def test_charge_and_discharge_have_same_health_cost_for_same_energy_path():
    charge = battery_health_cost(
        energy_start_kwh=_energy(90.0),
        energy_end_kwh=_energy(100.0),
        capacity_kwh=CAP,
        interval_hours=1.0,
    )
    discharge = battery_health_cost(
        energy_start_kwh=_energy(100.0),
        energy_end_kwh=_energy(90.0),
        capacity_kwh=CAP,
        interval_hours=1.0,
    )
    assert discharge["high_soc_occupancy_cost_ore"] == pytest.approx(charge["high_soc_occupancy_cost_ore"])
    assert discharge["cycle_wear_cost_ore"] == pytest.approx(charge["cycle_wear_cost_ore"])
    assert discharge["total_battery_health_cost_ore"] == pytest.approx(charge["total_battery_health_cost_ore"])


def test_late_full_charge_is_cheaper_than_holding_full_for_hours():
    held = battery_health_cost(
        energy_start_kwh=CAP,
        energy_end_kwh=CAP,
        capacity_kwh=CAP,
        interval_hours=4.0,
    )
    late_charge = battery_health_cost(
        energy_start_kwh=_energy(90.0),
        energy_end_kwh=CAP,
        capacity_kwh=CAP,
        interval_hours=1.0,
    )
    assert held["high_soc_occupancy_cost_ore"] > late_charge["high_soc_occupancy_cost_ore"]


def test_disabling_high_soc_cost_keeps_cycle_wear_only():
    p = BatteryHealthParameters(high_soc_enabled=False)
    result = battery_health_cost(
        energy_start_kwh=_energy(90.0),
        energy_end_kwh=CAP,
        capacity_kwh=CAP,
        interval_hours=1.0,
        parameters=p,
    )
    assert result["high_soc_occupancy_cost_ore"] == pytest.approx(0.0)
    assert result["cycle_wear_cost_ore"] == pytest.approx(9.8)
    assert result["total_battery_health_cost_ore"] == pytest.approx(9.8)


def test_invalid_thresholds_and_decreasing_costs_are_rejected():
    with pytest.raises(ValueError):
        BatteryHealthParameters(
            high_soc_threshold_1_pct=95.0,
            high_soc_threshold_2_pct=90.0,
        ).validated()
    with pytest.raises(ValueError):
        BatteryHealthParameters(
            high_soc_zone_1_cost_ore_per_kwh_hour=20.0,
            high_soc_zone_2_cost_ore_per_kwh_hour=10.0,
        ).validated()


def test_energy_inputs_are_clamped_to_physical_capacity_for_costing():
    result = battery_health_cost(
        energy_start_kwh=-2.0,
        energy_end_kwh=CAP + 5.0,
        capacity_kwh=CAP,
        interval_hours=1.0,
    )
    assert result["energy_start_kwh"] == 0.0
    assert result["energy_end_kwh"] == CAP
    assert math.isclose(result["internal_throughput_kwh"], CAP)
