from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import actuator_runtime_resilience as resilience


def _cfg():
    return {
        "actuator": {
            "state_max_age_seconds": 180.0,
            "candidate_grace_seconds": 120.0,
            "soc_guard_margin_pct": 1.0,
            "zero_deadband_kw": 0.05,
            "control_working_mode": "EMS BattCtrl",
        },
        "policy": {
            "battery": {
                "capacity_kwh": 19.6,
                "hard_min_soc_pct": 5.0,
                "hard_max_soc_pct": 100.0,
            }
        },
        "optimizer": {
            "battery_max_charge_kw": 8.0,
            "battery_max_discharge_kw": 8.0,
            "battery_charge_efficiency": 0.95,
            "battery_discharge_efficiency": 0.95,
            "physical_grid_import_limit_kw": 13.8,
            "grid_export_limit_kw": 10.0,
        },
    }


def test_remaining_horizon_does_not_create_minute_by_minute_soc_clamp():
    candidate = {"requested_action_kw": -0.9078, "valid_until": "2026-08-31T13:45:00+00:00"}
    actual = {
        "age_seconds": 8.272513,
        "soc_pct": 98.0,
        "load_kw": 2.031,
        "pv_kw": 1.207,
        "grid_kw": -1.690,
        "battery_kw": -0.916,
    }
    now = datetime(2026, 8, 31, 13, 39, 59, tzinfo=timezone.utc)

    result = resilience.safety_filter_time_aware(candidate, _cfg(), actual, now=now)

    assert result["safe_action_kw"] == pytest.approx(-0.9078)
    assert result["clamped"] is False
    assert 420 <= result["safety_horizon_seconds"] <= 422
    assert result["safe_interval_kw"]["min"] < -0.9078
    assert result["net_input_source"] == "load_minus_pv"


def test_horizon_includes_candidate_grace_used_by_validity_gate():
    candidate = {"requested_action_kw": 0.0, "valid_until": "2026-08-31T13:45:00+00:00"}
    now = datetime(2026, 8, 31, 13, 44, 0, tzinfo=timezone.utc)
    hours, seconds = resilience._candidate_safety_horizon_hours(candidate, _cfg(), now=now)

    assert seconds == pytest.approx(180.0)
    assert hours == pytest.approx(0.05)


def test_zero_age_is_not_accidentally_treated_as_stale():
    candidate = {"requested_action_kw": 0.0, "valid_until": "2026-08-31T13:45:00+00:00"}
    actual = {"age_seconds": 0.0, "soc_pct": 50.0, "load_kw": 1.0, "pv_kw": 0.0}
    now = datetime(2026, 8, 31, 13, 30, 0, tzinfo=timezone.utc)

    result = resilience.safety_filter_time_aware(candidate, _cfg(), actual, now=now)
    assert result["safe_action_kw"] == 0.0


def test_non_finite_soc_is_rejected():
    candidate = {"requested_action_kw": 0.0, "valid_until": "2026-08-31T13:45:00+00:00"}
    actual = {"age_seconds": 1.0, "soc_pct": float("nan"), "load_kw": 1.0, "pv_kw": 0.0}
    now = datetime(2026, 8, 31, 13, 30, 0, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="actual_state_missing_soc"):
        resilience.safety_filter_time_aware(candidate, _cfg(), actual, now=now)


def test_missing_pv_can_use_grid_plus_battery_for_generic_safety():
    candidate = {"requested_action_kw": -1.6766, "valid_until": "2026-09-01T11:45:00+00:00"}
    actual = {
        "age_seconds": 18.5,
        "soc_pct": 42.9,
        "load_kw": 0.997,
        "pv_kw": None,
        "grid_kw": -0.757,
        "battery_kw": -1.661,
    }
    now = datetime(2026, 9, 1, 11, 33, 0, tzinfo=timezone.utc)

    result = resilience.safety_filter_time_aware(candidate, _cfg(), actual, now=now)

    assert result["net_input_source"] == "grid_plus_battery"
    assert result["net_kw"] == pytest.approx(-2.418)
    assert result["safe_action_kw"] == pytest.approx(-1.6766)


def test_explicit_redundant_net_has_priority_when_provided_by_watchdog():
    candidate = {"requested_action_kw": -1.6766, "valid_until": "2026-09-01T11:45:00+00:00"}
    actual = {
        "age_seconds": 18.5,
        "soc_pct": 42.9,
        "load_kw": 0.997,
        "pv_kw": None,
        "grid_kw": -0.757,
        "battery_kw": -1.661,
        "net_kw": -2.437,
    }
    now = datetime(2026, 9, 1, 11, 33, 0, tzinfo=timezone.utc)

    result = resilience.safety_filter_time_aware(candidate, _cfg(), actual, now=now)

    assert result["net_input_source"] == "provided_net_kw"
    assert result["net_kw"] == pytest.approx(-2.437)


def test_zero_deadband_never_moves_target_outside_safe_interval():
    cfg = _cfg()
    cfg["actuator"]["zero_deadband_kw"] = 0.05
    cfg["optimizer"]["physical_grid_import_limit_kw"] = 1.0
    candidate = {"requested_action_kw": 0.03, "valid_until": "2026-08-31T13:45:00+00:00"}
    actual = {"age_seconds": 1.0, "soc_pct": 50.0, "load_kw": 1.03, "pv_kw": 0.0}
    now = datetime(2026, 8, 31, 13, 30, 0, tzinfo=timezone.utc)

    result = resilience.safety_filter_time_aware(candidate, cfg, actual, now=now)

    assert result["safe_interval_kw"]["min"] == pytest.approx(0.03)
    assert result["safe_action_kw"] == pytest.approx(0.03)
    assert result["zero_deadband_applied"] is False


def test_entity_resolution_is_cached(monkeypatch):
    calls = []
    resolved = SimpleNamespace(working_mode="select.mode", battery_power_target="number.target")

    async def original(_self):
        calls.append(1)
        return resolved

    monkeypatch.setattr(resilience, "_ORIGINAL_RESOLVE_ENTITIES", original)
    adapter = SimpleNamespace()

    first = asyncio.run(resilience._resolve_entities_cached(adapter))
    second = asyncio.run(resilience._resolve_entities_cached(adapter))

    assert first is resolved
    assert second is resolved
    assert len(calls) == 1


def test_dispatch_timeout_is_reconciled_when_target_was_applied(monkeypatch):
    async def original_dispatch(_self, _target):
        raise TimeoutError("service response lost")

    monkeypatch.setattr(resilience, "_ORIGINAL_DISPATCH", original_dispatch)

    class FakeAdapter:
        cfg = {"actuator": {"control_working_mode": "EMS BattCtrl"}}
        ack_tolerance_kw = 0.1

        async def resolve_entities(self):
            return object()

        async def readback(self, _entities=None):
            return {"working_mode": "EMS BattCtrl", "battery_power_target_kw": -0.83}

    result = asyncio.run(resilience._dispatch_with_reconciliation(FakeAdapter(), -0.8253))

    assert result["acknowledged"] is True
    assert result["reconciled_after_dispatch_error"] is True
    assert "TimeoutError" in result["dispatch_error"]


def test_dispatch_timeout_still_fails_when_readback_does_not_match(monkeypatch):
    async def original_dispatch(_self, _target):
        raise TimeoutError("service response lost")

    monkeypatch.setattr(resilience, "_ORIGINAL_DISPATCH", original_dispatch)

    class FakeAdapter:
        cfg = {"actuator": {"control_working_mode": "EMS BattCtrl"}}
        ack_tolerance_kw = 0.1

        async def resolve_entities(self):
            return object()

        async def readback(self, _entities=None):
            return {"working_mode": "EMS BattCtrl", "battery_power_target_kw": -0.30}

    with pytest.raises(TimeoutError, match="service response lost"):
        asyncio.run(resilience._dispatch_with_reconciliation(FakeAdapter(), -0.8253))


def test_runtime_hook_installs_resilience_before_canonical_watchdog():
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "actuator_physical_cap_v190.py").read_text(encoding="utf-8")
    assert "install_actuator_runtime_resilience_patch()" in source
    assert "install_actuator_watchdog_patch()" in source
    assert source.index("install_actuator_runtime_resilience_patch()") < source.index("install_actuator_watchdog_patch()")
