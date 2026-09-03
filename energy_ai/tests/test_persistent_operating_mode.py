from __future__ import annotations

from pathlib import Path

import app.persistent_operating_mode as pom
import app.production_state as production_state

ROOT = Path(__file__).resolve().parents[1]


def _reset_state(tmp_path, monkeypatch):
    db = tmp_path / "energy_ai.db"
    monkeypatch.setattr(pom, "DB_PATH", db)
    monkeypatch.setattr(production_state, "DB_PATH", db)
    production_state._CACHE = None
    production_state._INITIALIZED_PATH = None
    production_state._LAST_PERSISTENCE_ERROR = None
    return db


def test_persisted_active_intent_survives_clean_runtime_shadow_staging(tmp_path, monkeypatch):
    _reset_state(tmp_path, monkeypatch)
    production_state.status()
    production_state.mark_actuator_ready(True, detail="test_ready")
    production_state.set_mode("active", reason="test_active")

    first = pom.prepare_startup()
    assert first["legacy_migration"] is True
    assert first["desired_mode"] == "active"
    assert first["previous_shutdown_clean"] is True

    pom.mark_clean_shutdown()
    # This reproduces runtime.py's safe physical cleanup. Persistent operator
    # intent must not be derived from this transient Shadow state after migration.
    production_state.set_mode("shadow", reason="clean_shutdown")
    production_state.mark_actuator_ready(False, detail="clean_shutdown")

    second = pom.prepare_startup()
    assert second["previous_shutdown_clean"] is True
    assert second["desired_mode"] == "active"


def test_unclean_restart_forces_persistent_paused_and_records_fault(tmp_path, monkeypatch):
    _reset_state(tmp_path, monkeypatch)
    pom.prepare_startup()
    pom.set_desired_mode("active", reason="test")
    # No mark_clean_shutdown(): the next process must interpret this as a crash.
    restarted = pom.prepare_startup()
    assert restarted["previous_shutdown_clean"] is False
    assert restarted["desired_mode"] == "paused"
    assert restarted["startup_fault"]["reason"] == "unclean_previous_shutdown"
    assert pom.lifecycle_status()["desired_mode"] == "paused"
    assert pom.pending_fault() is not None


def test_actuator_fault_records_paused_intent_and_pending_notification(tmp_path, monkeypatch):
    _reset_state(tmp_path, monkeypatch)
    pom.prepare_startup()
    pom.mark_clean_shutdown()
    pom.set_desired_mode("active", reason="test")
    fault = pom.record_fault("watchdog_target_drift", {"target": 1.0})
    state = pom.lifecycle_status()
    assert state["desired_mode"] == "paused"
    assert state["last_fault_reason"] == "watchdog_target_drift"
    assert pom.pending_fault()["sequence"] == fault["sequence"]


def test_runtime_captures_intent_before_base_runtime_disarms():
    source = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    assert source.index("_STARTUP_MODE_STATE = prepare_startup()") < source.index("from . import runtime as base")


def test_fault_notification_is_nonblocking_for_actuator_safety_path():
    source = (ROOT / "app" / "persistent_operating_mode.py").read_text(encoding="utf-8")
    assert "result = await original_fail_safe(reason, payload)" in source
    assert "_schedule_notification(ha)" in source
    assert "loop.create_task(send_pending_fault_notification" in source
    assert "smtplib" not in source


def test_startup_restore_is_serialized_with_manual_mode_transitions():
    source = (ROOT / "app" / "persistent_operating_mode.py").read_text(encoding="utf-8")
    assert "transition_lock = asyncio.Lock()" in source
    assert 'lock_route("/control/operator-mode/active")' in source
    assert 'lock_route("/control/operator-mode/shadow")' in source
    assert "fail-safe never waits for it" in source


def test_notification_parameters_are_added_without_release_bump():
    source = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    assert 'RELEASE_BUILD = "1.0.124"' in source
    assert '"fault_notification_enabled"' in source
    assert '"fault_notification_service"' in source
    assert '"fault_notification_target"' in source
