from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import db, pool_model_store
from app.pool_control_contract import PoolPolicy, PoolThermalModelState
from app.pool_parameters import register_pool_parameters


def _state(value, *, unit=None):
    return {
        "entity_id": "sensor.test",
        "state": value,
        "available": True,
        "last_updated": "2026-09-10T12:00:00+00:00",
        "source_unit": unit,
        "normalized_unit": unit,
    }


def test_pool_policy_defaults_are_observe_only_and_validate_bounds():
    policy = PoolPolicy.from_mapping({})
    assert policy.enabled is False
    assert policy.control_mode == "observe"
    assert policy.minimum_temp_c <= policy.preferred_temp_c <= policy.maximum_temp_c

    with pytest.raises(ValueError):
        PoolPolicy.from_mapping(
            {
                "pool_minimum_temp_c": 29.0,
                "pool_preferred_temp_c": 28.0,
                "pool_maximum_temp_c": 30.0,
            }
        )


def test_pool_parameter_registration_updates_editor_lookup_without_duplicates():
    fake = SimpleNamespace(PARAMETERS=[], PARAM_BY_KEY={})
    first = register_pool_parameters(fake)
    second = register_pool_parameters(fake)

    assert first["total_pool_parameters"] >= 8
    assert first["added"]
    assert second["added"] == []
    assert "pool_preferred_temp_c" in fake.PARAM_BY_KEY
    assert len(fake.PARAMETERS) == first["total_pool_parameters"]


def test_pool_model_store_keeps_history_and_single_approved_model(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "energy_ai.db")
    db.init_db()

    first_id = pool_model_store.insert_pool_model_state(
        PoolThermalModelState(
            trained_at="2026-09-10T02:00:00+00:00",
            base_loss_c_per_hour=0.08,
            heat_gain_c_per_kwh=0.14,
            sample_count_off=100,
            sample_count_on=60,
            confidence=0.6,
        )
    )
    second_id = pool_model_store.insert_pool_model_state(
        PoolThermalModelState(
            trained_at="2026-09-11T02:00:00+00:00",
            base_loss_c_per_hour=0.07,
            heat_gain_c_per_kwh=0.15,
            sample_count_off=120,
            sample_count_on=80,
            confidence=0.7,
        )
    )

    assert second_id > first_id
    assert pool_model_store.approve_pool_model_state(first_id)["id"] == first_id
    approved = pool_model_store.approve_pool_model_state(second_id)
    assert approved["id"] == second_id

    with db.connect_db() as c:
        statuses = dict(c.execute("SELECT id,status FROM pool_thermal_model_state").fetchall())
    assert statuses[first_id] == "superseded"
    assert statuses[second_id] == "approved"


def test_pool_power_and_temperature_are_aggregated_into_15m_history(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "energy_ai.db")
    db.init_db()

    start = "2026-09-10T12:00:00+00:00"
    end = "2026-09-10T12:15:00+00:00"
    for minute, power, temp in ((0, 1.8, 27.4), (5, 2.0, 27.5), (10, 2.2, 27.6)):
        ts = f"2026-09-10T12:{minute:02d}:00+00:00"
        payload = {
            "pv_power_kw": _state(3.0, unit="kW"),
            "house_load_kw": _state(1.0, unit="kW"),
            "grid_power_kw": _state(0.0, unit="kW"),
            "battery_power_kw": _state(0.0, unit="kW"),
            "battery_soc_pct": _state(50.0, unit="%"),
            "spot_price_ore_kwh": _state(100.0, unit="öre/kWh"),
            "ev_power_kw": _state(0.0, unit="kW"),
            "pool_power_kw": _state(power, unit="kW"),
            "pool_temperature_c": _state(temp, unit="°C"),
            "load_components": {},
        }
        db.insert_raw(ts, payload)

    bucket = db.rebuild_15m_bucket(start, end, expected_samples=3)
    assert bucket is not None
    assert bucket["mean"]["pool_power_kw"] == pytest.approx(2.0)
    assert bucket["mean"]["pool_temperature_c"] == pytest.approx(27.5)
    assert bucket["min"]["pool_temperature_c"] == pytest.approx(27.4)
    assert bucket["max"]["pool_temperature_c"] == pytest.approx(27.6)

    with db.connect_db() as c:
        stored = c.execute("SELECT payload_json FROM state_15m WHERE bucket_start=?", (start,)).fetchone()
    assert stored is not None
    persisted = json.loads(stored[0])
    assert persisted["value_counts"]["pool_power_kw"] == 3
    assert persisted["value_counts"]["pool_temperature_c"] == 3
