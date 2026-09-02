from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app import db, production_state
from app.actuator_audit import ActuatorAuditBacklog
from app.actuator_control_lease import ActuatorControlLease


def _candidate(action: float = 1.25):
    now = datetime.now(timezone.utc)
    return {
        "source": "test",
        "source_id": "vintage",
        "engine_id": "deterministic_refined_v1",
        "decision_start": now.isoformat(),
        "valid_until": (now + timedelta(minutes=15)).isoformat(),
        "requested_action_kw": action,
    }


def test_control_lease_is_process_local_and_never_restored():
    lease = ActuatorControlLease()
    assert lease.current_command() is None
    assert lease.snapshot()["restored_from_database"] is False

    lease.acknowledge(
        _candidate(),
        target_kw=1.25,
        reason="acknowledged",
        readback={"working_mode": "EMS BattCtrl", "battery_power_target_kw": 1.25},
    )
    assert lease.current_command()["safe_action_kw"] == 1.25

    restarted = ActuatorControlLease()
    assert restarted.current_command() is None
    assert restarted.snapshot()["state"] == "unarmed"


def test_audit_backlog_retries_without_duplicates():
    backlog = ActuatorAuditBacklog(max_items=16)
    backlog.enqueue("event", {"audit_key": "one"})
    calls = []

    def locked(_item):
        raise sqlite3.OperationalError("database is locked")

    first = backlog.flush(lambda item: None, locked)
    assert first["pending"] == 1
    assert "locked" in first["last_error"]

    second = backlog.flush(lambda item: None, lambda item: calls.append(item["audit_key"]))
    assert second["pending"] == 0
    assert calls == ["one"]


def test_audit_flush_does_not_drop_next_record_during_concurrent_enqueue():
    backlog = ActuatorAuditBacklog(max_items=16)
    for index in range(16):
        backlog.enqueue("event", {"audit_key": str(index)})

    def write_first(_item):
        backlog.enqueue("event", {"audit_key": "new"})

    first = backlog.flush(lambda item: None, write_first, limit=1)
    assert first["pending"] == 16

    written = []
    backlog.flush(lambda item: None, lambda item: written.append(item["audit_key"]), limit=16)
    assert written == [str(index) for index in range(1, 16)] + ["new"]


def test_sqlite_is_configured_for_wal(tmp_path, monkeypatch):
    path = tmp_path / "energy.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    with sqlite3.connect(path) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_production_status_reads_memory_after_initialization(tmp_path, monkeypatch):
    path = tmp_path / "production.db"
    monkeypatch.setattr(production_state, "DB_PATH", path)
    monkeypatch.setattr(production_state, "_INITIALIZED_PATH", None)
    monkeypatch.setattr(production_state, "_CACHE", None)
    production_state._init()
    before = production_state.status()

    original_connect = production_state.sqlite3.connect
    monkeypatch.setattr(
        production_state.sqlite3,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    try:
        after = production_state.status()
    finally:
        monkeypatch.setattr(production_state.sqlite3, "connect", original_connect)

    assert after["operating_mode"] == before["operating_mode"]
    assert after["control_state_source"] == "process_memory"
