from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .engine_contract import EngineDecision, EngineInput
from .optimizer import (
    DT_HOURS,
    _continuation_profile,
    _dynamic_reserve_kwh,
    _interval_result,
    _reserve_policy_penalty_ore,
    _state_grid,
    _transition_action_kw,
)

ENGINE_ID = "deterministic_refined_v1"
ENGINE_VERSION = "1"
ALGORITHM_ID = "fine_grid_plus_pv_following_dp_v1"
DEFAULT_GRID_STEP_KWH = 0.1


@dataclass(frozen=True)
class _State:
    energy_kwh: float
    cost_ore: float


def _nearest_regular_index(states: list[float], value: float) -> int:
    return min(range(len(states)), key=lambda i: abs(states[i] - value))


def _candidate_key(states: list[float], energy_kwh: float, *, regular: bool) -> tuple[str, int]:
    idx = _nearest_regular_index(states, energy_kwh)
    return ("g" if regular else "p", idx)


def solve_refined_from_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    initial_soc_pct: float,
) -> dict[str, Any]:
    """Refined deterministic DP challenger.

    Relative to frozen deterministic_v35 this solver makes two deliberate
    representation changes while keeping the same physical/economic objective:

    1. a finer regular battery-energy grid (default 0.1 kWh);
    2. an exact PV-following charge candidate in every interval, allowing the
       optimizer to absorb the available PV surplus without forcing the action
       onto the regular grid.

    To avoid state explosion, at most one off-grid PV-following state is kept
    per regular-grid bucket at each time step, in addition to the regular state.
    """
    if not rows:
        raise ValueError("rows must contain at least one forecast interval")

    o = cfg.get("optimizer") or {}
    b = (cfg.get("policy") or {}).get("battery") or {}
    cap = float(b.get("capacity_kwh", 19.6))
    hmin = float(b.get("hard_min_soc_pct", 5))
    hmax = float(b.get("hard_max_soc_pct", 100))
    pmin = float(b.get("preferred_min_soc_pct", 15))
    pmax = float(b.get("preferred_max_soc_pct", 90))
    cmax = float(o.get("battery_max_charge_kw", 8))
    dmax = float(o.get("battery_max_discharge_kw", 8))
    ec = float(o.get("battery_charge_efficiency", 0.95))
    ed = float(o.get("battery_discharge_efficiency", 0.95))
    reqstep = float(o.get("refined_soc_grid_step_kwh", DEFAULT_GRID_STEP_KWH))
    if reqstep <= 0:
        raise ValueError("refined_soc_grid_step_kwh must be > 0")
    termtol = float(o.get("terminal_soc_tolerance_pct", 3))
    termtie = float(o.get("terminal_soc_tiebreak_ore_per_kwh", 5))

    measured_soc = float(initial_soc_pct)
    planning_soc = max(hmin, min(hmax, measured_soc))
    initial = cap * planning_soc / 100.0
    mink = cap * hmin / 100.0
    maxk = cap * hmax / 100.0
    pmaxk = cap * pmax / 100.0
    regular_states, effstep = _state_grid(mink, maxk, reqstep, initial)
    init_idx = _nearest_regular_index(regular_states, initial)
    init_key = ("g", init_idx)

    continuation = _continuation_profile(rows, cfg, cap, pmaxk, ed)
    known_n = sum(1 for r in rows if bool(r.get("price_known")))
    boundary = known_n - 1 if 0 < known_n < len(rows) else None
    excess_rate = max(0.0, float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))

    states_now: dict[tuple[str, int], _State] = {init_key: _State(initial, 0.0)}
    parents: list[dict[tuple[str, int], tuple[Any, ...]]] = []
    generated_pv_candidates = 0
    retained_pv_candidates = 0
    max_active_states = 1

    for t, row in enumerate(rows):
        reserve_kwh, reserve_pct = _dynamic_reserve_kwh(row, cfg, cap)
        next_states: dict[tuple[str, int], _State] = {}
        back: dict[tuple[str, int], tuple[Any, ...]] = {}

        def consider(
            prev_key: tuple[str, int],
            prev_energy: float,
            prior_cost: float,
            e1: float,
            *,
            regular: bool,
            pv_following: bool,
        ) -> None:
            nonlocal retained_pv_candidates
            e1 = max(mink, min(maxk, float(e1)))
            action = _transition_action_kw(prev_energy, e1, ec, ed)
            if action < -cmax - 1e-9 or action > dmax + 1e-9:
                return
            result = _interval_result(row, action, cfg)
            if not result["feasible"]:
                return
            reserve_adj = _reserve_policy_penalty_ore(e1, reserve_kwh, cfg, cap, hmin, pmin)
            upper_adj = max(0.0, e1 - pmaxk) * excess_rate * DT_HOURS
            continuation_adj = 0.0
            if boundary is not None and t == boundary:
                target = float(continuation.get("target_kwh") or 0.0)
                ref = float(continuation.get("reference_price_ore_kwh") or 0.0)
                risk = float(continuation.get("risk_premium_ore_kwh") or 0.0)
                continuation_adj -= e1 * ref
                if e1 < target:
                    continuation_adj += (target - e1) * risk
            adjustment = reserve_adj + upper_adj + continuation_adj
            total = prior_cost + float(result["interval_cost_ore"]) + adjustment
            key = _candidate_key(regular_states, e1, regular=regular)
            existing = next_states.get(key)
            if existing is None or total < existing.cost_ore - 1e-12:
                if pv_following and existing is None:
                    retained_pv_candidates += 1
                next_states[key] = _State(e1, total)
                back[key] = (
                    prev_key,
                    action,
                    result,
                    reserve_kwh,
                    reserve_pct,
                    adjustment,
                    reserve_adj,
                    upper_adj,
                    continuation_adj,
                    pv_following,
                )

        for prev_key, prev_state in states_now.items():
            e0 = prev_state.energy_kwh
            prior = prev_state.cost_ore

            # Regular fine-grid destinations. Restrict the scan to states that
            # can be reached within the configured battery power limits.
            min_reachable = max(mink, e0 - (dmax * DT_HOURS / max(ed, 1e-9)))
            max_reachable = min(maxk, e0 + (cmax * DT_HOURS * ec))
            for e1 in regular_states:
                if e1 < min_reachable - 1e-9 or e1 > max_reachable + 1e-9:
                    continue
                consider(prev_key, e0, prior, e1, regular=True, pv_following=False)

            # Exact PV-following charge transition. It absorbs only the PV
            # surplus available after house load, never requiring grid energy.
            pv_surplus_kw = max(0.0, float(row.get("pv_kw") or 0.0) - float(row.get("load_kw") or 0.0))
            pv_charge_kw = min(cmax, pv_surplus_kw)
            if pv_charge_kw > 1e-9 and e0 < maxk - 1e-9:
                generated_pv_candidates += 1
                e1_pv = min(maxk, e0 + pv_charge_kw * DT_HOURS * ec)
                if e1_pv > e0 + 1e-9:
                    consider(prev_key, e0, prior, e1_pv, regular=False, pv_following=True)

        if not next_states:
            raise RuntimeError(f"No feasible refined deterministic states at {row.get('start')}")
        states_now = next_states
        parents.append(back)
        max_active_states = max(max_active_states, len(states_now))

    if continuation.get("enabled"):
        best_key = min(states_now, key=lambda k: states_now[k].cost_ore)
        terminal_constraint_applied = False
    else:
        tol = cap * termtol / 100.0
        candidates = [k for k, s in states_now.items() if abs(s.energy_kwh - initial) <= tol + 1e-9]
        if not candidates:
            nearest = min(abs(s.energy_kwh - initial) for s in states_now.values())
            candidates = [k for k, s in states_now.items() if abs(abs(s.energy_kwh - initial) - nearest) <= 1e-9]
        best_key = min(
            candidates,
            key=lambda k: states_now[k].cost_ore + abs(states_now[k].energy_kwh - initial) * termtie,
        )
        terminal_constraint_applied = True

    terminal_state = states_now[best_key]
    path: list[dict[str, Any]] = []
    key = best_key
    for t in range(len(rows) - 1, -1, -1):
        (
            prev_key,
            action,
            result,
            reserve_kwh,
            reserve_pct,
            adjustment,
            reserve_adj,
            upper_adj,
            continuation_adj,
            pv_following,
        ) = parents[t][key]
        state = states_now[key] if t == len(rows) - 1 else None
        # Destination energy is recoverable from the key only for regular
        # states, so use the action and previous energy during backtracking.
        if t == 0:
            prev_energy = initial
        else:
            # The previous destination energy is reconstructed below after the
            # reverse path has been assembled.
            prev_energy = 0.0
        path.append({
            "row_index": t,
            "state_key": key,
            "prev_key": prev_key,
            "action_kw": float(action),
            "reserve_soc_pct": float(reserve_pct),
            "result": result,
            "policy_adjustment_ore": float(adjustment),
            "reserve_policy_penalty_ore": float(reserve_adj),
            "preferred_max_excess_penalty_ore": float(upper_adj),
            "continuation_policy_adjustment_ore": float(continuation_adj),
            "pv_following_transition": bool(pv_following),
            "_terminal_energy_if_last": terminal_state.energy_kwh if t == len(rows) - 1 else None,
        })
        key = prev_key
    path.reverse()

    # Reconstruct exact energy trajectory from actions. This avoids relying on
    # the bucket key for off-grid states.
    e = initial
    for item in path:
        action = float(item["action_kw"])
        if action < 0:
            e += (-action) * DT_HOURS * ec
        elif action > 0:
            e -= action * DT_HOURS / max(ed, 1e-9)
        e = max(mink, min(maxk, e))
        item["soc_end_pct"] = float(e / cap * 100.0)
        item.pop("_terminal_energy_if_last", None)
        item.pop("state_key", None)
        item.pop("prev_key", None)

    objective_cost = float(states_now[best_key].cost_ore)
    selected_pv_transitions = sum(1 for item in path if item["pv_following_transition"])

    return {
        "planner": ENGINE_ID,
        "engine": ALGORITHM_ID,
        "measured_initial_soc_pct": measured_soc,
        "planning_initial_soc_pct": planning_soc,
        "terminal_soc_pct": round(float(path[-1]["soc_end_pct"]), 4),
        "terminal_soc_constraint_applied": terminal_constraint_applied,
        "soc_grid_requested_step_kwh": reqstep,
        "soc_grid_effective_max_step_kwh": round(effstep, 6),
        "soc_grid_state_count": len(regular_states),
        "max_active_states": max_active_states,
        "pv_following_candidates_generated": generated_pv_candidates,
        "pv_following_candidates_retained": retained_pv_candidates,
        "pv_following_transitions_selected": selected_pv_transitions,
        "objective_cost_ore": round(objective_cost, 6),
        "continuation": continuation,
        "first_action_kw": round(float(path[0]["action_kw"]), 6),
        "first_expected_soc_pct": round(float(path[0]["soc_end_pct"]), 4),
        "rows": path,
    }


class DeterministicRefinedV1:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def decide(self, engine_input: EngineInput) -> EngineDecision:
        solved = solve_refined_from_rows(
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
                "pv_following_transition": bool(solved_row.get("pv_following_transition")),
            })
        return EngineDecision(
            engine_id=ENGINE_ID,
            engine_version=ENGINE_VERSION,
            family="deterministic",
            information_vintage_id=engine_input.information_vintage_id,
            generated_at=engine_input.generated_at,
            decision_start=engine_input.decision_start,
            requested_action_kw=float(solved["first_action_kw"]),
            expected_soc_pct=float(solved["first_expected_soc_pct"]),
            status="ok",
            plan_rows=tuple(plan_rows),
            diagnostics={
                "algorithm_id": ALGORITHM_ID,
                "terminal_soc_pct": solved.get("terminal_soc_pct"),
                "terminal_soc_constraint_applied": solved.get("terminal_soc_constraint_applied"),
                "objective_cost_ore": solved.get("objective_cost_ore"),
                "continuation": solved.get("continuation"),
                "soc_grid_requested_step_kwh": solved.get("soc_grid_requested_step_kwh"),
                "soc_grid_effective_max_step_kwh": solved.get("soc_grid_effective_max_step_kwh"),
                "soc_grid_state_count": solved.get("soc_grid_state_count"),
                "max_active_states": solved.get("max_active_states"),
                "pv_following_candidates_generated": solved.get("pv_following_candidates_generated"),
                "pv_following_candidates_retained": solved.get("pv_following_candidates_retained"),
                "pv_following_transitions_selected": solved.get("pv_following_transitions_selected"),
            },
            model={
                "kind": "deterministic_dynamic_programming",
                "trainable": False,
                "frozen_baseline": False,
                "representation": "fine_grid_plus_exact_pv_following_candidates",
            },
        )
