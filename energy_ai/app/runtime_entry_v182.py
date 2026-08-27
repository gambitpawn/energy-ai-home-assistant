from __future__ import annotations

import asyncio

from fastapi import Query

from . import model_selector as selector
from .adaptive_learning import active_run
from .engine_input_v2 import input_from_optimizer_plan_v2
from .engine_registry import registry_status
from .model_selector_policy import install_selector_policy_patch
from .model_selector_robust import install_robust_selector_patch
from .model_selector_state import install_selector_state_patch
from .neural_engine import neural_runtime_status
from .optimizer_store import latest_plan
from .runtime_entry_v180 import app, core

# Install the SQLite-safe state loader and 92/96 coverage rule first, then replace
# the v1.81 selector policy/routing with the robust 10-day, revision-aware policy.
install_selector_state_patch()
install_selector_policy_patch()
install_robust_selector_patch()

RUNTIME_BUILD = "1.0.82"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

_previous_refresh_optimizer_plan = core._refresh_optimizer_plan
_previous_maintenance_loop = core._forecast_maintenance_loop


async def _refresh_optimizer_plan_with_robust_selector():
    # All established engines first produce shadow decisions on the same vintage.
    # The selector then routes one logical control candidate. Physical writes stay
    # outside this layer and remain disabled.
    result = await _previous_refresh_optimizer_plan()
    try:
        plan = latest_plan(500)
        if plan.get("generated_at") is None or not plan.get("rows"):
            return {
                **result,
                "model_selector": {
                    "status": "no_information_vintage",
                    "physical_writes_enabled": False,
                },
            }
        engine_input = input_from_optimizer_plan_v2(plan, core.cfg)
        routed = await asyncio.to_thread(
            selector.route_selected_decision,
            core.cfg,
            engine_input.information_vintage_id,
            engine_input.decision_start,
        )
        return {**result, "model_selector": routed}
    except Exception as exc:
        return {
            **result,
            "model_selector": {
                "status": "failed",
                "error": repr(exc),
                "configured_fallback_engine_id": "deterministic_v35",
                "physical_writes_enabled": False,
            },
        }


core._refresh_optimizer_plan = _refresh_optimizer_plan_with_robust_selector


async def _robust_selector_automatic_maintenance_loop():
    # Selector evaluation is CPU-heavy and is deferred while adaptive learning is
    # active. Six-hour polling catches newly mature days without competing with
    # the nightly adaptive parameter search.
    await asyncio.sleep(900)
    while True:
        try:
            if await asyncio.to_thread(active_run) is None:
                await asyncio.to_thread(selector.automatic_selector_maintenance_once, core.cfg)
        except Exception:
            # Fail-safe behavior is inherent: deterministic_v35 remains the
            # persistent fallback even if selector maintenance itself fails.
            pass
        await asyncio.sleep(21600)


async def _maintenance_loop_with_robust_selector():
    await asyncio.gather(
        _previous_maintenance_loop(),
        _robust_selector_automatic_maintenance_loop(),
    )


core._forecast_maintenance_loop = _maintenance_loop_with_robust_selector


# Replace older registry route with runtime selection state.
app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != "/engines"]


@app.get("/engines", tags=["engines"], summary="Multi-engine registry with robust automatic selector")
async def engines_registry_v182():
    status = registry_status()
    selection = await asyncio.to_thread(selector.selector_status, core.cfg)
    neural = await asyncio.to_thread(neural_runtime_status)
    selected_engine = (selection.get("state") or {}).get("selected_engine_id")
    selected_model_key = selection.get("selected_model_key")
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
        item["logical_control_selected"] = engine_id == selected_engine
    status["selection"] = {
        "logical_control_selection_enabled": True,
        "selected_engine_id": selected_engine,
        "selected_model_key": selected_model_key,
        "fallback_engine_id": "deterministic_v35",
        "selection_mode": "robust_10_day_promotion_with_live_disqualification",
        "physical_writes_enabled": False,
        "requires_downstream_deterministic_safety": True,
    }
    return status


@app.get(
    "/engines/selector/status",
    tags=["engines-selector"],
    summary="Robust automatic model selector, circuit breaker and rollback status",
)
async def model_selector_status_v182():
    return await asyncio.to_thread(selector.selector_status, core.cfg)


@app.get(
    "/engines/selector/scores",
    tags=["engines-selector"],
    summary="Recent per-engine realized oracle-regret scorecards",
)
async def model_selector_scores_v182(days: int = Query(30, ge=1, le=180)):
    return await asyncio.to_thread(selector.selector_scores, core.cfg, days)


@app.get(
    "/engines/selector/control/latest",
    tags=["engines-selector"],
    summary="Latest logically routed control decision and live health result",
)
async def model_selector_control_latest_v182():
    return {
        "selection": await asyncio.to_thread(selector.latest_control_selection),
        "physical_writes_enabled": False,
    }


@app.post(
    "/engines/selector/evaluate",
    tags=["engines-selector"],
    summary="Evaluate one local day for same-revision shared-vintage engines",
)
async def model_selector_evaluate_v182(
    local_date: str = Query(..., description="Europe/Stockholm YYYY-MM-DD"),
    force: bool = Query(False),
):
    return await asyncio.to_thread(selector.evaluate_selector_day, core.cfg, local_date, force=force)


@app.post(
    "/engines/selector/run",
    tags=["engines-selector"],
    summary="Run robust selector maintenance and promotion policy now",
)
async def model_selector_run_v182(force: bool = Query(False)):
    running = await asyncio.to_thread(active_run)
    if running is not None:
        return {
            "ok": True,
            "status": "deferred",
            "reason": "adaptive_learning_active",
            "adaptive_run": running,
        }
    return await asyncio.to_thread(selector.automatic_selector_maintenance_once, core.cfg, force=force)


app.openapi_schema = None
