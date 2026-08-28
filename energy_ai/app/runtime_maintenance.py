from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from .adaptive_auto import automatic_maintenance_once as adaptive_maintenance_once
from .adaptive_learning import active_run
from .model_selector import LOCAL_TZ, automatic_selector_maintenance_once, evaluate_selector_day
from .neural_auto import automatic_maintenance_once as neural_maintenance_once
from .optimizer_evaluation import evaluate_matured_optimizer_days
from .pv_auto import automatic_pv_retraining_once


async def _neural_loop(cfg) -> None:
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(neural_maintenance_once, cfg)
        except Exception:
            pass
        await asyncio.sleep(3600)


async def _adaptive_loop(cfg) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await asyncio.to_thread(adaptive_maintenance_once, cfg)
        except Exception:
            pass
        await asyncio.sleep(3600)


async def _pv_loop(cfg) -> None:
    await asyncio.sleep(300)
    while True:
        try:
            if await asyncio.to_thread(active_run) is None:
                await asyncio.to_thread(automatic_pv_retraining_once)
        except Exception:
            pass
        await asyncio.sleep(21600)


async def _optimizer_day_loop(cfg) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(evaluate_matured_optimizer_days, cfg, 14)
        except Exception:
            pass
        await asyncio.sleep(21600)


async def _selector_loop(cfg) -> None:
    # First rebuild a bounded set of mature model-score days for the Models UI,
    # then fall back to normal promotion-aware selector maintenance.
    await asyncio.sleep(75)
    today = datetime.now(LOCAL_TZ).date()
    for age_days in range(2, 9):
        try:
            await asyncio.to_thread(evaluate_selector_day, cfg, (today - timedelta(days=age_days)).isoformat(), force=True)
        except Exception:
            pass
        await asyncio.sleep(3)
    await asyncio.sleep(max(0, 900 - 75 - 7 * 3))
    while True:
        try:
            if await asyncio.to_thread(active_run) is None:
                await asyncio.to_thread(automatic_selector_maintenance_once, cfg)
        except Exception:
            pass
        await asyncio.sleep(21600)


async def combined_maintenance_loop(
    base_loop: Callable[[], Awaitable[None]],
    cfg,
    soc_replanning_loop: Callable[[], Awaitable[None]],
    actuator_watchdog_loop: Callable[[], Awaitable[None]],
) -> None:
    await asyncio.gather(
        base_loop(),
        _neural_loop(cfg),
        _adaptive_loop(cfg),
        _pv_loop(cfg),
        _optimizer_day_loop(cfg),
        _selector_loop(cfg),
        soc_replanning_loop(),
        actuator_watchdog_loop(),
    )
