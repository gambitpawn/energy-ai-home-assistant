from __future__ import annotations

from app import pool
from app.pool_installation_profile import install_pool_installation_profile


def _entity(entity_id: str, state, name: str, unit: str | None = None, device_class: str | None = None, **attrs):
    attributes = {"friendly_name": name, **attrs}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    if device_class is not None:
        attributes["device_class"] = device_class
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


def _installation_states(
    compressor_current_a: float = 0.0,
    compressor_frequency_hz: float = 0.0,
    outlet_temperature_c: float = 25.0,
):
    return [
        _entity(
            "climate.289c6e4f1a5e",
            "heat",
            "Poolstyrning",
            current_temperature=25.0,
            temperature=26.0,
            fan_mode="auto",
            fan_modes=["auto", "low"],
        ),
        _entity("sensor.289c6e4f1a5e_comp_output_frequency_o07", compressor_frequency_hz, "Poolstyrning Comp. Output frequency [O07]", "Hz", "frequency"),
        _entity("sensor.289c6e4f1a5e_compressor_current_detect_t07", 1.0, "Poolstyrning Compressor current Detect [T07]", "A", "current"),
        _entity("sensor.289c6e4f1a5e_compressor_current_o08", compressor_current_a, "Poolstyrning Compressor current [O08]", "A", "current"),
        _entity("binary_sensor.289c6e4f1a5e_power", "on", "Poolstyrning Power", device_class="power"),
        _entity("sensor.289c6e4f1a5e_flow_rate_input_t09", 0.0, "Poolstyrning Flow Rate Input [T09]", "Hz", "frequency"),
        _entity("sensor.289c6e4f1a5e_flow_switch_s03", "unknown", "Poolstyrning Flow switch [S03]"),
        _entity("sensor.289c6e4f1a5e_inlet_water_temp_t02", 25.0, "Poolstyrning Inlet water Temp. [T02]", "°C", "temperature"),
        _entity("sensor.289c6e4f1a5e_outlet_water_temp_t03", outlet_temperature_c, "Poolstyrning Outlet water Temp. [T03]", "°C", "temperature"),
        _entity("sensor.289c6e4f1a5e_ambient_temp_t05", 17.0, "Poolstyrning Ambient Temp. [T05]", "°C", "temperature"),
    ]


def test_profile_prefers_actual_o08_compressor_current_over_t07_detect():
    install_pool_installation_profile()
    discovery = pool.discover_pool_entities_from_states(_installation_states())
    current = discovery["diagnostics"]["compressor_current_a"]
    assert current["entity_id"].endswith("compressor_current_o08")
    assert current["state"] == 0.0


def test_profile_does_not_treat_binary_power_status_as_measured_electrical_power():
    install_pool_installation_profile()
    discovery = pool.discover_pool_entities_from_states(_installation_states())
    assert discovery["electrical_power"] is None
    state = pool.pool_state_from_discovery(discovery)
    assert state["electrical_power_kw"] == 0.0
    assert state["electrical_power_source"] == "estimated_from_o08_compressor_current"
    assert state["electrical_power_estimated"] is True
    assert state["electrical_power_confidence"] == "low"


def test_profile_estimates_pool_power_from_o08_current_at_220v():
    install_pool_installation_profile()
    discovery = pool.discover_pool_entities_from_states(_installation_states(compressor_current_a=4.2))
    state = pool.pool_state_from_discovery(discovery)

    assert state["compressor_current_a"] == 4.2
    assert state["electrical_power_kw"] == 0.924
    assert state["electrical_power_estimated"] is True
    assert state["electrical_power_estimate"]["supply_voltage_v"] == 220.0
    assert state["electrical_power_estimate"]["power_factor_assumption"] == 1.0
    assert state["energy"]["current_load_kw"] == 0.924
    assert state["energy"]["current_load_estimated"] is True


def test_native_aquatemp_w_kw_register_cannot_override_o08_estimate():
    install_pool_installation_profile()
    states = _installation_states(compressor_current_a=3.0, compressor_frequency_hz=80.0) + [
        _entity(
            "sensor.289c6e4f1a5e_internal_output_power_h99",
            0.0,
            "Poolstyrning Internal Output Power [H99]",
            "kW",
            "power",
        )
    ]
    discovery = pool.discover_pool_entities_from_states(states)
    assert discovery["electrical_power"] is None
    state = pool.pool_state_from_discovery(discovery)
    assert state["electrical_power_kw"] == 0.66
    assert state["electrical_power_source"] == "estimated_from_o08_compressor_current"


def test_zero_external_power_measurement_is_rejected_when_compressor_plainly_runs():
    install_pool_installation_profile()
    states = _installation_states(compressor_current_a=3.0, compressor_frequency_hz=80.0) + [
        _entity("sensor.pool_heat_pump_power", 0.0, "Pool heat pump power", "kW", "power")
    ]
    discovery = pool.discover_pool_entities_from_states(states)
    assert discovery["electrical_power"]["entity_id"] == "sensor.pool_heat_pump_power"
    state = pool.pool_state_from_discovery(discovery)
    assert state["electrical_power_kw"] == 0.66
    assert state["electrical_power_source"] == "estimated_from_o08_compressor_current"
    assert state["electrical_power_rejected_measurement"]["reported_kw"] == 0.0
    assert "zero_measured_power" in state["electrical_power_rejected_measurement"]["reason"]


def test_profile_accepts_real_numeric_kw_power_sensor_when_present():
    install_pool_installation_profile()
    states = _installation_states(compressor_current_a=4.2, compressor_frequency_hz=50.0) + [
        _entity("sensor.pool_heat_pump_power", 1.84, "Pool heat pump power", "kW", "power")
    ]
    discovery = pool.discover_pool_entities_from_states(states)
    assert discovery["electrical_power"]["entity_id"] == "sensor.pool_heat_pump_power"
    state = pool.pool_state_from_discovery(discovery)
    assert state["electrical_power_kw"] == 1.84
    assert state["electrical_power_source"] == "measured_w_kw_sensor"
    assert state["electrical_power_estimated"] is False
    assert state["energy"]["current_load_kw"] == 1.84


def test_filter_cleaned_response_state_is_refreshed_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "DB_PATH", tmp_path / "energy_ai.db")
    install_pool_installation_profile()
    discovery = pool.discover_pool_entities_from_states(
        _installation_states(
            compressor_current_a=3.0,
            compressor_frequency_hz=80.0,
            outlet_temperature_c=27.5,
        )
    )
    state = pool.pool_state_from_discovery(discovery)
    assert state["filter_health"]["status"] == "not_calibrated"

    result = pool.mark_filter_cleaned(state)

    assert result["ok"] is True
    assert result["source"] == "thermal_proxy"
    assert state["filter_baseline"] is not None
    assert state["filter_baseline"]["source"] == "thermal_proxy"
    assert state["filter_health"]["status"] == "ok"
