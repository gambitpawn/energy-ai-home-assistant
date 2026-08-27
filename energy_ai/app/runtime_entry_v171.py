from __future__ import annotations

import asyncio

from fastapi import HTTPException, Query

from .engine_input_v2 import input_from_optimizer_plan_v2
from .engine_registry import BASELINE_ENGINE_ID, registry_status
from .engine_store import insert_engine_run, latest_engine_decisions
from .neural_engine import NeuralV1Engine, neural_runtime_status
from .neural_training import build_training_samples, train_model
from .optimizer_store import latest_plan
from .runtime_entry_v170 import app, core

RUNTIME_BUILD = "1.0.71"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

_legacy_refresh_optimizer_plan = core._refresh_optimizer_plan


async def _refresh_optimizer_plan_with_neural_challenger():
    result = await _legacy_refresh_optimizer_plan()
    neural = neural_runtime_status()
    if not neural.get("shadow_ready"):
        return {**result, "neural_v1": {"shadow_decision": False, "status": "model_not_ready", "samples": neural.get("samples")}}
    try:
        plan = latest_plan(500)
        if plan.get("generated_at") is None or not plan.get("rows"):
            return {**result, "neural_v1": {"shadow_decision": False, "status": "no_baseline_information_vintage"}}
        engine_input = input_from_optimizer_plan_v2(plan, core.cfg)
        decision = await asyncio.to_thread(NeuralV1Engine(core.cfg).decide, engine_input)
        await asyncio.to_thread(insert_engine_run, engine_input, [decision])
        return {
            **result,
            "neural_v1": {
                "shadow_decision": True,
                "information_vintage_id": engine_input.information_vintage_id,
                "decision_id": decision.decision_id,
                "requested_action_kw": decision.requested_action_kw,
                "confidence": decision.diagnostics.get("classification_confidence"),
            },
        }
    except Exception as exc:
        # A challenger must never break the frozen deterministic baseline refresh.
        return {**result, "neural_v1": {"shadow_decision": False, "status": "failed", "error": repr(exc)}}


core._refresh_optimizer_plan = _refresh_optimizer_plan_with_neural_challenger

# Replace the static registry endpoint from 1.0.70 with runtime model readiness.
app.router.routes[:] = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/engines"
]


@app.get("/engines", tags=["engines"], summary="Multi-engine registry with runtime challenger readiness")
async def engines_registry_v171():
    status = registry_status()
    neural = neural_runtime_status()
    for item in status.get("engines") or []:
        if item.get("engine_id") == "neural_v1":
            item["available"] = bool(neural.get("shadow_ready"))
            item["learning_enabled"] = bool(neural.get("model_exists"))
            item["runtime_status"] = {
                "model_exists": bool(neural.get("model_exists")),
                "samples": neural.get("samples"),
                "shadow_ready": bool(neural.get("shadow_ready")),
                "active_eligible": False,
                "trained_at": neural.get("trained_at"),
                "label_source": neural.get("label_source"),
            }
    return status


@app.get("/engines/neural/status", tags=["engines-neural"], summary="Neural v1 training and readiness status")
async def neural_status():
    return await asyncio.to_thread(neural_runtime_status)


@app.post("/engines/neural/build-samples", tags=["engines-neural"], summary="Build matured neural imitation-learning samples")
async def neural_build_samples(
    max_new: int = Query(32, ge=1, le=256),
    candidate_limit: int = Query(1500, ge=10, le=10000),
):
    try:
        return await asyncio.to_thread(build_training_samples, core.cfg, max_new, candidate_limit)
    except Exception as exc:
        raise HTTPException(500, f"Neural training sample build failed: {exc!r}")


@app.post("/engines/neural/train", tags=["engines-neural"], summary="Train neural v1 from matured teacher-labelled samples")
async def neural_train():
    try:
        return await asyncio.to_thread(train_model)
    except Exception as exc:
        raise HTTPException(500, f"Neural v1 training failed: {exc!r}")


@app.post("/engines/neural/bootstrap", tags=["engines-neural"], summary="Build samples and train neural v1 when sufficient data exists")
async def neural_bootstrap(
    max_new: int = Query(64, ge=1, le=256),
    candidate_limit: int = Query(2000, ge=10, le=10000),
):
    try:
        samples = await asyncio.to_thread(build_training_samples, core.cfg, max_new, candidate_limit)
        training = await asyncio.to_thread(train_model)
        return {"samples": samples, "training": training, "status": await asyncio.to_thread(neural_runtime_status)}
    except Exception as exc:
        raise HTTPException(500, f"Neural v1 bootstrap failed: {exc!r}")


@app.get("/engines/neural/latest", tags=["engines-neural"], summary="Latest persisted neural v1 shadow decisions")
async def neural_latest(limit: int = Query(5, ge=1, le=100)):
    decisions = await asyncio.to_thread(latest_engine_decisions, limit)
    return {
        "baseline_engine_id": BASELINE_ENGINE_ID,
        "neural_v1": decisions.get("neural_v1") or [],
        "physical_writes_enabled": False,
    }

app.openapi_schema = None
