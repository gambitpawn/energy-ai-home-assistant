from __future__ import annotations

from typing import Any

from . import pool


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
    return {
        "installed": True,
        "compressor_current_register": "O08",
        "electrical_power_requirement": "numeric_sensor_with_w_or_kw_unit",
        "binary_power_entity_is_status_only": True,
    }
