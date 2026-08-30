from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import actuator_watchdog_dynamic_safety as wd


class FakeAdapter:
    def __init__(self, *, target=7.5225, mode="EMS BattCtrl", dispatch_error=None):
        self.target = target
        self.mode = mode
        self.dispatch_error = dispatch_error
        self.dispatched = []

    async def readback(self):
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
        self.cfg = {"actuator": {"control_working_mode": "EMS BattCtrl", "ack_tolerance_kw": 0.1}}
        self.failures = []

    async def fail_safe(self, reason, payload=None):
        self.failures.append((reason, payload))
        return {"status": "fail_safe", "reason": reason}


def _last_command():
    return {
        "command_id": 158,
        "source": "live_soc_replan_safety_override",
        "source_id": "vintage",
        "engine_id": "deterministic_v36_live",
        "decision_start": "2026-08-29T19:15:00+00:00",
        "valid_until": "2026-08-29T19:30:00+00:00",
        "safe_action_kw": 7.5225,
    }


def _install_common(monkeypatch):
    monkeypatch.setattr(wd.da, "production_status", lambda: {
        "operating_mode": "active", "physical_writes_enabled": True, "actuator_ready": True
    })
    monkeypatch.setattr(wd, "effective_actuator_config_report", lambda cfg: {"restart_required": False})
    monkeypatch.setattr(wd.da, "_last_effective_command", _last_command)
    monkeypatch.setattr(wd.da, "_candidate_valid", lambda candidate, cfg: (True, "ok"))
    monkeypatch.setattr(wd.da, "_latest_actual", lambda: {
        "age_seconds": 8.4, "soc_pct": 15.7, "load_kw": 2.617, "pv_kw": 0.0,
        "grid_kw": 4.742, "battery_kw": 7.463,
    })


@pytest.mark.asyncio
async def test_watchdog_clamps_shrinking_soc_envelope_without_pausing(monkeypatch):
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
    events = []
    monkeypatch.setattr(wd.da, "_insert_command", lambda candidate, **kwargs: inserted.append((candidate, kwargs)) or 159)
    monkeypatch.setattr(wd.da, "_event", lambda *args: events.append(args))

    actuator = FakeActuator(FakeAdapter())
    result = await wd.watchdog_tick_with_dynamic_safety_correction(actuator)

    assert result["status"] == "healthy_corrected"
    assert result["old_target_kw"] == pytest.approx(7.5225)
    assert result["new_target_kw"] == pytest.approx(7.2246)
    assert actuator.adapter.dispatched == [pytest.approx(7.2246)]
    assert actuator.failures == []
    assert inserted[0][1]["reason"] == "watchdog_dynamic_safety_correction"
    assert events[0][0] == "actuator_watchdog_safety_correction"


@pytest.mark.asyncio
async def test_watchdog_can_correct_charge_all_the_way_toward_zero(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(wd.da, "_last_effective_command", lambda: {**_last_command(), "safe_action_kw": -0.4126})
    monkeypatch.setattr(wd.da, "safety_filter", lambda candidate, cfg, actual: {
        "requested_action_kw": -0.4126,
        "safe_action_kw": -0.3301,
        "clamped": True,
        "reasons": ["safety_clamped"],
        "safe_interval_kw": {"min": -0.3301, "max": 8.0},
        "actual": actual,
    })
    monkeypatch.setattr(wd.da, "_insert_command", lambda *args, **kwargs: 160)
    monkeypatch.setattr(wd.da, "_event", lambda *args, **kwargs: None)

    actuator = FakeActuator(FakeAdapter(target=-0.4126))
    result = await wd.watchdog_tick_with_dynamic_safety_correction(actuator)

    assert result["status"] == "healthy_corrected"
    assert actuator.adapter.dispatched == [pytest.approx(-0.3301)]
    assert actuator.failures == []


@pytest.mark.asyncio
async def test_watchdog_keeps_fail_safe_for_mode_drift(monkeypatch):
    _install_common(monkeypatch)
    actuator = FakeActuator(FakeAdapter(mode="ToU"))

    result = await wd.watchdog_tick_with_dynamic_safety_correction(actuator)

    assert result["status"] == "fail_safe"
    assert actuator.failures[0][0] == "watchdog_working_mode_drift"
    assert actuator.adapter.dispatched == []


@pytest.mark.asyncio
async def test_watchdog_fail_safes_if_safety_correction_cannot_be_dispatched(monkeypatch):
    _install_common(monkeypatch)
    monkeypatch.setattr(wd.da, "safety_filter", lambda candidate, cfg, actual: {
        "requested_action_kw": 7.5225,
        "safe_action_kw": 7.2246,
        "clamped": True,
        "reasons": ["safety_clamped"],
        "safe_interval_kw": {"min": -8.0, "max": 7.2246},
        "actual": actual,
    })
    actuator = FakeActuator(FakeAdapter(dispatch_error=RuntimeError("ack failed")))

    result = await wd.watchdog_tick_with_dynamic_safety_correction(actuator)

    assert result["status"] == "fail_safe"
    assert actuator.failures[0][0] == "watchdog_safety_correction_dispatch_failed"
