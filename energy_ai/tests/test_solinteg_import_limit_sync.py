from __future__ import annotations

import asyncio

from app.solinteg_command import SolintegCommandAdapter, _is_grid_import_limit_entity


class FakeHA:
    token = "test"
    authenticated = True
    base_url = "http://homeassistant.local/api"
    timeout = 1.0

    def __init__(self, states):
        self.states = states

    async def all_states(self):
        return list(self.states.values())


def cfg(import_limit=13.8, battery_charge_limit=8.0):
    return {
        "actuator": {"ack_timeout_seconds": 0.2, "ack_tolerance_kw": 0.10},
        "optimizer": {
            "physical_grid_import_limit_kw": import_limit,
            "battery_max_charge_kw": battery_charge_limit,
        },
    }


def import_limit_state(value=8.0):
    return {
        "entity_id": "number.solinteg_inverter_import_limit",
        "state": str(value),
        "attributes": {
            "friendly_name": "Solinteg Inverter Import Limit",
            "unit_of_measurement": "kW",
            "min": 0,
            "max": 100,
            "step": 0.1,
        },
    }


def ems_limit_state(value=-55.0):
    return {
        "entity_id": "number.solinteg_inverter_ems_battctrl_max_grid_import",
        "state": str(value),
        "attributes": {
            "friendly_name": "Solinteg Inverter EMS BattCtrl Max Grid Import",
            "unit_of_measurement": "kW",
            "min": -200,
            "max": 0,
            "step": 0.01,
        },
    }


def test_generic_import_limit_discovery_excludes_ems_battctrl_limit():
    assert _is_grid_import_limit_entity(import_limit_state()) is True
    assert _is_grid_import_limit_entity(ems_limit_state()) is False


def test_configured_grid_import_limit_is_not_battery_charge_limit():
    adapter = SolintegCommandAdapter(cfg(import_limit=13.8, battery_charge_limit=8.0), FakeHA({}))
    assert adapter.configured_grid_import_limit_kw() == 13.8


def test_ensure_grid_import_limit_updates_inverter_from_8_to_13_8(monkeypatch):
    generic = import_limit_state(8.0)
    ems = ems_limit_state(-55.0)
    states = {generic["entity_id"]: generic, ems["entity_id"]: ems}
    adapter = SolintegCommandAdapter(cfg(), FakeHA(states))
    writes = []

    async def fake_state(entity_id):
        return states[entity_id]

    async def fake_service(domain, service, payload):
        writes.append((domain, service, dict(payload)))
        if payload["entity_id"] == generic["entity_id"]:
            generic["state"] = str(payload["value"])

    monkeypatch.setattr(adapter, "_state", fake_state)
    monkeypatch.setattr(adapter, "_service", fake_service)

    result = asyncio.run(adapter.ensure_grid_import_limit())

    assert result["acknowledged"] is True
    assert result["changed"] is True
    assert result["previous_grid_import_limit_kw"] == 8.0
    assert result["grid_import_limit_kw"] == 13.8
    assert writes == [
        (
            "number",
            "set_value",
            {"entity_id": "number.solinteg_inverter_import_limit", "value": 13.8},
        )
    ]
    assert ems["state"] == "-55.0"


def test_ensure_grid_import_limit_does_not_rewrite_matching_value(monkeypatch):
    generic = import_limit_state(13.8)
    states = {generic["entity_id"]: generic}
    adapter = SolintegCommandAdapter(cfg(), FakeHA(states))
    writes = []

    async def fake_state(entity_id):
        return states[entity_id]

    async def fake_service(domain, service, payload):
        writes.append((domain, service, dict(payload)))

    monkeypatch.setattr(adapter, "_state", fake_state)
    monkeypatch.setattr(adapter, "_service", fake_service)

    result = asyncio.run(adapter.ensure_grid_import_limit())

    assert result["acknowledged"] is True
    assert result["changed"] is False
    assert result["grid_import_limit_kw"] == 13.8
    assert writes == []
