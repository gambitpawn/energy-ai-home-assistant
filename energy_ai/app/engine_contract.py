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

# Only ex-ante information belongs in a shared horizon. Engine outputs such as
# battery_action_kw, expected_soc_pct and objective_cost_ore are deliberately
# excluded so challenger vintages cannot be contaminated by baseline decisions.
HORIZON_INPUT_FIELDS = (
    "start",
    "load_kw",
    "base_load_kw",
    "component_forecast_kw",
    "load_uncertainty_kw",
    "pv_kw",
    "pv_uncertainty_kw",
    "price_known",
    "price_ore_kwh",
)

# Shared constraints are physical/user-policy facts, not implementation details
# of deterministic_v35 (for example DP state-count/effective grid spacing).
COMMON_CONSTRAINT_FIELDS = (
    "battery_capacity_kwh",
    "hard_min_soc_pct",
    "hard_max_soc_pct",
    "preferred_min_soc_pct",
    "preferred_max_soc_pct",
    "normal_reserve_soc_pct",
    "high_uncertainty_reserve_soc_pct",
    "reserve_uncertainty_full_scale_kw",
    "reserve_critical_soc_pct",
    "reserve_critical_penalty_ore_per_kwh_hour",
    "reserve_preferred_penalty_ore_per_kwh_hour",
    "reserve_target_penalty_ore_per_kwh_hour",
    "preferred_max_excess_penalty_ore_per_kwh_hour",
    "battery_max_charge_kw",
    "battery_max_discharge_kw",
    "physical_grid_import_limit_kw",
    "grid_export_limit_kw",
    "charge_efficiency",
    "discharge_efficiency",
    "terminal_soc_tolerance_pct",
)


def _utc_iso(value: str) -> str:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def normalize_horizon_row(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw.get("start"):
        raise ValueError("every horizon row requires start")
    row: dict[str, Any] = {"start": _utc_iso(str(raw["start"]))}
    for key in HORIZON_INPUT_FIELDS[1:]:
        if key in raw:
            row[key] = raw[key]
    row["load_kw"] = float(row.get("load_kw") or 0.0)
    row["base_load_kw"] = float(row.get("base_load_kw") or 0.0)
    row["component_forecast_kw"] = dict(row.get("component_forecast_kw") or {})
    row["load_uncertainty_kw"] = float(row.get("load_uncertainty_kw") or 0.0)
    row["pv_kw"] = float(row.get("pv_kw") or 0.0)
    row["pv_uncertainty_kw"] = float(row.get("pv_uncertainty_kw") or 0.0)
    row["price_known"] = bool(row.get("price_known", row.get("price_ore_kwh") is not None))
    row["price_ore_kwh"] = None if row.get("price_ore_kwh") is None else float(row["price_ore_kwh"])
    return row


def common_constraints_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    raw = dict(plan.get("constraints") or {})
    return {key: raw[key] for key in COMMON_CONSTRAINT_FIELDS if key in raw}


def common_objective_from_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    if not cfg:
        return {}
    economics = dict(((cfg.get("policy") or {}).get("economics") or {}))
    optimizer = dict(cfg.get("optimizer") or {})
    return {
        "economics": {
            "import_overhead_ore_kwh": float(economics.get("import_overhead_ore_kwh", 0.0)),
            "export_overhead_ore_kwh": float(economics.get("export_overhead_ore_kwh", 0.0)),
            "minimum_arbitrage_margin_ore_kwh": float(economics.get("minimum_arbitrage_margin_ore_kwh", 20.0)),
            "battery_degradation_ore_kwh": float(optimizer.get("battery_degradation_ore_kwh", 5.0)),
        },
        "tariffs": dict(cfg.get("tariffs") or {}),
        "evaluation_semantics": "common_realized_economic_cost",
    }


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
        object.__setattr__(self, "generated_at", _utc_iso(self.generated_at))
        object.__setattr__(self, "decision_start", _utc_iso(self.decision_start))
        normalized = tuple(normalize_horizon_row(dict(row)) for row in self.horizon_rows)
        object.__setattr__(self, "horizon_rows", normalized)
        starts = [str(row["start"]) for row in normalized]
        if starts[0] != self.decision_start:
            raise ValueError("decision_start must equal the first horizon interval")
        if any(b <= a for a, b in zip(starts, starts[1:])):
            raise ValueError("horizon rows must be strictly increasing")
        if not self.information_vintage_id:
            object.__setattr__(self, "information_vintage_id", self.compute_vintage_id())

    def vintage_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at": self.generated_at,
            "decision_start": self.decision_start,
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
            "generated_at": self.generated_at,
            "decision_start": self.decision_start,
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
        object.__setattr__(self, "generated_at", _utc_iso(self.generated_at))
        object.__setattr__(self, "decision_start", _utc_iso(self.decision_start))

    @property
    def decision_id(self) -> str:
        return _fingerprint({
            "schema": self.schema,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "information_vintage_id": self.information_vintage_id,
            "decision_start": self.decision_start,
        })

    def as_dict(self, *, include_plan_rows: bool = True) -> dict[str, Any]:
        result = {
            "schema": self.schema,
            "decision_id": self.decision_id,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "family": self.family,
            "information_vintage_id": self.information_vintage_id,
            "generated_at": self.generated_at,
            "decision_start": self.decision_start,
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


def input_from_optimizer_plan(plan: dict[str, Any], cfg: dict[str, Any] | None = None) -> EngineInput:
    rows = tuple(normalize_horizon_row(dict(r)) for r in (plan.get("rows") or []))
    if not rows:
        raise ValueError("optimizer plan has no rows")
    return EngineInput(
        generated_at=str(plan["generated_at"]),
        decision_start=str(rows[0]["start"]),
        initial_soc_pct=float(plan["initial_soc_pct"]),
        interval_minutes=int(plan.get("interval_minutes") or 15),
        horizon_rows=rows,
        constraints=common_constraints_from_plan(plan),
        objective=common_objective_from_cfg(cfg),
        source={
            "kind": "optimizer_plan_information_vintage",
            "source_planner": str(plan.get("planner") or "unknown"),
            "mode": str(plan.get("mode") or "unknown"),
        },
    )
