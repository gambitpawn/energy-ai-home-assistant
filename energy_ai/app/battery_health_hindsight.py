from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .battery_health_cost import (
    BATTERY_HEALTH_COST_VERSION,
    DEFAULT_BATTERY_HEALTH_PARAMETERS,
    BatteryHealthParameters,
    battery_health_cost,
)
from .optimizer import DT_HOURS, _reserve_policy_penalty_ore, _state_grid, _transition_action_kw
from .optimizer_evaluation import _actual_rows

HINDSIGHT_VERSION = "battery_health_hindsight_v2"
DEFAULT_GRID_STEP_KWH = 0.1

HEALTH_PROFILES: dict[str, BatteryHealthParameters] = {
    "mild": replace(
        DEFAULT_BATTERY_HEALTH_PARAMETERS,
        high_soc_zone_1_cost_ore_per_kwh_hour=2.0,
        high_soc_zone_2_cost_ore_per_kwh_hour=8.0,
        high_soc_zone_3_cost_ore_per_kwh_hour=25.0,
    ),
    "default": DEFAULT_BATTERY_HEALTH_PARAMETERS,
    "strong": replace(
        DEFAULT_BATTERY_HEALTH_PARAMETERS,
        high_soc_zone_1_cost_ore_per_kwh_hour=10.0,
        high_soc_zone_2_cost_ore_per_kwh_hour=30.0,
        high_soc_zone_3_cost_ore_per_kwh_hour=100.0,
    ),
}


def profile_parameters(name: str) -> BatteryHealthParameters:
    try:
        return HEALTH_PROFILES[str(name)].validated()
    except KeyError as exc:
        raise ValueError(f"unknown battery-health profile: {name}") from exc


def _dt(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _time_above_threshold_hours(e0: float, e1: float, threshold: float, duration: float) -> float:
    if duration <= 0.0:
        return 0.0
    a, b = float(e0), float(e1)
    t = float(threshold)
    if a >= t and b >= t:
        return float(duration)
    if a <= t and b <= t:
        return 0.0
    if abs(b - a) <= 1e-12:
        return float(duration) if a > t else 0.0
    crossing_fraction = (t - a) / (b - a)
    crossing_fraction = min(1.0, max(0.0, crossing_fraction))
    if b > a:
        return float(duration) * (1.0 - crossing_fraction)
    return float(duration) * crossing_fraction


def _state_grid_with_anchors(
    min_kwh: float,
    max_kwh: float,
    step_kwh: float,
    initial_kwh: float,
    terminal_kwh: float,
    parameters: BatteryHealthParameters,
    capacity_kwh: float,
) -> list[float]:
    states, _ = _state_grid(min_kwh, max_kwh, step_kwh, initial_kwh)
    anchors = [
        terminal_kwh,
        capacity_kwh * parameters.high_soc_threshold_1_pct / 100.0,
        capacity_kwh * parameters.high_soc_threshold_2_pct / 100.0,
        capacity_kwh * parameters.high_soc_threshold_3_pct / 100.0,
    ]
    return sorted(set(states + [round(max(min_kwh, min(max_kwh, x)), 6) for x in anchors]))


def _transition_health_cache(
    states: list[float],
    *,
    capacity_kwh: float,
    interval_hours: float,
    parameters: BatteryHealthParameters,
    charge_efficiency: float,
    discharge_efficiency: float,
    max_charge_kw: float,
    max_discharge_kw: float,
) -> dict[int, list[tuple[int, float, dict[str, Any]]]]:
    cache: dict[int, list[tuple[int, float, dict[str, Any]]]] = {}
    for i0, e0 in enumerate(states):
        min_reachable = e0 - max_discharge_kw * interval_hours / max(discharge_efficiency, 1e-9)
        max_reachable = e0 + max_charge_kw * interval_hours * charge_efficiency
        options: list[tuple[int, float, dict[str, Any]]] = []
        for i1, e1 in enumerate(states):
            if e1 < min_reachable - 1e-9 or e1 > max_reachable + 1e-9:
                continue
            action = float(_transition_action_kw(e0, e1, charge_efficiency, discharge_efficiency))
            if action < -max_charge_kw - 1e-9 or action > max_discharge_kw + 1e-9:
                continue
            health = battery_health_cost(
                energy_start_kwh=e0,
                energy_end_kwh=e1,
                capacity_kwh=capacity_kwh,
                interval_hours=interval_hours,
                parameters=parameters,
            )
            options.append((i1, action, health))
        cache[i0] = options
    return cache


def _interval_economics(row: dict[str, Any], action_kw: float, cfg: dict[str, Any]) -> dict[str, float | bool]:
    opt = cfg.get("optimizer") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    load = float(row["load_kw"])
    pv = float(row["pv_kw"])
    net = load - pv
    grid = net - float(action_kw)
    import_kw = max(0.0, grid)
    raw_export_kw = max(0.0, -grid)
    import_limit = float(opt.get("physical_grid_import_limit_kw", 13.8))
    export_limit = float(opt.get("grid_export_limit_kw", 10.0))
    export_kw = min(raw_export_kw, export_limit)
    curtailed_kw = max(0.0, raw_export_kw - export_limit)
    feasible = import_kw <= import_limit + 1e-9
    price = float(row["price_ore_kwh"])
    buy = price + float(econ.get("import_overhead_ore_kwh", 0.0))
    sell = max(0.0, price - float(econ.get("export_overhead_ore_kwh", 0.0)))
    energy_cost = (import_kw * buy - export_kw * sell) * DT_HOURS
    required = max(0.0, net - import_limit)
    discretionary = max(0.0, float(action_kw) - required) if action_kw > 0.0 else 0.0
    hurdle = discretionary * DT_HOURS * float(econ.get("minimum_arbitrage_margin_ore_kwh", 20.0))
    return {
        "feasible": feasible,
        "grid_import_kw": import_kw,
        "grid_export_kw": export_kw,
        "curtailed_kw": curtailed_kw,
        "energy_cost_ore": energy_cost,
        "discretionary_hurdle_cost_ore": hurdle,
    }


def solve_hindsight_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    initial_soc_pct: float,
    terminal_soc_pct: float,
    parameters: BatteryHealthParameters = DEFAULT_BATTERY_HEALTH_PARAMETERS,
    grid_step_kwh: float = DEFAULT_GRID_STEP_KWH,
    include_actions: bool = False,
) -> dict[str, Any]:
    """Perfect-information daily DP using canonical battery-health cost v1.

    This solver is diagnostic-only. It deliberately lives beside, rather than
    replacing, the deployed hindsight/oracle. The terminal SOC is fixed so
    profile comparisons cannot obtain an artificial advantage by ending the day
    with a different amount of stored energy.
    """
    if not rows:
        return {"status": "no_rows"}
    params = parameters.validated()
    if grid_step_kwh <= 0.0:
        raise ValueError("grid_step_kwh must be > 0")

    tariffs = cfg.get("tariffs") or {}
    active_tariffs = [
        name
        for name, item in tariffs.items()
        if isinstance(item, dict) and bool(item.get("enabled"))
    ]
    if bool(tariffs.get("enabled")) and active_tariffs:
        return {"status": "unsupported_active_tariffs", "active_tariffs": active_tariffs}

    battery = (cfg.get("policy") or {}).get("battery") or {}
    opt = cfg.get("optimizer") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    pmin = float(battery.get("preferred_min_soc_pct", 15.0))
    normal = float(battery.get("normal_reserve_soc_pct", 20.0))
    cmax = float(opt.get("battery_max_charge_kw", 8.0))
    dmax = float(opt.get("battery_max_discharge_kw", 8.0))
    ec = float(opt.get("battery_charge_efficiency", 0.95))
    ed = float(opt.get("battery_discharge_efficiency", 0.95))

    initial_soc = max(hmin, min(hmax, float(initial_soc_pct)))
    terminal_soc = max(hmin, min(hmax, float(terminal_soc_pct)))
    initial_e = cap * initial_soc / 100.0
    terminal_e = cap * terminal_soc / 100.0
    min_e = cap * hmin / 100.0
    max_e = cap * hmax / 100.0
    states = _state_grid_with_anchors(
        min_e,
        max_e,
        float(grid_step_kwh),
        initial_e,
        terminal_e,
        params,
        cap,
    )
    init_idx = min(range(len(states)), key=lambda i: abs(states[i] - initial_e))
    terminal_idx = min(range(len(states)), key=lambda i: abs(states[i] - terminal_e))
    if abs(states[terminal_idx] - terminal_e) > 1e-6:
        raise RuntimeError("terminal SOC anchor missing from diagnostic state grid")

    transitions = _transition_health_cache(
        states,
        capacity_kwh=cap,
        interval_hours=DT_HOURS,
        parameters=params,
        charge_efficiency=ec,
        discharge_efficiency=ed,
        max_charge_kw=cmax,
        max_discharge_kw=dmax,
    )
    reserve_kwh = cap * max(hmin, min(hmax, normal)) / 100.0

    costs: dict[int, float] = {init_idx: 0.0}
    parents: list[dict[int, tuple[Any, ...]]] = []
    max_active_states = 1
    for row in rows:
        nxt: dict[int, float] = {}
        back: dict[int, tuple[Any, ...]] = {}
        for i0, prior in costs.items():
            e0 = states[i0]
            for i1, action, health in transitions[i0]:
                economics = _interval_economics(row, action, cfg)
                if not bool(economics["feasible"]):
                    continue
                e1 = states[i1]
                reserve = _reserve_policy_penalty_ore(e1, reserve_kwh, cfg, cap, hmin, pmin)
                total = (
                    float(prior)
                    + float(economics["energy_cost_ore"])
                    + float(economics["discretionary_hurdle_cost_ore"])
                    + float(health["total_battery_health_cost_ore"])
                    + float(reserve)
                )
                if i1 not in nxt or total < nxt[i1] - 1e-12:
                    nxt[i1] = total
                    back[i1] = (i0, action, economics, health, reserve)
        if not nxt:
            return {"status": "infeasible", "reason": f"no feasible states at {row.get('start')}"}
        costs = nxt
        parents.append(back)
        max_active_states = max(max_active_states, len(costs))

    if terminal_idx not in costs:
        return {
            "status": "infeasible_terminal_soc",
            "terminal_soc_pct": terminal_soc,
            "nearest_terminal_soc_pct": round(states[min(costs, key=lambda i: abs(states[i] - terminal_e))] / cap * 100.0, 4),
        }

    idx = terminal_idx
    reverse_path: list[dict[str, Any]] = []
    for t in range(len(rows) - 1, -1, -1):
        if idx not in parents[t]:
            return {"status": "infeasible_backtrack", "row_index": t}
        prev, action, economics, health, reserve = parents[t][idx]
        reverse_path.append({
            "row_index": t,
            "state_index": idx,
            "prev_state_index": prev,
            "action_kw": float(action),
            "soc_start_pct": states[prev] / cap * 100.0,
            "soc_end_pct": states[idx] / cap * 100.0,
            "economics": economics,
            "health": health,
            "reserve_policy_penalty_ore": float(reserve),
        })
        idx = int(prev)
    path = list(reversed(reverse_path))

    energy_cost = 0.0
    hurdle_cost = 0.0
    reserve_cost = 0.0
    cycle_cost = 0.0
    high_soc_cost = 0.0
    import_kwh = 0.0
    export_kwh = 0.0
    throughput_kwh = 0.0
    max_soc = initial_soc
    hours_above = {90.0: 0.0, 95.0: 0.0, 98.0: 0.0}
    last_charge_start: str | None = None
    last_charge_end: str | None = None
    first_reach: dict[str, str | None] = {"95": None, "98": None, "99.5": None}
    actions: list[dict[str, Any]] = []

    for row, item in zip(rows, path):
        eco = item["economics"]
        health = item["health"]
        start_soc = float(item["soc_start_pct"])
        end_soc = float(item["soc_end_pct"])
        e0 = cap * start_soc / 100.0
        e1 = cap * end_soc / 100.0
        energy_cost += float(eco["energy_cost_ore"])
        hurdle_cost += float(eco["discretionary_hurdle_cost_ore"])
        reserve_cost += float(item["reserve_policy_penalty_ore"])
        cycle_cost += float(health["cycle_wear_cost_ore"])
        high_soc_cost += float(health["high_soc_occupancy_cost_ore"])
        import_kwh += float(eco["grid_import_kw"]) * DT_HOURS
        export_kwh += float(eco["grid_export_kw"]) * DT_HOURS
        throughput_kwh += float(health["internal_throughput_kwh"])
        max_soc = max(max_soc, end_soc)
        for threshold in hours_above:
            hours_above[threshold] += _time_above_threshold_hours(
                e0,
                e1,
                cap * threshold / 100.0,
                DT_HOURS,
            )
        start = str(row["start"])
        end = (_dt(start) + timedelta(hours=DT_HOURS)).isoformat()
        if float(item["action_kw"]) < -0.05:
            last_charge_start = start
            last_charge_end = end
        for threshold in (95.0, 98.0, 99.5):
            key = f"{threshold:g}"
            if first_reach[key] is None and end_soc >= threshold - 1e-9:
                first_reach[key] = end
        if include_actions:
            actions.append({
                "start": start,
                "load_kw": round(float(row["load_kw"]), 4),
                "pv_kw": round(float(row["pv_kw"]), 4),
                "price_ore_kwh": round(float(row["price_ore_kwh"]), 4),
                "action_kw": round(float(item["action_kw"]), 4),
                "soc_start_pct": round(start_soc, 3),
                "soc_end_pct": round(end_soc, 3),
                "grid_import_kw": round(float(eco["grid_import_kw"]), 4),
                "grid_export_kw": round(float(eco["grid_export_kw"]), 4),
                "energy_cost_ore": round(float(eco["energy_cost_ore"]), 4),
                "cycle_wear_cost_ore": round(float(health["cycle_wear_cost_ore"]), 4),
                "high_soc_cost_ore": round(float(health["high_soc_occupancy_cost_ore"]), 4),
                "reserve_policy_penalty_ore": round(float(item["reserve_policy_penalty_ore"]), 4),
            })

    battery_health_total = cycle_cost + high_soc_cost
    canonical = energy_cost + hurdle_cost + reserve_cost + battery_health_total
    result: dict[str, Any] = {
        "status": "optimal",
        "hindsight_version": HINDSIGHT_VERSION,
        "cost_version": BATTERY_HEALTH_COST_VERSION,
        "initial_soc_pct": round(initial_soc, 4),
        "terminal_soc_pct": round(terminal_soc, 4),
        "max_soc_pct": round(max_soc, 4),
        "soc_grid_step_kwh": float(grid_step_kwh),
        "soc_grid_state_count": len(states),
        "max_active_states": max_active_states,
        "battery_throughput_kwh": round(throughput_kwh, 4),
        "grid_import_kwh": round(import_kwh, 4),
        "grid_export_kwh": round(export_kwh, 4),
        "energy_cost_ore": round(energy_cost, 4),
        "energy_cost_sek": round(energy_cost / 100.0, 4),
        "cycle_wear_cost_ore": round(cycle_cost, 4),
        "high_soc_occupancy_cost_ore": round(high_soc_cost, 4),
        "battery_health_cost_ore": round(battery_health_total, 4),
        "battery_health_cost_sek": round(battery_health_total / 100.0, 4),
        "discretionary_hurdle_cost_ore": round(hurdle_cost, 4),
        "reserve_policy_penalty_ore": round(reserve_cost, 4),
        "canonical_objective_cost_ore": round(canonical, 4),
        "canonical_objective_cost_sek": round(canonical / 100.0, 4),
        "hours_above_90_soc": round(hours_above[90.0], 4),
        "hours_above_95_soc": round(hours_above[95.0], 4),
        "hours_above_98_soc": round(hours_above[98.0], 4),
        "first_reach_95_soc": first_reach["95"],
        "first_reach_98_soc": first_reach["98"],
        "first_reach_99_5_soc": first_reach["99.5"],
        "last_charge_start": last_charge_start,
        "last_charge_end": last_charge_end,
        "parameters": params.as_dict(),
        "intervals": len(rows),
    }
    if include_actions:
        result["actions"] = actions
    return result


def compare_profiles_for_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    initial_soc_pct: float,
    terminal_soc_pct: float,
    grid_step_kwh: float = DEFAULT_GRID_STEP_KWH,
    include_actions: bool = False,
) -> dict[str, Any]:
    results = {
        name: solve_hindsight_rows(
            cfg,
            rows,
            initial_soc_pct=initial_soc_pct,
            terminal_soc_pct=terminal_soc_pct,
            parameters=params,
            grid_step_kwh=grid_step_kwh,
            include_actions=include_actions,
        )
        for name, params in HEALTH_PROFILES.items()
    }
    return {
        "diagnostic_only": True,
        "planner_integration_enabled": False,
        "selector_integration_enabled": False,
        "physical_write_performed": False,
        "hindsight_version": HINDSIGHT_VERSION,
        "cost_version": BATTERY_HEALTH_COST_VERSION,
        "grid_step_kwh": float(grid_step_kwh),
        "profiles": results,
    }


def compare_profiles_for_day(
    cfg: dict[str, Any],
    local_date: str | date,
    *,
    grid_step_kwh: float = DEFAULT_GRID_STEP_KWH,
    include_actions: bool = False,
) -> dict[str, Any]:
    day = date.fromisoformat(local_date) if isinstance(local_date, str) else local_date
    rows, data = _actual_rows(day)
    if float(data.get("actual_coverage_fraction") or 0.0) < 0.90:
        return {
            "diagnostic_only": True,
            "hindsight_version": HINDSIGHT_VERSION,
            "local_date": day.isoformat(),
            "status": "insufficient_actual_coverage",
            "data": data,
        }
    first_soc = next(
        (r.get("battery_soc_start_pct") for r in rows if r.get("battery_soc_start_pct") is not None),
        None,
    )
    terminal_soc = next(
        (r.get("battery_soc_end_pct") for r in reversed(rows) if r.get("battery_soc_end_pct") is not None),
        None,
    )
    if first_soc is None:
        return {
            "diagnostic_only": True,
            "hindsight_version": HINDSIGHT_VERSION,
            "local_date": day.isoformat(),
            "status": "missing_initial_soc",
            "data": data,
        }
    if terminal_soc is None:
        return {
            "diagnostic_only": True,
            "hindsight_version": HINDSIGHT_VERSION,
            "local_date": day.isoformat(),
            "status": "missing_terminal_soc",
            "data": data,
        }

    comparison = compare_profiles_for_rows(
        cfg,
        rows,
        initial_soc_pct=float(first_soc),
        terminal_soc_pct=float(terminal_soc),
        grid_step_kwh=grid_step_kwh,
        include_actions=include_actions,
    )
    comparison.update({
        "local_date": day.isoformat(),
        "status": "ok",
        "data": data,
        "terminal_semantics": "fixed_to_observed_day_terminal_soc_for_profile_comparability",
        "observed_initial_soc_pct": round(float(first_soc), 4),
        "observed_terminal_soc_pct": round(float(terminal_soc), 4),
    })
    return comparison
