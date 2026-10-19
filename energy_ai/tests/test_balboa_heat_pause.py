from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
import pytest
from app import balboa_heat_pause as hp


def run(c):
    return asyncio.run(c)


def setup(monkeypatch, *, initial=37.0, initial_range="high"):
    record={"status":"idle","expires_at":None,"original_target":None,"original_range":None,"last_error":None}
    device={"temperature":initial}
    state={"range":initial_range, "authorized":False, "scheduled":False, "bada":{"state":"idle"}}
    commands=[]
    def save(status,expires=None,target=None,range_mode=None,error=None):
        record.update(status=status,expires_at=expires,original_target=target,original_range=range_mode,last_error=error)
    class Comfort:
        def __init__(self):
            self.lock=asyncio.Lock()
        async def _read(self):
            return {"state":"off","attributes":dict(device)}
    obj=hp.BalboaHeatPause(Comfort())
    async def fake_service(domain, service, payload):
        commands.append((domain, service, payload))
        if domain=="climate": device["temperature"]=float(payload["temperature"])
        elif domain=="select": state["range"]=payload["option"]
    async def verify(target):
        assert device["temperature"]==target
    monkeypatch.setattr(hp,"read_record",lambda:dict(record))
    monkeypatch.setattr(hp,"save_record",save)
    monkeypatch.setattr(hp,"set_authorized",lambda x: state.update(authorized=x))
    monkeypatch.setattr(hp,"set_control_enabled",lambda x: state.update(scheduled=x))
    monkeypatch.setattr(hp,"_state",lambda:state["bada"],raising=False)
    from app import balboa_comfort
    monkeypatch.setattr(balboa_comfort,"_state",lambda:state["bada"])
    obj._service=fake_service
    obj._verify_target=verify
    async def get_range():return state["range"]
    obj._range=get_range
    return obj,record,device,state,commands


def test_pause_sets_low_target_and_restores_previous(monkeypatch):
    p,r,d,s,w=setup(monkeypatch)
    result=run(p.activate(6))
    assert result["status"]=="active"
    assert d["temperature"]==10
    assert s["range"]=="low"
    assert s["authorized"] is False and s["scheduled"] is False
    assert run(p.finish())["restored"] is True
    assert d["temperature"]==37
    assert s["range"]=="high"
    assert r["status"]=="idle"


def test_pause_does_not_overwrite_manual_thermostat_change(monkeypatch):
    p,r,d,s,w=setup(monkeypatch)
    run(p.activate(6))
    d["temperature"]=32
    before=len(w)
    result=run(p.finish())
    assert result["restored"] is False
    assert len(w)==before
    assert d["temperature"]==32


def test_pause_rejects_invalid_duration(monkeypatch):
    p,r,d,s,w=setup(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        run(p.activate(25))
    assert exc.value.status_code==400
    assert not w


def test_expired_pause_restores(monkeypatch):
    p,r,d,s,w=setup(monkeypatch)
    run(p.activate(6))
    r["expires_at"]=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    run(p.reconcile())
    assert r["status"]=="idle" and d["temperature"]==37


def test_pause_preserves_original_low_range(monkeypatch):
    p,r,d,s,w=setup(monkeypatch,initial=26,initial_range="low")
    run(p.activate(3))
    run(p.finish())
    assert s["range"]=="low" and d["temperature"]==26
