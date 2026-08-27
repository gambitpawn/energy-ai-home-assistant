from __future__ import annotations

from typing import Any

from .engine_contract import EngineDescriptor, EngineDecision, EngineInput
from .engine_input_v2 import input_from_optimizer_plan_v2
from .optimizer import PLANNER_NAME
from .optimizer_v35_replay import solve_v35_from_rows

BASELINE_ENGINE_ID = "deterministic_v35"

DESCRIPTORS: tuple[EngineDescriptor, ...] = (
    EngineDescriptor(
        engine_id=BASELINE_ENGINE_ID,
        engine_version="3.5",
        family="deterministic",
        display_name="Deterministic",
        description="Frozen deterministic DP planner v3.5; permanent performance baseline.",
        baseline=True,
        available=True,
        trainable=False,
        learning_enabled=False,
    ),
    EngineDescriptor(
        engine_id="adaptive_deterministic_v1",
        engine_version="1",
        family="adaptive_deterministic",
        display_name="Adaptive deterministic",
        description="Reserved challenger: deterministic optimization with bounded learned parameters.",
        baseline=False,
        available=False,
        trainable=True,
        learning_enabled=False,
    ),
    EngineDescriptor(
        engine_id="neural_v1",
        engine_version="1",
        family="neural",
        display_name="Neural",
        description="Reserved challenger: learned battery policy with deterministic downstream safety.",
        baseline=False,
        available=False,
        trainable=True,
        learning_enabled=False,
    ),
    EngineDescriptor(
        engine_id="hybrid_v1",
        engine_version="1",
        family="hybrid",
        display_name="Hybrid",
        description="Reserved challenger: learned value/model components inside deterministic constrained optimization.",
        baseline=False,
        available=False,
        trainable=True,
        learning_enabled=False,
    ),
)

_BY_ID = {d.engine_id: d for d in DESCRIPTORS}


def descriptor(engine_id: str) -> EngineDescriptor:
    try:
        return _BY_ID[str(engine_id)]
    except KeyError as exc:
        raise KeyError(f"unknown engine_id: {engine_id}") from exc


def registry_status() -> dict[str, Any]:
    baseline = [d for d in DESCRIPTORS if d.baseline]
    if len(baseline) != 1 or baseline[0].engine_id != BASELINE_ENGINE_ID:
        raise RuntimeError("engine registry must contain exactly one deterministic_v35 baseline")
    return {
        "contract_version": "v1",
        "baseline_engine_id": BASELINE_ENGINE_ID,
        "baseline_policy": "immutable_reference",
        "ranking_reference_in_active_control": BASELINE_ENGINE_ID,
        "historical_pre_control_reference": "actual_app_or_inverter_behavior_when_available",
        "selection": {
            "active_control_enabled": False,
            "selected_engine_id": BASELINE_ENGINE_ID,
            "selection_mode": "manual_future_capability",
            "auto_selection_supported_by_contract": True,
            "note": "Engine selection is metadata only in this release; physical writes remain disabled.",
        },
        "engines": [d.as_dict() for d in DESCRIPTORS],
    }


class DeterministicV35Adapter:
    descriptor = descriptor(BASELINE_ENGINE_ID)

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def decide(self, engine_input: EngineInput) -> EngineDecision:
        solved = solve_v35_from_rows(
            self.cfg,
            [dict(r) for r in engine_input.horizon_rows],
            float(engine_input.initial_soc_pct),
        )
        plan_rows = []
        for source, solved_row in zip(engine_input.horizon_rows, solved.get("rows") or []):
            plan_rows.append({
                "start": source["start"],
                "requested_action_kw": round(float(solved_row["action_kw"]), 6),
                "expected_soc_pct": round(float(solved_row["soc_end_pct"]), 6),
                "reserve_soc_pct": round(float(solved_row["reserve_soc_pct"]), 6),
            })
        return EngineDecision(
            engine_id=self.descriptor.engine_id,
            engine_version=self.descriptor.engine_version,
            family=self.descriptor.family,
            information_vintage_id=engine_input.information_vintage_id,
            generated_at=engine_input.generated_at,
            decision_start=engine_input.decision_start,
            requested_action_kw=float(solved["first_action_kw"]),
            expected_soc_pct=float(solved["first_expected_soc_pct"]),
            status="ok",
            plan_rows=tuple(plan_rows),
            diagnostics={
                "source_planner_compatibility": PLANNER_NAME,
                "pure_solver_engine": solved.get("engine"),
                "terminal_soc_pct": solved.get("terminal_soc_pct"),
                "terminal_soc_constraint_applied": solved.get("terminal_soc_constraint_applied"),
                "objective_cost_ore": solved.get("objective_cost_ore"),
                "continuation": solved.get("continuation"),
            },
            model={
                "kind": "deterministic_dynamic_programming",
                "trainable": False,
                "frozen_baseline": True,
            },
        )


def baseline_decision_from_plan(cfg: dict[str, Any], plan: dict[str, Any]) -> tuple[EngineInput, EngineDecision]:
    engine_input = input_from_optimizer_plan_v2(plan, cfg)
    decision = DeterministicV35Adapter(cfg).decide(engine_input)
    return engine_input, decision
