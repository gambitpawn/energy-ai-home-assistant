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
    "spot_price": None,
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
            "pv_power": options.get("entity_pv_power") or ENTITY_DEFAULTS["pv_power"],
            "house_load": options.get("entity_house_load") or ENTITY_DEFAULTS["house_load"],
            "grid_power": options.get("entity_grid_power") or ENTITY_DEFAULTS["grid_power"],
            "battery_power": options.get("entity_battery_power") or ENTITY_DEFAULTS["battery_power"],
            "battery_soc": options.get("entity_battery_soc") or ENTITY_DEFAULTS["battery_soc"],
            "spot_price": options.get("entity_spot_price") or ENTITY_DEFAULTS["spot_price"],
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
                "nominal_peak_kw": 6.0,
                "learn_from_load_history": True,
            },
            "ev": {
                "max_power_kw": 11.0,
                "default_mode": "smart",
            },
        },
        "tariffs": {"enabled": False, "rules": []},
        "forecast": {
            "interval_minutes": 15,
            "horizon_hours": 36,
            "pv": {
                "primary_irradiance_feature": "global_tilted_irradiance",
                "uncertainty": {
                    "local_residual_minutes": 15,
                    "rolling_residual_hours": [1, 3],
                    "use_cumulative_daily_energy_residual": True,
                    "local_spike_must_not_reprice_full_day_confidence": True,
                },
            },
        },
        "llm": {
            "enabled": bool(options.get("llm_enabled", True)),
            "role": "explanation_only",
            "language": "sv",
        },
    }
