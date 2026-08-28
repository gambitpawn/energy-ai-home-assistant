from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .production_state import status as production_status


def _utc(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def candidate_start_status(candidate: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Return strict decision-start timing. A physical command is never early."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw = candidate.get("decision_start")
    if not raw:
        return {
            "state": "invalid",
            "reason": "candidate_missing_decision_start",
            "decision_start": None,
            "seconds_until_start": None,
        }
    try:
        start = _utc(str(raw))
    except Exception:
        return {
            "state": "invalid",
            "reason": "candidate_invalid_decision_start",
            "decision_start": str(raw),
            "seconds_until_start": None,
        }
    seconds = (start - now_utc).total_seconds()
    return {
        "state": "future" if seconds > 0.0 else "started",
        "reason": "decision_start_in_future" if seconds > 0.0 else "decision_start_reached",
        "decision_start": start.isoformat(),
        "seconds_until_start": round(max(0.0, seconds), 3),
    }


class DecisionStartScheduler:
    """Queues future control candidates and dispatches them at decision_start.

    The scheduler captures the fully patched actuator method (including
    diagnostics and the downstream physical cap) and calls that captured method
    only when the candidate interval has started. The previous inverter target is
    therefore left untouched while a future candidate is pending.
    """

    def __init__(self, actuator, *, now_fn: Callable[[], datetime] | None = None):
        self.actuator = actuator
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._original_process_candidate = actuator.process_candidate
        self._original_watchdog_tick = actuator.watchdog_tick
        self._original_disarm = actuator.disarm
        self._original_fail_safe = actuator.fail_safe
        self._original_status = actuator.status
        self._pending: dict[str, dict[str, Any]] = {}
        self._wake = asyncio.Event()
        self._control_lock = asyncio.Lock()
        self._runner_task: asyncio.Task | None = None
        self._last_submission: dict[str, Any] | None = None
        self._last_dispatch: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._closed = False

    def _now(self) -> datetime:
        return self._now_fn().astimezone(timezone.utc)

    def status(self) -> dict[str, Any]:
        now = self._now()
        pending = []
        for key in sorted(self._pending, key=_utc):
            item = self._pending[key]
            timing = candidate_start_status(item, now=now)
            pending.append({
                "decision_start": key,
                "seconds_until_start": timing.get("seconds_until_start"),
                "valid_until": item.get("valid_until"),
                "source": item.get("source"),
                "source_id": item.get("source_id"),
                "engine_id": item.get("engine_id"),
                "requested_action_kw": item.get("requested_action_kw"),
            })
        return {
            "policy": "strict_decision_start_v1",
            "no_early_dispatch": True,
            "previous_command_held_until_next_decision_start": True,
            "fresh_safety_recheck_at_dispatch": True,
            "pending_count": len(pending),
            "pending": pending,
            "next_decision_start": pending[0]["decision_start"] if pending else None,
            "runner_active": self._runner_task is not None and not self._runner_task.done(),
            "last_submission": self._last_submission,
            "last_dispatch": self._last_dispatch,
            "last_error": self._last_error,
        }

    def _ensure_runner(self) -> None:
        if self._closed:
            return
        if self._runner_task is None or self._runner_task.done():
            self._runner_task = asyncio.create_task(
                self._runner_loop(), name="energy-ai-decision-start-scheduler"
            )

    async def submit(self, candidate: dict[str, Any]) -> dict[str, Any]:
        timing = candidate_start_status(candidate, now=self._now())
        self._last_submission = {
            "at": self._now().isoformat(),
            "decision_start": timing.get("decision_start"),
            "state": timing.get("state"),
            "source": candidate.get("source"),
            "engine_id": candidate.get("engine_id"),
            "requested_action_kw": candidate.get("requested_action_kw"),
        }
        if timing["state"] == "invalid":
            return {
                "status": "rejected",
                "reason": timing["reason"],
                "physical_write_performed": False,
                "timing": timing,
            }
        if timing["state"] == "future":
            key = str(timing["decision_start"])
            replaced = key in self._pending
            self._pending[key] = dict(candidate)
            self._wake.set()
            self._ensure_runner()
            return {
                "status": "pending_decision_start",
                "reason": "decision_start_in_future",
                "decision_start": key,
                "valid_until": candidate.get("valid_until"),
                "seconds_until_start": timing["seconds_until_start"],
                "requested_action_kw": candidate.get("requested_action_kw"),
                "physical_write_performed": False,
                "pending_replaced": replaced,
                "previous_physical_target_unchanged": True,
            }
        return await self._dispatch_started(candidate)

    async def _dispatch_started(self, candidate: dict[str, Any]) -> dict[str, Any]:
        async with self._control_lock:
            result = await self._original_process_candidate(candidate)
        self._last_dispatch = {
            "at": self._now().isoformat(),
            "decision_start": candidate.get("decision_start"),
            "source": candidate.get("source"),
            "engine_id": candidate.get("engine_id"),
            "requested_action_kw": candidate.get("requested_action_kw"),
            "status": result.get("status") if isinstance(result, dict) else None,
            "physical_write_performed": bool(result.get("physical_write_performed")) if isinstance(result, dict) else None,
        }
        self._last_error = None
        return result

    async def run_due(self, *, now: datetime | None = None) -> dict[str, Any]:
        now_utc = (now or self._now()).astimezone(timezone.utc)
        due_keys = [key for key in self._pending if _utc(key) <= now_utc]
        if not due_keys:
            return {"status": "nothing_due", "dispatched": False}

        # If the event loop was delayed across more than one boundary, do not
        # briefly dispatch obsolete intervals. Keep only the latest due start.
        selected_key = max(due_keys, key=_utc)
        candidate = self._pending[selected_key]
        for key in due_keys:
            self._pending.pop(key, None)
        try:
            result = await self._dispatch_started(candidate)
            return {
                "status": "dispatched",
                "dispatched": True,
                "decision_start": selected_key,
                "result": result,
                "discarded_older_due_candidates": max(0, len(due_keys) - 1),
            }
        except Exception as exc:
            self._last_error = repr(exc)
            if production_status().get("physical_writes_enabled"):
                try:
                    await self._original_fail_safe(
                        "decision_start_dispatch_exception",
                        {"error": repr(exc), "candidate": candidate},
                    )
                except Exception:
                    pass
            return {
                "status": "failed",
                "dispatched": False,
                "decision_start": selected_key,
                "error": repr(exc),
            }

    async def _runner_loop(self) -> None:
        try:
            while not self._closed:
                due = await self.run_due()
                if due.get("dispatched") or due.get("status") == "failed":
                    continue
                if not self._pending:
                    return
                self._wake.clear()
                next_start = min((_utc(key) for key in self._pending), default=None)
                if next_start is None:
                    return
                delay = max(0.0, (next_start - self._now()).total_seconds())
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            if asyncio.current_task() is self._runner_task:
                self._runner_task = None

    async def activate_with(
        self,
        candidate: dict[str, Any],
        activate: Callable[[], Awaitable[Any]],
    ) -> dict[str, Any]:
        timing = candidate_start_status(candidate, now=self._now())
        if timing["state"] != "started":
            raise RuntimeError(f"candidate_not_started:{timing}")
        async with self._control_lock:
            await activate()
            result = await self._original_process_candidate(candidate)
        self._last_dispatch = {
            "at": self._now().isoformat(),
            "decision_start": candidate.get("decision_start"),
            "source": candidate.get("source"),
            "engine_id": candidate.get("engine_id"),
            "requested_action_kw": candidate.get("requested_action_kw"),
            "status": result.get("status") if isinstance(result, dict) else None,
            "physical_write_performed": bool(result.get("physical_write_performed")) if isinstance(result, dict) else None,
            "activation_transition": True,
        }
        return result

    async def watchdog_tick(self) -> dict[str, Any]:
        async with self._control_lock:
            return await self._original_watchdog_tick()

    async def clear_pending(self, reason: str) -> None:
        if self._pending:
            self._last_submission = {
                "at": self._now().isoformat(),
                "state": "cleared",
                "reason": reason,
                "cleared_count": len(self._pending),
            }
        self._pending.clear()
        self._wake.set()

    async def disarm(self, reason: str = "manual") -> dict[str, Any]:
        await self.clear_pending(f"disarm:{reason}")
        return await self._original_disarm(reason)

    async def fail_safe(self, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.clear_pending(f"fail_safe:{reason}")
        return await self._original_fail_safe(reason, payload)

    async def actuator_status(self) -> dict[str, Any]:
        data = await self._original_status()
        data["decision_start_timing"] = self.status()
        return data

    async def close(self) -> None:
        self._closed = True
        await self.clear_pending("runtime_shutdown")
        task = self._runner_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._runner_task = None


def install_decision_start_scheduler(actuator, *, now_fn: Callable[[], datetime] | None = None) -> DecisionStartScheduler:
    existing = getattr(actuator, "_decision_start_scheduler_v194", None)
    if isinstance(existing, DecisionStartScheduler):
        return existing

    scheduler = DecisionStartScheduler(actuator, now_fn=now_fn)
    actuator.process_candidate = scheduler.submit
    actuator.watchdog_tick = scheduler.watchdog_tick
    actuator.disarm = scheduler.disarm
    actuator.fail_safe = scheduler.fail_safe
    actuator.status = scheduler.actuator_status
    actuator._decision_start_scheduler_v194 = scheduler
    return scheduler
