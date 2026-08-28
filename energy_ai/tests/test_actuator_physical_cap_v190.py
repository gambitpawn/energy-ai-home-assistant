from __future__ import annotations

import pytest

from app import actuator_config as ac
from app.actuator_physical_cap_v190 import _promote_cap_diagnostics, apply_physical_command_cap


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
    assert result["deterministic_safe_interval_kw"] == {"min": -8.0, "max": 8.0}
    assert result["physical_command_interval_kw"] == {"min": -2.0, "max": 2.0}
    assert result["safe_interval_kw"] == {"min": -2.0, "max": 2.0}
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
    # The final command interval is still narrowed for held-command/watchdog safety.
    assert result["safe_interval_kw"] == {"min": -2.0, "max": 2.0}


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
    assert result["safe_interval_kw"] == {"min": -0.8, "max": 2.0}


def test_zero_cap_forces_zero_physical_target_and_safe_interval():
    result = apply_physical_command_cap(safety(1.5), cfg(0.0))
    assert result["pre_cap_safe_action_kw"] == pytest.approx(1.5)
    assert result["physical_target_kw"] == 0.0
    assert result["safe_action_kw"] == 0.0
    assert result["physical_command_interval_kw"] == {"min": 0.0, "max": 0.0}
    assert result["safe_interval_kw"] == {"min": 0.0, "max": 0.0}
    assert result["cap_applied"] is True


def test_cap_diagnostics_are_promoted_to_actuator_result_top_level():
    capped = apply_physical_command_cap(safety(-2.104421), cfg(2.0))
    result = _promote_cap_diagnostics(
        {
            "status": "dry_run",
            "safe_action_kw": capped["safe_action_kw"],
            "safety": capped,
            "physical_write_performed": False,
        }
    )
    assert result["pre_cap_safe_action_kw"] == pytest.approx(-2.1044)
    assert result["physical_target_kw"] == pytest.approx(-2.0)
    assert result["physical_command_cap_kw"] == pytest.approx(2.0)
    assert result["cap_applied"] is True


def test_physical_cap_setting_participates_in_restart_freshness_gate(monkeypatch):
    raw = dict(ac.ACTUATOR_DEFAULTS)
    runtime_cfg = {
        "entities": {
            "solinteg_working_mode": None,
            "solinteg_battery_power_target": None,
        },
        "actuator": {
            "control_working_mode": "EMS BattCtrl",
            "safe_working_mode": "ToU",
            "ack_timeout_seconds": 8.0,
            "ack_tolerance_kw": 0.10,
            "state_max_age_seconds": 180.0,
            "candidate_grace_seconds": 120.0,
            "soc_guard_margin_pct": 1.0,
            "zero_deadband_kw": 0.05,
            "min_action_change_kw": 0.10,
            "watchdog_poll_seconds": 30.0,
            "max_physical_command_kw": 2.0,
            "config_loaded_at": "2026-08-28T10:00:00+00:00",
        },
        "optimizer": {
            "battery_max_charge_kw": 8.0,
            "battery_max_discharge_kw": 8.0,
            "physical_grid_import_limit_kw": 13.8,
            "grid_export_limit_kw": 10.0,
        },
        "policy": {"battery": {"hard_min_soc_pct": 5.0, "hard_max_soc_pct": 100.0}},
    }
    raw["actuator_safe_working_mode"] = "ToU"
    monkeypatch.setattr(
        ac,
        "_option_layers",
        lambda: (raw, {"actuator_safe_working_mode": "ToU", "actuator_max_physical_command_kw": 4.0}),
    )
    report = ac.effective_actuator_config_report(runtime_cfg)
    assert report["restart_required"] is True
    mismatch = next(item for item in report["mismatches"] if item["setting"] == "max_physical_command_kw")
    assert mismatch["runtime"] == pytest.approx(2.0)
    assert mismatch["persisted"] == pytest.approx(4.0)
