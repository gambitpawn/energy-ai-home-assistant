from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any

from . import optimizer as opt
from .engine_contract import EngineDecision, EngineInput
from .engine_registry import descriptor
from .optimizer_v35_replay import solve_v35_from_rows

ENGINE_ID = "stochastic_deterministic_v1"
ENGINE_VERSION = "1"
ALGORITHM_ID = "two_stage_v35_scenario_recourse_v1"
SCENARIO_SIGMA = 1.0
CVAR_ALPHA = 0.80
RISK_AVERSION = 0.25
UNCERTAINTY_EPS_KW = 1e-6


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    weight: float
    load_sigma: float
    pv_sigma: float


# A symmetric five-scenario approximation. The weighted load/PV perturbations
# both sum to zero, so expected load and PV remain equal to the source forecast.
SCENARIO_SPECS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec("nominal", 0.40, 0.0, 0.0),
    ScenarioSpec("high_load_low_pv", 0.15, +SCENARIO_SIGMA, -SCENARIO_SIGMA),
    ScenarioSpec("low_load_high_pv", 0.15, -SCENARIO_SIGMA, +SCENARIO_SIGMA),
    ScenarioSpec("high_load_high_pv", 0.15, +SCENARIO_SIGMA, +SCENARIO_SIGMA),
    ScenarioSpec("low_load_low_pv", 0.15, -SCENARIO_SIGMA, -SCENARIO_SIGMA),
)


@dataclass(frozen=True)
class Scenario:
    spec: ScenarioSpec
    rows: tuple[dict[str, Any], ...]


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _model_identity() -> tuple[str, str]:
    payload = {
        "algorithm": ALGORITHM_ID,
        "engine_version": ENGINE_VERSION,
        "scenario_sigma": SCENARIO_SIGMA,
        "scenario_specs": [spec.__dict__ for spec in SCENARIO_SPECS],
        "cvar_alpha": CVAR_ALPHA,
        "risk_aversion": RISK_AVERSION,
    }
    revision = _canonical_hash(payload)[:20]
    return f"{ENGINE_ID}:{revision}", revision


def build_scenarios(rows: list[dict[str, Any]]) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for spec in SCENARIO_SPECS:
        scenario_rows: list[dict[str, Any]] = []
        for source in rows:
            row = dict(source)
            load = max(0.0, float(source.get("load_kw") or 0.0))
            pv = max(0.0, float(source.get("pv_kw") or 0.0))
            load_unc = max(0.0, float(source.get("load_uncertainty_kw") or 0.0))
            pv_unc = max(0.0, float(source.get("pv_uncertainty_kw") or 0.0))
            row["load_kw"] = max(0.0, load + spec.load_sigma * load_unc)
            row["pv_kw"] = max(0.0, pv + spec.pv_sigma * pv_unc)
            scenario_rows.append(row)
        scenarios.append(Scenario(spec=spec, rows=tuple(scenario_rows)))
    return scenarios


def _uncertainty_summary(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {
            "mean_load_uncertainty_kw": 0.0,
            "mean_pv_uncertainty_kw": 0.0,
            "mean_net_uncertainty_kw": 0.0,
            "max_net_uncertainty_kw": 0.0,
        }
    load = [max(0.0, float(row.get("load_uncertainty_kw") or 0.0)) for row in rows]
    pv = [max(0.0, float(row.get("pv_uncertainty_kw") or 0.0)) for row in rows]
    net = [math.hypot(a, b) for a, b in zip(load, pv)]
    return {
        "mean_load_uncertainty_kw": sum(load) / len(load),
        "mean_pv_uncertainty_kw": sum(pv) / len(pv),
        "mean_net_uncertainty_kw": sum(net) / len(net),
        "max_net_uncertainty_kw": max(net),
    }


def weighted_cvar(costs: list[tuple[float, float]], alpha: float = CVAR_ALPHA) -> float:
    """Return weighted upper-tail CVaR for (probability, cost) pairs."""
    if not costs:
        raise ValueError("costs must not be empty")
    total_weight = sum(max(0.0, float(weight)) for weight, _ in costs)
    if total_weight <= 0.0:
        raise ValueError("scenario weights must sum to a positive value")
    normalized = [
        (max(0.0, float(weight)) / total_weight, float(cost))
        for weight, cost in costs
        if float(weight) > 0.0
    ]
    tail_mass = max(1e-9, 1.0 - max(0.0, min(0.999999, float(alpha))))
    remaining = tail_mass
    tail_cost = 0.0
    for weight, cost in sorted(normalized, key=lambda item: item[1], reverse=True):
        take = min(remaining, weight)
        tail_cost += take * cost
        remaining -= take
        if remaining <= 1e-12:
            break
    if remaining > 1e-12:
        # Floating-point normalization guard. The final scenario fills any tiny
        # residual tail mass.
        tail_cost += remaining * max(cost for _, cost in normalized)
    return tail_cost / tail_mass


def _solver_context(cfg: dict[str, Any], initial_soc_pct: float) -> dict[str, Any]:
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
    excess_rate = max(0.0, float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))

    planning_soc = max(hmin, min(hmax, float(initial_soc_pct)))
    initial = cap * planning_soc / 100.0
    mink = cap * hmin / 100.0
    maxk = cap * hmax / 100.0
    pmaxk = cap * pmax / 100.0
    states, effstep = opt._state_grid(mink, maxk, reqstep, initial)
    init_idx = min(range(len(states)), key=lambda i: abs(states[i] - initial))
    return {
        "cap": cap,
        "hmin": hmin,
        "hmax": hmax,
        "pmin": pmin,
        "pmax": pmax,
        "pmaxk": pmaxk,
        "cmax": cmax,
        "dmax": dmax,
        "ec": ec,
        "ed": ed,
        "termtol": termtol,
        "termtie": termtie,
        "excess_rate": excess_rate,
        "planning_soc": planning_soc,
        "initial": initial,
        "states": states,
        "effstep": effstep,
        "init_idx": init_idx,
    }


def _transition_detail(
    cfg: dict[str, Any],
    row: dict[str, Any],
    *,
    action_kw: float,
    end_energy_kwh: float,
    t: int,
    boundary: int | None,
    continuation: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any] | None:
    result = opt._interval_result(row, action_kw, cfg)
    if not result["feasible"]:
        return None
    reserve_kwh, reserve_pct = opt._dynamic_reserve_kwh(row, cfg, ctx["cap"])
    reserve_adj = opt._reserve_policy_penalty_ore(
        end_energy_kwh,
        reserve_kwh,
        cfg,
        ctx["cap"],
        ctx["hmin"],
        ctx["pmin"],
    )
    upper_adj = max(0.0, end_energy_kwh - ctx["pmaxk"]) * ctx["excess_rate"] * opt.DT_HOURS
    continuation_adj = 0.0
    if boundary is not None and t == boundary:
        target = float(continuation.get("target_kwh") or 0.0)
        ref = float(continuation.get("reference_price_ore_kwh") or 0.0)
        risk = float(continuation.get("risk_premium_ore_kwh") or 0.0)
        continuation_adj -= end_energy_kwh * ref
        if end_energy_kwh < target:
            continuation_adj += (target - end_energy_kwh) * risk
    adjustment = reserve_adj + upper_adj + continuation_adj
    return {
        "result": result,
        "reserve_kwh": float(reserve_kwh),
        "reserve_soc_pct": float(reserve_pct),
        "reserve_policy_penalty_ore": float(reserve_adj),
        "preferred_max_excess_penalty_ore": float(upper_adj),
        "continuation_policy_adjustment_ore": float(continuation_adj),
        "policy_adjustment_ore": float(adjustment),
        "immediate_cost_ore": float(result["interval_cost_ore"]) + float(adjustment),
    }


def _scenario_recourse(
    cfg: dict[str, Any],
    rows: tuple[dict[str, Any], ...],
    selected_first_state: int,
    ctx: dict[str, Any],
) -> dict[str, Any] | None:
    if not rows:
        return None
    states = ctx["states"]
    initial = ctx["initial"]
    init_idx = ctx["init_idx"]
    continuation = opt._continuation_profile(list(rows), cfg, ctx["cap"], ctx["pmaxk"], ctx["ed"])
    known_n = sum(1 for row in rows if bool(row.get("price_known")))
    boundary = known_n - 1 if 0 < known_n < len(rows) else None

    if continuation.get("enabled"):
        value_next: dict[int, float] = {i: 0.0 for i in range(len(states))}
    else:
        tolerance = ctx["cap"] * ctx["termtol"] / 100.0
        terminal = [i for i, energy in enumerate(states) if abs(energy - initial) <= tolerance + 1e-9]
        if not terminal:
            nearest = min(abs(energy - initial) for energy in states)
            terminal = [i for i, energy in enumerate(states) if abs(abs(energy - initial) - nearest) <= 1e-9]
        value_next = {
            i: abs(states[i] - initial) * ctx["termtie"]
            for i in terminal
        }

    policies: dict[int, dict[int, tuple[int, float, dict[str, Any]]]] = {}
    for t in range(len(rows) - 1, 0, -1):
        row = rows[t]
        current_values: dict[int, float] = {}
        current_policy: dict[int, tuple[int, float, dict[str, Any]]] = {}
        for i0, e0 in enumerate(states):
            best_key = None
            best_payload = None
            for i1, e1 in enumerate(states):
                if i1 not in value_next:
                    continue
                action = float(opt._transition_action_kw(e0, e1, ctx["ec"], ctx["ed"]))
                if action < -ctx["cmax"] - 1e-9 or action > ctx["dmax"] + 1e-9:
                    continue
                detail = _transition_detail(
                    cfg,
                    row,
                    action_kw=action,
                    end_energy_kwh=e1,
                    t=t,
                    boundary=boundary,
                    continuation=continuation,
                    ctx=ctx,
                )
                if detail is None:
                    continue
                total = float(detail["immediate_cost_ore"]) + float(value_next[i1])
                key = (total, abs(action), i1)
                if best_key is None or key < best_key:
                    best_key = key
                    best_payload = (i1, action, detail)
            if best_key is not None and best_payload is not None:
                current_values[i0] = float(best_key[0])
                current_policy[i0] = best_payload
        if not current_values:
            return None
        value_next = current_values
        policies[t] = current_policy

    if selected_first_state not in value_next:
        return None
    first_energy = states[selected_first_state]
    first_action = float(opt._transition_action_kw(states[init_idx], first_energy, ctx["ec"], ctx["ed"]))
    if first_action < -ctx["cmax"] - 1e-9 or first_action > ctx["dmax"] + 1e-9:
        return None
    first_detail = _transition_detail(
        cfg,
        rows[0],
        action_kw=first_action,
        end_energy_kwh=first_energy,
        t=0,
        boundary=boundary,
        continuation=continuation,
        ctx=ctx,
    )
    if first_detail is None:
        return None
    total_cost = float(first_detail["immediate_cost_ore"]) + float(value_next[selected_first_state])

    path: list[dict[str, Any]] = [
        {
            "row_index": 0,
            "state_index": selected_first_state,
            "action_kw": first_action,
            "soc_end_pct": float(first_energy / ctx["cap"] * 100.0),
            **first_detail,
        }
    ]
    idx = selected_first_state
    for t in range(1, len(rows)):
        choice = policies[t].get(idx)
        if choice is None:
            return None
        i1, action, detail = choice
        path.append(
            {
                "row_index": t,
                "state_index": i1,
                "action_kw": float(action),
                "soc_end_pct": float(states[i1] / ctx["cap"] * 100.0),
                **detail,
            }
        )
        idx = i1

    return {
        "objective_cost_ore": total_cost,
        "first_action_kw": first_action,
        "first_expected_soc_pct": float(first_energy / ctx["cap"] * 100.0),
        "terminal_soc_pct": float(states[idx] / ctx["cap"] * 100.0),
        "continuation": continuation,
        "rows": path,
    }


def solve_stochastic_from_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    initial_soc_pct: float,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("rows must contain at least one forecast interval")
    started = time.perf_counter()
    uncertainty = _uncertainty_summary(rows)
    baseline = solve_v35_from_rows(cfg, rows, initial_soc_pct)

    if uncertainty["max_net_uncertainty_kw"] <= UNCERTAINTY_EPS_KW:
        return {
            "engine": ENGINE_ID,
            "algorithm": ALGORITHM_ID,
            "collapsed_to_deterministic": True,
            "collapse_reason": "forecast_uncertainty_is_zero",
            "first_action_kw": float(baseline["first_action_kw"]),
            "first_expected_soc_pct": float(baseline["first_expected_soc_pct"]),
            "baseline_action_kw": float(baseline["first_action_kw"]),
            "baseline_objective_cost_ore": float(baseline["objective_cost_ore"]),
            "expected_scenario_cost_ore": float(baseline["objective_cost_ore"]),
            "cvar_cost_ore": float(baseline["objective_cost_ore"]),
            "risk_adjusted_score_ore": float(baseline["objective_cost_ore"]),
            "risk_premium_ore": 0.0,
            "scenario_costs": [{"name": "nominal", "weight": 1.0, "cost_ore": float(baseline["objective_cost_ore"])}],
            "uncertainty": uncertainty,
            "terminal_soc_pct": float(baseline["terminal_soc_pct"]),
            "rows": list(baseline["rows"]),
            "compute_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    ctx = _solver_context(cfg, initial_soc_pct)
    scenarios = build_scenarios(rows)
    candidates: list[dict[str, Any]] = []
    states = ctx["states"]
    init_idx = ctx["init_idx"]
    for i1, e1 in enumerate(states):
        action = float(opt._transition_action_kw(states[init_idx], e1, ctx["ec"], ctx["ed"]))
        if action < -ctx["cmax"] - 1e-9 or action > ctx["dmax"] + 1e-9:
            continue
        outcomes: list[tuple[Scenario, dict[str, Any]]] = []
        feasible = True
        for scenario in scenarios:
            solved = _scenario_recourse(cfg, scenario.rows, i1, ctx)
            if solved is None:
                feasible = False
                break
            outcomes.append((scenario, solved))
        if not feasible:
            continue
        weighted = [(scenario.spec.weight, solved["objective_cost_ore"]) for scenario, solved in outcomes]
        expected = sum(float(weight) * float(cost) for weight, cost in weighted)
        cvar = weighted_cvar(weighted, CVAR_ALPHA)
        risk_premium = RISK_AVERSION * max(0.0, cvar - expected)
        score = expected + risk_premium
        nominal = next(solved for scenario, solved in outcomes if scenario.spec.name == "nominal")
        candidates.append(
            {
                "first_state_index": i1,
                "action_kw": action,
                "expected_cost_ore": expected,
                "cvar_cost_ore": cvar,
                "risk_premium_ore": risk_premium,
                "risk_adjusted_score_ore": score,
                "nominal": nominal,
                "outcomes": outcomes,
            }
        )

    if not candidates:
        raise RuntimeError("No first battery action is feasible across all stochastic scenarios")

    baseline_action = float(baseline["first_action_kw"])
    selected = min(
        candidates,
        key=lambda item: (
            float(item["risk_adjusted_score_ore"]),
            abs(float(item["action_kw"]) - baseline_action),
            abs(float(item["action_kw"])),
        ),
    )
    nominal = selected["nominal"]
    scenario_costs = [
        {
            "name": scenario.spec.name,
            "weight": scenario.spec.weight,
            "load_sigma": scenario.spec.load_sigma,
            "pv_sigma": scenario.spec.pv_sigma,
            "cost_ore": round(float(solved["objective_cost_ore"]), 6),
            "terminal_soc_pct": round(float(solved["terminal_soc_pct"]), 4),
        }
        for scenario, solved in selected["outcomes"]
    ]

    return {
        "engine": ENGINE_ID,
        "algorithm": ALGORITHM_ID,
        "collapsed_to_deterministic": False,
        "first_action_kw": float(selected["action_kw"]),
        "first_expected_soc_pct": float(nominal["first_expected_soc_pct"]),
        "baseline_action_kw": baseline_action,
        "baseline_objective_cost_ore": float(baseline["objective_cost_ore"]),
        "nominal_forced_objective_cost_ore": float(nominal["objective_cost_ore"]),
        "nominal_regret_ore": float(nominal["objective_cost_ore"]) - float(baseline["objective_cost_ore"]),
        "expected_scenario_cost_ore": float(selected["expected_cost_ore"]),
        "cvar_cost_ore": float(selected["cvar_cost_ore"]),
        "risk_adjusted_score_ore": float(selected["risk_adjusted_score_ore"]),
        "risk_premium_ore": float(selected["risk_premium_ore"]),
        "scenario_count": len(scenarios),
        "candidate_action_count": len(candidates),
        "scenario_costs": scenario_costs,
        "uncertainty": uncertainty,
        "terminal_soc_pct": float(nominal["terminal_soc_pct"]),
        "rows": list(nominal["rows"]),
        "compute_ms": round((time.perf_counter() - started) * 1000.0, 3),
    }


class StochasticDeterministicV1Engine:
    descriptor = descriptor(ENGINE_ID)

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def decide(self, engine_input: EngineInput) -> EngineDecision:
        solved = solve_stochastic_from_rows(
            self.cfg,
            [dict(row) for row in engine_input.horizon_rows],
            float(engine_input.initial_soc_pct),
        )
        model_id, model_revision = _model_identity()
        plan_rows = []
        for source, solved_row in zip(engine_input.horizon_rows, solved.get("rows") or []):
            plan_rows.append(
                {
                    "start": source["start"],
                    "requested_action_kw": round(float(solved_row["action_kw"]), 6),
                    "expected_soc_pct": round(float(solved_row["soc_end_pct"]), 6),
                    "reserve_soc_pct": round(float(solved_row.get("reserve_soc_pct") or 0.0), 6),
                }
            )

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
                "collapsed_to_deterministic": bool(solved.get("collapsed_to_deterministic")),
                "baseline_action_kw": solved.get("baseline_action_kw"),
                "baseline_objective_cost_ore": solved.get("baseline_objective_cost_ore"),
                "nominal_forced_objective_cost_ore": solved.get("nominal_forced_objective_cost_ore"),
                "nominal_regret_ore": solved.get("nominal_regret_ore", 0.0),
                "expected_scenario_cost_ore": solved.get("expected_scenario_cost_ore"),
                "cvar_cost_ore": solved.get("cvar_cost_ore"),
                "risk_adjusted_score_ore": solved.get("risk_adjusted_score_ore"),
                "risk_premium_ore": solved.get("risk_premium_ore"),
                "scenario_count": solved.get("scenario_count", 1),
                "candidate_action_count": solved.get("candidate_action_count"),
                "scenario_costs": solved.get("scenario_costs"),
                "uncertainty": solved.get("uncertainty"),
                "compute_ms": solved.get("compute_ms"),
                "price_uncertainty_strategy": "inherit_v35_unknown_price_continuation",
                "nonanticipativity": "same_first_action_across_all_scenarios",
            },
            model={
                "kind": "two_stage_stochastic_deterministic_dynamic_programming",
                "model_id": model_id,
                "model_revision": model_revision,
                "algorithm": ALGORITHM_ID,
                "trainable": False,
                "scenario_sigma": SCENARIO_SIGMA,
                "scenario_count": len(SCENARIO_SPECS),
                "cvar_alpha": CVAR_ALPHA,
                "risk_aversion": RISK_AVERSION,
                "qualification_required": "robust10_v1",
            },
        )
