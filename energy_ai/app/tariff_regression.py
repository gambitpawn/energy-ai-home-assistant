from __future__ import annotations

import copy
from typing import Any

from .optimizer import build_plan as build_v35_plan
from .tariff_live import build_shadow_plan


def _canonical(plan: dict[str, Any]) -> dict[str, Any]:
    row_keys = (
        "start",
        "battery_action_kw",
        "expected_soc_pct",
        "grid_import_kw",
        "grid_export_kw",
        "reason",
        "objective_cost_ore",
    )
    return {
        "planner": plan.get("planner"),
        "initial_soc_pct": plan.get("initial_soc_pct"),
        "constraints": plan.get("constraints"),
        "objective": plan.get("objective"),
        "continuation": plan.get("continuation"),
        "summary": plan.get("summary"),
        "rows": [{k: row.get(k) for k in row_keys} for row in (plan.get("rows") or [])],
    }


def disabled_tariff_wrapper_regression(cfg: dict[str, Any]) -> dict[str, Any]:
    reference = build_v35_plan(cfg)

    absent_cfg = copy.deepcopy(cfg)
    absent_cfg.pop("tariffs", None)
    absent = build_shadow_plan(absent_cfg)

    disabled_cfg = copy.deepcopy(cfg)
    disabled_cfg["tariffs"] = {
        "enabled": False,
        "consumption_demand": {"enabled": False},
        "production_demand": {"enabled": False},
    }
    disabled = build_shadow_plan(disabled_cfg)

    a = _canonical(reference)
    b = _canonical(absent)
    c = _canonical(disabled)
    return {
        "engine": "tariff_live_wrapper_regression_v1",
        "test": "disabled_tariff_live_wrapper_equals_v3_5",
        "test_only": True,
        "reference_planner": reference.get("planner"),
        "absent_wrapper_planner": absent.get("planner"),
        "disabled_wrapper_planner": disabled.get("planner"),
        "tariffs_absent_exact_match": a == b,
        "tariffs_disabled_exact_match": a == c,
        "pass": a == b == c,
        "compared_rows": len(a["rows"]),
    }
