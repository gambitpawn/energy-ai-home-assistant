from __future__ import annotations

import asyncio

from app import actuator_arm_control_mode as arm_fix
from app.actuator_control_lease import ActuatorControlLease


class FakeAdapter:
    def __init__(self, entered):
        self.entered = dict(entered)
        self.safe_release_calls = 0

    async def enter_control_mode_zero(self):
        return dict(self.entered)

    async def safe_release(self):
        self.safe_release_calls += 1
        return {"released": True, "working_mode": "ToU", "battery_power_target_kw": 0.0}


class FakeActuator:
    def __init__(self, entered):
        self.cfg = {
            "actuator": {
                "control_working_mode": "EMS BattCtrl",
                "ack_tolerance_kw": 0.10,
            }
        }
        self.adapter = FakeAdapter(entered)
        self.control_lease = ActuatorControlLease()

    async def preflight(self):
        return {"ok": True}


def test_successful_arm_holds_ems_control_mode_and_records_zero_as_effective_command(monkeypatch):
    readiness = []
    events = []
    commands = []
    monkeypatch.setattr(arm_fix.da, "mark_actuator_ready", lambda ready, detail="": readiness.append((ready, detail)))
    monkeypatch.setattr(arm_fix.da, "_event", lambda event_type, reason, payload=None: events.append((event_type, reason, payload)))
    monkeypatch.setattr(
        arm_fix.da,
        "_insert_command",
        lambda candidate, **kwargs: commands.append((candidate, kwargs)) or 501,
    )
    monkeypatch.setattr(
        arm_fix.da,
        "production_status",
        lambda: {"operating_mode": "shadow", "physical_writes_enabled": False, "actuator_ready": True},
    )
    actuator = FakeActuator(
        {
            "acknowledged": True,
            "working_mode": "EMS BattCtrl",
            "battery_power_target_kw": 0.0,
        }
    )

    result = asyncio.run(arm_fix.zero_handshake_and_arm_control_mode_held(actuator))

    assert result["ok"] is True
    assert result["control_mode_held"] is True
    assert result["control_mode_test"]["working_mode"] == "EMS BattCtrl"
    assert result["handshake_command_id"] == 501
    assert actuator.control_lease.current_command()["safe_action_kw"] == 0.0
    assert actuator.adapter.safe_release_calls == 0
    assert readiness[-1][0] is True
    assert len(commands) == 1
    candidate, kwargs = commands[0]
    assert candidate["source"] == "actuator_arm_zero_handshake"
    assert candidate["requested_action_kw"] == 0.0
    assert kwargs["safe_action_kw"] == 0.0
    assert kwargs["physical_write"] is True
    assert kwargs["status"] == "acknowledged"
    assert events[-1][0] == "actuator_armed"
    assert events[-1][2]["control_mode_held"] is True
    assert events[-1][2]["handshake_command_id"] == 501


def test_failed_arm_safe_releases_and_never_marks_ready(monkeypatch):
    readiness = []
    events = []
    commands = []
    monkeypatch.setattr(arm_fix.da, "mark_actuator_ready", lambda ready, detail="": readiness.append((ready, detail)))
    monkeypatch.setattr(arm_fix.da, "_event", lambda event_type, reason, payload=None: events.append((event_type, reason, payload)))
    monkeypatch.setattr(
        arm_fix.da,
        "_insert_command",
        lambda candidate, **kwargs: commands.append((candidate, kwargs)) or 502,
    )
    actuator = FakeActuator(
        {
            "acknowledged": True,
            "working_mode": "ToU",
            "battery_power_target_kw": 0.0,
        }
    )

    result = asyncio.run(arm_fix.zero_handshake_and_arm_control_mode_held(actuator))

    assert result["ok"] is False
    assert result["stage"] == "zero_handshake"
    assert actuator.adapter.safe_release_calls == 1
    assert readiness[-1][0] is False
    assert commands == []
    assert events[-1][0] == "actuator_arm_failed"


def test_runtime_installs_arm_patch_before_operator_activation():
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    assert "install_arm_control_mode_patch()" in source
    assert source.index("install_arm_control_mode_patch()") < source.index("install_operator_mode_control(")
