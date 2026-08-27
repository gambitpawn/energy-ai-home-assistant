from __future__ import annotations

import asyncio

from fastapi import Query

from .adaptive_learning import active_run
from .engine_input_v2 import input_from_optimizer_plan_v2
from .engine_registry import registry_status
from .model_selector import (
    automatic_selector_maintenance_once,
    evaluate_selector_day,
    latest_control_selection,
    route_selected_decision,
    selector_scores,
    selector_status,
)
from .neural_engine import neural_runtime_status
from .optimizer_store import latest_plan
from .runtime_entry_v180 import app, core

RUNTIME_BUILD = "1.0.81"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

_previous_refresh_optimizer_plan = core._refresh_optimizer_plan
_previous_maintenance_loop = core._forecast_maintenance_loop


async def _refresh_optimizer_plan_with_model_selector():
    # All established engines first produce shadow decisions for the same
    # information vintage. Only then is the persisted incumbent routed as the
    # logical control candidate. No physical write occurs here.
    result = await _previous_refresh_optimizer_plan()
    try:
        plan = latest_plan(500)
        if plan.get("generated_at") is None or not plan.get("rows"):
            return {**result, "model_selector": {"status": "no_information_vintage", "physical_writes_enabled": False}}
        engine_input = input_from_optimizer_plan_v2(plan, core.cfg)
        routed = await asyncio.to_thread(
            route_selected_decision,
            core.cfg,
            engine_input.information_vintage_id,
            engine_input.decision_start,
        )
        return {**result, "model_selector": routed}
    except Exception as exc:
        # Selection is downstream of the frozen baseline. Failure here must not
        # impair plan generation, collection or any shadow engine.
        return {
            **result,
            "model_selector": {
                "status": "failed",
                "error": repr(exc),
                "configured_fallback_engine_id": "deterministic_v35",
                "physical_writes_enabled": False,
            },
        }


core._refresh_optimizer_plan = _refresh_optimizer_plan_with_model_selector


async def _selector_automatic_maintenance_loop():
    # Let collection, forecasts and model learners settle first. Selector work is
    # daily in substance; six-hour polling only catches newly matured days and
    # health regressions without waiting until the next process restart.
    await asyncio.sleep(900)
    while True:
        try:
            if await asyncio.to_thread(active_run) is None:
                await asyncio.to_thread(automatic_selector_maintenance_once, core.cfg)
        except Exception:
            # The selector is fail-safe: deterministic_v35 remains the persisted
            # fallback even if evaluation or promotion maintenance fails.
            pass
        await asyncio.sleep(21600)


async def _maintenance_loop_with_model_selector():
    await asyncio.gather(
        _previous_maintenance_loop(),
        _selector_automatic_maintenance_loop(),
    )


core._forecast_maintenance_loop = _maintenance_loop_with_model_selector


# Replace the older registry route so selection state is no longer reported as
# metadata-only. Engine availability is still runtime-derived.
app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != "/engines"]


@app.get("/engines", tags=["engines"], summary="Multi-engine registry with automatic selector state")
async def engines_registry_v181():
    status = registry_status()
    selector = await asyncio.to_thread(selector_status, core.cfg)
    neural = await asyncio.to_thread(neural_runtime_status)
    for item in status.get("engines") or []:
        engine_id = item.get("engine_id")
        if engine_id == "neural_v1":
            item["available"] = bool(neural.get("shadow_ready"))
            item["learning_enabled"] = bool(neural.get("model_exists"))
            item["runtime_status"] = {
                "model_exists": bool(neural.get("model_exists")),
                "samples": neural.get("samples"),
                "shadow_ready": bool(neural.get("shadow_ready")),
                "trained_at": neural.get("trained_at"),
                "model_id": neural.get("model_id"),
            }
        item["logical_control_selected"] = engine_id == (selector.get("state") or {}).get("selected_engine_id")
    status["selection"] = {
        "logical_control_selection_enabled": True,
        "selected_engine_id": (selector.get("state") or {}).get("selected_engine_id"),
        "fallback_engine_id": "deterministic_v35",
        "selection_mode": "automatic_rolling_promotion_with_rollback",
        "physical_writes_enabled": False,
        "requires_downstream_deterministic_safety": True,
    }
    return status


@app.get("/engines/selector/status", tags=["engines-selector"], summary="Automatic model selector, promotion and rollback status")
async def model_selector_status():
    return await asyncio.to_thread(selector_status, core.cfg)


@app.get("/engines/selector/scores", tags=["engines-selector"], summary="Recent per-engine realized oracle-regret scorecards")
async def model_selector_scores(days: int = Query(30, ge=1, le=180)):
    return await asyncio.to_thread(selector_scores, core.cfg, days)


@app.get("/engines/selector/control/latest", tags=["engines-selector"], summary="Latest logically routed control decision")
async def model_selector_control_latest():
    return {
        "selection": await asyncio.to_thread(latest_control_selection),
        "physical_writes_enabled": False,
    }


@app.post("/engines/selector/evaluate", tags=["engines-selector"], summary="Evaluate one local day for all shared-vintage engines")
async def model_selector_evaluate(
    local_date: str = Query(..., description="Europe/Stockholm YYYY-MM-DD"),
    force: bool = Query(False),
):
    return await asyncio.to_thread(evaluate_selector_day, core.cfg, local_date, force=force)


@app.post("/engines/selector/run", tags=["engines-selector"], summary="Run selector maintenance and promotion policy now")
async def model_selector_run(force: bool = Query(False)):
    running = await asyncio.to_thread(active_run)
    if running is not None:
        return {"ok": True, "status": "deferred", "reason": "adaptive_learning_active", "adaptive_run": running}
    return await asyncio.to_thread(automatic_selector_maintenance_once, core.cfg, force=force)


app.openapi_schema = None
