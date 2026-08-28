from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .settings_store import load_setting_overrides

OPTIONS_PATH = Path("/data/options.json")


def _options() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    try:
        if OPTIONS_PATH.exists():
            value = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                raw = value
    except Exception:
        pass
    return {**raw, **load_setting_overrides()}


def _float(options: dict[str, Any], key: str, default: float) -> float:
    try:
        value = options.get(key, default)
        return float(default if value in (None, "") else value)
    except Exception:
        return float(default)


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
    }
    cfg["actuator"] = actuator
    return actuator
