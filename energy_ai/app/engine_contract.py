from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

ENGINE_INPUT_SCHEMA = "energy_ai_engine_input_v1"
ENGINE_DECISION_SCHEMA = "energy_ai_engine_decision_v1"

ENGINE_FAMILIES = {
    "deterministic",
    "adaptive_deterministic",
    "neural",
    "hybrid",
}


def _utc_iso(value: str) -> str:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EngineDescriptor:
    engine_id: str
    engine_version: str
    family: str
    display_name: str
    description: str
    baseline: bool = False
    available: bool = False
    trainable: bool = False
    learning_enabled: bool = False
    supports_shadow: bool = True
    supports_active: bool = True

    def __post_init__(self) -> None:
        if not self.engine_id or not self.engine_version:
            raise ValueError("engine_id and engine_version are required")
        if self.family not in ENGINE_FAMILIES:
            raise ValueError(f"unsupported engine family: {self.family}")
        if self.baseline and self.family != "deterministic":
            raise ValueError("the permanent baseline must be deterministic")

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "family": self.family,
            "display_name": self.display_name,
            "description": self.description,
            "baseline": self.baseline,
            "available": self.available,
            "trainable": self.trainable,
            "learning_enabled": self.learning_enabled,
            "supports_shadow": self.supports_shadow,
            "supports_active": self.supports_active,
        }


@dataclass(frozen=True)
class EngineInput:
    generated_at: str
    decision_start: str
    initial_soc_pct: float
    interval_minutes: int
    horizon_rows: tuple[dict[str, Any], ...]
    constraints: dict[str, Any] = field(default_factory=dict)
    objective: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    schema: str = ENGINE_INPUT_SCHEMA
    information_vintage_id: str = ""

    def __post_init__(self) -> None:
        if self.schema != ENGINE_INPUT_SCHEMA:
            raise ValueError(f"unsupported engine input schema: {self.schema}")
        if not self.horizon_rows:
            raise ValueError("engine input requires a non-empty horizon")
        if self.interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        if not (0.0 <= float(self.initial_soc_pct) <= 100.0):
            raise ValueError("initial_soc_pct must be between 0 and 100")
        starts = []
        for row in self.horizon_rows:
            if not isinstance(row, dict) or not row.get("start"):
                raise ValueError("every horizon row requires start")
            starts.append(_utc_iso(str(row["start"])))
        if starts[0] != _utc_iso(self.decision_start):
            raise ValueError("decision_start must equal the first horizon interval")
        if any(b <= a for a, b in zip(starts, starts[1:])):
            raise ValueError("horizon rows must be strictly increasing")
        if not self.information_vintage_id:
            object.__setattr__(self, "information_vintage_id", self.compute_vintage_id())

    def vintage_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at": _utc_iso(self.generated_at),
            "decision_start": _utc_iso(self.decision_start),
            "initial_soc_pct": round(float(self.initial_soc_pct), 6),
            "interval_minutes": int(self.interval_minutes),
            "horizon_rows": list(self.horizon_rows),
            "constraints": self.constraints,
            "objective": self.objective,
            "source": self.source,
        }

    def compute_vintage_id(self) -> str:
        return _fingerprint(self.vintage_payload())

    @property
    def price_known_intervals(self) -> int:
        return sum(1 for r in self.horizon_rows if bool(r.get("price_known", r.get("price_ore_kwh") is not None)))

    def as_dict(self, *, include_horizon: bool = True) -> dict[str, Any]:
        result = {
            "schema": self.schema,
            "information_vintage_id": self.information_vintage_id,
            "generated_at": _utc_iso(self.generated_at),
            "decision_start": _utc_iso(self.decision_start),
            "initial_soc_pct": round(float(self.initial_soc_pct), 6),
            "interval_minutes": int(self.interval_minutes),
            "horizon_intervals": len(self.horizon_rows),
            "price_known_intervals": self.price_known_intervals,
            "constraints": self.constraints,
            "objective": self.objective,
            "source": self.source,
        }
        if include_horizon:
            result["horizon_rows"] = list(self.horizon_rows)
        return result


@dataclass(frozen=True)
class EngineDecision:
    engine_id: str
    engine_version: str
    family: str
    information_vintage_id: str
    generated_at: str
    decision_start: str
    requested_action_kw: float
    expected_soc_pct: float | None
    status: str = "ok"
    plan_rows: tuple[dict[str, Any], ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)
    model: dict[str, Any] = field(default_factory=dict)
    schema: str = ENGINE_DECISION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ENGINE_DECISION_SCHEMA:
            raise ValueError(f"unsupported engine decision schema: {self.schema}")
        if self.family not in ENGINE_FAMILIES:
            raise ValueError(f"unsupported engine family: {self.family}")
        if not self.information_vintage_id:
            raise ValueError("information_vintage_id is required")
        if self.status not in {"ok", "degraded", "unavailable", "failed"}:
            raise ValueError(f"unsupported decision status: {self.status}")
        if self.expected_soc_pct is not None and not (0.0 <= float(self.expected_soc_pct) <= 100.0):
            raise ValueError("expected_soc_pct must be between 0 and 100")

    @property
    def decision_id(self) -> str:
        return _fingerprint({
            "schema": self.schema,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "information_vintage_id": self.information_vintage_id,
            "decision_start": _utc_iso(self.decision_start),
        })

    def as_dict(self, *, include_plan_rows: bool = True) -> dict[str, Any]:
        result = {
            "schema": self.schema,
            "decision_id": self.decision_id,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "family": self.family,
            "information_vintage_id": self.information_vintage_id,
            "generated_at": _utc_iso(self.generated_at),
            "decision_start": _utc_iso(self.decision_start),
            "status": self.status,
            "requested_action_kw": round(float(self.requested_action_kw), 6),
            "expected_soc_pct": None if self.expected_soc_pct is None else round(float(self.expected_soc_pct), 6),
            "diagnostics": self.diagnostics,
            "model": self.model,
            "safety_semantics": {
                "requested_action_is_pre_safety": True,
                "positive_kw": "battery discharge",
                "negative_kw": "battery charge",
                "physical_authority": False,
            },
        }
        if include_plan_rows:
            result["plan_rows"] = list(self.plan_rows)
        return result


class DecisionEngine(Protocol):
    descriptor: EngineDescriptor

    def decide(self, engine_input: EngineInput) -> EngineDecision:
        """Return a requested action. Physical safety/control is deliberately outside the engine."""
        ...


def input_from_optimizer_plan(plan: dict[str, Any]) -> EngineInput:
    rows = tuple(dict(r) for r in (plan.get("rows") or []))
    if not rows:
        raise ValueError("optimizer plan has no rows")
    return EngineInput(
        generated_at=str(plan["generated_at"]),
        decision_start=str(rows[0]["start"]),
        initial_soc_pct=float(plan["initial_soc_pct"]),
        interval_minutes=int(plan.get("interval_minutes") or 15),
        horizon_rows=rows,
        constraints=dict(plan.get("constraints") or {}),
        objective=dict(plan.get("objective") or {}),
        source={
            "kind": "optimizer_plan_information_vintage",
            "source_planner": str(plan.get("planner") or "unknown"),
            "mode": str(plan.get("mode") or "unknown"),
        },
    )
