from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .engine_contract import EngineDecision, EngineInput
from .optimizer import (
    DT_HOURS,
    _dynamic_reserve_kwh,
    _reserve_policy_penalty_ore,
    _state_grid,
    _transition_action_kw,
)

ENGINE_ID = "adaptive_deterministic_v1"
ENGINE_VERSION = "1"


@dataclass(frozen=True)
class AdaptiveParameters:
    """Learnable policy parameters. Physical constraints are deliberately absent."""

    pv_forecast_risk: float = 0.0
    load_forecast_risk: float = 0.0
    terminal_energy_value_ore_kwh: float = 150.0
    discharge_hurdle_ore_kwh: float = 20.0
    reserve_energy_value_ore_kwh: float = 10.0
    charge_hurdle_ore_kwh: float = 0.0
    cycling_penalty_ore_kwh: float = 5.0

    def bounded(self) -> "AdaptiveParameters":
        return replace(
            self,
            pv_forecast_risk=min(2.0, max(0.0, float(self.pv_forecast_risk))),
            load_forecast_risk=min(2.0, max(0.0, float(self.load_forecast_risk))),
            terminal_energy_value_ore_kwh=min(500.0, max(0.0, float(self.terminal_energy_value_ore_kwh))),
            discharge_hurdle_ore_kwh=min(100.0, max(0.0, float(self.discharge_hurdle_ore_kwh))),
            reserve_energy_value_ore_kwh=min(300.0, max(0.0, float(self.reserve_energy_value_ore_kwh))),
            charge_hurdle_ore_kwh=min(100.0, max(0.0, float(self.charge_hurdle_ore_kwh))),
            cycling_penalty_ore_kwh=min(50.0, max(0.0, float(self.cycling_penalty_ore_kwh))),
        )

    def as_dict(self) -> dict[str, float]:
        return {k: float(v) for k, v in asdict(self.bounded()).items()}


DEFAULT_PARAMETERS = AdaptiveParameters()


def risk_adjust_rows(rows: list[dict[str, Any]], params: AdaptiveParameters) -> list[dict[str, Any]]:
    """Convert forecast uncertainty into conservative point forecasts for this challenger only."""
    p = params.bounded()
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        pv = max(0.0, float(row.get("pv_kw") or 0.0))
        load = max(0.0, float(row.get("load_kw") or 0.0))
        pv_unc = max(0.0, float(row.get("pv_uncertainty_kw") or 0.0))
        load_unc = max(0.0, float(row.get("load_uncertainty_kw") or 0.0))
        row["raw_pv_kw"] = pv
        row["raw_load_kw"] = load
        row["pv_kw"] = max(0.0, pv - p.pv_forecast_risk * pv_unc)
        row["load_kw"] = max(0.0, load + p.load_forecast_risk * load_unc)
        out.append(row)
    return out


def _interval_result_adaptive(
    row: dict[str, Any], action: float, cfg: dict[str, Any], params: AdaptiveParameters
) -> dict[str, float]:
    p = params.bounded()
    o = cfg.get("optimizer") or {}
    e = (cfg.get("policy") or {}).get("economics") or {}
    ilim = float(o.get("physical_grid_import_limit_kw", 13.8))
    elim = float(o.get("grid_export_limit_kw", 10.0))
    load, pv = float(row["load_kw"]), float(row["pv_kw"])
    net = load - pv
    grid = net - action
    imp, rawexp = max(0.0, grid), max(0.0, -grid)
    exp = min(rawexp, elim)
    pv_surplus, charge = max(0.0, pv - load), max(0.0, -action)
    pv_charge = min(charge, pv_surplus)
    grid_charge = max(0.0, charge - pv_charge)
    batt_export = min(exp, max(0.0, action - max(0.0, net))) if action > 0 and exp > 0 else 0.0
    required = max(0.0, net - ilim)
    discretionary = max(0.0, action - required) if action > 0 else 0.0
    feasible = imp <= ilim + 1e-9

    cycling = abs(action) * DT_HOURS * p.cycling_penalty_ore_kwh
    discharge_hurdle = 0.0
    charge_hurdle = 0.0
    if not row["price_known"]:
        if grid_charge > 1e-6 or batt_export > 1e-6 or (required <= 1e-6 and action > 1e-6):
            feasible = False
        energy = 0.0
    else:
        price = float(row["price_ore_kwh"])
        buy = price + float(e.get("import_overhead_ore_kwh", 0.0))
        sell = max(0.0, price - float(e.get("export_overhead_ore_kwh", 0.0)))
        energy = imp * DT_HOURS * buy - exp * DT_HOURS * sell
        discharge_hurdle = discretionary * DT_HOURS * p.discharge_hurdle_ore_kwh
        charge_hurdle = grid_charge * DT_HOURS * p.charge_hurdle_ore_kwh

    return {
        "feasible": 1.0 if feasible else 0.0,
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "grid_charge_kw": grid_charge,
        "battery_export_kw": batt_export,
        "required_physical_discharge_kw": required,
        "discretionary_discharge_kw": discretionary,
        "energy_cost_ore": energy,
        "cycling_penalty_ore": cycling,
        "discharge_hurdle_cost_ore": discharge_hurdle,
        "charge_hurdle_cost_ore": charge_hurdle,
        "interval_cost_ore": energy + cycling + discharge_hurdle + charge_hurdle,
    }


def solve_adaptive_from_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    initial_soc_pct: float,
    params: AdaptiveParameters,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("rows must contain at least one forecast interval")
    p = params.bounded()
    rows = risk_adjust_rows(rows, p)
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

    initial = cap * max(hmin, min(hmax, float(initial_soc_pct))) / 100.0
    mink, maxk, pmaxk = cap * hmin / 100.0, cap * hmax / 100.0, cap * pmax / 100.0
    states, _ = _state_grid(mink, maxk, reqstep, initial)
    init_idx = min(range(len(states)), key=lambda i: abs(states[i] - initial))
    known_n = sum(1 for r in rows if bool(r.get("price_known")))
    boundary = known_n - 1 if 0 < known_n < len(rows) else None
    costs: dict[int, float] = {init_idx: 0.0}
    parents: list[dict[int, tuple[Any, ...]]] = []

    reserve_cfg = {
        **cfg,
        "optimizer": {
            **o,
            "reserve_target_penalty_ore_per_kwh_hour": p.reserve_energy_value_ore_kwh,
        },
    }

    for t, row in enumerate(rows):
        reserve_kwh, reserve_pct = _dynamic_reserve_kwh(row, cfg, cap)
        nxt: dict[int, float] = {}
        back: dict[int, tuple[Any, ...]] = {}
        for i0, prior in costs.items():
            for i1, e1 in enumerate(states):
                action = _transition_action_kw(states[i0], e1, ec, ed)
                if action < -cmax - 1e-9 or action > dmax + 1e-9:
                    continue
                result = _interval_result_adaptive(row, action, cfg, p)
                if not result["feasible"]:
                    continue
                reserve_adj = _reserve_policy_penalty_ore(e1, reserve_kwh, reserve_cfg, cap, hmin, pmin)
                upper_adj = (
                    max(0.0, e1 - pmaxk)
                    * max(0.0, float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))
                    * DT_HOURS
                )
                continuation_adj = 0.0
                if boundary is not None and t == boundary:
                    continuation_adj -= e1 * p.terminal_energy_value_ore_kwh
                total = prior + float(result["interval_cost_ore"]) + reserve_adj + upper_adj + continuation_adj
                if i1 not in nxt or total < nxt[i1]:
                    nxt[i1] = total
                    back[i1] = (i0, action, result, reserve_pct, reserve_adj, upper_adj, continuation_adj)
        if not nxt:
            raise RuntimeError(f"No feasible adaptive states at {row.get('start')}")
        costs, parents = nxt, parents + [back]

    if boundary is not None:
        best = min(costs, key=costs.get)
        terminal_constraint_applied = False
    else:
        tol = cap * termtol / 100.0
        candidates = [i for i in costs if abs(states[i] - initial) <= tol + 1e-9]
        if not candidates:
            nearest = min(abs(states[i] - initial) for i in costs)
            candidates = [i for i in costs if abs(abs(states[i] - initial) - nearest) <= 1e-9]
        best = min(candidates, key=costs.get)
        terminal_constraint_applied = True

    path: list[dict[str, Any]] = []
    idx = best
    for t in range(len(rows) - 1, -1, -1):
        prev, action, result, reserve_pct, reserve_adj, upper_adj, continuation_adj = parents[t][idx]
        path.append({
            "row_index": t,
            "action_kw": float(action),
            "soc_end_pct": float(states[idx] / cap * 100.0),
            "reserve_soc_pct": float(reserve_pct),
            "result": result,
            "reserve_policy_penalty_ore": float(reserve_adj),
            "preferred_max_excess_penalty_ore": float(upper_adj),
            "continuation_policy_adjustment_ore": float(continuation_adj),
        })
        idx = int(prev)
    path.reverse()
    return {
        "engine": ENGINE_ID,
        "parameters": p.as_dict(),
        "terminal_soc_pct": round(states[best] / cap * 100.0, 4),
        "terminal_soc_constraint_applied": terminal_constraint_applied,
        "objective_cost_ore": round(float(costs[best]), 6),
        "first_action_kw": round(float(path[0]["action_kw"]), 6),
        "first_expected_soc_pct": round(float(path[0]["soc_end_pct"]), 4),
        "rows": path,
    }


class AdaptiveDeterministicV1:
    def __init__(self, cfg: dict[str, Any], params: AdaptiveParameters | None = None):
        self.cfg = cfg
        self.params = (params or DEFAULT_PARAMETERS).bounded()

    def decide(self, engine_input: EngineInput) -> EngineDecision:
        solved = solve_adaptive_from_rows(
            self.cfg,
            [dict(r) for r in engine_input.horizon_rows],
            float(engine_input.initial_soc_pct),
            self.params,
        )
        plan_rows = []
        for source, solved_row in zip(engine_input.horizon_rows, solved["rows"]):
            plan_rows.append({
                "start": source["start"],
                "requested_action_kw": round(float(solved_row["action_kw"]), 6),
                "expected_soc_pct": round(float(solved_row["soc_end_pct"]), 6),
                "reserve_soc_pct": round(float(solved_row["reserve_soc_pct"]), 6),
            })
        return EngineDecision(
            engine_id=ENGINE_ID,
            engine_version=ENGINE_VERSION,
            family="adaptive_deterministic",
            information_vintage_id=engine_input.information_vintage_id,
            generated_at=engine_input.generated_at,
            decision_start=engine_input.decision_start,
            requested_action_kw=float(solved["first_action_kw"]),
            expected_soc_pct=float(solved["first_expected_soc_pct"]),
            status="ok",
            plan_rows=tuple(plan_rows),
            diagnostics={
                "objective_cost_ore": solved["objective_cost_ore"],
                "terminal_soc_pct": solved["terminal_soc_pct"],
                "terminal_soc_constraint_applied": solved["terminal_soc_constraint_applied"],
                "learned_parameters": solved["parameters"],
                "mode": "challenger_shadow",
            },
            model={
                "kind": "adaptive_deterministic_dynamic_programming",
                "trainable": True,
                "parameters": solved["parameters"],
            },
        )
