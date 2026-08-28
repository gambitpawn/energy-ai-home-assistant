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


def _installation_states():
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
        _entity("sensor.289c6e4f1a5e_comp_output_frequency_o07", 0.0, "Poolstyrning Comp. Output frequency [O07]", "Hz", "frequency"),
        _entity("sensor.289c6e4f1a5e_compressor_current_detect_t07", 1.0, "Poolstyrning Compressor current Detect [T07]", "A", "current"),
        _entity("sensor.289c6e4f1a5e_compressor_current_o08", 0.0, "Poolstyrning Compressor current [O08]", "A", "current"),
        _entity("binary_sensor.289c6e4f1a5e_power", "on", "Poolstyrning Power", device_class="power"),
        _entity("sensor.289c6e4f1a5e_flow_rate_input_t09", 0.0, "Poolstyrning Flow Rate Input [T09]", "Hz", "frequency"),
        _entity("sensor.289c6e4f1a5e_flow_switch_s03", "unknown", "Poolstyrning Flow switch [S03]"),
        _entity("sensor.289c6e4f1a5e_inlet_water_temp_t02", 25.0, "Poolstyrning Inlet water Temp. [T02]", "°C", "temperature"),
        _entity("sensor.289c6e4f1a5e_outlet_water_temp_t03", 25.0, "Poolstyrning Outlet water Temp. [T03]", "°C", "temperature"),
    ]


def test_profile_prefers_actual_o08_compressor_current_over_t07_detect():
    install_pool_installation_profile()
    discovery = pool.discover_pool_entities_from_states(_installation_states())
    current = discovery["diagnostics"]["compressor_current_a"]
    assert current["entity_id"].endswith("compressor_current_o08")
    assert current["state"] == 0.0


def test_profile_does_not_treat_binary_power_status_as_electrical_power():
    install_pool_installation_profile()
    discovery = pool.discover_pool_entities_from_states(_installation_states())
    assert discovery["electrical_power"] is None
    state = pool.pool_state_from_discovery(discovery)
    assert state["electrical_power_kw"] is None


def test_profile_accepts_real_numeric_kw_power_sensor_when_present():
    install_pool_installation_profile()
    states = _installation_states() + [
        _entity("sensor.pool_heat_pump_power", 1.84, "Pool heat pump power", "kW", "power")
    ]
    discovery = pool.discover_pool_entities_from_states(states)
    assert discovery["electrical_power"]["entity_id"] == "sensor.pool_heat_pump_power"
    state = pool.pool_state_from_discovery(discovery)
    assert state["electrical_power_kw"] == 1.84
