from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app import runtime_entry_v188 as rt


def test_active_preflight_uses_real_actuator_instance_and_returns_controlled_409(monkeypatch):
    calls: list[str] = []

    async def fake_preflight():
        return {"ok": False, "error": "forced_preflight_failure"}

    async def fake_previous(mode: str):
        calls.append(mode)
        return {"unexpected": True}

    # Regression for v1.0.90 production failure: runtime_entry_v187_final does
    # not expose ACTUATOR. The v1.0.88 wrapper must use runtime_entry_v187's
    # actual actuator instance instead of dereferencing the final wrapper module.
    assert rt.actuator_runtime.ACTUATOR is rt.v187.v187.ACTUATOR
    assert not hasattr(rt.v187, "ACTUATOR")

    monkeypatch.setattr(rt.actuator_runtime.ACTUATOR, "preflight", fake_preflight)
    monkeypatch.setattr(rt, "_previous_control_endpoint", fake_previous)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.production_control_mode_v188("active"))

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "actuator_preflight_required_before_active"
    assert exc.value.detail["preflight"]["error"] == "forced_preflight_failure"
    assert calls == []


def test_non_active_mode_delegates_to_hardened_v187_transition(monkeypatch):
    calls: list[str] = []

    async def fake_previous(mode: str):
        calls.append(mode)
        return {"requested_mode": mode}

    monkeypatch.setattr(rt, "_previous_control_endpoint", fake_previous)
    result = asyncio.run(rt.production_control_mode_v188("shadow"))

    assert result == {"requested_mode": "shadow"}
    assert calls == ["shadow"]
