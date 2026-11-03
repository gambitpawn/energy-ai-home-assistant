from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import db
from app import retention

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)


def _stamp(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def _setup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    with db.connect_db() as c:
        c.executescript("""
            CREATE TABLE optimizer_plan(generated_at TEXT, start_utc TEXT, payload_json TEXT);
            CREATE TABLE optimizer_plan_summary(generated_at TEXT, payload_json TEXT);
            CREATE TABLE optimizer_day_eval(local_date TEXT PRIMARY KEY,payload_json TEXT);
            CREATE TABLE engine_information_vintage(information_vintage_id TEXT PRIMARY KEY,generated_at TEXT);
            CREATE TABLE engine_decision(information_vintage_id TEXT,generated_at TEXT,
                FOREIGN KEY(information_vintage_id) REFERENCES engine_information_vintage(information_vintage_id));
            CREATE INDEX idx_test_decision_vintage ON engine_decision(information_vintage_id);
            CREATE TABLE load_forecast_15m(generated_at TEXT);
            CREATE TABLE protected_training(id INTEGER PRIMARY KEY, payload TEXT);
            INSERT INTO protected_training(payload) VALUES('model parameters must survive');
        """)


def _count(table: str) -> int:
    with db.connect_db() as c:
        return int(c.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def test_raw_is_only_deleted_when_aggregate_exists(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    raw_date = _stamp(45)
    dt = datetime.fromisoformat(raw_date)
    bucket = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0).isoformat()
    with db.connect_db() as c:
        c.execute("INSERT INTO raw_state(collected_at,payload_json) VALUES (?,?)", (raw_date, "{}"))
        c.execute("INSERT INTO raw_state(collected_at,payload_json) VALUES (?,?)", (_stamp(46), "{}"))
        c.execute("INSERT INTO raw_state(collected_at,payload_json) VALUES (?,?)", (_stamp(5), "{}"))
        c.execute("INSERT INTO state_15m(bucket_start,collected_at,payload_json) VALUES (?,?,?)",
                  (bucket, raw_date, "{}"))
    result = retention.run_retention_batch(now=NOW)
    assert result["removed"]["raw_state"] == 1
    assert result["protected"]["raw_state"] == 1
    assert _count("raw_state") == 2
    assert _count("state_15m") == 1
    assert _count("protected_training") == 1


def test_plans_only_retire_after_completed_daily_evaluation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    date_old = _stamp(20)
    local_day = datetime.fromisoformat(date_old).astimezone(ZoneInfo("Europe/Stockholm")).date().isoformat()
    with db.connect_db() as c:
        c.execute("INSERT INTO optimizer_plan VALUES (?,?,?)", (date_old, date_old, "{}"))
        c.execute("INSERT INTO optimizer_plan VALUES (?,?,?)", (_stamp(2), _stamp(2), "{}"))
        c.execute("INSERT INTO optimizer_plan_summary VALUES (?,?)", (_stamp(45), "{}"))
    first = retention.run_retention_batch(now=NOW)
    assert first["protected"]["optimizer_plan"] == 1
    assert _count("optimizer_plan") == 2
    assert _count("optimizer_plan_summary") == 0
    with db.connect_db() as c:
        c.execute("INSERT INTO optimizer_day_eval VALUES (?,?)", (local_day, '{"status":"ok"}'))
    second = retention.run_retention_batch(now=NOW)
    assert _count("optimizer_plan") == 1
    assert second["removed"]["optimizer_plan"] == 1


def test_fk_vintages_and_model_artifacts_survive(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with db.connect_db() as c:
        c.execute("INSERT INTO engine_information_vintage VALUES (?,?)", ("old", _stamp(45)))
        c.execute("INSERT INTO engine_information_vintage VALUES (?,?)", ("still_used", _stamp(45)))
        c.execute("INSERT INTO engine_decision VALUES (?,?)", ("still_used", _stamp(2)))
        c.execute("INSERT INTO load_forecast_15m VALUES (?)", (_stamp(40),))
    one = retention.run_retention_batch(now=NOW)
    assert one["removed"]["engine_information_vintage"] == 1
    assert _count("engine_information_vintage") == 1
    assert _count("engine_decision") == 1
    assert _count("load_forecast_15m") == 0
    assert _count("protected_training") == 1
    assert one["automatic_vacuum"] is False
    with db.connect_db() as c:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cursor_progresses_beyond_protected_old_row(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with db.connect_db() as c:
        c.execute("INSERT INTO raw_state(collected_at,payload_json) VALUES (?,?)", (_stamp(50), "{}"))
        eligible = _stamp(45)
        c.execute("INSERT INTO raw_state(collected_at,payload_json) VALUES (?,?)", (eligible, "{}"))
        bucket = datetime.fromisoformat(eligible).replace(minute=0, second=0, microsecond=0).isoformat()
        c.execute("INSERT INTO state_15m(bucket_start,collected_at,payload_json) VALUES (?,?,?)",
                  (bucket, eligible, "{}"))
    one = retention.run_retention_batch(batch_size=1, now=NOW)
    two = retention.run_retention_batch(batch_size=1, now=NOW)
    assert one["protected"]["raw_state"] == 1
    assert two["removed"]["raw_state"] == 1
    assert _count("raw_state") == 1
    assert retention.read_retention_status()["last_run"] == NOW.isoformat()


def test_no_large_live_write_path_deletions():
    root = Path(__file__).resolve().parents[1] / "app"
    for module in ("optimizer_store.py", "engine_store.py", "db.py", "load_evaluation.py"):
        source = (root / module).read_text(encoding="utf-8")
        # Shared maintenance owns both optimizer and forecast retention.
        assert 'DELETE FROM optimizer_plan WHERE generated_at' not in source
        assert 'DELETE FROM engine_decision WHERE generated_at' not in source
        assert 'DELETE FROM pv_forecast_15m WHERE generated_at' not in source
        assert 'DELETE FROM load_forecast_15m WHERE generated_at' not in source
    maintenance = (root / "runtime_maintenance.py").read_text(encoding="utf-8")
    assert "_retention_loop()" in maintenance
    assert 'lane=LANE_HEAVY' in maintenance
    assert 'timeout_seconds=180' in maintenance
