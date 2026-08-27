from __future__ import annotations

import asyncio

from fastapi import Query

from .adaptive_auto import automatic_maintenance_once, automatic_status
from .adaptive_learning import latest_learning_status
from .adaptive_replay import build_daily_evaluator
from .runtime_entry_v175 import app, core

RUNTIME_BUILD = "1.0.76"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

_previous_maintenance_loop = core._forecast_maintenance_loop


async def _adaptive_automatic_maintenance_loop():
    # Startup collection and the established forecast/neural loops get priority.
    await asyncio.sleep(120)
    while True:
        try:
            await asyncio.to_thread(automatic_maintenance_once, core.cfg)
        except Exception:
            # This is a shadow-only challenger. Learning must never interrupt v3.5,
            # forecasting, data collection, or neural training.
            pass
        await asyncio.sleep(3600)


async def _maintenance_loop_with_adaptive_learning():
    await asyncio.gather(
        _previous_maintenance_loop(),
        _adaptive_automatic_maintenance_loop(),
    )


# core.lifespan resolves this global after runtime modules have been imported.
core._forecast_maintenance_loop = _maintenance_loop_with_adaptive_learning


@app.get(
    "/engines/adaptive/status",
    tags=["engines-adaptive"],
    summary="Adaptive deterministic challenger learning status",
)
async def adaptive_status():
    return await asyncio.to_thread(automatic_status)


@app.post(
    "/engines/adaptive/auto/run",
    tags=["engines-adaptive"],
    summary="Run one adaptive 24h feedback cycle",
)
async def adaptive_auto_run(
    replay_date: str | None = Query(None, description="Local YYYY-MM-DD; defaults to previous complete day"),
    force: bool = Query(False, description="Allow rerunning a day already learned"),
):
    return await asyncio.to_thread(automatic_maintenance_once, core.cfg, replay_date, force=force)


@app.get(
    "/engines/adaptive/replay/check",
    tags=["engines-adaptive"],
    summary="Validate that a day is ready for adaptive closed-loop learning",
)
async def adaptive_replay_check(replay_date: str = Query(..., description="Local YYYY-MM-DD")):
    try:
        evaluator = await asyncio.to_thread(build_daily_evaluator, core.cfg, replay_date)
        status = await asyncio.to_thread(latest_learning_status)
        return {
            "ok": True,
            "replay_date": replay_date,
            "initial_soc_pct": evaluator.initial_soc_pct,
            "intervals": len(evaluator.rows),
            "actual_coverage_fraction": evaluator.data.get("actual_coverage_fraction"),
            "information_vintages": len(evaluator.vintage_map),
            "reference_price_ore_kwh": evaluator.reference_price_ore_kwh,
            "candidate_parameters": status.get("candidate_parameters"),
            "physical_writes_enabled": False,
        }
    except Exception as exc:
        return {"ok": False, "replay_date": replay_date, "reason": repr(exc), "physical_writes_enabled": False}


app.openapi_schema = None
