from __future__ import annotations

import re
from pathlib import Path

import app.persistent_operating_mode as pom
import app.production_state as production_state
from app.release_version import RELEASE_VERSION

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
    production_state.set_mode("shadow", reason="clean_shutdown")
    production_state.mark_actuator_ready(False, detail="clean_shutdown")

    second = pom.prepare_startup()
    assert second["previous_shutdown_clean"] is True
    assert second["desired_mode"] == "active"


def test_persisted_paused_intent_survives_clean_restart(tmp_path, monkeypatch):
    _reset_state(tmp_path, monkeypatch)
    pom.prepare_startup()
    pom.set_desired_mode("paused", reason="test_paused")
    pom.mark_clean_shutdown()
    restarted = pom.prepare_startup()
    assert restarted["previous_shutdown_clean"] is True
    assert restarted["desired_mode"] == "paused"
    assert restarted["startup_fault"] is None


def test_persisted_shadow_intent_survives_clean_restart(tmp_path, monkeypatch):
    _reset_state(tmp_path, monkeypatch)
    pom.prepare_startup()
    pom.set_desired_mode("shadow", reason="test_shadow")
    pom.mark_clean_shutdown()
    restarted = pom.prepare_startup()
    assert restarted["previous_shutdown_clean"] is True
    assert restarted["desired_mode"] == "shadow"
    assert restarted["startup_fault"] is None


def test_unclean_restart_forces_persistent_paused_and_records_fault(tmp_path, monkeypatch):
    _reset_state(tmp_path, monkeypatch)
    pom.prepare_startup()
    pom.set_desired_mode("active", reason="test")
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
    assert "await asyncio.to_thread(record_fault" in source
    assert "_schedule_notification(ha)" in source
    assert "loop.create_task(send_pending_fault_notification" in source
    assert "smtplib" not in source


def test_startup_restore_is_serialized_with_manual_mode_transitions_but_not_fail_safe():
    source = (ROOT / "app" / "persistent_operating_mode.py").read_text(encoding="utf-8")
    assert "transition_lock = asyncio.Lock()" in source
    assert 'lock_route("/control/operator-mode/active", "active")' in source
    assert 'lock_route("/control/operator-mode/shadow", "shadow")' in source
    assert "fail-safe path intentionally does not take this lock" in source


def test_manual_mode_intent_is_persisted_only_after_successful_transition():
    source = (ROOT / "app" / "persistent_operating_mode.py").read_text(encoding="utf-8")
    original_pos = source.index("result = await original()")
    persist_pos = source.index("set_desired_mode,", original_pos)
    assert original_pos < persist_pos
    assert 'desired_on_success == "active"' in source
    assert 'prod.get("operating_mode") == "shadow"' in source


def test_notification_parameters_and_release_metadata_remain_consistent():
    source = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    config = (ROOT / "config.yaml").read_text(encoding="utf-8")
    config_match = re.search(r'^version: "([^"]+)"', config, re.MULTILINE)
    assert config_match is not None
    assert RELEASE_VERSION == config_match.group(1)
    assert "from .release_version import RELEASE_VERSION" in source
    assert "RELEASE_BUILD = RELEASE_VERSION" in source
    assert 'base.core.cfg["runtime_build"] = RELEASE_BUILD' in source
    assert 'RELEASE_BUILD = "' not in source
    assert '"fault_notification_enabled"' in source
    assert '"fault_notification_service"' in source
    assert '"fault_notification_target"' in source


def test_existing_two_thread_cpu_cap_is_untouched():
    run_sh = (ROOT / "run.sh").read_text(encoding="utf-8")
    for key in ("LOKY_MAX_CPU_COUNT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        assert f"export {key}=2" in run_sh
