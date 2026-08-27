from __future__ import annotations

import copy
from typing import Any

from .price_economics import CURRENT_ECONOMICS, economics_payload


def install_neural_teacher_economics(cfg: dict[str, Any]) -> dict[str, Any]:
    from . import neural_teacher_v2 as teacher
    from . import tariff_scenarios as tariff

    original_cfg_from_input = teacher._cfg_from_input

    def cfg_from_input(base_cfg, engine_input):
        rebuilt = original_cfg_from_input(base_cfg, engine_input)
        objective = engine_input.objective or {}
        input_economics = objective.get("economics") or {}
        # Objective vintages produced by v1.79 carry all spot-linked terms. If an
        # older persisted input reaches this path, CURRENT_ECONOMICS is the
        # deliberate training fallback so labels are still priced for today.
        if input_economics.get("pricing_model") == "spot_linked_grid_v1":
            economics = economics_payload(input_economics)
            if "battery_degradation_ore_kwh" in input_economics:
                economics["battery_degradation_ore_kwh"] = float(input_economics["battery_degradation_ore_kwh"])
        else:
            economics = economics_payload(base_cfg)
            economics["battery_degradation_ore_kwh"] = float((base_cfg.get("optimizer") or {}).get("battery_degradation_ore_kwh", 5.0))
        rebuilt.setdefault("policy", {})["economics"] = copy.deepcopy(economics)
        rebuilt.setdefault("optimizer", {})["battery_degradation_ore_kwh"] = float(economics.get("battery_degradation_ore_kwh", (base_cfg.get("optimizer") or {}).get("battery_degradation_ore_kwh", 5.0)))
        return rebuilt

    teacher._cfg_from_input = cfg_from_input
    teacher._solve_rows = tariff._solve_rows
    return {
        "installed": True,
        "economics_mode": CURRENT_ECONOMICS,
        "patched_paths": [
            "neural_teacher_v2._cfg_from_input",
            "neural_teacher_v2._solve_rows",
        ],
    }
