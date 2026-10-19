from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from app import balboa_comfort as spa


def run(coro):
    return asyncio.run(coro)


def fixture(monkeypatch, initial="rest"):
    record = {"state": "idle", "ends_at": None, "previous_preset": None, "last_error": None}
    ha = {"preset_mode": initial, "preset_modes": ["ready", "rest"], "temperature": 39}
    writes = []

    def state():
        return dict(record)

    def save(state, ends_at=None, previous_preset=None, last_error=None):
        record.update(state=state, ends_at=ends_at, previous_preset=previous_preset, last_error=last_error)

    async def read():
        return {"state": "heat", "attributes": dict(ha)}

    async def set_preset(preset):
        writes.append(preset)
        ha["preset_mode"] = preset

    monkeypatch.setattr(spa, "authorized", lambda: True)
    monkeypatch.setattr(spa, "_state", state)
    monkeypatch.setattr(spa, "_save", save)
    monkeypatch.setattr(spa.BalboaComfort, "_read", lambda self: read())
    monkeypatch.setattr(spa.BalboaComfort, "_set_preset", lambda self, preset: set_preset(preset))
    return spa.BalboaComfort(), record, ha, writes


def test_bada_nu_activates_ready_without_temperature_changes(monkeypatch):
    comfort, record, ha, writes = fixture(monkeypatch)
    result = run(comfort.activate())
    assert result["active"] is True
    assert writes == ["ready"]
    assert ha["temperature"] == 39
    remaining = datetime.fromisoformat(record["ends_at"]) - datetime.now(timezone.utc)
    assert timedelta(hours=2, minutes=59) < remaining <= timedelta(hours=3)


def test_bada_nu_restores_previous_rest_mode(monkeypatch):
    comfort, record, ha, writes = fixture(monkeypatch, "ready_in_rest")
    run(comfort.activate())
    run(comfort.finish())
    assert writes == ["ready", "rest"]
    assert record["state"] == "idle"


def test_bada_nu_preserves_preexisting_ready(monkeypatch):
    comfort, record, ha, writes = fixture(monkeypatch, "ready")
    run(comfort.activate())
    run(comfort.finish())
    assert writes == []


def test_bada_nu_does_not_revert_other_manual_mode(monkeypatch):
    comfort, record, ha, writes = fixture(monkeypatch)
    run(comfort.activate())
    ha["preset_mode"] = "rest"
    run(comfort.finish())
    assert writes == ["ready"]


def test_bada_nu_expiry(monkeypatch):
    comfort, record, ha, writes = fixture(monkeypatch)
    run(comfort.activate())
    record["ends_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    run(comfort.reconcile())
    assert record["state"] == "idle"
    assert writes == ["ready", "rest"]


def test_bada_nu_rejects_unknown_mode_without_storing_error(monkeypatch):
    comfort, record, ha, writes = fixture(monkeypatch, "unexpected")
    with pytest.raises(HTTPException) as exc:
        run(comfort.activate())
    assert exc.value.status_code == 409
    assert record["state"] == "idle"
    assert writes == []
