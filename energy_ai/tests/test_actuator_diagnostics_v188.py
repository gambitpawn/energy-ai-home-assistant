from __future__ import annotations

from app import actuator_config as ac


def _cfg(*, safe_mode: str = "General"):
    return {
        "entities": {
            "solinteg_working_mode": None,
            "solinteg_battery_power_target": None,
        },
        "actuator": {
            "control_working_mode": "EMS BattCtrl",
            "safe_working_mode": safe_mode,
            "ack_timeout_seconds": 8.0,
            "ack_tolerance_kw": 0.10,
            "state_max_age_seconds": 180.0,
            "candidate_grace_seconds": 120.0,
            "soc_guard_margin_pct": 1.0,
            "zero_deadband_kw": 0.05,
            "min_action_change_kw": 0.10,
            "watchdog_poll_seconds": 30.0,
            "config_loaded_at": "2026-08-28T08:00:00+00:00",
        },
        "optimizer": {
            "battery_max_charge_kw": 8.0,
            "battery_max_discharge_kw": 8.0,
            "physical_grid_import_limit_kw": 13.8,
            "grid_export_limit_kw": 10.0,
        },
        "policy": {
            "battery": {
                "hard_min_soc_pct": 5.0,
                "hard_max_soc_pct": 100.0,
            }
        },
    }


def _ha_defaults():
    return {
        "actuator_control_working_mode": "EMS BattCtrl",
        "actuator_safe_working_mode": "General",
        "actuator_ack_timeout_seconds": 8.0,
        "actuator_ack_tolerance_kw": 0.10,
        "actuator_state_max_age_seconds": 180,
        "actuator_candidate_grace_seconds": 120,
        "actuator_soc_guard_margin_pct": 1.0,
        "actuator_zero_deadband_kw": 0.05,
        "actuator_min_action_change_kw": 0.10,
        "actuator_watchdog_poll_seconds": 30,
        "entity_solinteg_working_mode": "",
        "entity_solinteg_battery_power_target": "",
    }


def test_db_override_not_loaded_requires_restart(monkeypatch):
    raw = _ha_defaults()
    monkeypatch.setattr(ac, "_option_layers", lambda: (raw, {"actuator_safe_working_mode": "ToU"}))
    report = ac.effective_actuator_config_report(_cfg(safe_mode="General"))
    assert report["restart_required"] is True
    assert report["runtime_matches_persisted"] is False
    assert report["sources"]["safe_working_mode"] == "db_override"
    assert report["persisted"]["safe_working_mode"] == "ToU"
    assert report["runtime"]["safe_working_mode"] == "General"


def test_loaded_db_override_is_fresh(monkeypatch):
    raw = _ha_defaults()
    monkeypatch.setattr(ac, "_option_layers", lambda: (raw, {"actuator_safe_working_mode": "ToU"}))
    report = ac.effective_actuator_config_report(_cfg(safe_mode="ToU"))
    assert report["restart_required"] is False
    assert report["runtime_matches_persisted"] is True
    assert report["hard_limits"]["physical_grid_import_limit_kw"] == 13.8


def test_preflight_blocks_safe_mode_different_from_current_normal_mode(monkeypatch):
    raw = _ha_defaults()
    monkeypatch.setattr(ac, "_option_layers", lambda: (raw, {}))
    report = ac.effective_actuator_config_report(_cfg(safe_mode="General"))
    gate = ac.actuator_preflight_config_gate(report, "ToU")
    assert gate["ok"] is False
    assert gate["error"] == "safe_release_mode_mismatch_current_working_mode"


def test_preflight_allows_matching_tou_safe_mode(monkeypatch):
    raw = _ha_defaults()
    monkeypatch.setattr(ac, "_option_layers", lambda: (raw, {"actuator_safe_working_mode": "ToU"}))
    report = ac.effective_actuator_config_report(_cfg(safe_mode="ToU"))
    gate = ac.actuator_preflight_config_gate(report, "ToU")
    assert gate["ok"] is True
    assert gate["error"] is None


def test_preflight_blocks_stale_runtime_before_mode_semantics(monkeypatch):
    raw = _ha_defaults()
    monkeypatch.setattr(ac, "_option_layers", lambda: (raw, {"actuator_safe_working_mode": "ToU"}))
    report = ac.effective_actuator_config_report(_cfg(safe_mode="General"))
    gate = ac.actuator_preflight_config_gate(report, "General")
    assert gate["ok"] is False
    assert gate["error"] == "actuator_runtime_config_stale_restart_required"
