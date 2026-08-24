from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
import json
import os
import socket

import httpx
import websockets

from .component_registry import component_power_entities
from .models import EnergyState, StateValue


def _normalize_ha_api_url(raw_url: str) -> str:
    url = (raw_url or "").strip().rstrip("/")
    if not url:
        url = "http://homeassistant.local"
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/api") or path == "/api":
        return url
    return f"{url}/api"


def _normalize_value(raw: Any, source_unit: str | None, target_unit: str | None) -> Any:
    if raw in (None, "unknown", "unavailable", ""):
        return None
    if target_unit is None:
        return raw
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return raw
    unit = (source_unit or "").strip().lower()
    if target_unit == "kW":
        if unit == "w": return value / 1000.0
        if unit == "kw": return value
    if target_unit == "%": return value
    if target_unit == "öre/kWh":
        if unit in {"sek/kwh", "kr/kwh"}: return value * 100.0
        if unit in {"öre/kwh", "ore/kwh"}: return value
        return value
    return value


class HomeAssistantClient:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        supervisor_token = os.getenv("SUPERVISOR_TOKEN") or ""
        fallback_token = os.getenv("HA_ACCESS_TOKEN") or ""
        if supervisor_token:
            self.raw_base_url = "http://supervisor/core/api"
            self.base_url = self.raw_base_url
            self.token = supervisor_token
            self.auth_mode = "supervisor"
        else:
            self.raw_base_url = os.getenv("HA_BASE_URL") or "http://homeassistant.local"
            self.base_url = _normalize_ha_api_url(self.raw_base_url)
            self.token = fallback_token
            self.auth_mode = "long_lived_token" if fallback_token else "none"
        self.entities = cfg.get("entities", {})
        self.timeout = 10.0

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        if not self.token: return {}
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _websocket_url(self) -> str:
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/api/websocket"

    async def system_config(self) -> dict[str, Any]:
        if not self.token: raise RuntimeError("No Home Assistant API token is available")
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.base_url}/config", headers=self._headers(), timeout=self.timeout)
            response.raise_for_status(); data = response.json()
        return {"latitude":data.get("latitude"),"longitude":data.get("longitude"),"elevation":data.get("elevation"),"time_zone":data.get("time_zone"),"unit_system":data.get("unit_system")}

    async def diagnostics(self) -> dict[str, Any]:
        parsed=urlparse(self.base_url); host=parsed.hostname; port=parsed.port or (443 if parsed.scheme=="https" else 80)
        result={"configured_base_url":self.raw_base_url,"effective_api_base_url":self.base_url,"auth_mode":self.auth_mode,"token_present":bool(self.token),"scheme":parsed.scheme,"host":host,"port":port,"dns":None,"tcp":None,"http":None}
        if host:
            try: result["dns"]=socket.gethostbyname_ex(host)
            except Exception as exc: result["dns"]={"error":repr(exc)}
            try:
                _,writer=await __import__("asyncio").wait_for(__import__("asyncio").open_connection(host,port),timeout=5.0); writer.close(); await writer.wait_closed(); result["tcp"]="ok"
            except Exception as exc: result["tcp"]={"error":repr(exc)}
        if self.token:
            try:
                async with httpx.AsyncClient() as client: response=await client.get(f"{self.base_url}/",headers=self._headers(),timeout=self.timeout)
                result["http"]={"status_code":response.status_code,"content_type":response.headers.get("content-type"),"body_preview":response.text[:300]}
            except Exception as exc: result["http"]={"error":repr(exc)}
        return result

    async def all_states(self) -> list[dict[str, Any]]:
        if not self.token: raise RuntimeError("No Home Assistant API token is available")
        async with httpx.AsyncClient() as client:
            response=await client.get(f"{self.base_url}/states",headers=self._headers(),timeout=self.timeout); response.raise_for_status(); return response.json()

    async def nordpool_config_entry_id(self) -> str:
        if not self.token: raise RuntimeError("No Home Assistant API token is available")
        async with websockets.connect(self._websocket_url(), open_timeout=self.timeout, close_timeout=3) as ws:
            hello=json.loads(await ws.recv())
            if hello.get("type") != "auth_required": raise RuntimeError(f"Unexpected websocket hello: {hello}")
            await ws.send(json.dumps({"type":"auth","access_token":self.token})); auth=json.loads(await ws.recv())
            if auth.get("type") != "auth_ok": raise RuntimeError(f"Websocket authentication failed: {auth}")
            await ws.send(json.dumps({"id":1,"type":"config_entries/get","domain":"nordpool"})); msg=json.loads(await ws.recv())
            if not msg.get("success"): raise RuntimeError(f"Could not read Nord Pool config entries: {msg}")
            entries=msg.get("result") or []
            if not entries: raise RuntimeError("No Nord Pool config entry found")
            entry_id=entries[0].get("entry_id") or entries[0].get("id")
            if not entry_id: raise RuntimeError(f"Nord Pool config entry has no id: {entries[0]}")
            return str(entry_id)

    async def nordpool_prices_15m(self, date: str, area: str = "SE4", currency: str = "SEK") -> list[dict[str, Any]]:
        entry_id=await self.nordpool_config_entry_id(); payload={"config_entry":entry_id,"date":date,"areas":[area],"currency":currency,"resolution":15}
        async with httpx.AsyncClient() as client:
            response=await client.post(f"{self.base_url}/services/nordpool/get_price_indices_for_date?return_response",headers=self._headers(),json=payload,timeout=20.0); response.raise_for_status(); data=response.json()
        service_response=data.get("service_response",data); rows=service_response.get(area)
        if rows is None and isinstance(service_response,dict) and len(service_response)==1: rows=next(iter(service_response.values()))
        if not isinstance(rows,list): raise RuntimeError(f"Unexpected Nord Pool response: {service_response}")
        return [{"start":r["start"],"end":r["end"],"source_price_per_mwh":float(r["price"]),"currency":currency,"price_ore_kwh":float(r["price"])/10.0} for r in rows]

    async def _get_entity(self, client:httpx.AsyncClient, entity_id:str|None, target_unit:str|None=None) -> StateValue:
        if not entity_id: return StateValue(entity_id=None,available=False,normalized_unit=target_unit)
        if not self.token: return StateValue(entity_id=entity_id,available=False,normalized_unit=target_unit)
        try:
            response=await client.get(f"{self.base_url}/states/{entity_id}",headers=self._headers(),timeout=self.timeout)
            if response.status_code==404: return StateValue(entity_id=entity_id,available=False,normalized_unit=target_unit)
            response.raise_for_status(); data=response.json(); raw=data.get("state"); available=raw not in (None,"unknown","unavailable",""); attrs=data.get("attributes",{}) or {}; source_unit=attrs.get("unit_of_measurement"); state=_normalize_value(raw,source_unit,target_unit) if available else None
            return StateValue(entity_id=entity_id,state=state,available=available,last_updated=data.get("last_updated"),source_unit=source_unit,normalized_unit=target_unit or source_unit)
        except Exception:
            return StateValue(entity_id=entity_id,available=False,normalized_unit=target_unit)

    async def snapshot(self) -> EnergyState:
        keys={"pv_power_kw":("pv_power","kW"),"house_load_kw":("house_load","kW"),"grid_power_kw":("grid_power","kW"),"battery_power_kw":("battery_power","kW"),"battery_soc_pct":("battery_soc","%"),"spot_price_ore_kwh":("spot_price","öre/kWh"),"sauna_reserve":("sauna_reserve",None),"sauna_reserve_until":("sauna_reserve_until",None),"ev_mode":("ev_mode",None),"ev_connected":("ev_connected",None),"ev_soc_pct":("ev_soc","%"),"ev_target_soc_pct":("ev_target_soc","%"),"ev_ready_by":("ev_ready_by",None),"ev_power_kw":("ev_power","kW"),"demand_tariff_enabled":("demand_tariff_enabled",None),"import_power_target_kw":("import_power_target_kw","kW"),"export_power_target_kw":("export_power_target_kw","kW")}
        async with httpx.AsyncClient() as client:
            values={output_key:await self._get_entity(client,self.entities.get(config_key),target_unit) for output_key,(config_key,target_unit) in keys.items()}
            component_values={cid:await self._get_entity(client,entity_id,"kW") for cid,entity_id in component_power_entities(self.cfg).items()}
        return EnergyState(collected_at=datetime.now(timezone.utc).isoformat(),load_components=component_values,**values)
