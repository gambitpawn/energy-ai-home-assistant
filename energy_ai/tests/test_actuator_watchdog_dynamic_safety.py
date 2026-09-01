from __future__ import annotations

import asyncio

import pytest

from app import actuator_watchdog as wd


class FakeAdapter:
    def __init__(self, *, target=7.5225, mode="EMS BattCtrl", dispatch_error=None, readbacks=None):
        self.target = target
        self.mode = mode
        self.dispatch_error = dispatch_error
        self.dispatched = []
        self.readbacks = list(readbacks or [])

    async def readback(self):
        if self.readbacks:
            item = self.readbacks.pop(0)
            if isinstance(item, Exception):
                raise item
            self.mode = item.get("working_mode", self.mode)
            self.target = item.get("battery_power_target_kw", self.target)
            return dict(item)
        return {"working_mode": self.mode, "battery_power_target_kw": self.target}

    async def dispatch(self, target):
        if self.dispatch_error:
            raise self.dispatch_error
        self.dispatched.append(float(target))
        self.target = float(target)
        return {"working_mode": self.mode, "battery_power_target_kw": float(target), "acknowledged": True}


class FakeActuator:
    def __init__(self, adapter):
        self.adapter = adapter
        self.cfg = {"actuator": {
            "control_working_mode": "EMS BattCtrl",
            "ack_tolerance_kw": 0.1,
            "zero_deadband_kw": 0.05,
            "state_max_age_seconds": 180.0,
        }}
        self.failures = []

    async def fail_safe(self, reason, payload=None):
        self.failures.append((reason, payload))
        return {"status": "fail_safe", "reason": reason}


def _last_command(action=7.5225):
    return {
        "command_id": 158,
        "source": "live_soc_replan_safety_override",
        "source_id": "vintage",
        "engine_id": "deterministic_v36_live",
        "decision_start": "2026-08-29T19:15:00+00:00",
        "valid_until": "2026-08-29T19:30:00+00:00",
        "safe_action_kw": action,
    }


def _actual(*, age=8.4):
    return {
        "age_seconds": age,
        "soc_pct": 15.7,
        "load_kw": 2.617,
        "pv_kw": 0.0,
        "grid_kw": 4.742,
        "battery_kw": 7.463,
    }


def _install_common(monkeypatch, *, action=7.5225):
    monkeypatch.setattr(wd.da, "production_status", lambda: {
        "operating_mode": "active", "physical_writes_enabled": True, "actuator_ready": True
    })
    monkeypatch.setattr(wd, "effective_actuator_config_report", lambda cfg: {"restart_required": False})
    monkeypatch.setattr(wd.da, "_last_effective_command", lambda: _last_command(action))
    monkeypatch.setattr(wd.da, "_candidate_valid", lambda candidate, cfg: (True, "ok"))
    monkeypatch.setattr(wd.da, "_latest_actual", lambda: _actual())
    monkeypatch.setattr(wd.da, "_event", lambda *args, **kwargs: None)


def test_watchdog_corrects_material_safety_envelope_violation_without_pausing(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(wd.da, "safety_filter", lambda candidate, cfg, actual: {
        "requested_action_kw": 7.5225,
        "safe_action_kw": 7.2246,
        "clamped": True,
        "reasons": ["safety_clamped"],
        "safe_interval_kw": {"min": -8.0, "max": 7.2246},
        "actual": actual,
    })
    inserted = []
    monkeypatch.setattr(wd.da, "_insert_command", lambda candidate, **kwargs: inserted.append((candidate, kwargs)) or 159)

    actuator = FakeActuator(FakeAdapter())
    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "healthy_corrected"
    assert actuator.adapter.dispatched == [pytest.approx(7.2246)]
    assert actuator.failures == []
    assert inserted[0][1]["reason"] == "watchdog_dynamic_safety_correction"


def test_watchdog_does_not_chatter_for_sub_resolution_envelope_movement(monkeypatch):
    _install_common(monkeypatch, action=-0.4126)
    monkeypatch.setattr(wd.da, "safety_filter", lambda candidate, cfg, actual: {
        "requested_action_kw": -0.4126,
        "safe_action_kw": -0.3301,
        "clamped": True,
        "reasons": ["safety_clamped"],
        "safe_interval_kw": {"min": -0.3301, "max": 8.0},
        "actual": actual,
    })

    actuator = FakeActuator(FakeAdapter(target=-0.4126))
    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "healthy_within_safety_tolerance"
    assert result["envelope_violation_kw"] == pytest.approx(0.0825)
    assert actuator.adapter.dispatched == []
    assert actuator.failures == []


def test_watchdog_confirms_readback_before_declaring_target_drift(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(wd.da, "safety_filter", lambda candidate, cfg, actual: {
        "safe_action_kw": 7.5225,
        "safe_interval_kw": {"min": -8.0, "max": 8.0},
    })
    adapter = FakeAdapter(readbacks=[
        {"working_mode": "EMS BattCtrl", "battery_power_target_kw": 0.0},
        {"working_mode": "EMS BattCtrl", "battery_power_target_kw": 7.52},
    ])
    actuator = FakeActuator(adapter)

    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "healthy"
    assert actuator.failures == []


def test_watchdog_keeps_fail_safe_for_persistent_mode_drift(monkeypatch):
    _install_common(monkeypatch)
    actuator = FakeActuator(FakeAdapter(mode="ToU"))

    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "fail_safe"
    assert actuator.failures[0][0] == "watchdog_working_mode_drift"


def test_watchdog_keeps_active_when_telemetry_is_stale_and_verified_target_is_zero(monkeypatch):
    _install_common(monkeypatch, action=0.0)
    monkeypatch.setattr(wd.da, "_latest_actual", lambda: _actual(age=304.8))

    actuator = FakeActuator(FakeAdapter(target=0.0))
    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "healthy_waiting_safe_zero"
    assert result["reason"] == "stale_actual_state_zero_target_held"
    assert actuator.failures == []


def test_watchdog_fail_safes_on_stale_telemetry_with_nonzero_target(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(wd.da, "_latest_actual", lambda: _actual(age=304.8))

    actuator = FakeActuator(FakeAdapter(target=7.5225))
    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "fail_safe"
    assert actuator.failures[0][0] == "watchdog_stale_actual_state_nonzero_target"


def test_expired_verified_zero_command_waits_for_fresh_candidate(monkeypatch):
    _install_common(monkeypatch, action=0.0)
    monkeypatch.setattr(wd.da, "_candidate_valid", lambda candidate, cfg: (False, "candidate_expired"))

    actuator = FakeActuator(FakeAdapter(target=0.0))
    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "healthy_waiting_safe_zero"
    assert result["reason"] == "expired_candidate_zero_target_held"
    assert actuator.failures == []


def test_expired_nonzero_command_is_a_fail_safe_condition(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(wd.da, "_candidate_valid", lambda candidate, cfg: (False, "candidate_expired"))

    actuator = FakeActuator(FakeAdapter(target=7.5225))
    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "fail_safe"
    assert actuator.failures[0][0] == "watchdog_candidate_expired"


def test_watchdog_fail_safes_if_material_safety_correction_cannot_be_dispatched(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(wd.da, "safety_filter", lambda candidate, cfg, actual: {
        "requested_action_kw": 7.5225,
        "safe_action_kw": 7.2246,
        "safe_interval_kw": {"min": -8.0, "max": 7.2246},
    })
    actuator = FakeActuator(FakeAdapter(dispatch_error=RuntimeError("ack failed")))

    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "fail_safe"
    assert actuator.failures[0][0] == "watchdog_safety_correction_dispatch_failed"


def test_unexpected_watchdog_exception_cannot_escape_active_control(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(wd.da, "safety_filter", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bug")))
    actuator = FakeActuator(FakeAdapter())

    result = asyncio.run(wd.watchdog_tick(actuator))

    assert result["status"] == "fail_safe"
    assert actuator.failures[0][0] == "watchdog_safety_filter_failed"
