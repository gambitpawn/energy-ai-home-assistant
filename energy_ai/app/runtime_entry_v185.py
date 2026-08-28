from __future__ import annotations

import asyncio
from datetime import timedelta

from .model_selector import LOCAL_TZ, evaluate_selector_day, automatic_selector_maintenance_once
from .runtime_entry_v184 import app, core

RUNTIME_BUILD = "1.0.85"

_previous_maintenance_loop = core._forecast_maintenance_loop


async def _model_score_backfill_loop() -> None:
    # Model scoring is materially heavier than the ordinary optimizer-day replay:
    # each canonical decision is evaluated against a realized 24 h oracle. Start
    # after the ordinary startup work and backfill only genuinely mature days.
    await asyncio.sleep(75)

    # Force a bounded historical backfill independent of the selector epoch.
    # This is required for the Models UI: older stored competition vintages remain
    # valid comparison evidence even if a config/economics change reset promotion
    # eligibility more recently.
    today = __import__('datetime').datetime.now(LOCAL_TZ).date()
    for age_days in range(2, 9):
        day = today - timedelta(days=age_days)
        try:
            await asyncio.to_thread(evaluate_selector_day, core.cfg, day.isoformat(), force=True)
        except Exception:
            pass
        # Avoid monopolising the Raspberry Pi while rebuilding several oracle days.
        await asyncio.sleep(3)

    while True:
        try:
            # Normal maintenance respects the current selector epoch and promotion
            # policy after the one-time UI/history backfill above.
            await asyncio.to_thread(automatic_selector_maintenance_once, core.cfg)
        except Exception:
            pass
        await asyncio.sleep(21600)


async def _maintenance_loop_v185() -> None:
    await asyncio.gather(
        _previous_maintenance_loop(),
        _model_score_backfill_loop(),
    )


core._forecast_maintenance_loop = _maintenance_loop_v185
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
