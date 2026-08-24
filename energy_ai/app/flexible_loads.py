from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from .db import DB_PATH


def _parse_ts(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _state_value(item: Any) -> float | None:
    try:
        if not isinstance(item, dict) or not item.get("available"):
            return None
        return float(item.get("state"))
    except (TypeError, ValueError):
        return None


def _latest_raw_payload() -> dict[str, Any]:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT payload_json FROM raw_state ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}


def _recent_15m(hours: int = 4) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? ORDER BY bucket_start ASC", (cutoff,)).fetchall()
    out = []
    for ts, payload_json in rows:
        try:
            p = json.loads(payload_json)
            means = p.get("mean") or {}
            load = means.get("house_load_kw")
            if load is None:
                continue
            out.append({"ts": _parse_ts(ts), "house_load_kw": float(load)})
        except Exception:
            continue
    return out


def ev_state(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = _latest_raw_payload()
    power = _state_value(raw.get("ev_power_kw"))
    connected_item = raw.get("ev_connected") or {}
    mode = connected_item.get("state") if isinstance(connected_item, dict) and connected_item.get("available") else None
    mode_norm = str(mode).strip().lower() if mode is not None else None

    connected = None
    charging_from_mode = False
    if mode_norm is not None:
        if mode_norm in {"connected_charging", "connected_requesting", "connected_finished", "connected", "charging", "on", "true", "1", "yes"}:
            connected = True
        elif mode_norm in {"disconnected", "off", "false", "0", "no"}:
            connected = False
        charging_from_mode = mode_norm in {"connected_charging", "charging"}

    active = bool((power is not None and power > 0.25) or charging_from_mode)
    return {
        "configured": bool((cfg.get("entities") or {}).get("ev_power")),
        "power_kw": round(power, 4) if power is not None else None,
        "connected": connected,
        "charger_mode": mode_norm,
        "active": active,
        "source": "home_assistant_zaptec" if power is not None or mode_norm is not None else "unavailable",
    }


def sauna_state(cfg: dict[str, Any]) -> dict[str, Any]:
    policy = (cfg.get("policy") or {}).get("sauna") or {}
    nominal = float(policy.get("nominal_peak_kw", 6.0))
    threshold = float(policy.get("detection_step_kw", 4.0))
    rows = _recent_15m(4)
    ev = ev_state(cfg)
    ev_now = float(ev.get("power_kw") or 0.0)
    if len(rows) < 5:
        return {"active": False, "confidence": "insufficient_history", "nominal_peak_kw": nominal, "detected_start": None, "excess_kw": 0.0}

    net = [max(0.0, float(r["house_load_kw"]) - (ev_now if i == len(rows) - 1 else 0.0)) for i, r in enumerate(rows)]
    detected_idx = None
    detected_excess = 0.0
    for i in range(4, len(rows)):
        baseline = median(net[max(0, i - 4):i])
        step = net[i] - baseline
        if step >= threshold:
            detected_idx = i
            detected_excess = step
    if detected_idx is None:
        return {"active": False, "confidence": "none", "nominal_peak_kw": nominal, "detected_start": None, "excess_kw": 0.0}

    start = rows[detected_idx]["ts"]
    age_min = (datetime.now(timezone.utc) - start).total_seconds() / 60.0
    active = age_min <= 180
    confidence = "high" if detected_excess >= nominal * 0.75 else "medium"
    return {
        "active": active,
        "confidence": confidence if active else "expired",
        "nominal_peak_kw": nominal,
        "detected_start": start.isoformat(),
        "age_minutes": round(age_min, 1),
        "excess_kw": round(detected_excess, 3),
        "method": "house_load_step_signature",
    }


def flexible_load_forecast(cfg: dict[str, Any], starts: list[datetime]) -> dict[str, Any]:
    ev = ev_state(cfg)
    sauna = sauna_state(cfg)
    ev_power = float(ev.get("power_kw") or 0.0) if ev.get("active") else 0.0
    sauna_start = _parse_ts(sauna["detected_start"]) if sauna.get("active") and sauna.get("detected_start") else None
    nominal = float(sauna.get("nominal_peak_kw") or 6.0)
    now = datetime.now(timezone.utc)

    rows = []
    for stamp in starts:
        lead_h = max(0.0, (stamp - now).total_seconds() / 3600.0)
        ev_kw = ev_power if lead_h <= 2.0 else 0.0

        sauna_kw = 0.0
        if sauna_start is not None:
            age_h = (stamp - sauna_start).total_seconds() / 3600.0
            if 0.0 <= age_h < 1.0:
                sauna_kw = nominal
            elif 1.0 <= age_h < 2.0:
                sauna_kw = nominal * 0.35
            elif 2.0 <= age_h < 3.0:
                sauna_kw = nominal * 0.20

        rows.append({"start": stamp.isoformat(), "ev_forecast_kw": round(ev_kw, 4), "sauna_forecast_kw": round(sauna_kw, 4), "flexible_load_forecast_kw": round(ev_kw + sauna_kw, 4)})

    return {"ev": ev, "sauna": sauna, "rows": rows, "provisional": True}


async def discover_flexible_load_entities(ha_client) -> dict[str, Any]:
    states = await ha_client.all_states()
    ranked = {"ev_power": [], "ev_connected": [], "sauna_power": []}
    for entity in states:
        entity_id = str(entity.get("entity_id") or "")
        attrs = entity.get("attributes") or {}
        name = str(attrs.get("friendly_name") or "")
        unit = str(attrs.get("unit_of_measurement") or "").lower()
        device_class = str(attrs.get("device_class") or "").lower()
        state = str(entity.get("state") or "").lower()
        text = f"{entity_id} {name}".lower()
        row = {"entity_id": entity_id, "friendly_name": name, "state": entity.get("state"), "unit": attrs.get("unit_of_measurement"), "device_class": device_class}

        is_zaptec = "zaptec" in text or "zap361270" in text
        looks_non_ev = any(k in text for k in ("solinteg", "inverter", "battery", "battctrl", "discharge_power_target"))
        ev_score = 0
        if is_zaptec: ev_score += 20
        if any(k in text for k in ("total_charge_power", "total charge power", "laddeffekt", "charge power", "charging power")): ev_score += 15
        elif any(k in text for k in ("charger", "charge", "charging", "ladd")): ev_score += 4
        if device_class == "power" or unit in {"w", "kw"}: ev_score += 6
        if looks_non_ev: ev_score -= 30
        if is_zaptec and ev_score >= 20 and (device_class == "power" or unit in {"w", "kw"}):
            ranked["ev_power"].append({**row, "score": ev_score})

        conn_score = 0
        if is_zaptec: conn_score += 20
        if "charger_mode" in text or "charger mode" in text or "laddstatus" in text: conn_score += 20
        if state in {"connected_charging", "connected_requesting", "connected_finished", "disconnected"}: conn_score += 20
        if any(k in text for k in ("connected", "connection", "plug", "charging", "status", "mode", "ladd")): conn_score += 5
        if is_zaptec and entity_id.startswith(("binary_sensor.", "sensor.")) and conn_score >= 20:
            ranked["ev_connected"].append({**row, "score": conn_score})

        sauna_score = 0
        if any(k in text for k in ("sauna", "bastu")): sauna_score += 10
        if device_class == "power" or unit in {"w", "kw"}: sauna_score += 6
        if sauna_score >= 10:
            ranked["sauna_power"].append({**row, "score": sauna_score})

    for key in ranked:
        ranked[key].sort(key=lambda r: (-r["score"], r["entity_id"]))
        ranked[key] = ranked[key][:30]
    return ranked
