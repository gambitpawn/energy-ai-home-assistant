from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query

from .battery_health_cost import (
    BATTERY_HEALTH_COST_VERSION,
    DEFAULT_BATTERY_HEALTH_PARAMETERS,
    BatteryHealthParameters,
    battery_health_cost,
)


def battery_health_test_payload(
    cfg: dict[str, Any],
    *,
    soc_start_pct: float,
    soc_end_pct: float,
    interval_hours: float = 1.0,
    capacity_kwh: float | None = None,
    cycle_wear_ore_per_kwh: float | None = None,
    threshold_1_pct: float | None = None,
    threshold_2_pct: float | None = None,
    threshold_3_pct: float | None = None,
    zone_1_ore_per_kwh_hour: float | None = None,
    zone_2_ore_per_kwh_hour: float | None = None,
    zone_3_ore_per_kwh_hour: float | None = None,
    high_soc_enabled: bool = True,
) -> dict[str, Any]:
    """Evaluate the standalone battery-health function for one synthetic interval.

    This endpoint helper is deliberately diagnostic-only. It does not modify
    planner configuration, selector state, actuator state or physical control.
    """
    battery = (cfg.get("policy") or {}).get("battery") or {}
    cap = float(capacity_kwh if capacity_kwh is not None else battery.get("capacity_kwh", 19.6))
    if cap <= 0.0:
        raise ValueError("capacity_kwh must be > 0")
    if not (0.0 <= float(soc_start_pct) <= 100.0):
        raise ValueError("soc_start_pct must be between 0 and 100")
    if not (0.0 <= float(soc_end_pct) <= 100.0):
        raise ValueError("soc_end_pct must be between 0 and 100")
    if float(interval_hours) < 0.0:
        raise ValueError("interval_hours must be non-negative")

    defaults = DEFAULT_BATTERY_HEALTH_PARAMETERS
    params = BatteryHealthParameters(
        cycle_wear_ore_per_kwh=(
            float(cycle_wear_ore_per_kwh)
            if cycle_wear_ore_per_kwh is not None
            else defaults.cycle_wear_ore_per_kwh
        ),
        high_soc_enabled=bool(high_soc_enabled),
        high_soc_threshold_1_pct=(
            float(threshold_1_pct) if threshold_1_pct is not None else defaults.high_soc_threshold_1_pct
        ),
        high_soc_threshold_2_pct=(
            float(threshold_2_pct) if threshold_2_pct is not None else defaults.high_soc_threshold_2_pct
        ),
        high_soc_threshold_3_pct=(
            float(threshold_3_pct) if threshold_3_pct is not None else defaults.high_soc_threshold_3_pct
        ),
        high_soc_zone_1_cost_ore_per_kwh_hour=(
            float(zone_1_ore_per_kwh_hour)
            if zone_1_ore_per_kwh_hour is not None
            else defaults.high_soc_zone_1_cost_ore_per_kwh_hour
        ),
        high_soc_zone_2_cost_ore_per_kwh_hour=(
            float(zone_2_ore_per_kwh_hour)
            if zone_2_ore_per_kwh_hour is not None
            else defaults.high_soc_zone_2_cost_ore_per_kwh_hour
        ),
        high_soc_zone_3_cost_ore_per_kwh_hour=(
            float(zone_3_ore_per_kwh_hour)
            if zone_3_ore_per_kwh_hour is not None
            else defaults.high_soc_zone_3_cost_ore_per_kwh_hour
        ),
    ).validated()

    result = battery_health_cost(
        energy_start_kwh=cap * float(soc_start_pct) / 100.0,
        energy_end_kwh=cap * float(soc_end_pct) / 100.0,
        capacity_kwh=cap,
        interval_hours=float(interval_hours),
        parameters=params,
    )
    return {
        "diagnostic_only": True,
        "planner_integration_enabled": False,
        "physical_write_performed": False,
        "cost_version": BATTERY_HEALTH_COST_VERSION,
        "input": {
            "soc_start_pct": float(soc_start_pct),
            "soc_end_pct": float(soc_end_pct),
            "interval_hours": float(interval_hours),
            "capacity_kwh": cap,
        },
        "result": {
            **result,
            "cycle_wear_cost_sek": float(result["cycle_wear_cost_ore"]) / 100.0,
            "high_soc_occupancy_cost_sek": float(result["high_soc_occupancy_cost_ore"]) / 100.0,
            "total_battery_health_cost_sek": float(result["total_battery_health_cost_ore"]) / 100.0,
        },
    }


def install_battery_health_routes(app: FastAPI, cfg: dict[str, Any]) -> None:
    @app.get("/diagnostics/battery-health/defaults", tags=["diagnostics"])
    async def battery_health_defaults():
        battery = (cfg.get("policy") or {}).get("battery") or {}
        return {
            "cost_version": BATTERY_HEALTH_COST_VERSION,
            "diagnostic_only": True,
            "planner_integration_enabled": False,
            "installation_capacity_kwh": float(battery.get("capacity_kwh", 19.6)),
            "parameters": DEFAULT_BATTERY_HEALTH_PARAMETERS.as_dict(),
        }

    @app.get("/diagnostics/battery-health/cost", tags=["diagnostics"])
    async def battery_health_cost_test(
        soc_start_pct: float = Query(..., ge=0.0, le=100.0),
        soc_end_pct: float = Query(..., ge=0.0, le=100.0),
        interval_hours: float = Query(1.0, ge=0.0, le=48.0),
        capacity_kwh: float | None = Query(None, gt=0.0, le=500.0),
        cycle_wear_ore_per_kwh: float | None = Query(None, ge=0.0, le=10000.0),
        threshold_1_pct: float | None = Query(None, ge=0.0, lt=100.0),
        threshold_2_pct: float | None = Query(None, ge=0.0, lt=100.0),
        threshold_3_pct: float | None = Query(None, ge=0.0, lt=100.0),
        zone_1_ore_per_kwh_hour: float | None = Query(None, ge=0.0, le=10000.0),
        zone_2_ore_per_kwh_hour: float | None = Query(None, ge=0.0, le=10000.0),
        zone_3_ore_per_kwh_hour: float | None = Query(None, ge=0.0, le=10000.0),
        high_soc_enabled: bool = Query(True),
    ):
        return battery_health_test_payload(
            cfg,
            soc_start_pct=soc_start_pct,
            soc_end_pct=soc_end_pct,
            interval_hours=interval_hours,
            capacity_kwh=capacity_kwh,
            cycle_wear_ore_per_kwh=cycle_wear_ore_per_kwh,
            threshold_1_pct=threshold_1_pct,
            threshold_2_pct=threshold_2_pct,
            threshold_3_pct=threshold_3_pct,
            zone_1_ore_per_kwh_hour=zone_1_ore_per_kwh_hour,
            zone_2_ore_per_kwh_hour=zone_2_ore_per_kwh_hour,
            zone_3_ore_per_kwh_hour=zone_3_ore_per_kwh_hour,
            high_soc_enabled=high_soc_enabled,
        )
