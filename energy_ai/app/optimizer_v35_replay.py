from __future__ import annotations

from typing import Any

from .optimizer import (
    DT_HOURS,
    PLANNER_NAME,
    _continuation_profile,
    _dynamic_reserve_kwh,
    _interval_result,
    _reserve_policy_penalty_ore,
    _state_grid,
    _transition_action_kw,
)


def solve_v35_from_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    initial_soc_pct: float,
) -> dict[str, Any]:
    """Pure v3.5 solve using an injected information horizon and SOC.

    This deliberately mirrors optimizer.build_plan's DP/terminal logic while
    avoiding all live-DB reads. It is used only for historical evaluation.
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
    reqstep = float(o.get("soc_grid_step_kwh", 0.5))
    termtol = float(o.get("terminal_soc_tolerance_pct", 3))
    termtie = float(o.get("terminal_soc_tiebreak_ore_per_kwh", 5))

    measured_soc = float(initial_soc_pct)
    planning_soc = max(hmin, min(hmax, measured_soc))
    initial = cap * planning_soc / 100.0
    mink = cap * hmin / 100.0
    maxk = cap * hmax / 100.0
    pmaxk = cap * pmax / 100.0
    states, effstep = _state_grid(mink, maxk, reqstep, initial)
    init_idx = min(range(len(states)), key=lambda i: abs(states[i] - initial))

    continuation = _continuation_profile(rows, cfg, cap, pmaxk, ed)
    known_n = sum(1 for r in rows if bool(r.get("price_known")))
    boundary = known_n - 1 if 0 < known_n < len(rows) else None
    costs: dict[int, float] = {init_idx: 0.0}
    parents: list[dict[int, tuple[Any, ...]]] = []
    excess_rate = max(0.0, float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))

    for t, row in enumerate(rows):
        reserve_kwh, reserve_pct = _dynamic_reserve_kwh(row, cfg, cap)
        nxt: dict[int, float] = {}
        back: dict[int, tuple[Any, ...]] = {}
        for i0, prior in costs.items():
            for i1, e1 in enumerate(states):
                action = _transition_action_kw(states[i0], e1, ec, ed)
                if action < -cmax - 1e-9 or action > dmax + 1e-9:
                    continue
                result = _interval_result(row, action, cfg)
                if not result["feasible"]:
                    continue
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
                total = prior + float(result["interval_cost_ore"]) + adjustment
                if i1 not in nxt or total < nxt[i1]:
                    nxt[i1] = total
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
                    )
        if not nxt:
            raise RuntimeError(f"No feasible v3.5 replay states at {row.get('start')}")
        costs = nxt
        parents.append(back)

    if continuation.get("enabled"):
        best = min(costs, key=costs.get)
        terminal_constraint_applied = False
    else:
        tol = cap * termtol / 100.0
        candidates = [i for i in costs if abs(states[i] - initial) <= tol + 1e-9]
        if not candidates:
            nearest = min(abs(states[i] - initial) for i in costs)
            candidates = [i for i in costs if abs(abs(states[i] - initial) - nearest) <= 1e-9]
        best = min(candidates, key=lambda i: costs[i] + abs(states[i] - initial) * termtie)
        terminal_constraint_applied = True

    path: list[dict[str, Any]] = []
    idx = best
    for t in range(len(rows) - 1, -1, -1):
        prev, action, result, reserve_kwh, reserve_pct, adjustment, reserve_adj, upper_adj, continuation_adj = parents[t][idx]
        path.append({
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
        })
        idx = int(prev)
    path.reverse()

    return {
        "planner": PLANNER_NAME,
        "engine": "pure_v35_replay_v1",
        "measured_initial_soc_pct": measured_soc,
        "planning_initial_soc_pct": planning_soc,
        "terminal_soc_pct": round(states[best] / cap * 100.0, 4),
        "terminal_soc_constraint_applied": terminal_constraint_applied,
        "soc_grid_effective_max_step_kwh": round(effstep, 6),
        "objective_cost_ore": round(float(costs[best]), 6),
        "continuation": continuation,
        "first_action_kw": round(float(path[0]["action_kw"]), 6),
        "first_expected_soc_pct": round(float(path[0]["soc_end_pct"]), 4),
        "rows": path,
    }
