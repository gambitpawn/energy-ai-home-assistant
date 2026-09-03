from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db
from app import main as core
from app.ha import HomeAssistantClient


LOCAL_TZ = ZoneInfo("Europe/Stockholm")


@pytest.fixture(autouse=True)
def isolated_price_runtime_state(monkeypatch):
    monkeypatch.setattr(core, "_PRICE_REFRESH_LOCK", asyncio.Lock())
    monkeypatch.setattr(core, "_PRICE_REFRESH_STATE", {
        "policy": "coverage_gated_daily_retry_v1",
        "retry_start_local": "13:00",
        "retry_interval_seconds": core.PRICE_RETRY_SECONDS,
        "running": False,
        "attempt_count": 0,
        "last_started_at": None,
        "last_completed_at": None,
        "last_result": None,
        "last_error": None,
        "next_retry_at": None,
    })


def _price_rows(day: date):
    start = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    rows = []
    stamp = start
    while stamp < end:
        rows.append({
            "start": stamp.isoformat(),
            "end": (stamp + timedelta(minutes=15)).isoformat(),
            "price_ore_kwh": 100.0,
            "currency": "SEK",
            "source_price_per_mwh": 1000.0,
        })
        stamp += timedelta(minutes=15)
    return rows


def test_supervisor_websocket_url_uses_core_websocket(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    monkeypatch.delenv("HA_ACCESS_TOKEN", raising=False)
    client = HomeAssistantClient({})

    assert client.auth_mode == "supervisor"
    assert client.base_url == "http://supervisor/core/api"
    assert client._websocket_url() == "ws://supervisor/core/websocket"


def test_long_lived_token_websocket_url_uses_home_assistant_api(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("HA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("HA_BASE_URL", "http://homeassistant.local")
    client = HomeAssistantClient({})

    assert client.auth_mode == "long_lived_token"
    assert client._websocket_url() == "ws://homeassistant.local/api/websocket"


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 3, 29), 92),
        (date(2026, 9, 2), 96),
        (date(2026, 10, 25), 100),
    ],
)
def test_price_coverage_handles_swedish_dst_days(tmp_path, monkeypatch, day, expected):
    path = tmp_path / "prices.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    rows = _price_rows(day)
    db.upsert_prices("SE4", rows, datetime.now(timezone.utc).isoformat())

    coverage = db.price_day_coverage("SE4", day.isoformat())

    assert len(rows) == expected
    assert coverage["expected_intervals"] == expected
    assert coverage["stored_intervals"] == expected
    assert coverage["complete"] is True


def test_price_coverage_rejects_gap_even_when_row_count_matches(tmp_path, monkeypatch):
    day = date(2026, 9, 2)
    path = tmp_path / "prices.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    rows = _price_rows(day)
    rows.pop(20)
    rows.append({
        "start": (datetime(2026, 9, 2, 0, 7, tzinfo=LOCAL_TZ)).astimezone(timezone.utc).isoformat(),
        "end": (datetime(2026, 9, 2, 0, 22, tzinfo=LOCAL_TZ)).astimezone(timezone.utc).isoformat(),
        "price_ore_kwh": 100.0,
        "currency": "SEK",
        "source_price_per_mwh": 1000.0,
    })
    db.upsert_prices("SE4", rows, datetime.now(timezone.utc).isoformat())

    coverage = db.price_day_coverage("SE4", day.isoformat())

    assert coverage["stored_intervals"] == 96
    assert coverage["missing_intervals"] == 1
    assert coverage["unexpected_intervals"] == 1
    assert coverage["complete"] is False


def test_partial_price_response_is_stored_but_not_reported_complete(monkeypatch):
    day = date(2026, 9, 3)
    stored = []

    class FakeHA:
        async def nordpool_prices_15m(self, *_args):
            return _price_rows(day)[:20]

    async def coverage(_day):
        return {"complete": False, "stored_intervals": len(stored), "expected_intervals": 96}

    monkeypatch.setattr(core.collector, "ha", FakeHA())
    monkeypatch.setattr(core, "upsert_prices", lambda _area, rows, _fetched: stored.extend(rows))
    monkeypatch.setattr(core, "_coverage", coverage)

    result = asyncio.run(core._refresh_price_horizon([day]))

    assert len(stored) == 20
    assert result["complete"] is False
    assert result["dates"][day.isoformat()]["error"] == "price_day_incomplete"


def test_parallel_price_refreshes_are_serialized(monkeypatch):
    day = date(2026, 9, 3)
    active = 0
    peak = 0

    class FakeHA:
        async def nordpool_prices_15m(self, *_args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _price_rows(day)

    async def coverage(_day):
        return {"complete": True, "stored_intervals": 96, "expected_intervals": 96}

    monkeypatch.setattr(core.collector, "ha", FakeHA())
    monkeypatch.setattr(core, "upsert_prices", lambda *_args: None)
    monkeypatch.setattr(core, "_coverage", coverage)

    async def scenario():
        await asyncio.gather(
            core._refresh_price_horizon([day]),
            core._refresh_price_horizon([day]),
        )

    asyncio.run(scenario())
    assert peak == 1


def test_missing_current_day_retries_before_thirteen_but_tomorrow_waits(monkeypatch):
    today = date(2026, 9, 2)

    async def coverage(day):
        return {"complete": day != today}

    monkeypatch.setattr(core, "_coverage", coverage)
    before = datetime(2026, 9, 2, 10, 0, tzinfo=LOCAL_TZ)

    assert asyncio.run(core._missing_price_targets(before)) == [today]


def test_tomorrow_retries_after_thirteen_until_complete(monkeypatch):
    today = date(2026, 9, 2)
    tomorrow = today + timedelta(days=1)

    async def coverage(day):
        return {"complete": day == today}

    monkeypatch.setattr(core, "_coverage", coverage)
    after = datetime(2026, 9, 2, 13, 1, tzinfo=LOCAL_TZ)

    assert asyncio.run(core._missing_price_targets(after)) == [tomorrow]


def test_retry_loop_uses_ten_minutes_after_failure(monkeypatch):
    target = date(2026, 9, 3)
    refresh_calls = []
    delays = []

    async def missing(_now=None):
        return [target]

    async def refresh(days=None):
        refresh_calls.append(days)
        return {"complete": len(refresh_calls) > 1}

    async def sleep(delay):
        delays.append(delay)
        if len(delays) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(core, "_missing_price_targets", missing)
    monkeypatch.setattr(core, "_refresh_price_horizon", refresh)
    monkeypatch.setattr(core, "_price_retry_backoff_remaining", lambda: 0.0)
    monkeypatch.setattr(core.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(core._price_retry_loop())

    assert refresh_calls == [[target], [target]]
    assert delays == [core.PRICE_RETRY_SECONDS, 1.0]


def test_startup_failure_backoff_prevents_immediate_second_request(monkeypatch):
    finished = datetime(2026, 9, 2, 11, 0, tzinfo=timezone.utc)
    monkeypatch.setitem(core._PRICE_REFRESH_STATE, "last_completed_at", finished.isoformat())

    remaining = core._price_retry_backoff_remaining(finished + timedelta(seconds=75))

    assert remaining == pytest.approx(core.PRICE_RETRY_SECONDS - 75)


def test_complete_prices_sleep_until_next_local_thirteen(monkeypatch):
    calls = []

    async def missing(_now=None):
        return []

    async def refresh(_days=None):
        calls.append(1)
        return {"complete": True}

    async def sleep(delay):
        assert delay > 1.0
        raise asyncio.CancelledError

    monkeypatch.setattr(core, "_missing_price_targets", missing)
    monkeypatch.setattr(core, "_refresh_price_horizon", refresh)
    monkeypatch.setattr(core.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(core._price_retry_loop())

    assert calls == []


def test_price_refresh_fix_bumps_addon_version():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    assert 'version: "1.0.116"' in (root / "config.yaml").read_text(encoding="utf-8")
    assert 'RELEASE_BUILD = "1.0.116"' in (root / "app" / "runtime_operator.py").read_text(encoding="utf-8")
