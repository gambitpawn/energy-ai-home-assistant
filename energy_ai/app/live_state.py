from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import websockets

from .ha import _normalize_value


LIVE_FIELDS: dict[str, tuple[str, str | None]] = {
    "pv_power_kw": ("pv_power", "kW"),
    "house_load_kw": ("house_load", "kW"),
    "grid_power_kw": ("grid_power", "kW"),
    "battery_power_kw": ("battery_power", "kW"),
    "battery_soc_pct": ("battery_soc", "%"),
    "ev_power_kw": ("ev_power", "kW"),
    "ev_connected": ("ev_connected", None),
    "ev_soc_pct": ("ev_soc", "%"),
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
        for output_field, (config_key, target_unit) in LIVE_FIELDS.items():
            entity_id = entities.get(config_key)
            if entity_id:
                entity_id = str(entity_id)
                self.entity_to_field[entity_id] = (output_field, target_unit)
                self.field_to_entity[output_field] = entity_id
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

    def _bootstrap_states(self, states: list[dict[str, Any]]) -> int:
        matched = 0
        seen: set[str] = set()
        for state in states:
            entity_id = str(state.get("entity_id") or "")
            if entity_id not in self.entity_to_field:
                continue
            self._apply_new_state(entity_id, state)
            matched += 1
            seen.add(entity_id)
        # A configured entity missing from get_states is not a valid live source.
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

            # Always bootstrap from Home Assistant's current state table before
            # declaring the cache live. This prevents stale collector seed values
            # from being presented as current simply because no state_changed event
            # has happened since the add-on started.
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
