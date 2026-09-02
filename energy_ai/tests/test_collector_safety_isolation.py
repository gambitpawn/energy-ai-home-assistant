from __future__ import annotations

import asyncio
import threading

from app import collector as collector_module


class FakeState:
    def __init__(self, collected_at: str):
        self.collected_at = collected_at

    def model_dump(self):
        return {"marker": "state"}


class FakeHA:
    def __init__(self, state: FakeState):
        self.state = state
        self.calls = 0

    async def snapshot(self):
        self.calls += 1
        return self.state


def test_run_once_only_collects_and_buckets_state(monkeypatch):
    inserted = []
    rebuilt = []
    state = FakeState("2026-08-31T04:43:27+00:00")

    monkeypatch.setattr(
        collector_module,
        "insert_raw",
        lambda collected_at, payload: inserted.append((collected_at, payload)),
    )
    monkeypatch.setattr(
        collector_module,
        "rebuild_15m_bucket",
        lambda start, end, expected_samples: rebuilt.append((start, end, expected_samples)),
    )

    collector = collector_module.Collector.__new__(collector_module.Collector)
    collector.cfg = {"collector": {"poll_seconds": 60}}
    collector.ha = FakeHA(state)
    collector.poll_seconds = 60
    collector.latest = None
    collector.last_error = "old"
    collector.running = False

    async def scenario():
        result = await collector.run_once()
        await collector._persistence_queue.join()
        await collector.close()
        return result

    result = asyncio.run(scenario())

    assert result is state
    assert collector.ha.calls == 1
    assert inserted == [(state.collected_at, {"marker": "state"})]
    assert len(rebuilt) == 1
    assert rebuilt[0][2] == 15
    assert collector.latest is state
    assert collector.last_error is None


def test_live_snapshot_is_published_before_persistence_finishes(monkeypatch):
    state = FakeState("2026-08-31T04:43:27+00:00")
    entered = threading.Event()
    release = threading.Event()

    def blocked_insert(*_args):
        entered.set()
        release.wait(timeout=2.0)

    monkeypatch.setattr(collector_module, "insert_raw", blocked_insert)
    monkeypatch.setattr(collector_module, "rebuild_15m_bucket", lambda *args, **kwargs: None)
    collector = collector_module.Collector.__new__(collector_module.Collector)
    collector.cfg = {"collector": {"poll_seconds": 60}}
    collector.ha = FakeHA(state)
    collector.poll_seconds = 60
    collector.latest = None
    collector.last_error = None
    collector.running = False

    async def scenario():
        result = await collector.run_once()
        assert result is state
        assert collector.latest is state
        assert await asyncio.to_thread(entered.wait, 1.0)
        assert collector.latest is state
        release.set()
        await collector._persistence_queue.join()
        await collector.close()

    asyncio.run(scenario())


def test_transient_persistence_failure_is_retried_without_blocking_live_state(monkeypatch):
    state = FakeState("2026-08-31T04:43:27+00:00")
    attempts = []

    def flaky_insert(*_args):
        attempts.append("attempt")
        if len(attempts) == 1:
            raise RuntimeError("database is locked")

    monkeypatch.setattr(collector_module, "insert_raw", flaky_insert)
    monkeypatch.setattr(collector_module, "rebuild_15m_bucket", lambda *args, **kwargs: None)
    collector = collector_module.Collector.__new__(collector_module.Collector)
    collector.cfg = {"collector": {"poll_seconds": 60}}
    collector.ha = FakeHA(state)
    collector.poll_seconds = 60
    collector.latest = None
    collector.last_error = None
    collector.running = False

    async def scenario():
        await collector.run_once()
        assert collector.latest is state
        await collector._persistence_queue.join()
        await collector.close()

    asyncio.run(scenario())
    assert len(attempts) == 2
    assert collector.persistence_retried == 1
    assert collector.persistence_written == 1


def test_collector_has_no_inline_forecast_maintenance():
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "collector.py").read_text(encoding="utf-8")

    assert "LoadForecaster" not in source
    assert "evaluate_matured_load_forecasts" not in source
    assert "_load_forecast_maintenance" not in source
    assert "await self._load_forecast_maintenance" not in source


def test_forecast_maintenance_remains_in_separate_main_loop():
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    assert "async def _forecast_maintenance_loop" in source
    assert "await _refresh_load_forecast()" in source
