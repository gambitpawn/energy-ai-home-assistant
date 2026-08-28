from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from . import model_selector as selector
from .adaptive_auto import automatic_maintenance_once as adaptive_maintenance_once
from .adaptive_learning import active_run
from .gradient_training import automatic_maintenance_once as gradient_maintenance_once
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


async def _gradient_loop(cfg) -> None:
    # Neural sample collection runs first. Gradient training consumes the same
    # current-schema teacher samples but maintains its own model revisions.
    await asyncio.sleep(180)
    while True:
        try:
            await asyncio.to_thread(gradient_maintenance_once, cfg)
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
    # Resolve selector functions through the module at call time. The consolidated
    # runtime installs the robust selector patches after modules are imported, so
    # capturing function objects here would accidentally bypass those patches.
    await asyncio.sleep(75)
    today = datetime.now(selector.LOCAL_TZ).date()
    for age_days in range(2, 9):
        try:
            await asyncio.to_thread(
                selector.evaluate_selector_day,
                cfg,
                (today - timedelta(days=age_days)).isoformat(),
                force=True,
            )
        except Exception:
            pass
        await asyncio.sleep(3)
    await asyncio.sleep(max(0, 900 - 75 - 7 * 3))
    while True:
        try:
            if await asyncio.to_thread(active_run) is None:
                await asyncio.to_thread(selector.automatic_selector_maintenance_once, cfg)
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
        _gradient_loop(cfg),
        _pv_loop(cfg),
        _optimizer_day_loop(cfg),
        _selector_loop(cfg),
        soc_replanning_loop(),
        actuator_watchdog_loop(),
    )
