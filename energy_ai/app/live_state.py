from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import websockets

from .ha import _normalize_value


LIVE_FIELDS: dict[str, tuple[str | None, str | None]] = {
    "pv_power_kw": ("pv_power", "kW"),
    "house_load_kw": ("house_load", "kW"),
    "grid_power_kw": ("grid_power", "kW"),
    "battery_power_kw": ("battery_power", "kW"),
    "battery_soc_pct": ("battery_soc", "%"),
    "ev_power_kw": ("ev_power", "kW"),
    "ev_connected": ("ev_connected", None),
    "ev_soc_pct": ("ev_soc", "%"),
    "ev_target_soc_pct": ("ev_target_soc", "%"),
    "ev_ready_by": ("ev_ready_by", None),
    # Polestar charging status has no dedicated add-on option yet. It is bound
    # only when Home Assistant exposes exactly one unambiguous Polestar sensor.
    "ev_charging_status": (None, None),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ws_url(ha: Any) -> str:
    parsed = urlparse(ha.base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    if getattr(ha, "auth_mode", None) == "supervisor":
        return f"{scheme}://{parsed.netloc}/core/websocket"
    return f"{scheme}://{parsed.netloc}/api/websocket"


class LiveStateCache:
    def __init__(self, cfg: dict[str, Any], ha: Any):
        self.cfg = cfg
        self.ha = ha
        entities = cfg.get("entities") or {}
        self.entity_to_field: dict[str, tuple[str, str | None]] = {}
        self.field_to_entity: dict[str, str] = {}
        self.mapping_origin: dict[str, str] = {}
        for output_field, (config_key, target_unit) in LIVE_FIELDS.items():
            if not config_key:
                continue
            entity_id = entities.get(config_key)
            if entity_id:
                self._bind(output_field, str(entity_id), target_unit, "configured")
        self.values: dict[str, Any] = {field: None for field in LIVE_FIELDS}
        self.source_updated: dict[str, str | None] = {field: None for field in LIVE_FIELDS}
        self.connected = False
        self.started_at: str | None = None
        self.connected_at: str | None = None
        self.last_event_at: str | None = None
        self.last_error: str | None = None
        self.bootstrap_at: str | None = None
        self.bootstrap_matched = 0
        self.reconnects = 0
        self.running = False

    def _bind(self, field: str, entity_id: str, target_unit: str | None, origin: str) -> None:
        old = self.field_to_entity.get(field)
        if old and old != entity_id:
            self.entity_to_field.pop(old, None)
        self.field_to_entity[field] = entity_id
        self.entity_to_field[entity_id] = (field, target_unit)
        self.mapping_origin[field] = origin

    def seed(self, state: Any | None) -> None:
        """Temporary fallback until the websocket get_states bootstrap completes."""
        if state is None:
            return
        for field in LIVE_FIELDS:
            item = getattr(state, field, None)
            if item is None:
                continue
            available = bool(getattr(item, "available", False))
            self.values[field] = getattr(item, "state", None) if available else None
            self.source_updated[field] = getattr(item, "last_updated", None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "transport": "home_assistant_websocket",
            "connected": self.connected,
            "started_at": self.started_at,
            "connected_at": self.connected_at,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "reconnects": self.reconnects,
            "bootstrap_at": self.bootstrap_at,
            "bootstrap_matched": self.bootstrap_matched,
            "configured_entities": sorted(self.entity_to_field),
            "field_entities": dict(self.field_to_entity),
            "mapping_origin": dict(self.mapping_origin),
            "values": dict(self.values),
            "source_updated": dict(self.source_updated),
            "served_at": _now(),
        }

    def _apply_new_state(self, entity_id: str, new_state: dict[str, Any] | None) -> None:
        spec = self.entity_to_field.get(entity_id)
        if spec is None:
            return
        output_field, target_unit = spec
        if not new_state:
            self.values[output_field] = None
            self.source_updated[output_field] = None
            return
        raw = new_state.get("state")
        attrs = new_state.get("attributes") or {}
        available = raw not in (None, "unknown", "unavailable", "")
        self.values[output_field] = _normalize_value(raw, attrs.get("unit_of_measurement"), target_unit) if available else None
        self.source_updated[output_field] = new_state.get("last_updated") or new_state.get("last_changed")

    @staticmethod
    def _entity_text(state: dict[str, Any]) -> tuple[str, str, str, str]:
        entity_id = str(state.get("entity_id") or "")
        attrs = state.get("attributes") or {}
        friendly = str(attrs.get("friendly_name") or "")
        unit = str(attrs.get("unit_of_measurement") or "").lower()
        device_class = str(attrs.get("device_class") or "").lower()
        return entity_id, f"{entity_id} {friendly}".lower(), unit, device_class

    def _auto_bind_ev_entities(self, states: list[dict[str, Any]]) -> None:
        """Bind unique EV sources only when explicit configuration is absent or missing.

        Zaptec remains authoritative for charger power/status. Polestar is preferred
        for vehicle SOC and vehicle-side charging status. No candidate is selected
        when more than one equally credible vehicle/charger exists.
        """
        present = {str(s.get("entity_id") or "") for s in states}
        candidates: dict[str, list[str]] = {
            "ev_power_kw": [],
            "ev_connected": [],
            "ev_soc_pct": [],
            "ev_charging_status": [],
        }
        for state in states:
            entity_id, text, unit, dc = self._entity_text(state)
            if not entity_id:
                continue
            is_zaptec = "zaptec" in text or "zap" in text
            is_polestar = "polestar" in text
            if is_zaptec:
                if any(k in text for k in ("total_charge_power", "total charge power", "laddeffekt", "charge power")) and (dc == "power" or unit in {"w", "kw"}):
                    candidates["ev_power_kw"].append(entity_id)
                if any(k in text for k in ("charger_operation_mode", "charger operation mode", "laddstatus", "operation mode")):
                    candidates["ev_connected"].append(entity_id)
            if is_polestar:
                if any(k in text for k in ("battery_charge_level", "battery level")) and (dc == "battery" or unit == "%"):
                    candidates["ev_soc_pct"].append(entity_id)
                if "charging_status" in text or "charging status" in text:
                    if "connection" not in text:
                        candidates["ev_charging_status"].append(entity_id)

        targets = {
            "ev_power_kw": "kW",
            "ev_connected": None,
            "ev_soc_pct": "%",
            "ev_charging_status": None,
        }
        for field, ids in candidates.items():
            unique = sorted(set(ids))
            current = self.field_to_entity.get(field)
            # Explicit/configured source wins while it still exists in HA.
            if current and current in present and self.mapping_origin.get(field) == "configured":
                continue
            if len(unique) == 1:
                origin = "auto_zaptec_unique" if field in {"ev_power_kw", "ev_connected"} else "auto_polestar_unique"
                self._bind(field, unique[0], targets[field], origin)

    def _bootstrap_states(self, states: list[dict[str, Any]]) -> int:
        self._auto_bind_ev_entities(states)
        matched = 0
        seen: set[str] = set()
        for state in states:
            entity_id = str(state.get("entity_id") or "")
            if entity_id not in self.entity_to_field:
                continue
            self._apply_new_state(entity_id, state)
            matched += 1
            seen.add(entity_id)
        # A mapped entity missing from get_states is not a valid live source.
        for entity_id, (field, _target_unit) in self.entity_to_field.items():
            if entity_id not in seen:
                self.values[field] = None
                self.source_updated[field] = None
        self.bootstrap_at = _now()
        self.bootstrap_matched = matched
        return matched

    async def _subscribe_once(self) -> None:
        if not self.ha.token:
            raise RuntimeError("No Home Assistant API token is available for live-state websocket")
        async with websockets.connect(
            _ws_url(self.ha),
            open_timeout=self.ha.timeout,
            close_timeout=3,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected Home Assistant websocket hello: {hello}")
            await ws.send(json.dumps({"type": "auth", "access_token": self.ha.token}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant websocket authentication failed: {auth}")

            await ws.send(json.dumps({"id": 1, "type": "get_states"}))
            current = json.loads(await ws.recv())
            if current.get("type") != "result" or not current.get("success") or not isinstance(current.get("result"), list):
                raise RuntimeError(f"Could not bootstrap Home Assistant states: {current}")
            self._bootstrap_states(current["result"])

            await ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"}))
            result = json.loads(await ws.recv())
            if result.get("type") != "result" or not result.get("success"):
                raise RuntimeError(f"Could not subscribe to Home Assistant state_changed: {result}")
            self.connected = True
            self.connected_at = _now()
            self.last_error = None
            while self.running:
                msg = json.loads(await ws.recv())
                if msg.get("type") != "event":
                    continue
                event = msg.get("event") or {}
                data = event.get("data") or {}
                entity_id = str(data.get("entity_id") or "")
                if entity_id in self.entity_to_field:
                    self._apply_new_state(entity_id, data.get("new_state"))
                    self.last_event_at = _now()

    async def run(self) -> None:
        self.running = True
        self.started_at = _now()
        backoff = 1.0
        while self.running:
            try:
                await self._subscribe_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = repr(exc)
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)
        self.connected = False

    def stop(self) -> None:
        self.running = False
