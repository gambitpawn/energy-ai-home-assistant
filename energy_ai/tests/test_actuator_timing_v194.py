from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.actuator_timing_v194 import (
    POST_BOUNDARY_REFRESH_GRACE_SECONDS,
    candidate_start_status,
    install_decision_start_scheduler,
)


class FakeActuator:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    async def process_candidate(self, candidate):
        self.calls.append(("process", candidate["requested_action_kw"]))
        return {
            "status": "acknowledged",
            "physical_write_performed": True,
            "requested_action_kw": candidate["requested_action_kw"],
        }

    async def watchdog_tick(self):
        self.calls.append(("watchdog", None))
        return {"status": "healthy"}

    async def disarm(self, reason="manual"):
        self.calls.append(("disarm", reason))
        return {"ok": True}

    async def fail_safe(self, reason, payload=None):
        self.calls.append(("fail_safe", reason))
        return {"ok": False, "status": "fail_safe", "reason": reason}

    async def status(self):
        return {"base_status": True}


def _candidate(start: datetime, action: float, *, source_id: str = "v1", engine_id: str = "deterministic_v35"):
    return {
        "source": "selector_quarter_control",
        "source_id": source_id,
        "engine_id": engine_id,
        "decision_start": start.isoformat(),
        "valid_until": (start + timedelta(minutes=15)).isoformat(),
        "requested_action_kw": action,
    }


def test_candidate_start_status_is_strictly_future_until_boundary():
    now = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
    start = now + timedelta(minutes=15)
    assert candidate_start_status(_candidate(start, 1.0), now=now)["state"] == "future"
    assert candidate_start_status(_candidate(start, 1.0), now=start - timedelta(microseconds=1))["state"] == "future"
    assert candidate_start_status(_candidate(start, 1.0), now=start)["state"] == "started"


def test_future_candidate_waits_for_post_boundary_refresh_before_fallback_dispatch():
    async def scenario():
        clock = [datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)]
        actuator = FakeActuator()
        scheduler = install_decision_start_scheduler(actuator, now_fn=lambda: clock[0])
        start = clock[0] + timedelta(minutes=15)

        pending = await actuator.process_candidate(_candidate(start, 1.85))
        assert pending["status"] == "pending_decision_start"
        assert pending["physical_write_performed"] is False
        assert pending["previous_physical_target_unchanged"] is True
        assert actuator.calls == []

        clock[0] = start
        waiting = await scheduler.run_due(now=clock[0])
        assert waiting["status"] == "waiting_for_post_boundary_refresh"
        assert actuator.calls == []
        status = scheduler.status()
        assert status["pre_boundary_candidate_waits_for_fresh_selection"] is True
        assert status["pending"][0]["waiting_for_post_boundary_refresh"] is True

        clock[0] = start + timedelta(seconds=POST_BOUNDARY_REFRESH_GRACE_SECONDS)
        due = await scheduler.run_due(now=clock[0])
        assert due["status"] == "dispatched"
        assert due["fallback_pre_boundary_candidate"] is True
        assert actuator.calls == [("process", 1.85)]
        await scheduler.close()

    asyncio.run(scenario())


def test_post_boundary_refreshed_candidate_replaces_stale_candidate_without_transient_write():
    async def scenario():
        clock = [datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)]
        actuator = FakeActuator()
        scheduler = install_decision_start_scheduler(actuator, now_fn=lambda: clock[0])
        start = clock[0] + timedelta(minutes=15)

        await actuator.process_candidate(
            _candidate(start, -1.65, source_id="pre-boundary-vintage", engine_id="deterministic_v35")
        )

        clock[0] = start
        waiting = await scheduler.run_due(now=clock[0])
        assert waiting["status"] == "waiting_for_post_boundary_refresh"
        assert actuator.calls == []

        # Mirrors the observed production sequence: the current-quarter pipeline
        # finishes a few seconds after the boundary with a new routed engine.
        clock[0] = start + timedelta(seconds=3)
        refreshed = await actuator.process_candidate(
            _candidate(start, -0.40, source_id="fresh-vintage", engine_id="deterministic_refined_v1")
        )
        assert refreshed["status"] == "acknowledged"
        assert refreshed["replaced_pre_boundary_candidate"] is True
        assert actuator.calls == [("process", -0.40)]
        assert scheduler.status()["pending_count"] == 0
        await scheduler.close()

    asyncio.run(scenario())


def test_same_start_newer_future_candidate_replaces_pending_candidate():
    async def scenario():
        clock = [datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)]
        actuator = FakeActuator()
        scheduler = install_decision_start_scheduler(actuator, now_fn=lambda: clock[0])
        start = clock[0] + timedelta(minutes=15)

        first = await actuator.process_candidate(_candidate(start, 1.0, source_id="old"))
        second = await actuator.process_candidate(_candidate(start, 1.7, source_id="new"))
        assert first["pending_replaced"] is False
        assert second["pending_replaced"] is True
        assert scheduler.status()["pending_count"] == 1

        clock[0] = start + timedelta(seconds=POST_BOUNDARY_REFRESH_GRACE_SECONDS)
        await scheduler.run_due(now=clock[0])
        assert actuator.calls == [("process", 1.7)]
        await scheduler.close()

    asyncio.run(scenario())


def test_delayed_runner_skips_obsolete_due_intervals():
    async def scenario():
        clock = [datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)]
        actuator = FakeActuator()
        scheduler = install_decision_start_scheduler(actuator, now_fn=lambda: clock[0])
        first_start = clock[0] + timedelta(minutes=15)
        second_start = clock[0] + timedelta(minutes=30)

        await actuator.process_candidate(_candidate(first_start, 1.0, source_id="first"))
        await actuator.process_candidate(_candidate(second_start, -1.0, source_id="second"))
        clock[0] = second_start + timedelta(seconds=POST_BOUNDARY_REFRESH_GRACE_SECONDS + 1)
        result = await scheduler.run_due(now=clock[0])

        assert result["discarded_older_due_candidates"] == 1
        assert actuator.calls == [("process", -1.0)]
        await scheduler.close()

    asyncio.run(scenario())


def test_active_transition_serializes_mode_change_and_current_dispatch():
    async def scenario():
        clock = [datetime(2026, 8, 28, 14, 45, tzinfo=timezone.utc)]
        actuator = FakeActuator()
        scheduler = install_decision_start_scheduler(actuator, now_fn=lambda: clock[0])
        events: list[str] = []

        original_process = scheduler._original_process_candidate

        async def process_with_event(candidate):
            events.append("dispatch")
            return await original_process(candidate)

        scheduler._original_process_candidate = process_with_event

        async def activate():
            events.append("activate")

        result = await scheduler.activate_with(_candidate(clock[0], 0.5), activate)
        assert result["status"] == "acknowledged"
        assert events == ["activate", "dispatch"]
        await scheduler.close()

    asyncio.run(scenario())


def test_disarm_clears_pending_candidate():
    async def scenario():
        clock = [datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)]
        actuator = FakeActuator()
        scheduler = install_decision_start_scheduler(actuator, now_fn=lambda: clock[0])
        start = clock[0] + timedelta(minutes=15)

        await actuator.process_candidate(_candidate(start, 1.0))
        assert scheduler.status()["pending_count"] == 1
        await actuator.disarm("test")
        assert scheduler.status()["pending_count"] == 0
        assert actuator.calls == [("disarm", "test")]
        await scheduler.close()

    asyncio.run(scenario())
