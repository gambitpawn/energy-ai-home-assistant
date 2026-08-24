from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class LoadComponentSpec:
    id: str
    type: str
    source: str
    power_entity: str | None = None
    state_entity: str | None = None
    controllable: bool = False
    interruptible: bool = False
    max_power_kw: float | None = None
    thermal: bool = False
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _custom_specs(cfg: dict[str, Any]) -> list[LoadComponentSpec]:
    raw = ((cfg.get("components") or {}).get("custom") or [])
    out: list[LoadComponentSpec] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        out.append(LoadComponentSpec(
            id=str(item["id"]),
            type=str(item.get("type") or "generic"),
            source=str(item.get("source") or "home_assistant"),
            power_entity=item.get("power_entity"),
            state_entity=item.get("state_entity"),
            controllable=bool(item.get("controllable", False)),
            interruptible=bool(item.get("interruptible", False)),
            max_power_kw=float(item["max_power_kw"]) if item.get("max_power_kw") not in (None, "") else None,
            thermal=bool(item.get("thermal", False)),
            enabled=bool(item.get("enabled", True)),
        ))
    return out


def component_specs(cfg: dict[str, Any]) -> list[LoadComponentSpec]:
    entities = cfg.get("entities") or {}
    policy = cfg.get("policy") or {}
    ev = policy.get("ev") or {}
    sauna = policy.get("sauna") or {}
    builtins = [
        LoadComponentSpec("ev", "ev", "zaptec", entities.get("ev_power"), entities.get("ev_connected"), True, True, float(ev.get("max_power_kw", 11.0))),
        LoadComponentSpec("sauna", "sauna", "inferred_or_meter", entities.get("sauna_power"), None, False, False, float(sauna.get("nominal_peak_kw", 6.0))),
        LoadComponentSpec("spa", "thermal", "home_assistant", entities.get("spa_power"), entities.get("spa_temperature"), True, True, None, True, bool(entities.get("spa_power") or entities.get("spa_temperature"))),
        LoadComponentSpec("pool_heat_pump", "thermal", "home_assistant", entities.get("pool_heat_pump_power"), entities.get("pool_temperature"), True, True, None, True, bool(entities.get("pool_heat_pump_power") or entities.get("pool_temperature"))),
    ]
    known = {s.id for s in builtins}
    return builtins + [s for s in _custom_specs(cfg) if s.id not in known]


def component_power_entities(cfg: dict[str, Any]) -> dict[str, str]:
    return {s.id: s.power_entity for s in component_specs(cfg) if s.enabled and s.power_entity}


def registry_status(cfg: dict[str, Any]) -> dict[str, Any]:
    specs = component_specs(cfg)
    return {
        "schema_version": 1,
        "composition": "base_load + sum(load_components)",
        "components": [s.to_dict() for s in specs],
        "active_ids": [s.id for s in specs if s.enabled],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
