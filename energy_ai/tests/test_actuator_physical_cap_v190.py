from __future__ import annotations

import pytest

from app.actuator_physical_cap_v190 import apply_physical_command_cap


def cfg(cap: float = 2.0):
    return {"actuator": {"max_physical_command_kw": cap}}


def safety(value: float, *, load: float = 0.7, pv: float = 1.5, lo: float = -8.0, hi: float = 8.0):
    return {
        "requested_action_kw": value,
        "safe_action_kw": value,
        "clamped": False,
        "reasons": [],
        "actual": {"load_kw": load, "pv_kw": pv},
        "predicted_grid_kw": load - pv - value,
        "safe_interval_kw": {"min": lo, "max": hi},
    }


def test_charge_request_is_capped_after_deterministic_safety():
    result = apply_physical_command_cap(safety(-2.104421), cfg(2.0))
    assert result["pre_cap_safe_action_kw"] == pytest.approx(-2.1044)
    assert result["safe_action_kw"] == pytest.approx(-2.0)
    assert result["physical_target_kw"] == pytest.approx(-2.0)
    assert result["physical_command_cap_kw"] == pytest.approx(2.0)
    assert result["cap_applied"] is True
    assert result["clamped"] is True
    assert "physical_command_cap" in result["reasons"]
    assert result["physical_command_interval_kw"] == {"min": -2.0, "max": 2.0}
    # net=-0.8 kW, charging at 2 kW -> 1.2 kW grid import.
    assert result["predicted_grid_kw"] == pytest.approx(1.2)


def test_discharge_request_is_capped_symmetrically():
    result = apply_physical_command_cap(safety(7.0, load=2.0, pv=0.0), cfg(2.0))
    assert result["pre_cap_safe_action_kw"] == pytest.approx(7.0)
    assert result["physical_target_kw"] == pytest.approx(2.0)
    assert result["predicted_grid_kw"] == pytest.approx(0.0)
    assert result["cap_applied"] is True


def test_action_inside_cap_is_unchanged():
    result = apply_physical_command_cap(safety(-1.25), cfg(2.0))
    assert result["pre_cap_safe_action_kw"] == pytest.approx(-1.25)
    assert result["physical_target_kw"] == pytest.approx(-1.25)
    assert result["safe_action_kw"] == pytest.approx(-1.25)
    assert result["cap_applied"] is False
    assert result["clamped"] is False
    assert result["reasons"] == []


def test_existing_safety_clamp_remains_tighter_than_physical_cap():
    base = safety(-0.8, lo=-0.8, hi=8.0)
    base["requested_action_kw"] = -8.0
    base["clamped"] = True
    base["reasons"] = ["safety_clamped"]
    result = apply_physical_command_cap(base, cfg(2.0))
    assert result["pre_cap_safe_action_kw"] == pytest.approx(-0.8)
    assert result["physical_target_kw"] == pytest.approx(-0.8)
    assert result["cap_applied"] is False
    assert result["clamped"] is True
    assert result["reasons"] == ["safety_clamped"]
    assert result["physical_command_interval_kw"]["min"] == pytest.approx(-0.8)


def test_zero_cap_forces_zero_physical_target():
    result = apply_physical_command_cap(safety(1.5), cfg(0.0))
    assert result["pre_cap_safe_action_kw"] == pytest.approx(1.5)
    assert result["physical_target_kw"] == 0.0
    assert result["safe_action_kw"] == 0.0
    assert result["physical_command_interval_kw"] == {"min": 0.0, "max": 0.0}
    assert result["cap_applied"] is True
