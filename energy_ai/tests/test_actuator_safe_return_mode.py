from __future__ import annotations

import asyncio

from app import actuator_safe_return_mode as sr


class FakeHA:
    authenticated = True


class FakeAdapter:
    def __init__(self, mode="EMS General", target=0.0):
        self.mode = mode
        self.target = target
        self.ha = FakeHA()
        self.cfg = {
            "actuator": {
                "control_working_mode": "EMS BattCtrl",
                "safe_working_mode": "ToU",
                "ack_tolerance_kw": 0.1,
                "zero_deadband_kw": 0.05,
            }
        }
        self.mode_writes = []
        self.target_writes = []

    async def resolve_entities(self):
        return object()

    async def readback(self, entities=None):
        return {
            "working_mode": self.mode,
            "battery_power_target_kw": self.target,
            "acknowledged": True,
        }

    async def set_power_target(self, value, entities=None):
        self.target = float(value)
        self.target_writes.append(float(value))

    async def set_working_mode(self, mode, entities=None):
        self.mode = str(mode)
        self.mode_writes.append(str(mode))

    async def wait_for_ack(self, *, expected_mode=None, expected_target_kw=None, entities=None):
        if expected_mode is not None:
            assert self.mode == expected_mode
        if expected_target_kw is not None:
            assert abs(self.target - expected_target_kw) <= 0.1
        return await self.readback(entities)


class FakeActuator:
    def __init__(self, adapter):
        self.adapter = adapter
        self.cfg = adapter.cfg


def _blocked_preflight(mode="EMS General", target=0.0):
    return {
        "ok": False,
        "error": "safe_release_mode_mismatch_current_working_mode",
        "readback": {
            "working_mode": mode,
            "battery_power_target_kw": target,
        },
        "config_gate": {
            "ok": False,
            "error": "safe_release_mode_mismatch_current_working_mode",
            "current_working_mode": mode,
            "configured_safe_working_mode": "ToU",
            "warnings": ["old warning"],
        },
        "warnings": ["old warning"],
    }


def test_preflight_accepts_zero_power_non_control_mode(monkeypatch):
    async def original(self):
        return _blocked_preflight()

    monkeypatch.setattr(sr, "_ORIGINAL_PREFLIGHT", original)
    actuator = FakeActuator(FakeAdapter())

    result = asyncio.run(sr.preflight_with_dynamic_safe_return(actuator))

    assert result["ok"] is True
    assert result["error"] is None
    assert result["config_gate"]["session_safe_return_mode"] == "EMS General"
    assert result["config_gate"]["static_safe_working_mode_ignored_for_session"] == "ToU"


def test_preflight_keeps_block_for_nonzero_target(monkeypatch):
    async def original(self):
        return _blocked_preflight(target=1.0)

    monkeypatch.setattr(sr, "_ORIGINAL_PREFLIGHT", original)
    actuator = FakeActuator(FakeAdapter(target=1.0))

    result = asyncio.run(sr.preflight_with_dynamic_safe_return(actuator))

    assert result["ok"] is False
    assert result["error"] == "safe_release_mode_mismatch_current_working_mode"


def test_arm_captures_actual_prearm_mode(monkeypatch):
    async def original(self):
        self.adapter.mode = "EMS BattCtrl"
        return {"ok": True, "stage": "armed"}

    monkeypatch.setattr(sr, "_ORIGINAL_ZERO_HANDSHAKE_AND_ARM", original)
    adapter = FakeAdapter(mode="EMS General", target=0.0)
    actuator = FakeActuator(adapter)

    result = asyncio.run(sr.arm_with_captured_return_mode(actuator))

    assert result["ok"] is True
    assert result["session_safe_return_mode"] == "EMS General"
    assert adapter._energy_ai_session_safe_return_mode == "EMS General"


def test_safe_release_restores_captured_session_mode():
    adapter = FakeAdapter(mode="EMS BattCtrl", target=-2.0)
    adapter._energy_ai_session_safe_return_mode = "EMS General"
    adapter._energy_ai_session_safe_return_source = "captured_pre_arm_readback"

    result = asyncio.run(sr.safe_release_with_session_return(adapter))

    assert result["released"] is True
    assert result["return_mode"] == "EMS General"
    assert result["return_mode_source"] == "captured_pre_arm_readback"
    assert adapter.target_writes == [0.0]
    assert adapter.mode_writes == ["EMS General"]
    assert adapter.mode == "EMS General"
    assert adapter.target == 0.0


def test_startup_release_preserves_existing_noncontrol_mode_without_session_memory():
    adapter = FakeAdapter(mode="EMS General", target=0.0)

    result = asyncio.run(sr.safe_release_with_session_return(adapter))

    assert result["released"] is True
    assert result["return_mode"] == "EMS General"
    assert result["return_mode_source"] == "current_non_control_mode_preserved"
    assert adapter.target_writes == [0.0]
    assert adapter.mode_writes == []


def test_startup_release_uses_static_fallback_only_if_still_in_control_mode():
    adapter = FakeAdapter(mode="EMS BattCtrl", target=0.0)

    result = asyncio.run(sr.safe_release_with_session_return(adapter))

    assert result["released"] is True
    assert result["return_mode"] == "ToU"
    assert result["return_mode_source"] == "configured_fallback"
    assert adapter.mode_writes == ["ToU"]
