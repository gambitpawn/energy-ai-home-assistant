from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .db import DB_PATH

DEFAULT_POOL_NAME = "Poolstyrning"

# AquaTemp exposes a large register surface. Keep the first pool contract narrow
# and explicit: these are the values that are useful to an operator or to later
# energy optimization. Register codes are preferred because translations and
# friendly names vary between installations.
DIAGNOSTIC_HINTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "inlet_temperature_c": ("[t02]", ("inlet water temp",)),
    "outlet_temperature_c": ("[t03]", ("outlet water temp",)),
    "ambient_temperature_c": ("[t05]", ("ambient temp",)),
    "compressor_frequency_hz": ("[007]", ("comp. output frequency", "compressor frequency")),
    "compressor_current_a": ("[008]", ("compressor current",)),
    "fan_output_pct": ("[t08]", ("ac fan output",)),
    "fan_speed_rpm": ("[t17]", ("speed of fan motor1", "fan motor speed")),
    "flow_rate_hz": ("[t09]", ("flow rate input",)),
    "flow_switch": ("[s03]", ("flow switch",)),
    "pressure_sensor_bar": ("[t10]", ("pressure sensor",)),
    "heating_set_c": ("[r02]", ("heating set",)),
}


def _text(entity: dict[str, Any]) -> str:
    attrs = entity.get("attributes") or {}
    return f"{entity.get('entity_id') or ''} {attrs.get('friendly_name') or ''}".lower()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _finite_number(value: Any) -> float | None:
    if value in (None, "", "unknown", "unavailable", "Okänd", "okänd"):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _entity_view(entity: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entity:
        return None
    attrs = entity.get("attributes") or {}
    return {
        "entity_id": entity.get("entity_id"),
        "friendly_name": attrs.get("friendly_name"),
        "state": entity.get("state"),
        "unit": attrs.get("unit_of_measurement"),
        "device_class": attrs.get("device_class"),
        "last_updated": entity.get("last_updated"),
        "attributes": attrs,
    }


def _climate_score(entity: dict[str, Any], target_name: str) -> int:
    entity_id = str(entity.get("entity_id") or "")
    if not entity_id.startswith("climate."):
        return -10_000
    attrs = entity.get("attributes") or {}
    friendly = str(attrs.get("friendly_name") or "")
    friendly_n = friendly.strip().lower()
    target_n = target_name.strip().lower()
    target_slug = _slug(target_name)
    score = 0
    if friendly_n == target_n:
        score += 100
    elif target_n and target_n in friendly_n:
        score += 60
    if target_slug and target_slug in entity_id.lower():
        score += 50
    if attrs.get("current_temperature") is not None:
        score += 15
    if attrs.get("temperature") is not None:
        score += 10
    if attrs.get("fan_modes"):
        score += 5
    if "pool" in f"{entity_id} {friendly_n}":
        score += 8
    return score


def _diagnostic_score(entity: dict[str, Any], code: str, phrases: tuple[str, ...], context_tokens: set[str]) -> int:
    text = _text(entity)
    score = 0
    if code and code in text:
        score += 100
    for phrase in phrases:
        if phrase in text:
            score += 45
    for token in context_tokens:
        if len(token) >= 4 and token in text:
            score += 8
    if str(entity.get("entity_id") or "").startswith("sensor."):
        score += 2
    if entity.get("state") not in (None, "", "unknown", "unavailable"):
        score += 2
    return score


def _power_sensor(states: list[dict[str, Any]], context_tokens: set[str]) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for entity in states:
        attrs = entity.get("attributes") or {}
        unit = str(attrs.get("unit_of_measurement") or "").lower()
        device_class = str(attrs.get("device_class") or "").lower()
        if device_class != "power" and unit not in {"w", "kw"}:
            continue
        text = _text(entity)
        score = 0
        if "power" in text or "effekt" in text:
            score += 10
        score += sum(8 for token in context_tokens if len(token) >= 4 and token in text)
        if "pool" in text or "aquatemp" in text:
            score += 12
        if score > 0:
            candidates.append((score, entity))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], str(item[1].get("entity_id") or "")))
    return candidates[0][1]


def discover_pool_entities_from_states(
    states: list[dict[str, Any]],
    *,
    target_name: str = DEFAULT_POOL_NAME,
    explicit_climate_entity: str | None = None,
    explicit_power_entity: str | None = None,
) -> dict[str, Any]:
    by_id = {str(item.get("entity_id") or ""): item for item in states if item.get("entity_id")}
    climate: dict[str, Any] | None = None
    if explicit_climate_entity:
        climate = by_id.get(str(explicit_climate_entity))
    if climate is None:
        ranked = sorted(
            ((_climate_score(entity, target_name), entity) for entity in states),
            key=lambda item: (-item[0], str(item[1].get("entity_id") or "")),
        )
        if ranked and ranked[0][0] > 0:
            climate = ranked[0][1]

    climate_view = _entity_view(climate)
    context_tokens: set[str] = {"aquatemp", "pool"}
    if climate:
        entity_id = str(climate.get("entity_id") or "")
        friendly = str((climate.get("attributes") or {}).get("friendly_name") or "")
        context_tokens.update(x for x in re.split(r"[^a-z0-9]+", entity_id.lower()) if x)
        context_tokens.update(x for x in re.split(r"[^a-z0-9]+", friendly.lower()) if x)
    context_tokens.update(x for x in re.split(r"[^a-z0-9]+", target_name.lower()) if x)

    diagnostics: dict[str, dict[str, Any] | None] = {}
    candidate_debug: dict[str, list[dict[str, Any]]] = {}
    for key, (code, phrases) in DIAGNOSTIC_HINTS.items():
        scored: list[tuple[int, dict[str, Any]]] = []
        for entity in states:
            if climate is entity:
                continue
            score = _diagnostic_score(entity, code, phrases, context_tokens)
            if score >= 40:
                scored.append((score, entity))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("entity_id") or "")))
        diagnostics[key] = _entity_view(scored[0][1]) if scored else None
        candidate_debug[key] = [
            {"score": score, **(_entity_view(entity) or {})}
            for score, entity in scored[:5]
        ]

    power_entity = by_id.get(str(explicit_power_entity)) if explicit_power_entity else None
    if power_entity is None:
        power_entity = _power_sensor(states, context_tokens)

    warnings: list[str] = []
    if climate is None:
        warnings.append("No pool climate entity could be identified. Configure or discover the Poolstyrning climate entity first.")
    if diagnostics.get("flow_rate_hz") is None and diagnostics.get("flow_switch") is None:
        warnings.append("No usable direct water-flow signal was discovered; filter health can only use a thermal proxy after calibration.")
    if diagnostics.get("pressure_sensor_bar") is not None:
        warnings.append("Pressure Sensor [T10] is exposed for diagnostics only and is not assumed to be pool-filter pressure.")

    return {
        "target_name": target_name,
        "climate": climate_view,
        "diagnostics": diagnostics,
        "electrical_power": _entity_view(power_entity),
        "warnings": warnings,
        "candidate_debug": candidate_debug,
    }


def _value(view: dict[str, Any] | None) -> float | None:
    return None if not view else _finite_number(view.get("state"))


def _bool_state(view: dict[str, Any] | None) -> bool | None:
    if not view:
        return None
    raw = str(view.get("state") or "").strip().lower()
    if raw in {"on", "true", "1", "yes", "open", "active"}:
        return True
    if raw in {"off", "false", "0", "no", "closed", "inactive"}:
        return False
    return None


def _normalize_power_kw(view: dict[str, Any] | None) -> float | None:
    if not view:
        return None
    value = _finite_number(view.get("state"))
    if value is None:
        return None
    unit = str(view.get("unit") or "").strip().lower()
    if unit == "w":
        return value / 1000.0
    if unit == "kw":
        return value
    return None


def _init_filter_tables() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.execute(
            '''CREATE TABLE IF NOT EXISTS pool_filter_baseline(
                   singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                   captured_at TEXT NOT NULL,
                   source TEXT NOT NULL,
                   metrics_json TEXT NOT NULL
               )'''
        )


def filter_baseline() -> dict[str, Any] | None:
    _init_filter_tables()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        row = c.execute(
            "SELECT captured_at,source,metrics_json FROM pool_filter_baseline WHERE singleton=1"
        ).fetchone()
    if not row:
        return None
    try:
        metrics = json.loads(row[2] or "{}")
    except Exception:
        metrics = {}
    return {"captured_at": str(row[0]), "source": str(row[1]), "metrics": metrics}


def mark_filter_cleaned(state: dict[str, Any]) -> dict[str, Any]:
    flow = _finite_number(state.get("flow_rate_hz"))
    compressor_hz = _finite_number(state.get("compressor_frequency_hz"))
    delta_t = _finite_number(state.get("water_delta_t_c"))
    ambient = _finite_number(state.get("ambient_temperature_c"))

    if flow is not None and flow > 0.1:
        source = "flow_rate"
        metrics = {"flow_rate_hz": flow}
    elif (
        bool(state.get("compressor_running"))
        and compressor_hz is not None
        and compressor_hz >= 10.0
        and delta_t is not None
        and delta_t > 0.1
    ):
        source = "thermal_proxy"
        metrics = {
            "delta_t_per_compressor_hz": delta_t / compressor_hz,
            "compressor_frequency_hz": compressor_hz,
            "ambient_temperature_c": ambient,
        }
    else:
        return {
            "ok": False,
            "status": "baseline_not_captured",
            "reason": "Mark filter cleaned while water is circulating; preferably while the heat pump compressor is running.",
        }

    captured_at = datetime.now(timezone.utc).isoformat()
    _init_filter_tables()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT INTO pool_filter_baseline(singleton,captured_at,source,metrics_json)
               VALUES (1,?,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                 captured_at=excluded.captured_at,
                 source=excluded.source,
                 metrics_json=excluded.metrics_json''',
            (captured_at, source, json.dumps(metrics, sort_keys=True)),
        )
    return {"ok": True, "status": "baseline_captured", "captured_at": captured_at, "source": source, "metrics": metrics}


def evaluate_filter_health(state: dict[str, Any]) -> dict[str, Any]:
    flow_switch = state.get("flow_switch")
    compressor_running = bool(state.get("compressor_running"))
    if compressor_running and flow_switch is False:
        return {
            "status": "check_now",
            "severity": "critical",
            "message": "No water flow is indicated while the compressor is running. Check circulation and the pool filter now.",
            "basis": "flow_switch",
            "confidence": "high",
        }

    baseline = filter_baseline()
    if baseline is None:
        return {
            "status": "not_calibrated",
            "severity": "info",
            "message": "Filter condition is not calibrated yet. Mark the filter as cleaned after a fresh backwash/cleaning while water is circulating.",
            "basis": "none",
            "confidence": "none",
        }

    source = str(baseline.get("source") or "")
    metrics = baseline.get("metrics") or {}
    if source == "flow_rate":
        base_flow = _finite_number(metrics.get("flow_rate_hz"))
        current_flow = _finite_number(state.get("flow_rate_hz"))
        if base_flow and current_flow is not None and current_flow > 0:
            ratio = current_flow / base_flow
            if ratio < 0.80:
                return {
                    "status": "clean_filter",
                    "severity": "warning",
                    "message": "Water-flow signal is more than 20% below the clean-filter baseline. Backwash/clean the pool filter and inspect circulation.",
                    "basis": "flow_rate_vs_clean_baseline",
                    "confidence": "medium",
                    "ratio_to_clean_baseline": round(ratio, 3),
                }
            if ratio < 0.90:
                return {
                    "status": "check_filter",
                    "severity": "notice",
                    "message": "Water-flow signal is 10–20% below the clean-filter baseline. Check the pool filter soon.",
                    "basis": "flow_rate_vs_clean_baseline",
                    "confidence": "medium",
                    "ratio_to_clean_baseline": round(ratio, 3),
                }
            return {
                "status": "ok",
                "severity": "ok",
                "message": "Water-flow signal is close to the clean-filter baseline.",
                "basis": "flow_rate_vs_clean_baseline",
                "confidence": "medium",
                "ratio_to_clean_baseline": round(ratio, 3),
            }

    if source == "thermal_proxy" and compressor_running:
        base_proxy = _finite_number(metrics.get("delta_t_per_compressor_hz"))
        base_ambient = _finite_number(metrics.get("ambient_temperature_c"))
        compressor_hz = _finite_number(state.get("compressor_frequency_hz"))
        delta_t = _finite_number(state.get("water_delta_t_c"))
        ambient = _finite_number(state.get("ambient_temperature_c"))
        ambient_close = base_ambient is None or ambient is None or abs(ambient - base_ambient) <= 7.0
        if base_proxy and compressor_hz and compressor_hz >= 10.0 and delta_t is not None and delta_t > 0 and ambient_close:
            ratio = (delta_t / compressor_hz) / base_proxy
            if ratio > 1.50:
                return {
                    "status": "clean_filter",
                    "severity": "warning",
                    "message": "Temperature rise across the heat pump is much higher than the clean-filter baseline at comparable compressor load. Check water flow and backwash/clean the filter.",
                    "basis": "thermal_flow_proxy",
                    "confidence": "low",
                    "ratio_to_clean_baseline": round(ratio, 3),
                }
            if ratio > 1.25:
                return {
                    "status": "check_filter",
                    "severity": "notice",
                    "message": "Temperature rise across the heat pump is higher than the clean-filter baseline. Check water flow/filter if this persists.",
                    "basis": "thermal_flow_proxy",
                    "confidence": "low",
                    "ratio_to_clean_baseline": round(ratio, 3),
                }
            return {
                "status": "ok",
                "severity": "ok",
                "message": "Thermal flow proxy is close to the clean-filter baseline.",
                "basis": "thermal_flow_proxy",
                "confidence": "low",
                "ratio_to_clean_baseline": round(ratio, 3),
            }

    return {
        "status": "monitoring",
        "severity": "info",
        "message": "A clean-filter baseline exists, but current operating conditions are not comparable enough to assess the filter.",
        "basis": source or "baseline",
        "confidence": "none",
    }


def pool_state_from_discovery(discovery: dict[str, Any]) -> dict[str, Any]:
    climate = discovery.get("climate") or {}
    attrs = climate.get("attributes") or {}
    diagnostics = discovery.get("diagnostics") or {}

    inlet = _value(diagnostics.get("inlet_temperature_c"))
    outlet = _value(diagnostics.get("outlet_temperature_c"))
    ambient = _value(diagnostics.get("ambient_temperature_c"))
    compressor_hz = _value(diagnostics.get("compressor_frequency_hz"))
    compressor_current = _value(diagnostics.get("compressor_current_a"))
    fan_output = _value(diagnostics.get("fan_output_pct"))
    fan_speed = _value(diagnostics.get("fan_speed_rpm"))
    flow_rate = _value(diagnostics.get("flow_rate_hz"))
    flow_switch = _bool_state(diagnostics.get("flow_switch"))
    pressure = _value(diagnostics.get("pressure_sensor_bar"))
    heating_set = _value(diagnostics.get("heating_set_c"))

    current_temp = _finite_number(attrs.get("current_temperature"))
    if current_temp is None:
        current_temp = inlet
    target_temp = _finite_number(attrs.get("temperature"))
    if target_temp is None:
        target_temp = heating_set

    hvac_mode = str(climate.get("state") or "unknown") if climate else "unavailable"
    hvac_action = attrs.get("hvac_action")
    fan_mode = attrs.get("fan_mode")
    power_on = hvac_mode not in {"off", "unavailable", "unknown", ""}
    compressor_running = bool(
        (compressor_hz is not None and compressor_hz > 0.5)
        or (compressor_current is not None and compressor_current > 0.2)
        or str(hvac_action or "").lower() == "heating"
    )
    delta_t = None if inlet is None or outlet is None else outlet - inlet
    electrical_power_kw = _normalize_power_kw(discovery.get("electrical_power"))

    if not power_on:
        operating_state = "off"
    elif compressor_running:
        operating_state = "heating"
    elif target_temp is not None and current_temp is not None and target_temp > current_temp + 0.1:
        operating_state = "waiting_for_heat"
    else:
        operating_state = "idle"

    state = {
        "available": bool(climate),
        "climate_entity_id": climate.get("entity_id"),
        "current_temperature_c": current_temp,
        "target_temperature_c": target_temp,
        "hvac_mode": hvac_mode,
        "hvac_action": hvac_action,
        "fan_mode": fan_mode,
        "power_on": power_on,
        "operating_state": operating_state,
        "inlet_temperature_c": inlet,
        "outlet_temperature_c": outlet,
        "water_delta_t_c": delta_t,
        "ambient_temperature_c": ambient,
        "compressor_running": compressor_running,
        "compressor_frequency_hz": compressor_hz,
        "compressor_current_a": compressor_current,
        "fan_output_pct": fan_output,
        "fan_speed_rpm": fan_speed,
        "flow_rate_hz": flow_rate,
        "flow_switch": flow_switch,
        "pressure_sensor_bar": pressure,
        "pressure_sensor_role": "diagnostic_only_not_assumed_pool_filter_pressure",
        "electrical_power_kw": electrical_power_kw,
        "filter_baseline": filter_baseline(),
        "diagnostic_entities": {
            key: None if view is None else view.get("entity_id")
            for key, view in diagnostics.items()
        },
        "warnings": list(discovery.get("warnings") or []),
    }
    state["filter_health"] = evaluate_filter_health(state)
    state["energy"] = {
        "integration_stage": "read_only_pool_state_v1",
        "controllable_load": True,
        "smart_control_enabled": False,
        "current_load_kw": electrical_power_kw,
        "temperature_headroom_to_target_c": None if current_temp is None or target_temp is None else round(target_temp - current_temp, 3),
        "heating_now": compressor_running,
        "note": "Pool state is normalized for Energy AI; scheduling/control is intentionally not enabled in this release.",
    }
    return state


async def discover_pool_entities(ha_client, cfg: dict[str, Any], target_name: str = DEFAULT_POOL_NAME) -> dict[str, Any]:
    states = await ha_client.all_states()
    entities = cfg.get("entities") or {}
    return discover_pool_entities_from_states(
        states,
        target_name=target_name,
        explicit_power_entity=entities.get("pool_heat_pump_power"),
    )


async def read_pool_state(ha_client, cfg: dict[str, Any], target_name: str = DEFAULT_POOL_NAME) -> dict[str, Any]:
    discovery = await discover_pool_entities(ha_client, cfg, target_name)
    return pool_state_from_discovery(discovery)


def install_pool_routes(app: FastAPI, cfg: dict[str, Any], ha_client) -> None:
    @app.get("/pool/discover", tags=["pool"])
    async def pool_discover(name: str = DEFAULT_POOL_NAME):
        discovery = await discover_pool_entities(ha_client, cfg, name)
        return JSONResponse(discovery)

    @app.get("/pool/status", tags=["pool"])
    async def pool_status(name: str = DEFAULT_POOL_NAME):
        state = await read_pool_state(ha_client, cfg, name)
        return JSONResponse(state)

    @app.post("/pool/filter-cleaned", tags=["pool"])
    async def pool_filter_cleaned(name: str = DEFAULT_POOL_NAME):
        state = await read_pool_state(ha_client, cfg, name)
        result = mark_filter_cleaned(state)
        return JSONResponse({**result, "pool": state})
