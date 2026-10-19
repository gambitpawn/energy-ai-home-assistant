"""Regression tests: disabled spa control must never write Balboa."""
import asyncio
from datetime import datetime, timezone

import pytest

from app import balboa_permission as gate
from app import balboa_heat_scheduler as heat
from app import balboa_comfort as comfort


def run(coro):
    return asyncio.run(coro)


def test_new_permission_defaults_disabled(monkeypatch):
    monkeypatch.setenv("ENERGY_AI_DB", "/tmp/balboa_permission_test.db")
    gate.set_authorized(False)
    assert gate.authorized() is False
    with pytest.raises(gate.BalboaControlDisabled):
        gate.require_authorized()


def test_spa_heat_write_requires_permission(monkeypatch):
    monkeypatch.setattr(gate, "authorized", lambda: False)
    client = comfort.BalboaComfort()
    scheduler = heat.BalboaHeatScheduler(client)
    with pytest.raises(gate.BalboaControlDisabled):
        run(scheduler._set_mode("heat"))


def test_spa_preset_write_requires_permission(monkeypatch):
    monkeypatch.setattr(gate, "authorized", lambda: False)
    with pytest.raises(gate.BalboaControlDisabled):
        run(comfort.BalboaComfort()._set_preset("ready"))


def test_bada_nu_cannot_activate_while_locked(monkeypatch):
    monkeypatch.setattr(comfort, "authorized", lambda: False)
    monkeypatch.setattr(comfort, "_state", lambda: {"state":"idle","ends_at":None,"previous_preset":None,"last_error":None})
    with pytest.raises(Exception) as e:
        run(comfort.BalboaComfort().activate())
    assert getattr(e.value, "status_code", None) == 409
