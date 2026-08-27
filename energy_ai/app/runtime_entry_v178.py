from __future__ import annotations

import asyncio

from fastapi import Query

from .adaptive_learning import active_run
from .pv_auto import automatic_pv_retraining_once, pv_auto_status
from .runtime_entry_v177 import app, core

RUNTIME_BUILD = "1.0.78"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

_previous_maintenance_loop = core._forecast_maintenance_loop


async def _pv_automatic_retraining_loop():
    # Forecasting, collection, neural maintenance and adaptive learning get priority.
    await asyncio.sleep(300)
    while True:
        try:
            if active_run() is None:
                await asyncio.to_thread(automatic_pv_retraining_once)
        except Exception:
            # PV calibration is advisory/shadow-safe and must never break runtime maintenance.
            pass
        await asyncio.sleep(21600)


async def _maintenance_loop_with_pv_retraining():
    await asyncio.gather(
        _previous_maintenance_loop(),
        _pv_automatic_retraining_loop(),
    )


core._forecast_maintenance_loop = _maintenance_loop_with_pv_retraining


@app.get(
    "/forecast/pv/auto/status",
    tags=["forecast-pv"],
    summary="Automatic PV calibration retraining and promotion status",
)
async def pv_retraining_status():
    return await asyncio.to_thread(pv_auto_status)


@app.post(
    "/forecast/pv/auto/run",
    tags=["forecast-pv"],
    summary="Run one PV calibration retraining attempt",
)
async def pv_retraining_run(force: bool = Query(False)):
    running = await asyncio.to_thread(active_run)
    if running is not None:
        return {
            "ok": True,
            "status": "deferred",
            "reason": "adaptive_learning_active",
            "adaptive_run": running,
        }
    return await asyncio.to_thread(automatic_pv_retraining_once, force=force)


app.openapi_schema = None
