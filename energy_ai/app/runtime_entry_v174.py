from __future__ import annotations

import asyncio

from fastapi import Query

from .neural_auto import automatic_maintenance_once, automatic_status
from .neural_training import model_history
from .runtime_entry_v173 import app, core

RUNTIME_BUILD = "1.0.74"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

_legacy_maintenance_loop = core._forecast_maintenance_loop


async def _neural_automatic_maintenance_loop():
    # Let startup collection/forecast/optimizer initialization finish first.
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(automatic_maintenance_once, core.cfg)
        except Exception:
            # Neural learning is a shadow-only subsystem and must never break the
            # established forecast/optimizer maintenance loop.
            pass
        await asyncio.sleep(3600)


async def _maintenance_loop_with_neural_learning():
    await asyncio.gather(
        _legacy_maintenance_loop(),
        _neural_automatic_maintenance_loop(),
    )


# core.lifespan resolves this module global when the FastAPI application starts.
core._forecast_maintenance_loop = _maintenance_loop_with_neural_learning


@app.get(
    "/engines/neural/auto/status",
    tags=["engines-neural"],
    summary="Automatic neural sample collection and retraining policy status",
)
async def neural_auto_status():
    return await asyncio.to_thread(automatic_status)


@app.post(
    "/engines/neural/auto/run",
    tags=["engines-neural"],
    summary="Run one automatic neural maintenance cycle now",
)
async def neural_auto_run():
    return await asyncio.to_thread(automatic_maintenance_once, core.cfg)


@app.get(
    "/engines/neural/models",
    tags=["engines-neural"],
    summary="Version history for trained neural v1 models",
)
async def neural_models(limit: int = Query(20, ge=1, le=100)):
    return {
        "engine_id": "neural_v1",
        "models": await asyncio.to_thread(model_history, limit),
        "physical_writes_enabled": False,
    }


app.openapi_schema = None
