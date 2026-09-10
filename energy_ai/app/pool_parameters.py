from __future__ import annotations

from types import ModuleType
from typing import Any

from .pool_control_contract import POOL_POLICY_DEFAULTS, PoolPolicy
from .settings_store import load_setting_overrides


def _definitions() -> list[dict[str, Any]]:
    return [
        {
            "section": "Pool",
            "key": "pool_optimization_enabled",
            "label": "Pool optimization enabled",
            "kind": "bool",
            "default": POOL_POLICY_DEFAULTS["pool_optimization_enabled"],
            "help": "Master switch for future pool optimization. Sprint 1 only stores the setting; it does not alter optimizer or actuator behavior.",
            "unit": "",
            "recommended": "Keep disabled until pool optimization is introduced in a later sprint.",
            "physical": None,
            "min": None,
            "max": None,
            "step": None,
        },
        {
            "section": "Pool",
            "key": "pool_control_mode",
            "label": "Pool control mode",
            "kind": "str",
            "default": POOL_POLICY_DEFAULTS["pool_control_mode"],
            "help": "Pool-only mode reserved for staged rollout: observe, simulate or active. This does not change the production mode of the rest of Energy AI.",
            "unit": "",
            "recommended": "observe during Sprint 1.",
            "physical": "Allowed values: observe, simulate, active.",
            "min": None,
            "max": None,
            "step": None,
        },
        {
            "section": "Pool",
            "key": "pool_preferred_temp_c",
            "label": "Preferred pool temperature",
            "kind": "float",
            "default": POOL_POLICY_DEFAULTS["pool_preferred_temp_c"],
            "help": "Comfort target used by future pool optimization.",
            "unit": "°C",
            "recommended": None,
            "physical": "Set the temperature normally desired for pool use.",
            "min": 5,
            "max": 40,
            "step": 0.1,
        },
        {
            "section": "Pool",
            "key": "pool_minimum_temp_c",
            "label": "Minimum pool temperature",
            "kind": "float",
            "default": POOL_POLICY_DEFAULTS["pool_minimum_temp_c"],
            "help": "Hard lower comfort boundary for future optimization.",
            "unit": "°C",
            "recommended": "Keep below the preferred temperature but high enough to remain acceptable.",
            "physical": None,
            "min": 5,
            "max": 40,
            "step": 0.1,
        },
        {
            "section": "Pool",
            "key": "pool_maximum_temp_c",
            "label": "Maximum pool temperature",
            "kind": "float",
            "default": POOL_POLICY_DEFAULTS["pool_maximum_temp_c"],
            "help": "Hard upper boundary for thermal pre-heating/storage.",
            "unit": "°C",
            "recommended": "Keep only modestly above the preferred temperature unless the pool equipment allows otherwise.",
            "physical": "Do not exceed equipment or pool safety limits.",
            "min": 5,
            "max": 40,
            "step": 0.1,
        },
        {
            "section": "Pool",
            "key": "pool_maximum_boost_c",
            "label": "Maximum thermal boost",
            "kind": "float",
            "default": POOL_POLICY_DEFAULTS["pool_maximum_boost_c"],
            "help": "Maximum amount future optimization may raise the target above the preferred temperature, still bounded by the absolute maximum temperature.",
            "unit": "°C",
            "recommended": "0.5–1.5 °C is a conservative starting range.",
            "physical": None,
            "min": 0,
            "max": 5,
            "step": 0.1,
        },
        {
            "section": "Pool",
            "key": "pool_grid_heating_allowed",
            "label": "Grid heating allowed",
            "kind": "bool",
            "default": POOL_POLICY_DEFAULTS["pool_grid_heating_allowed"],
            "help": "Whether future joint optimization may schedule pool heating when net grid import is required. The optimizer will still compare this against battery, PV and export value.",
            "unit": "",
            "recommended": "Enabled gives the optimizer the full economic choice set; minimum temperature will remain a separate safety constraint later.",
            "physical": None,
            "min": None,
            "max": None,
            "step": None,
        },
        {
            "section": "Pool",
            "key": "pool_min_on_minutes",
            "label": "Minimum heating run",
            "kind": "int",
            "default": POOL_POLICY_DEFAULTS["pool_min_on_minutes"],
            "help": "Minimum future commanded heating interval used to avoid excessive cycling.",
            "unit": "min",
            "recommended": "Verify against the heat-pump documentation before active control.",
            "physical": None,
            "min": 0,
            "max": 240,
            "step": 5,
        },
        {
            "section": "Pool",
            "key": "pool_min_off_minutes",
            "label": "Minimum heating pause",
            "kind": "int",
            "default": POOL_POLICY_DEFAULTS["pool_min_off_minutes"],
            "help": "Minimum future pause between commanded heating intervals.",
            "unit": "min",
            "recommended": "Verify against the heat-pump documentation before active control.",
            "physical": None,
            "min": 0,
            "max": 240,
            "step": 5,
        },
        {
            "section": "Pool",
            "key": "pool_manual_override_minutes",
            "label": "Manual override duration",
            "kind": "int",
            "default": POOL_POLICY_DEFAULTS["pool_manual_override_minutes"],
            "help": "How long future Energy AI pool control should remain suspended after a manual setpoint change is detected.",
            "unit": "min",
            "recommended": "360 minutes keeps manual changes in force for a useful part of the day.",
            "physical": None,
            "min": 0,
            "max": 1440,
            "step": 30,
        },
        {
            "section": "Pool",
            "key": "pool_nominal_power_kw",
            "label": "Pool heat-pump nominal power",
            "kind": "float",
            "default": POOL_POLICY_DEFAULTS["pool_nominal_power_kw"],
            "help": "Optional fallback electrical load for future optimization when trustworthy measured/estimated power is unavailable. Zero means no manual fallback is configured.",
            "unit": "kW",
            "recommended": "Leave at 0 when reliable telemetry is available; otherwise enter measured typical electrical input rather than thermal output.",
            "physical": "Use electrical input power, not rated heating output.",
            "min": 0,
            "max": 20,
            "step": 0.1,
        },
    ]


def register_pool_parameters(ui_parameters_module: ModuleType) -> dict[str, Any]:
    """Add pool parameters to the existing editor without changing generic UI code."""
    added: list[str] = []
    existing = {str(item.get("key")) for item in ui_parameters_module.PARAMETERS}
    for meta in _definitions():
        key = str(meta["key"])
        if key in existing:
            ui_parameters_module.PARAM_BY_KEY[key] = next(
                item for item in ui_parameters_module.PARAMETERS if item.get("key") == key
            )
            continue
        ui_parameters_module.PARAMETERS.append(meta)
        ui_parameters_module.PARAM_BY_KEY[key] = meta
        existing.add(key)
        added.append(key)
    return {"registered": True, "added": added, "total_pool_parameters": len(_definitions())}


def current_pool_policy() -> PoolPolicy:
    return PoolPolicy.from_mapping(load_setting_overrides())
