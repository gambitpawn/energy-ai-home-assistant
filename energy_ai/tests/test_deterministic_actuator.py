from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.deterministic_actuator import _candidate_valid, safety_filter
from app.solinteg_command import _rank


def cfg():
    return {
        "actuator": {
            "state_max_age_seconds": 180,
            "candidate_grace_seconds": 120,
            "soc_guard_margin_pct": 1.0,
            "zero_deadband_kw": 0.05,
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


def actual(*, soc=50.0, load=2.0, pv=0.0, age=10.0):
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "age_seconds": age,
        "soc_pct": soc,
        "load_kw": load,
        "pv_kw": pv,
        "grid_kw": load - pv,
        "battery_kw": 0.0,
    }


def candidate(action):
    now = datetime.now(timezone.utc)
    return {
        "source": "test",
        "source_id": "x",
        "engine_id": "deterministic_v35",
        "decision_start": now.isoformat(),
        "valid_until": (now + timedelta(minutes=15)).isoformat(),
        "requested_action_kw": action,
    }


def test_hard_soc_guard_blocks_discharge_near_minimum():
    result = safety_filter(candidate(8.0), cfg(), actual(soc=5.5, load=1.0, pv=0.0))
    assert result["safe_action_kw"] == 0.0
    assert result["clamped"] is True


def test_grid_export_limit_clamps_discharge():
    result = safety_filter(candidate(8.0), cfg(), actual(soc=50.0, load=0.5, pv=10.0))
    # net=-9.5 kW and export limit=10 => no more than 0.5 kW battery discharge.
    assert result["safe_action_kw"] == pytest.approx(0.5)
    assert result["predicted_grid_kw"] == pytest.approx(-10.0)


def test_grid_import_limit_clamps_charging():
    result = safety_filter(candidate(-8.0), cfg(), actual(soc=50.0, load=13.0, pv=0.0))
    # Existing load is 13 kW; only 0.8 kW additional import is available.
    assert result["safe_action_kw"] == pytest.approx(-0.8)
    assert result["predicted_grid_kw"] == pytest.approx(13.8)


def test_stale_actual_state_is_rejected():
    with pytest.raises(RuntimeError, match="actual_state_stale"):
        safety_filter(candidate(1.0), cfg(), actual(age=181.0))


def test_expired_candidate_is_rejected_after_grace():
    now = datetime.now(timezone.utc)
    item = candidate(1.0)
    item["valid_until"] = (now - timedelta(seconds=121)).isoformat()
    valid, reason = _candidate_valid(item, cfg(), now=now)
    assert valid is False
    assert reason == "candidate_expired"


def test_solinteg_entity_ranking_prefers_expected_control_entities():
    working = {
        "entity_id": "select.solinteg_inverter_working_mode",
        "state": "General",
        "attributes": {"friendly_name": "Solinteg Inverter Working Mode", "options": ["General", "EMS BattCtrl"]},
    }
    target = {
        "entity_id": "number.solinteg_inverter_battery_charge_discharge_power_target",
        "state": "0.0",
        "attributes": {"friendly_name": "Solinteg Inverter EMS BattCtrl Charge Discharge Power Target"},
    }
    assert _rank(working, "working_mode") >= 20
    assert _rank(target, "battery_power_target") >= 20
