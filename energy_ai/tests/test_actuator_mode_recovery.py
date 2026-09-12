from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app import actuator_mode_recovery as recovery


class FakeAdapter:
    def __init__(self, *, recovered_mode="EMS BattCtrl", recovered_target=0.0, error=None):
        self.recovered_mode = recovered_mode
        self.recovered_target = recovered_target
        self.error = error
        self.enter_calls = 0

    async def enter_control_mode_zero(self):
        self.enter_calls += 1
        if self.error:
            raise self.error
        return {
            "working_mode": self.recovered_mode,
            "battery_power_target_kw": self.recovered_target,
            "acknowledged": True,
        }


class FakeActuator:
    def __init__(self, adapter):
        self.adapter = adapter
        self.cfg = {
            "actuator": {
                "control_working_mode": "EMS BattCtrl",
                "ack_tolerance_kw": 0.1,
                "zero_deadband_kw": 0.05,
            }
        }


def _payload(*, expected=0.0, actual=0.0):
    return {
        "last_command": {"safe_action_kw": expected, "source": "selector_quarter_control"},
        "readback": {"working_mode": "General", "battery_power_target_kw": actual},
    }


def _arm_session(actuator, *, age_seconds=60.0, attempts=0):
    state = recovery._session_state(actuator)
    state["armed_at"] = (recovery._now() - timedelta(seconds=age_seconds)).isoformat()
    state["recovery_attempts"] = attempts
    return state


def _install_no_io(monkeypatch):
    failures = []

    async def fail_safe(self, reason, payload=None):
        failures.append((reason, payload))
        return {"status": "fail_safe", "reason": reason}

    monkeypatch.setattr(recovery, "_ORIGINAL_FAIL_SAFE", fail_safe)
    monkeypatch.setattr(recovery.da, "_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(recovery.da, "production_status", lambda: {"operating_mode": "active"})
    monkeypatch.setattr(recovery.wd, "_record", lambda *args, **kwargs: None)
    return failures


def test_recovers_one_early_zero_power_mode_drift(monkeypatch):
    failures = _install_no_io(monkeypatch)
    actuator = FakeActuator(FakeAdapter())
    state = _arm_session(actuator, age_seconds=600.0)

    result = asyncio.run(
        recovery.recover_startup_mode_drift_or_fail(
            actuator,
            "watchdog_working_mode_drift",
            _payload(),
        )
    )

    assert result["status"] == "healthy_corrected"
    assert result["reason"] == "startup_zero_mode_drift_recovered"
    assert actuator.adapter.enter_calls == 1
    assert state["recovery_attempts"] == 1
    assert failures == []


def test_second_mode_drift_after_recovery_fails_safe(monkeypatch):
    failures = _install_no_io(monkeypatch)
    actuator = FakeActuator(FakeAdapter())
    _arm_session(actuator, age_seconds=120.0, attempts=1)

    result = asyncio.run(
        recovery.recover_startup_mode_drift_or_fail(
            actuator,
            "watchdog_working_mode_drift",
            _payload(),
        )
    )

    assert result["status"] == "fail_safe"
    assert actuator.adapter.enter_calls == 0
    assert failures[0][0] == "watchdog_working_mode_drift"


@pytest.mark.parametrize(
    ("expected", "actual"),
    [(1.0, 0.0), (0.0, -1.0), (2.0, 2.0)],
)
def test_nonzero_command_or_readback_never_auto_recovers(monkeypatch, expected, actual):
    failures = _install_no_io(monkeypatch)
    actuator = FakeActuator(FakeAdapter())
    _arm_session(actuator, age_seconds=120.0)

    result = asyncio.run(
        recovery.recover_startup_mode_drift_or_fail(
            actuator,
            "watchdog_working_mode_drift",
            _payload(expected=expected, actual=actual),
        )
    )

    assert result["status"] == "fail_safe"
    assert actuator.adapter.enter_calls == 0
    assert failures[0][0] == "watchdog_working_mode_drift"


def test_mode_drift_outside_startup_grace_fails_safe(monkeypatch):
    failures = _install_no_io(monkeypatch)
    actuator = FakeActuator(FakeAdapter())
    _arm_session(actuator, age_seconds=recovery._RECOVERY_GRACE_SECONDS + 1.0)

    result = asyncio.run(
        recovery.recover_startup_mode_drift_or_fail(
            actuator,
            "watchdog_working_mode_drift",
            _payload(),
        )
    )

    assert result["status"] == "fail_safe"
    assert actuator.adapter.enter_calls == 0
    assert failures[0][0] == "watchdog_working_mode_drift"


def test_failed_recovery_attempt_fails_safe_and_is_not_retried(monkeypatch):
    failures = _install_no_io(monkeypatch)
    actuator = FakeActuator(FakeAdapter(error=RuntimeError("inverter still rebooting")))
    state = _arm_session(actuator, age_seconds=300.0)

    result = asyncio.run(
        recovery.recover_startup_mode_drift_or_fail(
            actuator,
            "watchdog_working_mode_drift",
            _payload(),
        )
    )

    assert result["status"] == "fail_safe"
    assert actuator.adapter.enter_calls == 1
    assert state["recovery_attempts"] == 1
    assert failures[0][0] == "watchdog_working_mode_recovery_failed"


def test_successful_arm_opens_new_recovery_window(monkeypatch):
    async def arm(self):
        return {"ok": True, "stage": "armed"}

    monkeypatch.setattr(recovery, "_ORIGINAL_ZERO_HANDSHAKE_AND_ARM", arm)
    actuator = FakeActuator(FakeAdapter())
    old = _arm_session(actuator, age_seconds=500.0, attempts=1)

    result = asyncio.run(recovery.zero_handshake_and_arm_with_recovery_window(actuator))

    assert result["ok"] is True
    assert old["recovery_attempts"] == 0
    assert old["armed_at"] is not None
    assert recovery.recovery_status(actuator)["age_seconds"] < 2.0
