from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import optimizer as opt
from .engine_contract import EngineDecision, EngineInput
from .engine_registry import descriptor
from .neural_features import FEATURE_SCHEMA, vectorize
from .neural_training import load_model

ENGINE_ID = "hybrid_v1"
ENGINE_VERSION = "1"
ALGORITHM_ID = "neural_guided_v35_first_action_prior_v1"
MAX_NEURAL_PRIOR_STRENGTH_ORE = 6.0
MAX_BACKBONE_REGRET_ORE = 5.0
PROBABILITY_FLOOR = 1e-6


@dataclass(frozen=True)
class NeuralActionPrior:
    probabilities: dict[float, float]
    top_action_kw: float
    confidence: float
    normalized_confidence: float
    prior_strength_ore: float
    neural_model: dict[str, Any]

    def probability_for(self, action_kw: float) -> tuple[float, float]:
        if not self.probabilities:
            return 0.0, float(action_kw)
        nearest = min(self.probabilities, key=lambda value: abs(float(value) - float(action_kw)))
        return float(self.probabilities[nearest]), float(nearest)


def _canonical_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _neural_prior(engine_input: EngineInput) -> NeuralActionPrior:
    model, meta = load_model()
    if not bool(meta.get("shadow_ready")):
        raise RuntimeError("neural_v1 is not shadow-ready")

    x = np.asarray([vectorize(engine_input)], dtype=float)
    predicted = float(model.predict(x)[0])
    probabilities: dict[float, float] = {}
    if hasattr(model, "predict_proba") and hasattr(model, "classes_"):
        raw = model.predict_proba(x)[0]
        probabilities = {
            float(action): max(0.0, float(probability))
            for action, probability in zip(model.classes_, raw)
        }
    if not probabilities:
        probabilities = {predicted: 1.0}

    top_action, confidence = max(probabilities.items(), key=lambda item: item[1])
    class_count = max(1, len(probabilities))
    if class_count == 1:
        normalized = 1.0
    else:
        uniform = 1.0 / float(class_count)
        normalized = (float(confidence) - uniform) / max(1e-9, 1.0 - uniform)
        normalized = max(0.0, min(1.0, normalized))
    strength = MAX_NEURAL_PRIOR_STRENGTH_ORE * normalized

    return NeuralActionPrior(
        probabilities=probabilities,
        top_action_kw=float(top_action),
        confidence=float(confidence),
        normalized_confidence=float(normalized),
        prior_strength_ore=float(strength),
        neural_model=dict(meta),
    )


def _prior_penalty_ore(prior: NeuralActionPrior | None, action_kw: float) -> tuple[float, float | None, float | None]:
    if prior is None or prior.prior_strength_ore <= 1e-12 or not prior.probabilities:
        return 0.0, None, None
    probability, mapped_class = prior.probability_for(action_kw)
    top_probability = max(prior.probabilities.values())
    ratio = max(PROBABILITY_FLOOR, float(top_probability)) / max(PROBABILITY_FLOOR, float(probability))
    penalty = prior.prior_strength_ore * max(0.0, math.log(ratio))
    return float(penalty), float(probability), float(mapped_class)


def _solve_v35_with_prior(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    initial_soc_pct: float,
    prior: NeuralActionPrior | None,
) -> dict[str, Any]:
    """Mirror frozen v3.5 DP while adding a neural penalty only at t=0.

    All physical feasibility, reserve policy, continuation logic and terminal SOC
    handling remain deterministic. The neural model only changes the ranking of
    otherwise feasible first transitions.
    """
    if not rows:
        raise ValueError("rows must contain at least one forecast interval")

    o = cfg.get("optimizer") or {}
    b = (cfg.get("policy") or {}).get("battery") or {}
    cap = float(b.get("capacity_kwh", 19.6))
    hmin = float(b.get("hard_min_soc_pct", 5.0))
    hmax = float(b.get("hard_max_soc_pct", 100.0))
    pmin = float(b.get("preferred_min_soc_pct", 15.0))
    pmax = float(b.get("preferred_max_soc_pct", 90.0))
    cmax = float(o.get("battery_max_charge_kw", 8.0))
    dmax = float(o.get("battery_max_discharge_kw", 8.0))
    ec = float(o.get("battery_charge_efficiency", 0.95))
    ed = float(o.get("battery_discharge_efficiency", 0.95))
    reqstep = float(o.get("soc_grid_step_kwh", 0.5))
    termtol = float(o.get("terminal_soc_tolerance_pct", 3.0))
    termtie = float(o.get("terminal_soc_tiebreak_ore_per_kwh", 5.0))

    measured_soc = float(initial_soc_pct)
    planning_soc = max(hmin, min(hmax, measured_soc))
    initial = cap * planning_soc / 100.0
    mink = cap * hmin / 100.0
    maxk = cap * hmax / 100.0
    pmaxk = cap * pmax / 100.0
    states, effstep = opt._state_grid(mink, maxk, reqstep, initial)
    init_idx = min(range(len(states)), key=lambda i: abs(states[i] - initial))

    continuation = opt._continuation_profile(rows, cfg, cap, pmaxk, ed)
    known_n = sum(1 for row in rows if bool(row.get("price_known")))
    boundary = known_n - 1 if 0 < known_n < len(rows) else None
    score_costs: dict[int, float] = {init_idx: 0.0}
    base_costs: dict[int, float] = {init_idx: 0.0}
    parents: list[dict[int, tuple[Any, ...]]] = []
    excess_rate = max(0.0, float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))

    for t, row in enumerate(rows):
        reserve_kwh, reserve_pct = opt._dynamic_reserve_kwh(row, cfg, cap)
        next_score: dict[int, float] = {}
        next_base: dict[int, float] = {}
        back: dict[int, tuple[Any, ...]] = {}
        for i0, prior_score in score_costs.items():
            prior_base = base_costs[i0]
            for i1, e1 in enumerate(states):
                action = opt._transition_action_kw(states[i0], e1, ec, ed)
                if action < -cmax - 1e-9 or action > dmax + 1e-9:
                    continue
                result = opt._interval_result(row, action, cfg)
                if not result["feasible"]:
                    continue
                reserve_adj = opt._reserve_policy_penalty_ore(e1, reserve_kwh, cfg, cap, hmin, pmin)
                upper_adj = max(0.0, e1 - pmaxk) * excess_rate * opt.DT_HOURS
                continuation_adj = 0.0
                if boundary is not None and t == boundary:
                    target = float(continuation.get("target_kwh") or 0.0)
                    ref = float(continuation.get("reference_price_ore_kwh") or 0.0)
                    risk = float(continuation.get("risk_premium_ore_kwh") or 0.0)
                    continuation_adj -= e1 * ref
                    if e1 < target:
                        continuation_adj += (target - e1) * risk
                adjustment = reserve_adj + upper_adj + continuation_adj
                base_total = prior_base + float(result["interval_cost_ore"]) + adjustment
                neural_penalty, neural_probability, mapped_class = (
                    _prior_penalty_ore(prior, action) if t == 0 else (0.0, None, None)
                )
                score_total = prior_score + float(result["interval_cost_ore"]) + adjustment + neural_penalty
                replace = i1 not in next_score or score_total < next_score[i1] - 1e-12
                if not replace and i1 in next_score and abs(score_total - next_score[i1]) <= 1e-12:
                    replace = base_total < next_base[i1] - 1e-12
                if replace:
                    next_score[i1] = score_total
                    next_base[i1] = base_total
                    back[i1] = (
                        i0,
                        action,
                        result,
                        reserve_kwh,
                        reserve_pct,
                        adjustment,
                        reserve_adj,
                        upper_adj,
                        continuation_adj,
                        neural_penalty,
                        neural_probability,
                        mapped_class,
                    )
        if not next_score:
            raise RuntimeError(f"No feasible hybrid states at {row.get('start')}")
        score_costs = next_score
        base_costs = next_base
        parents.append(back)

    if continuation.get("enabled"):
        best = min(score_costs, key=score_costs.get)
        terminal_constraint_applied = False
    else:
        tolerance = cap * termtol / 100.0
        candidates = [i for i in score_costs if abs(states[i] - initial) <= tolerance + 1e-9]
        if not candidates:
            nearest = min(abs(states[i] - initial) for i in score_costs)
            candidates = [i for i in score_costs if abs(abs(states[i] - initial) - nearest) <= 1e-9]
        best = min(candidates, key=lambda i: score_costs[i] + abs(states[i] - initial) * termtie)
        terminal_constraint_applied = True

    path: list[dict[str, Any]] = []
    idx = best
    for t in range(len(rows) - 1, -1, -1):
        (
            prev,
            action,
            result,
            reserve_kwh,
            reserve_pct,
            adjustment,
            reserve_adj,
            upper_adj,
            continuation_adj,
            neural_penalty,
            neural_probability,
            mapped_class,
        ) = parents[t][idx]
        path.append(
            {
                "row_index": t,
                "state_index": idx,
                "action_kw": float(action),
                "soc_end_pct": float(states[idx] / cap * 100.0),
                "reserve_soc_pct": float(reserve_pct),
                "result": result,
                "policy_adjustment_ore": float(adjustment),
                "reserve_policy_penalty_ore": float(reserve_adj),
                "preferred_max_excess_penalty_ore": float(upper_adj),
                "continuation_policy_adjustment_ore": float(continuation_adj),
                "neural_prior_penalty_ore": float(neural_penalty),
                "neural_probability": neural_probability,
                "neural_mapped_action_class_kw": mapped_class,
            }
        )
        idx = int(prev)
    path.reverse()

    return {
        "measured_initial_soc_pct": measured_soc,
        "planning_initial_soc_pct": planning_soc,
        "terminal_soc_pct": float(states[best] / cap * 100.0),
        "terminal_soc_constraint_applied": terminal_constraint_applied,
        "soc_grid_effective_max_step_kwh": float(effstep),
        "objective_cost_ore": float(base_costs[best]),
        "selection_score_ore": float(score_costs[best]),
        "continuation": continuation,
        "first_action_kw": float(path[0]["action_kw"]),
        "first_expected_soc_pct": float(path[0]["soc_end_pct"]),
        "first_neural_prior_penalty_ore": float(path[0]["neural_prior_penalty_ore"]),
        "rows": path,
    }


def solve_hybrid_from_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    initial_soc_pct: float,
    prior: NeuralActionPrior,
    *,
    max_backbone_regret_ore: float = MAX_BACKBONE_REGRET_ORE,
) -> dict[str, Any]:
    backbone = _solve_v35_with_prior(cfg, rows, initial_soc_pct, None)
    guided = _solve_v35_with_prior(cfg, rows, initial_soc_pct, prior)
    regret = float(guided["objective_cost_ore"]) - float(backbone["objective_cost_ore"])
    accepted = regret <= max(0.0, float(max_backbone_regret_ore)) + 1e-9
    chosen = guided if accepted else backbone
    changed = abs(float(guided["first_action_kw"]) - float(backbone["first_action_kw"])) > 1e-9

    return {
        "engine": ENGINE_ID,
        "algorithm": ALGORITHM_ID,
        "accepted_neural_guidance": bool(accepted),
        "neural_changed_first_action": bool(changed and accepted),
        "backbone_action_kw": float(backbone["first_action_kw"]),
        "guided_action_kw": float(guided["first_action_kw"]),
        "first_action_kw": float(chosen["first_action_kw"]),
        "first_expected_soc_pct": float(chosen["first_expected_soc_pct"]),
        "backbone_objective_cost_ore": float(backbone["objective_cost_ore"]),
        "guided_backbone_objective_cost_ore": float(guided["objective_cost_ore"]),
        "selected_backbone_objective_cost_ore": float(chosen["objective_cost_ore"]),
        "guided_selection_score_ore": float(guided["selection_score_ore"]),
        "backbone_regret_ore": float(regret),
        "max_backbone_regret_ore": float(max_backbone_regret_ore),
        "rejection_reason": None if accepted else "guided_path_exceeded_backbone_regret_guard",
        "terminal_soc_pct": float(chosen["terminal_soc_pct"]),
        "terminal_soc_constraint_applied": bool(chosen["terminal_soc_constraint_applied"]),
        "continuation": chosen["continuation"],
        "rows": chosen["rows"],
    }


def _model_identity(prior: NeuralActionPrior) -> tuple[str, str]:
    meta = prior.neural_model
    neural_identity = (
        meta.get("model_id")
        or meta.get("model_revision")
        or meta.get("trained_at")
        or "unknown"
    )
    payload = {
        "algorithm": ALGORITHM_ID,
        "engine_version": ENGINE_VERSION,
        "feature_schema": FEATURE_SCHEMA,
        "neural_identity": neural_identity,
        "max_neural_prior_strength_ore": MAX_NEURAL_PRIOR_STRENGTH_ORE,
        "max_backbone_regret_ore": MAX_BACKBONE_REGRET_ORE,
    }
    revision = _canonical_hash(payload)[:20]
    return f"hybrid_v1:{revision}", revision


class HybridV1Engine:
    descriptor = descriptor(ENGINE_ID)

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def decide(self, engine_input: EngineInput) -> EngineDecision:
        prior = _neural_prior(engine_input)
        solved = solve_hybrid_from_rows(
            self.cfg,
            [dict(row) for row in engine_input.horizon_rows],
            float(engine_input.initial_soc_pct),
            prior,
        )
        model_id, model_revision = _model_identity(prior)
        plan_rows = []
        for source, solved_row in zip(engine_input.horizon_rows, solved["rows"]):
            plan_rows.append(
                {
                    "start": source["start"],
                    "requested_action_kw": round(float(solved_row["action_kw"]), 6),
                    "expected_soc_pct": round(float(solved_row["soc_end_pct"]), 6),
                    "reserve_soc_pct": round(float(solved_row["reserve_soc_pct"]), 6),
                }
            )

        probabilities = [
            {"action_kw": action, "probability": round(probability, 6)}
            for action, probability in sorted(
                prior.probabilities.items(), key=lambda item: item[1], reverse=True
            )[:5]
        ]
        meta = prior.neural_model
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
                "algorithm": ALGORITHM_ID,
                "backbone": "deterministic_v35",
                "backbone_action_kw": round(float(solved["backbone_action_kw"]), 6),
                "guided_action_kw": round(float(solved["guided_action_kw"]), 6),
                "backbone_objective_cost_ore": round(float(solved["backbone_objective_cost_ore"]), 6),
                "guided_backbone_objective_cost_ore": round(float(solved["guided_backbone_objective_cost_ore"]), 6),
                "backbone_regret_ore": round(float(solved["backbone_regret_ore"]), 6),
                "max_backbone_regret_ore": MAX_BACKBONE_REGRET_ORE,
                "accepted_neural_guidance": bool(solved["accepted_neural_guidance"]),
                "neural_changed_first_action": bool(solved["neural_changed_first_action"]),
                "rejection_reason": solved["rejection_reason"],
                "classification_confidence": round(float(prior.confidence), 6),
                "normalized_confidence": round(float(prior.normalized_confidence), 6),
                "neural_prior_strength_ore": round(float(prior.prior_strength_ore), 6),
                "neural_top_action_kw": float(prior.top_action_kw),
                "top_action_probabilities": probabilities,
                "terminal_soc_pct": round(float(solved["terminal_soc_pct"]), 6),
                "terminal_soc_constraint_applied": bool(solved["terminal_soc_constraint_applied"]),
                "expected_soc_is_pre_safety": True,
                "mode": "challenger_shadow",
            },
            model={
                "kind": "neural_guided_deterministic_dynamic_programming",
                "model_id": model_id,
                "model_revision": model_revision,
                "algorithm": ALGORITHM_ID,
                "feature_schema": FEATURE_SCHEMA,
                "neural_model_id": meta.get("model_id"),
                "neural_model_revision": meta.get("model_revision"),
                "neural_trained_at": meta.get("trained_at"),
                "neural_training_samples": meta.get("samples"),
                "neural_label_source": meta.get("label_source"),
                "max_neural_prior_strength_ore": MAX_NEURAL_PRIOR_STRENGTH_ORE,
                "max_backbone_regret_ore": MAX_BACKBONE_REGRET_ORE,
                "trainable": False,
                "uses_learned_component": True,
                "active_eligible": False,
                "qualification_required": "robust10_v1",
            },
        )
