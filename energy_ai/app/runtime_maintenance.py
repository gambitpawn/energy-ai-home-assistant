from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from . import model_selector as selector
from .adaptive_auto import automatic_maintenance_once as adaptive_maintenance_once
from .adaptive_learning import active_run
from .evaluation_decomposition import run_pending_evaluation_decomposition
from .gradient_training import automatic_maintenance_once as gradient_maintenance_once
from .maintenance_coordination import run_low_priority
from .neural_auto import automatic_maintenance_once as neural_maintenance_once
from .optimizer_evaluation import evaluate_matured_optimizer_days
from .pv_auto import automatic_pv_retraining_once


def _seconds_until_slot(*, minute: int, period_hours: int = 1, phase_hour: int = 0) -> float:
    """Return seconds to the next fixed UTC maintenance slot.

    Fixed wall-clock slots keep restarts from accidentally moving heavy work
    onto quarter-boundary control planning.
    """
    now = datetime.now(timezone.utc)
    target = now.replace(minute=int(minute), second=0, microsecond=0)
    if period_hours > 1:
        delta = (int(phase_hour) - target.hour) % int(period_hours)
        target += timedelta(hours=delta)
    if target <= now:
        target += timedelta(hours=int(period_hours))
    return max(0.0, (target - now).total_seconds())


def _seconds_until_daily_utc(*, hour: int, minute: int) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


async def _neural_loop(cfg) -> None:
    while True:
        await asyncio.sleep(_seconds_until_slot(minute=5))
        try:
            await run_low_priority("neural_maintenance", neural_maintenance_once, cfg)
        except Exception:
            pass


async def _adaptive_loop(cfg) -> None:
    while True:
        await asyncio.sleep(_seconds_until_slot(minute=20))
        try:
            await run_low_priority("adaptive_maintenance", adaptive_maintenance_once, cfg)
        except Exception:
            pass


async def _gradient_loop(cfg) -> None:
    # Neural sample collection runs first. Gradient training consumes the same
    # current-schema teacher samples but maintains its own model revisions.
    while True:
        await asyncio.sleep(_seconds_until_slot(minute=35))
        try:
            await run_low_priority("gradient_maintenance", gradient_maintenance_once, cfg)
        except Exception:
            pass


async def _pv_loop(cfg) -> None:
    while True:
        await asyncio.sleep(_seconds_until_slot(minute=50, period_hours=6, phase_hour=3))
        try:
            if await asyncio.to_thread(active_run) is None:
                await run_low_priority("pv_retraining", automatic_pv_retraining_once)
        except Exception:
            pass


async def _optimizer_day_loop(cfg) -> None:
    while True:
        await asyncio.sleep(_seconds_until_slot(minute=8, period_hours=6, phase_hour=1))
        try:
            await run_low_priority("optimizer_day_evaluation", evaluate_matured_optimizer_days, cfg, 14)
        except Exception:
            pass


async def _evaluation_decomposition_loop(cfg) -> None:
    """Nightly detailed evaluation, including initial retroactive backfill.

    02:50 UTC sits halfway between the hourly gradient job at :35 and the next
    neural job at :05, and it does not coincide with the six-hour PV job (03:50,
    09:50, 15:50, 21:50 UTC). On the first night the loop can therefore backfill
    the small existing history. Each day is a separate low-priority job with a
    short pause between days, so other maintenance can interleave if necessary.
    Control planning, arming and the watchdog never acquire the low-priority lock.
    """
    while True:
        await asyncio.sleep(_seconds_until_daily_utc(hour=2, minute=50))
        for _ in range(14):
            try:
                result = await run_low_priority(
                    "evaluation_decomposition",
                    run_pending_evaluation_decomposition,
                    cfg,
                    1,
                )
            except Exception:
                break
            if int(result.get("processed_count") or 0) == 0:
                break
            await asyncio.sleep(20)


async def _selector_loop(cfg) -> None:
    # Resolve selector functions through the module at call time. The consolidated
    # runtime installs the robust selector patches after modules are imported, so
    # capturing function objects here would accidentally bypass those patches.
    await asyncio.sleep(_seconds_until_slot(minute=23, period_hours=6, phase_hour=5))
    today = datetime.now(selector.LOCAL_TZ).date()
    for age_days in range(2, 9):
        try:
            await run_low_priority(
                f"selector_backfill_{age_days}",
                selector.evaluate_selector_day,
                cfg,
                (today - timedelta(days=age_days)).isoformat(),
                force=True,
            )
        except Exception:
            pass
        await asyncio.sleep(3)
    while True:
        await asyncio.sleep(_seconds_until_slot(minute=23, period_hours=6, phase_hour=5))
        try:
            if await asyncio.to_thread(active_run) is None:
                await run_low_priority(
                    "selector_maintenance",
                    selector.automatic_selector_maintenance_once,
                    cfg,
                )
        except Exception:
            pass


async def combined_maintenance_loop(
    base_loop: Callable[[], Awaitable[None]],
    cfg,
    soc_replanning_loop: Callable[[], Awaitable[None]],
    actuator_watchdog_loop: Callable[[], Awaitable[None]],
    actuator_audit_loop: Callable[[], Awaitable[None]],
) -> None:
    await asyncio.gather(
        base_loop(),
        _neural_loop(cfg),
        _adaptive_loop(cfg),
        _gradient_loop(cfg),
        _pv_loop(cfg),
        _optimizer_day_loop(cfg),
        _evaluation_decomposition_loop(cfg),
        _selector_loop(cfg),
        soc_replanning_loop(),
        actuator_watchdog_loop(),
        actuator_audit_loop(),
    )
