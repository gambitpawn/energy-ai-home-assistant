from __future__ import annotations
import json
from pathlib import Path
from typing import Any

OPTIONS_PATH = Path("/data/options.json")

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
    "ev_connected": None,
    "ev_soc": None,
    "ev_target_soc": None,
    "ev_ready_by": None,
    "ev_power": None,
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


def load_config() -> dict[str, Any]:
    options = {}
    if OPTIONS_PATH.exists():
        options = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))

    return {
        "collector": {
            "poll_seconds": int(options.get("poll_seconds", 60)),
            "stale_after_seconds": int(options.get("stale_after_seconds", 180)),
        },
        "entities": {
            **ENTITY_DEFAULTS,
            "pv_power": _entity_option(options, "pv_power"),
            "house_load": _entity_option(options, "house_load"),
            "grid_power": _entity_option(options, "grid_power"),
            "battery_power": _entity_option(options, "battery_power"),
            "battery_soc": _entity_option(options, "battery_soc"),
            "spot_price": _entity_option(options, "spot_price"),
            "sauna_power": _entity_option(options, "sauna_power"),
            "ev_power": _entity_option(options, "ev_power"),
            "ev_connected": _entity_option(options, "ev_connected"),
            "ev_soc": _entity_option(options, "ev_soc"),
            "ev_target_soc": _entity_option(options, "ev_target_soc"),
            "ev_ready_by": _entity_option(options, "ev_ready_by"),
        },
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
        "tariffs": {"enabled": False, "rules": []},
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
