from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .settings_store import load_setting_overrides

OPTIONS_PATH = Path("/data/options.json")
DEFAULTS = {
    "optimizer_soc_replan_threshold_pct": 2.0,
    "optimizer_soc_replan_emergency_threshold_pct": 5.0,
    "optimizer_soc_replan_min_interval_seconds": 60.0,
    "optimizer_soc_observation_max_age_seconds": 180.0,
}


def _options() -> dict[str, Any]:
    try:
        raw = json.loads(OPTIONS_PATH.read_text(encoding="utf-8")) if OPTIONS_PATH.exists() else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def install_replanning_config(cfg: dict[str, Any]) -> dict[str, float]:
    values = {**_options(), **load_setting_overrides()}
    resolved = {
        "soc_replan_threshold_pct": max(
            0.1,
            float(values.get("optimizer_soc_replan_threshold_pct", DEFAULTS["optimizer_soc_replan_threshold_pct"])),
        ),
        "soc_replan_emergency_threshold_pct": max(
            0.1,
            float(values.get("optimizer_soc_replan_emergency_threshold_pct", DEFAULTS["optimizer_soc_replan_emergency_threshold_pct"])),
        ),
        "soc_replan_min_interval_seconds": max(
            0.0,
            float(values.get("optimizer_soc_replan_min_interval_seconds", DEFAULTS["optimizer_soc_replan_min_interval_seconds"])),
        ),
        "soc_observation_max_age_seconds": max(
            15.0,
            float(values.get("optimizer_soc_observation_max_age_seconds", DEFAULTS["optimizer_soc_observation_max_age_seconds"])),
        ),
    }
    if resolved["soc_replan_emergency_threshold_pct"] < resolved["soc_replan_threshold_pct"]:
        resolved["soc_replan_emergency_threshold_pct"] = resolved["soc_replan_threshold_pct"]
    cfg.setdefault("optimizer", {}).update(resolved)
    return resolved
