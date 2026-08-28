from __future__ import annotations

from typing import Any

from . import pool

POOL_SUPPLY_VOLTAGE_V = 220.0
POOL_POWER_FACTOR_ASSUMPTION = 1.0
_BASE_POOL_STATE_FROM_DISCOVERY = pool.pool_state_from_discovery


def _strict_power_sensor(states: list[dict[str, Any]], context_tokens: set[str]) -> dict[str, Any] | None:
    """Return only a genuine numeric W/kW sensor.

    AquaTemp exposes a binary ``Power`` entity with device_class=power. It is an
    on/off status, not electrical power, and must never be presented to Energy AI
    as kW telemetry.
    """
    candidates: list[tuple[int, dict[str, Any]]] = []
    for entity in states:
        entity_id = str(entity.get("entity_id") or "")
        if not entity_id.startswith("sensor."):
            continue
        attrs = entity.get("attributes") or {}
        unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
        if unit not in {"w", "kw"}:
            continue
        try:
            float(entity.get("state"))
        except (TypeError, ValueError):
            continue
        text = pool._text(entity)
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


def _pool_state_with_current_power_estimate(discovery: dict[str, Any]) -> dict[str, Any]:
    """Fill missing pool power from the verified O08 compressor-current register.

    The installed heat pump is single-phase 220 V. Without a true W/kW sensor,
    U*I therefore provides a useful apparent-power proxy. We intentionally use
    power factor 1.0 rather than hiding an unverified correction factor. The
    resulting value is marked estimated/low-confidence because compressor current
    need not equal total line current and auxiliary fan/control loads are not
    separately measured.
    """
    state = _BASE_POOL_STATE_FROM_DISCOVERY(discovery)
    measured_kw = pool._finite_number(state.get("electrical_power_kw"))
    current_a = pool._finite_number(state.get("compressor_current_a"))

    if measured_kw is not None:
        state["electrical_power_source"] = "measured_w_kw_sensor"
        state["electrical_power_estimated"] = False
        state["electrical_power_confidence"] = "high"
        state["electrical_power_estimate"] = None
        effective_kw = measured_kw
    elif current_a is not None and current_a >= 0.0:
        effective_kw = round(
            POOL_SUPPLY_VOLTAGE_V * current_a * POOL_POWER_FACTOR_ASSUMPTION / 1000.0,
            3,
        )
        state["electrical_power_kw"] = effective_kw
        state["electrical_power_source"] = "estimated_from_o08_compressor_current"
        state["electrical_power_estimated"] = True
        state["electrical_power_confidence"] = "low"
        state["electrical_power_estimate"] = {
            "formula": "voltage_v * compressor_current_a * power_factor / 1000",
            "supply_voltage_v": POOL_SUPPLY_VOLTAGE_V,
            "compressor_current_a": current_a,
            "power_factor_assumption": POOL_POWER_FACTOR_ASSUMPTION,
            "apparent_power_proxy_kva": round(POOL_SUPPLY_VOLTAGE_V * current_a / 1000.0, 3),
            "limitations": (
                "O08 compressor current is used as a proxy for appliance input current; "
                "fan/control loads and true AC power factor are not separately measured."
            ),
        }
    else:
        effective_kw = None
        state["electrical_power_source"] = "unavailable"
        state["electrical_power_estimated"] = False
        state["electrical_power_confidence"] = "none"
        state["electrical_power_estimate"] = None

    energy = state.setdefault("energy", {})
    energy["current_load_kw"] = effective_kw
    energy["current_load_source"] = state["electrical_power_source"]
    energy["current_load_estimated"] = bool(state["electrical_power_estimated"])
    energy["current_load_confidence"] = state["electrical_power_confidence"]
    return state


_pool_state_with_current_power_estimate._pool_installation_power_patch = True  # type: ignore[attr-defined]


def install_pool_installation_profile() -> dict[str, Any]:
    """Apply mappings verified against the installed AquaTemp entity surface.

    The installation exposes both ``Compressor current Detect [T07]`` and the
    actual measured ``Compressor current [O08]``. T07 is a detection/threshold
    parameter and must not be used as running compressor current.
    """
    pool.DIAGNOSTIC_HINTS["compressor_current_a"] = (
        "[o08]",
        ("compressor current [o08]", "compressor current"),
    )
    pool._power_sensor = _strict_power_sensor
    if not getattr(pool.pool_state_from_discovery, "_pool_installation_power_patch", False):
        pool.pool_state_from_discovery = _pool_state_with_current_power_estimate
    return {
        "installed": True,
        "compressor_current_register": "O08",
        "electrical_power_requirement": "numeric_sensor_with_w_or_kw_unit_preferred",
        "binary_power_entity_is_status_only": True,
        "fallback_power_estimate": {
            "enabled": True,
            "source": "O08_compressor_current",
            "supply_voltage_v": POOL_SUPPLY_VOLTAGE_V,
            "power_factor_assumption": POOL_POWER_FACTOR_ASSUMPTION,
            "confidence": "low",
        },
    }
