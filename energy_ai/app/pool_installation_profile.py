from __future__ import annotations

from typing import Any

from . import pool

POOL_SUPPLY_VOLTAGE_V = 220.0
POOL_POWER_FACTOR_ASSUMPTION = 1.0
AQUATEMP_NATIVE_DEVICE_TOKEN = "289c6e4f1a5e"
_BASE_POOL_STATE_FROM_DISCOVERY = pool.pool_state_from_discovery
_BASE_MARK_FILTER_CLEANED = pool.mark_filter_cleaned


def _strict_power_sensor(states: list[dict[str, Any]], context_tokens: set[str]) -> dict[str, Any] | None:
    """Return only credible external numeric W/kW telemetry.

    The installed AquaTemp device exposes many diagnostics under the same native
    device token. Some of those can have W/kW-like units without representing
    whole-appliance mains consumption. Native AquaTemp diagnostics are therefore
    excluded from automatic measured-power discovery. A separately configured
    ``pool_heat_pump_power`` entity still bypasses this auto-discovery function.
    """
    candidates: list[tuple[int, dict[str, Any]]] = []
    for entity in states:
        entity_id = str(entity.get("entity_id") or "")
        entity_id_l = entity_id.lower()
        if not entity_id.startswith("sensor."):
            continue
        if AQUATEMP_NATIVE_DEVICE_TOKEN in entity_id_l:
            continue

        attrs = entity.get("attributes") or {}
        unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
        device_class = str(attrs.get("device_class") or "").strip().lower()
        if unit not in {"w", "kw"}:
            continue
        try:
            float(entity.get("state"))
        except (TypeError, ValueError):
            continue

        text = pool._text(entity)
        semantic_power = any(
            hint in text
            for hint in ("power", "effekt", "consumption", "förbruk", "heat pump", "värmepump")
        )
        if not semantic_power:
            continue

        score = 20 if device_class == "power" else 0
        if "power" in text or "effekt" in text:
            score += 20
        if "heat pump" in text or "värmepump" in text:
            score += 20
        score += sum(4 for token in context_tokens if len(token) >= 4 and token in text)
        if "pool" in text:
            score += 12
        candidates.append((score, entity))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], str(item[1].get("entity_id") or "")))
    return candidates[0][1]


def _pool_state_with_current_power_estimate(discovery: dict[str, Any]) -> dict[str, Any]:
    """Use measured W/kW when credible, otherwise estimate from O08 at 220 V.

    ``U * I`` is exposed as a low-confidence apparent-power proxy. It is also a
    safe fallback if a measured-power candidate reports zero while the compressor
    is plainly running at material frequency/current, which protects Energy AI
    from a stale or semantically wrong zero-valued W/kW entity.
    """
    state = _BASE_POOL_STATE_FROM_DISCOVERY(discovery)
    measured_kw = pool._finite_number(state.get("electrical_power_kw"))
    current_a = pool._finite_number(state.get("compressor_current_a"))
    compressor_hz = pool._finite_number(state.get("compressor_frequency_hz"))
    measured_view = discovery.get("electrical_power") or {}
    measured_entity_id = measured_view.get("entity_id")

    rejected_measurement: dict[str, Any] | None = None
    compressor_materially_running = bool(
        current_a is not None
        and current_a >= 0.8
        and compressor_hz is not None
        and compressor_hz >= 10.0
    )
    if measured_kw is not None and measured_kw <= 0.02 and compressor_materially_running:
        rejected_measurement = {
            "entity_id": measured_entity_id,
            "reported_kw": measured_kw,
            "reason": "zero_measured_power_while_o08_current_and_compressor_frequency_show_running",
        }
        measured_kw = None

    if measured_kw is not None:
        state["electrical_power_source"] = "measured_w_kw_sensor"
        state["electrical_power_entity_id"] = measured_entity_id
        state["electrical_power_estimated"] = False
        state["electrical_power_confidence"] = "high"
        state["electrical_power_estimate"] = None
        state["electrical_power_rejected_measurement"] = None
        effective_kw = measured_kw
    elif current_a is not None and current_a >= 0.0:
        effective_kw = round(
            POOL_SUPPLY_VOLTAGE_V * current_a * POOL_POWER_FACTOR_ASSUMPTION / 1000.0,
            3,
        )
        state["electrical_power_kw"] = effective_kw
        state["electrical_power_source"] = "estimated_from_o08_compressor_current"
        state["electrical_power_entity_id"] = None
        state["electrical_power_estimated"] = True
        state["electrical_power_confidence"] = "low"
        state["electrical_power_rejected_measurement"] = rejected_measurement
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
        state["electrical_power_entity_id"] = measured_entity_id
        state["electrical_power_estimated"] = False
        state["electrical_power_confidence"] = "none"
        state["electrical_power_estimate"] = None
        state["electrical_power_rejected_measurement"] = rejected_measurement

    energy = state.setdefault("energy", {})
    energy["current_load_kw"] = effective_kw
    energy["current_load_source"] = state["electrical_power_source"]
    energy["current_load_estimated"] = bool(state["electrical_power_estimated"])
    energy["current_load_confidence"] = state["electrical_power_confidence"]
    return state


_pool_state_with_current_power_estimate._pool_installation_power_patch = True  # type: ignore[attr-defined]


def _mark_filter_cleaned_with_refreshed_state(state: dict[str, Any]) -> dict[str, Any]:
    """Make the POST response reflect the baseline that was just persisted."""
    result = _BASE_MARK_FILTER_CLEANED(state)
    if result.get("ok"):
        state["filter_baseline"] = pool.filter_baseline()
        state["filter_health"] = pool.evaluate_filter_health(state)
    return result


_mark_filter_cleaned_with_refreshed_state._pool_filter_refresh_patch = True  # type: ignore[attr-defined]


def install_pool_installation_profile() -> dict[str, Any]:
    """Apply mappings and safeguards verified on the installed AquaTemp surface."""
    pool.DIAGNOSTIC_HINTS["compressor_current_a"] = (
        "[o08]",
        ("compressor current [o08]", "compressor current"),
    )
    pool._power_sensor = _strict_power_sensor
    if not getattr(pool.pool_state_from_discovery, "_pool_installation_power_patch", False):
        pool.pool_state_from_discovery = _pool_state_with_current_power_estimate
    if not getattr(pool.mark_filter_cleaned, "_pool_filter_refresh_patch", False):
        pool.mark_filter_cleaned = _mark_filter_cleaned_with_refreshed_state
    return {
        "installed": True,
        "compressor_current_register": "O08",
        "electrical_power_requirement": "explicit_or_credible_external_numeric_w_kw_sensor_preferred",
        "native_aquatemp_power_registers_auto_excluded": True,
        "binary_power_entity_is_status_only": True,
        "fallback_power_estimate": {
            "enabled": True,
            "source": "O08_compressor_current",
            "supply_voltage_v": POOL_SUPPLY_VOLTAGE_V,
            "power_factor_assumption": POOL_POWER_FACTOR_ASSUMPTION,
            "confidence": "low",
        },
    }
