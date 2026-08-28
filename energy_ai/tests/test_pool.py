from __future__ import annotations

from app import pool


def _entity(entity_id: str, state, name: str, unit: str | None = None, **attrs):
    attributes = {"friendly_name": name, **attrs}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attributes,
        "last_updated": "2026-08-29T10:00:00+00:00",
    }


def _states(flow_rate: float = 12.0):
    return [
        _entity(
            "climate.poolstyrning",
            "heat",
            "Poolstyrning",
            current_temperature=25.0,
            temperature=26.0,
            fan_mode="auto",
            fan_modes=["auto", "low"],
            hvac_action="heating",
        ),
        _entity("sensor.poolstyrning_inlet_water_temp_t02", 25.0, "Inlet water Temp. [T02]", "°C"),
        _entity("sensor.poolstyrning_outlet_water_temp_t03", 27.0, "Outlet water Temp. [T03]", "°C"),
        _entity("sensor.poolstyrning_ambient_temp_t05", 18.0, "Ambient Temp. [T05]", "°C"),
        _entity("sensor.poolstyrning_comp_output_frequency_007", 50.0, "Comp. Output frequency [007]", "Hz"),
        _entity("sensor.poolstyrning_compressor_current_008", 4.2, "Compressor current [008]", "A"),
        _entity("sensor.poolstyrning_ac_fan_output_t08", 65.0, "AC Fan Output [T08]", "%"),
        _entity("sensor.poolstyrning_fan_motor_t17", 720.0, "Speed of fan motor1 [T17]", "r"),
        _entity("sensor.poolstyrning_flow_rate_t09", flow_rate, "Flow Rate Input [T09]", "Hz"),
        _entity("binary_sensor.poolstyrning_flow_switch_s03", "on", "Flow switch [S03]"),
        _entity("sensor.poolstyrning_pressure_sensor_t10", 0.0, "Pressure Sensor [T10]", "bar"),
        _entity("sensor.poolstyrning_heating_set_r02", 26.0, "Heating set [R02]", "°C"),
    ]


def test_discovers_poolstyrning_and_normalizes_operator_state(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "DB_PATH", tmp_path / "energy_ai.db")
    discovery = pool.discover_pool_entities_from_states(_states(), target_name="Poolstyrning")

    assert discovery["climate"]["entity_id"] == "climate.poolstyrning"
    assert discovery["diagnostics"]["inlet_temperature_c"]["entity_id"].endswith("t02")
    assert discovery["diagnostics"]["outlet_temperature_c"]["entity_id"].endswith("t03")
    assert discovery["diagnostics"]["compressor_frequency_hz"]["entity_id"].endswith("007")

    state = pool.pool_state_from_discovery(discovery)
    assert state["available"] is True
    assert state["current_temperature_c"] == 25.0
    assert state["target_temperature_c"] == 26.0
    assert state["water_delta_t_c"] == 2.0
    assert state["compressor_running"] is True
    assert state["operating_state"] == "heating"
    assert state["fan_mode"] == "auto"
    assert state["pressure_sensor_role"] == "diagnostic_only_not_assumed_pool_filter_pressure"
    assert state["filter_health"]["status"] == "not_calibrated"
    assert state["energy"]["smart_control_enabled"] is False


def test_clean_filter_flow_baseline_warns_on_material_flow_drop(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "DB_PATH", tmp_path / "energy_ai.db")

    clean = pool.pool_state_from_discovery(
        pool.discover_pool_entities_from_states(_states(flow_rate=12.0))
    )
    captured = pool.mark_filter_cleaned(clean)
    assert captured["ok"] is True
    assert captured["source"] == "flow_rate"

    reduced = pool.pool_state_from_discovery(
        pool.discover_pool_entities_from_states(_states(flow_rate=9.0))
    )
    health = reduced["filter_health"]
    assert health["status"] == "clean_filter"
    assert health["basis"] == "flow_rate_vs_clean_baseline"
    assert health["ratio_to_clean_baseline"] == 0.75


def test_flow_switch_fault_is_high_confidence_and_does_not_use_t10_pressure(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "DB_PATH", tmp_path / "energy_ai.db")
    states = _states()
    for item in states:
        if item["entity_id"].startswith("binary_sensor.poolstyrning_flow_switch"):
            item["state"] = "off"
    state = pool.pool_state_from_discovery(
        pool.discover_pool_entities_from_states(states)
    )
    health = state["filter_health"]
    assert health["status"] == "check_now"
    assert health["basis"] == "flow_switch"
    assert health["confidence"] == "high"
    assert state["pressure_sensor_bar"] == 0.0
