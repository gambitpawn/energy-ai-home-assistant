from __future__ import annotations

import asyncio
from types import SimpleNamespace

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

    result = asyncio.run(collector.run_once())

    assert result is state
    assert collector.ha.calls == 1
    assert inserted == [(state.collected_at, {"marker": "state"})]
    assert len(rebuilt) == 1
    assert rebuilt[0][2] == 15
    assert collector.latest is state
    assert collector.last_error is None


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
