from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from app import collector as collector_module
from app import db


def _sample(value: float = 2.0) -> dict:
    return {
        "house_load_kw": {"available": True, "state": value},
        "pv_power_kw": {"available": True, "state": 0.5},
        "battery_soc_pct": {"available": True, "state": 55.0},
    }


def _bucket(delta_minutes: int = -30) -> str:
    current = datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)
    return current.replace(minute=(current.minute // 15) * 15, second=0, microsecond=0).isoformat()


def test_idempotent_raw_persistence_preserves_old_duplicate_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "energy_ai.db")
    db.init_db()
    stamp = _bucket()
    db.insert_raw(stamp, _sample())
    db.insert_raw(stamp, _sample())
    with db.connect_db() as c:
        assert c.execute("SELECT COUNT(*) FROM raw_state WHERE collected_at=?", (stamp,)).fetchone()[0] == 1
        # Simulate duplicate rows written by earlier releases; new inserts must
        # not fail or create another duplicate, nor delete existing user history.
        c.execute("INSERT INTO raw_state(collected_at,payload_json) VALUES (?,?)", (stamp, '{"house_load_kw":{"available":true,"state":8}}'))
    db.insert_raw(stamp, _sample())
    with db.connect_db() as c:
        assert c.execute("SELECT COUNT(*) FROM raw_state WHERE collected_at=?", (stamp,)).fetchone()[0] == 2
    end = (datetime.fromisoformat(stamp) + timedelta(minutes=15)).isoformat()
    result = db.rebuild_15m_bucket(stamp, end, 15)
    assert result["samples"] == 1
    assert result["mean"]["house_load_kw"] == 8.0  # newest duplicate retained once


def test_bounded_missing_bucket_repair_only_reconstructs_existing_raw(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "energy_ai.db")
    db.init_db()
    starts = [_bucket(-75), _bucket(-45), _bucket(-30)]
    assert len(set(starts)) == 3
    for index, stamp in enumerate(starts):
        db.insert_raw((datetime.fromisoformat(stamp) + timedelta(minutes=1)).isoformat(), _sample(index + 1))
    first = db.repair_missing_15m(60, lookback_hours=6, max_buckets=1)
    assert first["repaired"] == 1
    assert first["remaining_missing"] == 2
    second = db.repair_missing_15m(60, lookback_hours=6, max_buckets=3)
    assert second["repaired"] == 2
    assert second["remaining_missing"] == 0
    again = db.repair_missing_15m(60, lookback_hours=6, max_buckets=3)
    assert again["repaired"] == 0
    with db.connect_db() as c:
        assert c.execute("SELECT COUNT(*) FROM state_15m").fetchone()[0] == 3


def test_aggregation_retries_do_not_duplicate_raw_write(monkeypatch):
    class State:
        collected_at = "2026-11-03T13:29:15+00:00"
        def model_dump(self):
            return _sample()

    class HA:
        async def snapshot(self):
            return State()

    raw_calls = []
    bucket_calls = []

    def save_raw(ts, payload):
        raw_calls.append(ts)

    def rebuild(start, end, expected_samples):
        bucket_calls.append(start)
        if len(bucket_calls) <= 2:
            raise sqlite3.OperationalError("database is locked")
        return {"samples": 1}

    monkeypatch.setattr(collector_module, "insert_raw", save_raw)
    monkeypatch.setattr(collector_module, "rebuild_15m_bucket", rebuild)
    collector = collector_module.Collector.__new__(collector_module.Collector)
    collector.cfg = {"collector": {"poll_seconds": 60}}
    collector.ha = HA()
    collector.poll_seconds = 60
    collector.latest = None
    collector.last_error = None
    collector.running = False

    async def scenario():
        await collector.run_once()
        await collector._persistence_queue.join()
        result = collector.persistence_status()
        await collector.close()
        return result

    status = asyncio.run(scenario())
    assert len(raw_calls) == 1
    assert len(bucket_calls) == 3
    assert status["raw_written"] == 1
    assert status["aggregated"] == 1
    assert status["retried"] == 2
    assert status["last_aggregation_error"] is None
    assert status["last_error"] is None


def test_persistent_aggregation_error_visible_without_rewriting_raw(monkeypatch):
    class State:
        collected_at = "2026-11-03T13:29:15+00:00"
        def model_dump(self):
            return _sample()

    class HA:
        async def snapshot(self):
            return State()

    saves = []
    monkeypatch.setattr(collector_module, "insert_raw", lambda ts, payload: saves.append(ts))
    def broken_aggregate(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(collector_module, "rebuild_15m_bucket", broken_aggregate)
    collector = collector_module.Collector.__new__(collector_module.Collector)
    collector.cfg = {"collector": {"poll_seconds": 60}}
    collector.ha = HA()
    collector.poll_seconds = 60
    collector.latest = None
    collector.last_error = None
    collector.running = False

    async def scenario():
        await collector.run_once()
        await collector._persistence_queue.join()
        result = collector.persistence_status()
        await collector.close()
        return result

    status = asyncio.run(scenario())
    assert len(saves) == 1
    assert status["dropped"] == 1
    assert status["retried"] == 3
    assert status["written"] == 0
    assert "database is locked" in status["last_aggregation_error"]
    assert "database is locked" in status["last_error"]


def test_repair_runs_in_heavy_maintenance_worker_not_on_startup():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app"
    maintenance = (root / "runtime_maintenance.py").read_text(encoding="utf-8")
    main = (root / "main.py").read_text(encoding="utf-8")
    assert "async def _history_repair_loop" in maintenance
    block = maintenance[maintenance.index("async def _history_repair_loop"):maintenance.index("async def _optimizer_day_loop")]
    assert "run_low_priority(" in block and "lane=LANE_HEAVY" in block
    assert "repair_missing_15m" in block and "max_buckets" not in block
    assert "rebuild_recent_15m(" not in main
    assert '"persistence":persistence' in main
