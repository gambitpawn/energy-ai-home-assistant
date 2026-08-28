from __future__ import annotations

import asyncio

from .optimizer_evaluation import evaluate_matured_optimizer_days
from .runtime_entry_v183_final import app, core
from .ui_v184 import install_ui_v184

RUNTIME_BUILD = "1.0.84"

# History must advance without requiring the user to press "Evaluate matured
# days". The previous runtime evaluated PV/load forecasts automatically but the
# optimizer day evaluator was only exposed as a manual endpoint.
_previous_maintenance_loop = core._forecast_maintenance_loop


async def _optimizer_day_evaluation_loop() -> None:
    # Give startup collection/forecast work a short head start, then backfill
    # mature days. Re-run periodically so yesterday appears automatically.
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(evaluate_matured_optimizer_days, core.cfg, 14)
        except Exception:
            pass
        await asyncio.sleep(21600)


async def _maintenance_loop_v184() -> None:
    await asyncio.gather(
        _previous_maintenance_loop(),
        _optimizer_day_evaluation_loop(),
    )


core._forecast_maintenance_loop = _maintenance_loop_v184
install_ui_v184(app)

core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
