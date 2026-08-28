from __future__ import annotations

from typing import Any

from . import ui_v164
from .replanning_config import install_replanning_config
from .runtime_entry_v186 import app, core

RUNTIME_BUILD = "1.0.86"
REPLANNING_CONFIG = install_replanning_config(core.cfg)


def _install_replanning_parameters() -> None:
    definitions: list[dict[str, Any]] = [
        ui_v164.p(
            "Optimizer – live replanning",
            "optimizer_soc_replan_threshold_pct",
            "SOC replan threshold",
            "float",
            2.0,
            "Recalculate a deterministic live plan between quarter boundaries when measured battery SOC differs from the interpolated plan by at least this many percentage points.",
            unit="percentage points",
            recommended="2 percentage points balances responsiveness against unnecessary replans.",
            minimum=0.1,
            maximum=20,
            step=0.1,
        ),
        ui_v164.p(
            "Optimizer – live replanning",
            "optimizer_soc_replan_emergency_threshold_pct",
            "Emergency SOC deviation",
            "float",
            5.0,
            "A deviation at or above this level bypasses the ordinary replan cooldown.",
            unit="percentage points",
            recommended="5 percentage points.",
            minimum=0.1,
            maximum=30,
            step=0.1,
        ),
        ui_v164.p(
            "Optimizer – live replanning",
            "optimizer_soc_replan_min_interval_seconds",
            "Minimum replan interval",
            "int",
            60,
            "Minimum time between ordinary SOC-triggered live replans. Emergency deviations can bypass it.",
            unit="s",
            recommended="60 seconds, matching the normal Home Assistant collection cadence.",
            minimum=0,
            maximum=900,
            step=15,
        ),
        ui_v164.p(
            "Optimizer – live replanning",
            "optimizer_soc_observation_max_age_seconds",
            "Maximum SOC observation age",
            "int",
            180,
            "The live planner refuses to re-anchor on an SOC observation older than this limit.",
            unit="s",
            recommended="180 seconds with a 60-second collector cadence.",
            minimum=15,
            maximum=1800,
            step=15,
        ),
    ]
    existing = {item.get("key") for item in ui_v164.PARAMETERS}
    for item in definitions:
        if item["key"] not in existing:
            ui_v164.PARAMETERS.append(item)
    ui_v164.PARAM_BY_KEY.clear()
    ui_v164.PARAM_BY_KEY.update({item["key"]: item for item in ui_v164.PARAMETERS})


_install_replanning_parameters()

core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
