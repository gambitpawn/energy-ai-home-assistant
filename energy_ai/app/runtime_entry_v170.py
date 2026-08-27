from __future__ import annotations

import asyncio

from fastapi import HTTPException, Query

from .engine_contract import ENGINE_DECISION_SCHEMA, ENGINE_INPUT_SCHEMA
from .engine_registry import BASELINE_ENGINE_ID, baseline_decision_from_plan, registry_status
from .optimizer_store import latest_plan
from .runtime_entry_v169 import app, core

RUNTIME_BUILD = "1.0.70"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD


@app.get(
    "/engines",
    tags=["engines"],
    summary="Multi-engine registry and permanent deterministic baseline",
)
async def engines_registry():
    return registry_status()


@app.get(
    "/engines/contract",
    tags=["engines"],
    summary="Decision-engine contract semantics",
)
async def engines_contract():
    return {
        "contract_version": "v1",
        "input_schema": ENGINE_INPUT_SCHEMA,
        "decision_schema": ENGINE_DECISION_SCHEMA,
        "baseline_engine_id": BASELINE_ENGINE_ID,
        "comparison_policy": {
            "active_control_baseline": BASELINE_ENGINE_ID,
            "challengers": ["adaptive_deterministic_v1", "neural_v1", "hybrid_v1"],
            "actual_app_role": "historical_pre_control_reference_only_when_observed",
            "oracle_role": "perfect_hindsight_for_matured_evaluation_only",
        },
        "shared_information_vintage": {
            "required": True,
            "identity": "sha256 of normalized generated_at, decision_start, initial SOC, complete horizon, constraints, objective and source metadata",
            "fair_comparison_rule": "engines compared at one decision point must receive the same information_vintage_id",
        },
        "engine_output": {
            "requested_action_kw": "pre-safety battery request; positive discharge, negative charge",
            "expected_soc_pct": "engine expectation before realized-data replay",
            "plan_rows": "optional common future-action trace for evaluation/UI",
            "diagnostics": "engine-specific non-authoritative metadata",
            "model": "version/training/model metadata",
        },
        "safety_boundary": {
            "inside_engine": False,
            "engine_has_physical_authority": False,
            "required_downstream_layer": "deterministic constraints, stale/fault handling, clamps, hysteresis and write-rate controls",
        },
        "control_mode": {
            "part_of_engine_identity": False,
            "values": ["shadow", "active"],
            "current": "shadow",
            "physical_writes_enabled": False,
        },
        "selection": {
            "future_user_selectable": True,
            "future_auto_selectable": True,
            "current_selected_engine_id": BASELINE_ENGINE_ID,
            "writes_enabled": False,
        },
    }


@app.get(
    "/engines/baseline/latest",
    tags=["engines"],
    summary="Re-express the latest frozen v3.5 plan through the common engine contract",
)
async def engines_baseline_latest(
    include_horizon: bool = Query(False),
    include_plan_rows: bool = Query(False),
):
    plan = latest_plan(500)
    if plan.get("generated_at") is None or not plan.get("rows"):
        raise HTTPException(404, "No deterministic optimizer plan is available")
    try:
        engine_input, decision = await asyncio.to_thread(baseline_decision_from_plan, core.cfg, plan)
    except Exception as exc:
        raise HTTPException(500, f"Baseline contract adapter failed: {exc!r}")

    stored_first = float(plan["rows"][0]["battery_action_kw"])
    replayed_first = float(decision.requested_action_kw)
    difference = replayed_first - stored_first
    return {
        "contract_version": "v1",
        "baseline_engine_id": BASELINE_ENGINE_ID,
        "engine_input": engine_input.as_dict(include_horizon=include_horizon),
        "engine_decision": decision.as_dict(include_plan_rows=include_plan_rows),
        "compatibility": {
            "stored_source_planner": plan.get("planner"),
            "stored_first_action_kw": round(stored_first, 6),
            "contract_first_action_kw": round(replayed_first, 6),
            "difference_kw": round(difference, 6),
            "tolerance_kw": 0.00011,
            "pass": abs(difference) <= 0.00011,
        },
        "physical_writes_enabled": False,
    }

app.openapi_schema = None
