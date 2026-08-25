from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import DB_PATH

PLANNER_NAME = "deterministic_battery_dp_v2"
DT_HOURS = 0.25


def _parse_ts(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _canonical_ts(value: str) -> str:
    return _parse_ts(value).replace(second=0, microsecond=0).isoformat()


def _latest_soc_pct() -> float | None:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT payload_json FROM raw_state ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        p = json.loads(row[0])
        item = p.get("battery_soc_pct") or {}
        if not item.get("available"):
            return None
        return float(item.get("state"))
    except Exception:
        return None


def _latest_load_rows() -> dict[str, dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT MAX(generated_at) FROM load_forecast_15m").fetchone()
        generated = row[0] if row else None
        if not generated:
            return {}
        rows = c.execute(
            "SELECT start_utc,payload_json FROM load_forecast_15m WHERE generated_at=? ORDER BY start_utc",
            (generated,),
        ).fetchall()
    out = {}
    for start, payload in rows:
        try:
            out[_canonical_ts(str(start))] = json.loads(payload)
        except Exception:
            continue
    return out


def _latest_pv_rows() -> dict[str, dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT MAX(generated_at) FROM pv_forecast_15m").fetchone()
        generated = row[0] if row else None
        if not generated:
            return {}
        rows = c.execute(
            "SELECT start_utc,payload_json,forecast_kw,uncertainty_kw FROM pv_forecast_15m WHERE generated_at=? ORDER BY start_utc",
            (generated,),
        ).fetchall()
    out = {}
    for start, payload, forecast_kw, uncertainty_kw in rows:
        item = {
            "pv_power_forecast_kw": float(forecast_kw),
            "pv_power_uncertainty_kw": float(uncertainty_kw),
        }
        if payload:
            try:
                item.update(json.loads(payload))
            except Exception:
                pass
        try:
            out[_canonical_ts(str(start))] = item
        except Exception:
            continue
    return out


def _price_rows() -> dict[str, float]:
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT start_utc,price_ore_kwh FROM price_15m ORDER BY start_utc").fetchall()
    out = {}
    for start, price in rows:
        try:
            out[_canonical_ts(str(start))] = float(price)
        except Exception:
            continue
    return out


def _nearest_price(stamp: datetime, prices: dict[str, float]) -> float | None:
    exact = prices.get(_canonical_ts(stamp.isoformat()))
    if exact is not None:
        return exact
    best = None
    best_delta = None
    for key, price in prices.items():
        try:
            d = abs((_parse_ts(key) - stamp).total_seconds())
        except Exception:
            continue
        if d <= 60 and (best_delta is None or d < best_delta):
            best = price
            best_delta = d
    return best


def horizon_diagnostics(cfg: dict[str, Any]) -> dict[str, Any]:
    load = _latest_load_rows()
    pv = _latest_pv_rows()
    prices = _price_rows()
    matched = sorted(set(load).intersection(pv), key=_parse_ts)
    return {
        "load_intervals": len(load),
        "pv_intervals": len(pv),
        "price_intervals": len(prices),
        "matched_load_pv_intervals": len(matched),
        "first_load": min(load, key=_parse_ts) if load else None,
        "first_pv": min(pv, key=_parse_ts) if pv else None,
        "first_match": matched[0] if matched else None,
        "requested_horizon_intervals": int((cfg.get("forecast") or {}).get("horizon_hours", 36)) * 4,
        "timestamp_join": "normalized_utc_minute",
    }


def _build_horizon(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    load = _latest_load_rows()
    pv = _latest_pv_rows()
    prices = _price_rows()
    starts = sorted(set(load).intersection(pv), key=_parse_ts)
    horizon_intervals = int((cfg.get("forecast") or {}).get("horizon_hours", 36)) * 4
    starts = starts[:horizon_intervals]
    if not starts:
        diag = horizon_diagnostics(cfg)
        raise RuntimeError(f"No overlapping load and PV forecast rows are available after UTC normalization; diagnostics={diag}")
    fallback_price = sorted(prices.values())[len(prices) // 2] if prices else None
    rows = []
    for start in starts:
        stamp = _parse_ts(start)
        l = load[start]
        p = pv[start]
        price = _nearest_price(stamp, prices)
        if price is None:
            if fallback_price is None:
                raise RuntimeError(f"No electricity price available for {start}")
            price = fallback_price
        rows.append({
            "start": stamp.isoformat(),
            "load_kw": float(l.get("house_load_forecast_kw") or l.get("total_forecast_kw") or 0.0),
            "base_load_kw": float(l.get("base_household_forecast_kw") or 0.0),
            "component_forecast_kw": l.get("component_forecast_kw") or {},
            "load_uncertainty_kw": float(l.get("house_load_uncertainty_kw") or 0.0),
            "pv_kw": float(p.get("pv_power_forecast_kw") or 0.0),
            "pv_uncertainty_kw": float(p.get("pv_power_uncertainty_kw") or 0.0),
            "price_ore_kwh": float(price),
        })
    return rows


def _state_grid(min_kwh: float, max_kwh: float, step_kwh: float, initial_kwh: float) -> list[float]:
    n = max(1, int(math.floor((max_kwh - min_kwh) / step_kwh)))
    states = [round(min_kwh + i * step_kwh, 6) for i in range(n + 1)]
    if states[-1] < max_kwh - 1e-6:
        states.append(round(max_kwh, 6))
    states.append(round(max(min_kwh, min(max_kwh, initial_kwh)), 6))
    return sorted(set(states))


def _transition_action_kw(e0: float, e1: float, eta_charge: float, eta_discharge: float) -> float:
    delta = e1 - e0
    if delta > 0:
        return -(delta / eta_charge) / DT_HOURS
    if delta < 0:
        return ((-delta) * eta_discharge) / DT_HOURS
    return 0.0


def _dynamic_reserve_kwh(row: dict[str, Any], cfg: dict[str, Any], capacity: float) -> tuple[float, float]:
    policy = (cfg.get("policy") or {}).get("battery") or {}
    opt = cfg.get("optimizer") or {}
    normal_pct = float(policy.get("normal_reserve_soc_pct", 20.0))
    high_pct = float(policy.get("high_uncertainty_reserve_soc_pct", 28.0))
    full_scale_kw = max(0.01, float(opt.get("reserve_uncertainty_full_scale_kw", 3.0)))
    uncertainty = max(0.0, float(row.get("load_uncertainty_kw") or 0.0)) + max(0.0, float(row.get("pv_uncertainty_kw") or 0.0))
    ratio = min(1.0, uncertainty / full_scale_kw)
    reserve_pct = normal_pct + (high_pct - normal_pct) * ratio
    return capacity * reserve_pct / 100.0, reserve_pct


def _interval_result(row: dict[str, Any], action_kw: float, cfg: dict[str, Any]) -> dict[str, float]:
    opt = cfg.get("optimizer") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    import_limit = float(opt.get("grid_import_limit_kw", 8.0))
    export_limit = float(opt.get("grid_export_limit_kw", 10.0))
    import_overhead = float(econ.get("import_overhead_ore_kwh", 0.0))
    export_overhead = float(econ.get("export_overhead_ore_kwh", 0.0))
    degradation = float(opt.get("battery_degradation_ore_kwh", 5.0))
    arbitrage_margin = float(econ.get("minimum_arbitrage_margin_ore_kwh", 20.0))

    net_before_battery = float(row["load_kw"]) - float(row["pv_kw"])
    grid_net = net_before_battery - action_kw
    grid_import = max(0.0, grid_net)
    raw_export = max(0.0, -grid_net)
    grid_export = min(raw_export, export_limit)
    curtailed = max(0.0, raw_export - export_limit)
    feasible = grid_import <= import_limit + 1e-9

    buy = float(row["price_ore_kwh"]) + import_overhead
    sell = max(0.0, float(row["price_ore_kwh"]) - export_overhead)
    energy_cost = grid_import * DT_HOURS * buy - grid_export * DT_HOURS * sell
    degradation_cost = abs(action_kw) * DT_HOURS * degradation

    # Only battery energy exported to the grid must clear the configured net-profit hurdle.
    # Charging cost, round-trip efficiency and degradation are already represented elsewhere
    # in the DP, so this is an additional required profit margin rather than a duplicate cost.
    battery_export_kw = 0.0
    if action_kw > 0.0 and grid_export > 0.0:
        discharge_needed_for_self_consumption = max(0.0, net_before_battery)
        battery_export_kw = min(grid_export, max(0.0, action_kw - discharge_needed_for_self_consumption))
    arbitrage_hurdle_cost = battery_export_kw * DT_HOURS * arbitrage_margin

    interval_cost = energy_cost + degradation_cost + arbitrage_hurdle_cost
    return {
        "feasible": 1.0 if feasible else 0.0,
        "grid_import_kw": grid_import,
        "grid_export_kw": grid_export,
        "battery_export_kw": battery_export_kw,
        "curtailed_kw": curtailed,
        "energy_cost_ore": energy_cost,
        "degradation_cost_ore": degradation_cost,
        "arbitrage_hurdle_cost_ore": arbitrage_hurdle_cost,
        "interval_cost_ore": interval_cost,
    }


def _baseline_cost(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> float:
    return sum(_interval_result(r, 0.0, cfg)["interval_cost_ore"] for r in rows)


def _classify_action(row: dict[str, Any], action_kw: float, result: dict[str, float]) -> tuple[str, dict[str, float]]:
    pv_surplus = max(0.0, float(row["pv_kw"]) - float(row["load_kw"]))
    if action_kw > 0.05:
        if result.get("battery_export_kw", 0.0) > 0.05:
            return "export_arbitrage", {
                "battery_to_grid_kw": result.get("battery_export_kw", 0.0),
                "battery_to_load_kw": max(0.0, action_kw - result.get("battery_export_kw", 0.0)),
            }
        return "self_consumption_discharge", {"battery_to_load_kw": action_kw, "battery_to_grid_kw": 0.0}
    if action_kw < -0.05:
        charge_kw = -action_kw
        pv_charge_kw = min(charge_kw, pv_surplus)
        grid_charge_kw = max(0.0, charge_kw - pv_charge_kw)
        if pv_charge_kw > 0.05 and grid_charge_kw > 0.05:
            reason = "mixed_charge"
        elif pv_charge_kw > 0.05:
            reason = "pv_charge"
        else:
            reason = "grid_charge"
        return reason, {"pv_to_battery_kw": pv_charge_kw, "grid_to_battery_kw": grid_charge_kw}
    return "hold", {}


def build_plan(cfg: dict[str, Any]) -> dict[str, Any]:
    rows = _build_horizon(cfg)
    policy = (cfg.get("policy") or {}).get("battery") or {}
    opt = cfg.get("optimizer") or {}
    capacity = float(policy.get("capacity_kwh", 19.6))
    hard_min_pct = float(policy.get("hard_min_soc_pct", 5.0))
    hard_max_pct = float(policy.get("hard_max_soc_pct", 100.0))
    preferred_min_pct = float(policy.get("preferred_min_soc_pct", 15.0))
    preferred_max_pct = float(policy.get("preferred_max_soc_pct", 90.0))
    normal_reserve_pct = float(policy.get("normal_reserve_soc_pct", 20.0))
    high_reserve_pct = float(policy.get("high_uncertainty_reserve_soc_pct", 28.0))
    max_charge_kw = float(opt.get("battery_max_charge_kw", 8.0))
    max_discharge_kw = float(opt.get("battery_max_discharge_kw", 8.0))
    eta_charge = float(opt.get("battery_charge_efficiency", 0.95))
    eta_discharge = float(opt.get("battery_discharge_efficiency", 0.95))
    state_step = float(opt.get("soc_grid_step_kwh", 0.5))
    reserve_penalty = float(opt.get("reserve_penalty_ore_per_kwh", 100.0))
    terminal_tolerance_pct = float(opt.get("terminal_soc_tolerance_pct", 3.0))
    terminal_tiebreak_ore_kwh = float(opt.get("terminal_soc_tiebreak_ore_per_kwh", 5.0))

    soc = _latest_soc_pct()
    if soc is None:
        raise RuntimeError("Current battery SOC is unavailable")
    initial_kwh = capacity * soc / 100.0
    min_kwh = capacity * hard_min_pct / 100.0
    max_kwh = capacity * hard_max_pct / 100.0
    preferred_min_kwh = capacity * preferred_min_pct / 100.0
    preferred_max_kwh = capacity * preferred_max_pct / 100.0
    states = _state_grid(min_kwh, max_kwh, state_step, initial_kwh)
    initial_idx = min(range(len(states)), key=lambda i: abs(states[i] - initial_kwh))

    costs = {initial_idx: 0.0}
    parents: list[dict[int, tuple[int, float, dict[str, float], float, float]]] = []
    for row in rows:
        reserve_kwh, reserve_pct = _dynamic_reserve_kwh(row, cfg, capacity)
        next_costs: dict[int, float] = {}
        back: dict[int, tuple[int, float, dict[str, float], float, float]] = {}
        for i0, prior_cost in costs.items():
            e0 = states[i0]
            for i1, e1 in enumerate(states):
                action = _transition_action_kw(e0, e1, eta_charge, eta_discharge)
                if action < -max_charge_kw - 1e-9 or action > max_discharge_kw + 1e-9:
                    continue
                result = _interval_result(row, action, cfg)
                if not result["feasible"]:
                    continue
                penalty = 0.0
                if e1 < reserve_kwh:
                    penalty += (reserve_kwh - e1) * reserve_penalty
                if e1 < preferred_min_kwh:
                    penalty += (preferred_min_kwh - e1) * reserve_penalty * 0.10
                if e1 > preferred_max_kwh:
                    penalty += (e1 - preferred_max_kwh) * reserve_penalty * 0.02
                total = prior_cost + result["interval_cost_ore"] + penalty
                if i1 not in next_costs or total < next_costs[i1]:
                    next_costs[i1] = total
                    back[i1] = (i0, action, result, reserve_kwh, reserve_pct)
        if not next_costs:
            raise RuntimeError(f"No feasible optimizer states at {row['start']}; check grid/battery limits")
        costs = next_costs
        parents.append(back)

    # Fair-horizon comparison: optimized plan must end near its starting SOC.
    tolerance_kwh = capacity * terminal_tolerance_pct / 100.0
    terminal_candidates = [idx for idx in costs if abs(states[idx] - initial_kwh) <= tolerance_kwh + 1e-9]
    if not terminal_candidates:
        nearest_delta = min(abs(states[idx] - initial_kwh) for idx in costs)
        terminal_candidates = [idx for idx in costs if abs(abs(states[idx] - initial_kwh) - nearest_delta) <= 1e-9]

    best_idx = min(
        terminal_candidates,
        key=lambda idx: costs[idx] + abs(states[idx] - initial_kwh) * terminal_tiebreak_ore_kwh,
    )

    path: list[tuple[int, int, float, dict[str, float], float, float]] = []
    idx = best_idx
    for t in range(len(rows) - 1, -1, -1):
        prev_idx, action, result, reserve_kwh, reserve_pct = parents[t][idx]
        path.append((prev_idx, idx, action, result, reserve_kwh, reserve_pct))
        idx = prev_idx
    path.reverse()

    out_rows = []
    expected_cost = 0.0
    total_battery_export_kwh = 0.0
    total_arbitrage_hurdle_ore = 0.0
    for row, (_, i1, action, result, reserve_kwh, reserve_pct) in zip(rows, path):
        expected_cost += result["interval_cost_ore"]
        total_battery_export_kwh += result.get("battery_export_kw", 0.0) * DT_HOURS
        total_arbitrage_hurdle_ore += result.get("arbitrage_hurdle_cost_ore", 0.0)
        reason, flow = _classify_action(row, action, result)
        out_rows.append({
            **row,
            "battery_action_kw": round(action, 4),
            "expected_soc_pct": round(states[i1] / capacity * 100.0, 2),
            "reserve_soc_pct": round(reserve_pct, 2),
            "grid_import_kw": round(result["grid_import_kw"], 4),
            "grid_export_kw": round(result["grid_export_kw"], 4),
            "battery_export_kw": round(result.get("battery_export_kw", 0.0), 4),
            "curtailed_kw": round(result["curtailed_kw"], 4),
            "energy_cost_ore": round(result["energy_cost_ore"], 4),
            "degradation_cost_ore": round(result["degradation_cost_ore"], 4),
            "arbitrage_hurdle_cost_ore": round(result.get("arbitrage_hurdle_cost_ore", 0.0), 4),
            "interval_cost_ore": round(result["interval_cost_ore"], 4),
            "reason": reason,
            "flow_breakdown_kw": {k: round(v, 4) for k, v in flow.items()},
        })

    baseline = _baseline_cost(rows, cfg)
    generated = datetime.now(timezone.utc).isoformat()
    terminal_soc_pct = states[best_idx] / capacity * 100.0
    terminal_delta_pct = terminal_soc_pct - soc
    return {
        "generated_at": generated,
        "planner": PLANNER_NAME,
        "mode": "shadow_read_only",
        "interval_minutes": 15,
        "horizon_hours": len(rows) // 4,
        "initial_soc_pct": round(soc, 2),
        "horizon_diagnostics": horizon_diagnostics(cfg),
        "constraints": {
            "battery_capacity_kwh": capacity,
            "hard_min_soc_pct": hard_min_pct,
            "hard_max_soc_pct": hard_max_pct,
            "preferred_min_soc_pct": preferred_min_pct,
            "preferred_max_soc_pct": preferred_max_pct,
            "normal_reserve_soc_pct": normal_reserve_pct,
            "high_uncertainty_reserve_soc_pct": high_reserve_pct,
            "reserve_uncertainty_full_scale_kw": float(opt.get("reserve_uncertainty_full_scale_kw", 3.0)),
            "battery_max_charge_kw": max_charge_kw,
            "battery_max_discharge_kw": max_discharge_kw,
            "grid_import_limit_kw": float(opt.get("grid_import_limit_kw", 8.0)),
            "grid_export_limit_kw": float(opt.get("grid_export_limit_kw", 10.0)),
            "charge_efficiency": eta_charge,
            "discharge_efficiency": eta_discharge,
            "soc_grid_step_kwh": state_step,
            "terminal_soc_tolerance_pct": terminal_tolerance_pct,
        },
        "objective": {
            "energy_cost": True,
            "battery_degradation_cost": True,
            "dynamic_uncertainty_reserve": True,
            "fair_terminal_soc_constraint": True,
            "battery_export_arbitrage": True,
            "minimum_net_arbitrage_margin_ore_kwh": float(((cfg.get("policy") or {}).get("economics") or {}).get("minimum_arbitrage_margin_ore_kwh", 20.0)),
            "opportunity_value_via_full_horizon_dp": True,
            "component_forecasts_included": True,
        },
        "summary": {
            "expected_cost_ore": round(expected_cost, 2),
            "baseline_cost_ore": round(baseline, 2),
            "expected_saving_ore": round(baseline - expected_cost, 2),
            "expected_saving_sek": round((baseline - expected_cost) / 100.0, 2),
            "terminal_soc_pct": round(terminal_soc_pct, 2),
            "terminal_soc_delta_pct": round(terminal_delta_pct, 2),
            "battery_export_kwh": round(total_battery_export_kwh, 3),
            "arbitrage_hurdle_cost_ore": round(total_arbitrage_hurdle_ore, 2),
        },
        "rows": out_rows,
    }
