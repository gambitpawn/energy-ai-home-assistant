from __future__ import annotations
import json
from pathlib import Path
from typing import Any

OPTIONS_PATH = Path("/data/options.json")
RUNTIME_BUILD = "1.0.52"

ENTITY_DEFAULTS = {
    "pv_power": "sensor.solinteg_inverter_pv_power_total",
    "house_load": "sensor.solinteg_inverter_house_total_load",
    "grid_power": "sensor.solinteg_inverter_meter_active_power",
    "battery_power": "sensor.solinteg_inverter_battery_power",
    "battery_soc": "sensor.solinteg_inverter_battery_soc",
    "spot_price": "sensor.nord_pool_se4_aktuellt_pris",
    "sauna_power": None,
    "sauna_reserve": "input_boolean.energy_ai_sauna_reserve",
    "sauna_reserve_until": "input_datetime.energy_ai_sauna_reserve_until",
    "ev_mode": "input_select.energy_ai_ev_mode",
    "ev_connected": "sensor.zap361270_laddstatus",
    "ev_soc": None,
    "ev_target_soc": None,
    "ev_ready_by": None,
    "ev_power": "sensor.zap361270_laddeffekt",
    "spa_power": None,
    "spa_temperature": None,
    "pool_heat_pump_power": None,
    "pool_temperature": None,
    "demand_tariff_enabled": "input_boolean.energy_ai_demand_tariff_enabled",
    "import_power_target_kw": "input_number.energy_ai_import_power_target_kw",
    "export_power_target_kw": "input_number.energy_ai_export_power_target_kw",
}

LEGACY_PLACEHOLDERS = {
    "pv_power": "sensor.energy_pv_power",
    "house_load": "sensor.energy_house_load",
    "grid_power": "sensor.energy_grid_power",
    "battery_power": "sensor.energy_battery_power",
    "battery_soc": "sensor.energy_battery_soc",
    "spot_price": "sensor.energy_spot_price",
}


def _entity_option(options: dict[str, Any], key: str) -> str | None:
    raw = options.get(f"entity_{key}")
    if not raw or raw == LEGACY_PLACEHOLDERS.get(key):
        return ENTITY_DEFAULTS[key]
    return str(raw)


def _optional_float(options: dict[str, Any], key: str, default: float | None = None) -> float | None:
    raw = options.get(key, default)
    if raw in (None, ""):
        return None
    return float(raw)


def _component_json(options: dict[str, Any]) -> list[dict[str, Any]]:
    raw = options.get("load_components_json", "")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _int_list(raw: Any, default: list[int]) -> list[int]:
    if raw in (None, ""):
        return list(default)
    if isinstance(raw, list):
        values = raw
    else:
        values = [x.strip() for x in str(raw).split(",") if x.strip()]
    try:
        return sorted(set(int(x) for x in values))
    except Exception:
        return list(default)


def load_config() -> dict[str, Any]:
    options = {}
    if OPTIONS_PATH.exists():
        options = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))

    entities = {**ENTITY_DEFAULTS}
    for key in (
        "pv_power","house_load","grid_power","battery_power","battery_soc","spot_price",
        "sauna_power","ev_power","ev_connected","ev_soc","ev_target_soc","ev_ready_by",
        "spa_power","spa_temperature","pool_heat_pump_power","pool_temperature",
    ):
        entities[key] = _entity_option(options, key)

    physical_grid_import_limit_kw = float(options.get("optimizer_physical_grid_import_limit_kw", 13.8))
    legacy_reserve_penalty = float(
        options.get(
            "optimizer_reserve_shortfall_penalty_ore_per_kwh_hour",
            options.get("optimizer_reserve_penalty_ore_per_kwh", 100.0),
        )
    )
    target_reserve_raw = options.get("optimizer_reserve_target_penalty_ore_per_kwh_hour")
    if target_reserve_raw is None or abs(float(target_reserve_raw) - 25.0) < 1e-9:
        reserve_target_penalty = 10.0
    else:
        reserve_target_penalty = float(target_reserve_raw)

    return {
        "runtime_build": RUNTIME_BUILD,
        "collector": {
            "poll_seconds": int(options.get("poll_seconds", 60)),
            "stale_after_seconds": int(options.get("stale_after_seconds", 180)),
        },
        "entities": entities,
        "components": {"custom": _component_json(options)},
        "policy": {
            "battery": {
                "capacity_kwh": float(options.get("battery_capacity_kwh", 19.6)),
                "hard_min_soc_pct": float(options.get("hard_min_soc_pct", 5)),
                "hard_max_soc_pct": 100.0,
                "preferred_min_soc_pct": float(options.get("preferred_min_soc_pct", 15)),
                "preferred_max_soc_pct": float(options.get("preferred_max_soc_pct", 90)),
                "normal_reserve_soc_pct": float(options.get("normal_reserve_soc_pct", 20)),
                "high_uncertainty_reserve_soc_pct": float(options.get("high_uncertainty_reserve_soc_pct", 28)),
            },
            "economics": {
                "import_overhead_ore_kwh": float(options.get("import_overhead_ore_kwh", 0)),
                "export_overhead_ore_kwh": float(options.get("export_overhead_ore_kwh", 0)),
                "minimum_arbitrage_margin_ore_kwh": float(options.get("minimum_arbitrage_margin_ore_kwh", 20)),
            },
            "sauna": {
                "nominal_peak_kw": float(options.get("sauna_nominal_peak_kw", 6.0)),
                "detection_step_kw": float(options.get("sauna_detection_step_kw", 4.0)),
                "learn_from_load_history": True,
            },
            "ev": {"max_power_kw": float(options.get("ev_max_power_kw", 11.0)), "default_mode": "smart"},
        },
        "optimizer": {
            "mode": "shadow_read_only",
            "planner": "deterministic_battery_dp_v3_5",
            "tariff_capable_planner": "tariff_aware_battery_milp_v1",
            "battery_max_charge_kw": float(options.get("optimizer_battery_max_charge_kw", 8.0)),
            "battery_max_discharge_kw": float(options.get("optimizer_battery_max_discharge_kw", 8.0)),
            "battery_charge_efficiency": float(options.get("optimizer_battery_charge_efficiency", 0.95)),
            "battery_discharge_efficiency": float(options.get("optimizer_battery_discharge_efficiency", 0.95)),
            "battery_degradation_ore_kwh": float(options.get("optimizer_battery_degradation_ore_kwh", 5.0)),
            "physical_grid_import_limit_kw": physical_grid_import_limit_kw,
            "grid_export_limit_kw": float(options.get("optimizer_grid_export_limit_kw", 10.0)),
            "soc_grid_step_kwh": float(options.get("optimizer_soc_grid_step_kwh", 0.5)),
            "reserve_critical_soc_pct": float(options.get("optimizer_reserve_critical_soc_pct", 10.0)),
            "reserve_critical_penalty_ore_per_kwh_hour": float(options.get("optimizer_reserve_critical_penalty_ore_per_kwh_hour", legacy_reserve_penalty * 3.0)),
            "reserve_preferred_penalty_ore_per_kwh_hour": float(options.get("optimizer_reserve_preferred_penalty_ore_per_kwh_hour", legacy_reserve_penalty)),
            "reserve_target_penalty_ore_per_kwh_hour": reserve_target_penalty,
            "preferred_max_excess_penalty_ore_per_kwh_hour": float(options.get("optimizer_preferred_max_excess_penalty_ore_per_kwh_hour", legacy_reserve_penalty * 0.02)),
            "reserve_uncertainty_full_scale_kw": float(options.get("optimizer_reserve_uncertainty_full_scale_kw", 3.0)),
            "terminal_soc_tolerance_pct": float(options.get("optimizer_terminal_soc_tolerance_pct", 3.0)),
            "terminal_soc_tiebreak_ore_per_kwh": float(options.get("optimizer_terminal_soc_tiebreak_ore_per_kwh", 5.0)),
            "unknown_price_energy_coverage_fraction": float(options.get("optimizer_unknown_price_energy_coverage_fraction", 0.35)),
            "unknown_price_risk_premium_ore_kwh": float(options.get("optimizer_unknown_price_risk_premium_ore_kwh", 40.0)),
            "unknown_price_default_continuation_value_ore_kwh": float(options.get("optimizer_unknown_price_default_continuation_value_ore_kwh", 150.0)),
        },
        "tariffs": {
            "enabled": bool(options.get("tariff_enabled", False)),
            "consumption_demand": {
                "enabled": bool(options.get("tariff_consumption_enabled", False)),
                "kind": "import_top3_mean",
                "rate_sek_per_kw": float(options.get("tariff_consumption_rate_sek_per_kw", 105.0)),
                "start_hour": int(options.get("tariff_consumption_start_hour", 7)),
                "end_hour": int(options.get("tariff_consumption_end_hour", 19)),
                "active_months": _int_list(options.get("tariff_consumption_active_months", "1,2,11,12"), [1,2,11,12]),
                "day_rule": str(options.get("tariff_consumption_day_rule", "workdays")),
                "top_n": 3,
                "measurement": "clock_hour_average_import_kw",
                "source_status": "user_configured",
            },
            "production_demand": {
                "enabled": bool(options.get("tariff_production_enabled", False)),
                "kind": "export_max_hour",
                "rate_sek_per_kw": float(options.get("tariff_production_rate_sek_per_kw", 10.0)),
                "start_hour": int(options.get("tariff_production_start_hour", 8)),
                "end_hour": int(options.get("tariff_production_end_hour", 16)),
                "active_months": _int_list(options.get("tariff_production_active_months", "4,5,6,7,8"), [4,5,6,7,8]),
                "day_rule": str(options.get("tariff_production_day_rule", "weekends_holidays_midsummer_eve")),
                "measurement": "clock_hour_average_export_kw",
                "source_status": "preliminary_default_requires_verification_before_enable",
            },
        },
        "forecast": {
            "interval_minutes": 15,
            "horizon_hours": 36,
            "pv": {
                "capacity_kw": float(options.get("pv_capacity_kw", 10.0)),
                "tilt_deg": _optional_float(options, "pv_tilt_deg", 35.5),
                "azimuth_deg": _optional_float(options, "pv_azimuth_deg", -79.0),
                "primary_irradiance_feature": "global_tilted_irradiance",
                "uncertainty": {
                    "local_residual_minutes": 15,
                    "rolling_residual_hours": [1, 3],
                    "use_cumulative_daily_energy_residual": True,
                    "local_spike_must_not_reprice_full_day_confidence": True,
                },
            },
        },
        "llm": {"enabled": bool(options.get("llm_enabled", True)), "role": "explanation_only", "language": "sv"},
    }