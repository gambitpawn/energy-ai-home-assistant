from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings_store import load_setting_overrides

OPTIONS_PATH = Path("/data/options.json")

ACTUATOR_DEFAULTS: dict[str, Any] = {
    "entity_solinteg_working_mode": "",
    "entity_solinteg_battery_power_target": "",
    "actuator_control_working_mode": "EMS BattCtrl",
    "actuator_safe_working_mode": "General",
    "actuator_ack_timeout_seconds": 8.0,
    "actuator_ack_tolerance_kw": 0.10,
    "actuator_state_max_age_seconds": 180.0,
    "actuator_candidate_grace_seconds": 120.0,
    "actuator_soc_guard_margin_pct": 1.0,
    "actuator_zero_deadband_kw": 0.05,
    "actuator_min_action_change_kw": 0.10,
    "actuator_watchdog_poll_seconds": 30.0,
}

OPTION_TO_RUNTIME = {
    "entity_solinteg_working_mode": ("entities", "solinteg_working_mode"),
    "entity_solinteg_battery_power_target": ("entities", "solinteg_battery_power_target"),
    "actuator_control_working_mode": ("actuator", "control_working_mode"),
    "actuator_safe_working_mode": ("actuator", "safe_working_mode"),
    "actuator_ack_timeout_seconds": ("actuator", "ack_timeout_seconds"),
    "actuator_ack_tolerance_kw": ("actuator", "ack_tolerance_kw"),
    "actuator_state_max_age_seconds": ("actuator", "state_max_age_seconds"),
    "actuator_candidate_grace_seconds": ("actuator", "candidate_grace_seconds"),
    "actuator_soc_guard_margin_pct": ("actuator", "soc_guard_margin_pct"),
    "actuator_zero_deadband_kw": ("actuator", "zero_deadband_kw"),
    "actuator_min_action_change_kw": ("actuator", "min_action_change_kw"),
    "actuator_watchdog_poll_seconds": ("actuator", "watchdog_poll_seconds"),
}


def _raw_options() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    try:
        if OPTIONS_PATH.exists():
            value = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                raw = value
    except Exception:
        pass
    return raw


def _option_layers() -> tuple[dict[str, Any], dict[str, Any]]:
    return _raw_options(), load_setting_overrides()


def _options() -> dict[str, Any]:
    raw, overrides = _option_layers()
    return {**raw, **overrides}


def _float(options: dict[str, Any], key: str, default: float) -> float:
    try:
        value = options.get(key, default)
        return float(default if value in (None, "") else value)
    except Exception:
        return float(default)


def _persisted_value(key: str, raw: dict[str, Any], overrides: dict[str, Any]) -> Any:
    if key in overrides:
        value = overrides[key]
    elif key in raw:
        value = raw[key]
    else:
        value = ACTUATOR_DEFAULTS[key]
    default = ACTUATOR_DEFAULTS[key]
    if isinstance(default, float):
        try:
            return float(default if value in (None, "") else value)
        except Exception:
            return float(default)
    return str(value or "").strip()


def _source(key: str, raw: dict[str, Any], overrides: dict[str, Any]) -> str:
    if key in overrides:
        return "db_override"
    if key in raw:
        return "home_assistant_options"
    return "code_default"


def _runtime_value(cfg: dict[str, Any], key: str) -> Any:
    section, runtime_key = OPTION_TO_RUNTIME[key]
    value = (cfg.get(section) or {}).get(runtime_key)
    default = ACTUATOR_DEFAULTS[key]
    if key.startswith("entity_"):
        return str(value or "").strip()
    if isinstance(default, float):
        try:
            return float(value)
        except Exception:
            return float(default)
    return str(value or "").strip()


def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= 1e-9
        except Exception:
            return False
    return str(a) == str(b)


def effective_actuator_config_report(cfg: dict[str, Any]) -> dict[str, Any]:
    """Expose the actuator config actually loaded by this process vs persistent settings.

    UI settings are persisted immediately, while the actuator's command adapter is
    intentionally configured at process startup. A mismatch therefore means the
    operator must restart the add-on before any arm/write transition is allowed.
    """
    raw, overrides = _option_layers()
    runtime: dict[str, Any] = {}
    persisted: dict[str, Any] = {}
    sources: dict[str, str] = {}
    mismatches: list[dict[str, Any]] = []

    for option_key in ACTUATOR_DEFAULTS:
        public_key = OPTION_TO_RUNTIME[option_key][1]
        rv = _runtime_value(cfg, option_key)
        pv = _persisted_value(option_key, raw, overrides)
        runtime[public_key] = rv
        persisted[public_key] = pv
        sources[public_key] = _source(option_key, raw, overrides)
        if not _same_value(rv, pv):
            mismatches.append(
                {
                    "setting": public_key,
                    "option_key": option_key,
                    "runtime": rv,
                    "persisted": pv,
                    "persisted_source": sources[public_key],
                }
            )

    optimizer = cfg.get("optimizer") or {}
    battery = (cfg.get("policy") or {}).get("battery") or {}
    actuator = cfg.get("actuator") or {}
    hard_limits = {
        "battery_max_charge_kw": float(optimizer.get("battery_max_charge_kw", 8.0)),
        "battery_max_discharge_kw": float(optimizer.get("battery_max_discharge_kw", 8.0)),
        "physical_grid_import_limit_kw": float(optimizer.get("physical_grid_import_limit_kw", 13.8)),
        "grid_export_limit_kw": float(optimizer.get("grid_export_limit_kw", 10.0)),
        "hard_min_soc_pct": float(battery.get("hard_min_soc_pct", 5.0)),
        "hard_max_soc_pct": float(battery.get("hard_max_soc_pct", 100.0)),
    }
    return {
        "config_loaded_at": actuator.get("config_loaded_at"),
        "runtime": runtime,
        "persisted": persisted,
        "sources": sources,
        "runtime_matches_persisted": not mismatches,
        "restart_required": bool(mismatches),
        "mismatches": mismatches,
        "hard_limits": hard_limits,
        "settings_precedence": ["code_default", "home_assistant_options", "db_override"],
    }


def actuator_preflight_config_gate(report: dict[str, Any], current_working_mode: str | None) -> dict[str, Any]:
    """Safety gate for configuration freshness and safe-mode semantics."""
    if report.get("restart_required"):
        return {
            "ok": False,
            "error": "actuator_runtime_config_stale_restart_required",
            "mismatches": report.get("mismatches") or [],
            "warnings": [],
        }

    runtime = report.get("runtime") or {}
    safe_mode = str(runtime.get("safe_working_mode") or "General")
    control_mode = str(runtime.get("control_working_mode") or "EMS BattCtrl")
    current = None if current_working_mode is None else str(current_working_mode)
    warnings: list[str] = []

    # When not already in our EMS control mode, a zero handshake must return the
    # inverter to the mode it is currently operating in. This prevents an arm test
    # from silently changing a site's normal inverter policy (e.g. ToU -> General).
    if current and current != control_mode and current != safe_mode:
        return {
            "ok": False,
            "error": "safe_release_mode_mismatch_current_working_mode",
            "current_working_mode": current,
            "configured_safe_working_mode": safe_mode,
            "warnings": [
                "Set Safe release working mode to the inverter's intended normal mode, save it, and restart the add-on before arming."
            ],
        }
    if current == control_mode:
        warnings.append("Inverter is already in Energy AI control mode; verify why before arming or changing production mode.")
    return {"ok": True, "error": None, "warnings": warnings}


def install_actuator_config(cfg: dict[str, Any]) -> dict[str, Any]:
    options = _options()
    entities = cfg.setdefault("entities", {})
    working = str(options.get("entity_solinteg_working_mode") or "").strip()
    target = str(options.get("entity_solinteg_battery_power_target") or "").strip()
    if working:
        entities["solinteg_working_mode"] = working
    else:
        entities.setdefault("solinteg_working_mode", None)
    if target:
        entities["solinteg_battery_power_target"] = target
    else:
        entities.setdefault("solinteg_battery_power_target", None)

    actuator = {
        "adapter": "solinteg_home_assistant_ems_battctrl_v1",
        "control_working_mode": str(options.get("actuator_control_working_mode") or "EMS BattCtrl"),
        "safe_working_mode": str(options.get("actuator_safe_working_mode") or "General"),
        "ack_timeout_seconds": _float(options, "actuator_ack_timeout_seconds", 8.0),
        "ack_tolerance_kw": _float(options, "actuator_ack_tolerance_kw", 0.10),
        "state_max_age_seconds": _float(options, "actuator_state_max_age_seconds", 180.0),
        "candidate_grace_seconds": _float(options, "actuator_candidate_grace_seconds", 120.0),
        "soc_guard_margin_pct": _float(options, "actuator_soc_guard_margin_pct", 1.0),
        "zero_deadband_kw": _float(options, "actuator_zero_deadband_kw", 0.05),
        "min_action_change_kw": _float(options, "actuator_min_action_change_kw", 0.10),
        "watchdog_poll_seconds": _float(options, "actuator_watchdog_poll_seconds", 30.0),
        "startup_policy": "always_disarmed_requires_zero_handshake",
        "process_crash_inverter_timeout_guaranteed": False,
        "config_loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    cfg["actuator"] = actuator
    return actuator
