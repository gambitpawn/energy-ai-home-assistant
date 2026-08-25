from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .optimizer import (
    DT_HOURS,
    _build_horizon,
    _continuation_profile,
    _dynamic_reserve_kwh,
    _latest_soc_pct,
    build_plan,
)

LOCAL_TZ = ZoneInfo("Europe/Stockholm")
ENGINE_NAME = "tariff_shadow_milp_v1"

DEFAULT_TEMPLATES = {
    "consumption_demand": {
        "kind": "import_top3_mean",
        "rate_sek_per_kw": 105.0,
        "start_hour": 7,
        "end_hour": 19,
        "active_months": [1, 2, 11, 12],
        "day_rule": "workdays",
        "top_n": 3,
        "measurement": "clock_hour_average_import_kw",
        "source_status": "user_prior_spec_with_current_07_19_window",
    },
    "production_demand": {
        "kind": "export_max_hour",
        "rate_sek_per_kw": 10.0,
        "start_hour": 8,
        "end_hour": 16,
        "active_months": [4, 5, 6, 7, 8],
        "day_rule": "weekends_holidays_midsummer_eve",
        "measurement": "clock_hour_average_export_kw",
        "source_status": "preliminary_prior_assumption_not_verified_tariff_rule",
    },
}


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _midsummer_day(year: int) -> date:
    d = date(year, 6, 20)
    return d + timedelta(days=(5 - d.weekday()) % 7)


def _midsummer_eve(year: int) -> date:
    return _midsummer_day(year) - timedelta(days=1)


def _swedish_public_holidays(year: int) -> set[date]:
    easter = _easter_sunday(year)
    midsummer = _midsummer_day(year)
    d = date(year, 10, 31)
    all_saints = d + timedelta(days=(5 - d.weekday()) % 7)
    return {
        date(year, 1, 1), date(year, 1, 6), easter - timedelta(days=2), easter,
        easter + timedelta(days=1), date(year, 5, 1), easter + timedelta(days=39),
        date(year, 6, 6), midsummer, all_saints, date(year, 12, 25), date(year, 12, 26),
    }


def _is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in _swedish_public_holidays(d.year)


def _template(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    configured = ((cfg.get("tariffs") or {}).get("test_scenarios") or {}).get(name)
    if configured:
        return dict(configured)
    if name not in DEFAULT_TEMPLATES:
        raise ValueError(f"Unknown tariff scenario: {name}")
    return dict(DEFAULT_TEMPLATES[name])


def _calendar_active(local_dt: datetime, template: dict[str, Any], force_window: bool) -> bool:
    hour = local_dt.hour
    if not (int(template["start_hour"]) <= hour < int(template["end_hour"])):
        return False
    if force_window:
        return True
    months = set(int(x) for x in template.get("active_months") or [])
    if months and local_dt.month not in months:
        return False
    d = local_dt.date()
    day_rule = template.get("day_rule")
    if day_rule == "workdays":
        return _is_workday(d)
    if day_rule == "weekends_holidays_midsummer_eve":
        return d.weekday() >= 5 or d in _swedish_public_holidays(d.year) or d == _midsummer_eve(d.year)
    return True


def _hour_groups(rows: list[dict[str, Any]], template: dict[str, Any], force_window: bool) -> list[dict[str, Any]]:
    groups: dict[tuple[date, int], list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        local_dt = datetime.fromisoformat(str(row["start"])).astimezone(LOCAL_TZ)
        if _calendar_active(local_dt, template, force_window):
            groups[(local_dt.date(), local_dt.hour)].append(i)
    out = []
    for (d, hour), idxs in sorted(groups.items()):
        idxs = sorted(idxs)
        if len(idxs) != 4:
            continue
        minutes = [datetime.fromisoformat(str(rows[i]["start"])).astimezone(LOCAL_TZ).minute for i in idxs]
        if minutes != [0, 15, 30, 45]:
            continue
        out.append({"date": d.isoformat(), "hour": hour, "indices": idxs})
    return out


class _LP:
    def __init__(self):
        self.names: dict[str, np.ndarray] = {}
        self.lb: list[float] = []
        self.ub: list[float] = []
        self.integrality: list[int] = []
        self.obj: list[float] = []
        self.rows: list[dict[int, float]] = []
        self.clb: list[float] = []
        self.cub: list[float] = []

    def add_vars(self, name: str, count: int, lb: float = 0.0, ub: float = np.inf, integral: bool = False) -> np.ndarray:
        start = len(self.lb)
        idx = np.arange(start, start + count, dtype=int)
        self.names[name] = idx
        self.lb.extend([lb] * count); self.ub.extend([ub] * count)
        self.integrality.extend([1 if integral else 0] * count); self.obj.extend([0.0] * count)
        return idx

    def set_obj(self, idx: int | np.ndarray, coeff: float | np.ndarray):
        if np.isscalar(idx):
            self.obj[int(idx)] += float(coeff); return
        arr = np.asarray(idx)
        vals = np.full(arr.shape, float(coeff)) if np.isscalar(coeff) else np.asarray(coeff, dtype=float)
        for i, v in zip(arr.tolist(), vals.tolist()): self.obj[int(i)] += float(v)

    def constraint(self, coeffs: dict[int, float], lb: float = -np.inf, ub: float = np.inf):
        self.rows.append({int(k): float(v) for k, v in coeffs.items() if abs(v) > 1e-12})
        self.clb.append(float(lb)); self.cub.append(float(ub))

    def solve(self, time_limit: float = 20.0):
        rr, cc, vv = [], [], []
        for r, row in enumerate(self.rows):
            for c, v in row.items(): rr.append(r); cc.append(c); vv.append(v)
        A = coo_matrix((vv, (rr, cc)), shape=(len(self.rows), len(self.lb))).tocsr()
        return milp(
            c=np.asarray(self.obj), integrality=np.asarray(self.integrality, dtype=int),
            bounds=Bounds(np.asarray(self.lb), np.asarray(self.ub)),
            constraints=LinearConstraint(A, np.asarray(self.clb), np.asarray(self.cub)),
            options={"time_limit": time_limit, "mip_rel_gap": 0.002, "presolve": True},
        )


def _tariff_metric_from_hourly(hourly_values: list[float], template: dict[str, Any], historical_peaks_kw: list[float] | None = None) -> dict[str, Any]:
    historical = [max(0.0, float(x)) for x in (historical_peaks_kw or [])]
    values = historical + [max(0.0, float(x)) for x in hourly_values]
    rate = float(template["rate_sek_per_kw"])
    if template["kind"] == "import_top3_mean":
        top = sorted(values, reverse=True)[:3]; top += [0.0] * (3 - len(top))
        metric = sum(top) / 3.0
        return {"metric_kw": metric, "top_values_kw": top, "cost_sek": metric * rate}
    if template["kind"] == "export_max_hour":
        metric = max(values, default=0.0)
        return {"metric_kw": metric, "top_values_kw": [metric], "cost_sek": metric * rate}
    raise ValueError(template["kind"])


def evaluate_plan_tariff(rows: list[dict[str, Any]], template: dict[str, Any], force_window: bool = True, historical_peaks_kw: list[float] | None = None) -> dict[str, Any]:
    groups = _hour_groups(rows, template, force_window)
    key = "grid_import_kw" if template["kind"] == "import_top3_mean" else "grid_export_kw"
    hourly, details = [], []
    for g in groups:
        value = sum(float(rows[i].get(key) or 0.0) for i in g["indices"]) / 4.0
        hourly.append(value); details.append({"date": g["date"], "hour": g["hour"], "kw": round(value, 4)})
    metric = _tariff_metric_from_hourly(hourly, template, historical_peaks_kw)
    return {**metric, "active_hours": details, "force_window": force_window}


def _solve_rows(rows_full: list[dict[str, Any]], cfg: dict[str, Any], template: dict[str, Any] | None, *, force_window: bool = True, historical_peaks_kw: list[float] | None = None, initial_soc_pct: float | None = None) -> dict[str, Any]:
    rows = []
    for row in rows_full:
        if not row.get("price_known"): break
        rows.append(dict(row))
    if not rows: raise RuntimeError("No contiguous published-price intervals for tariff test")

    o = cfg.get("optimizer") or {}; b = (cfg.get("policy") or {}).get("battery") or {}; econf = (cfg.get("policy") or {}).get("economics") or {}
    cap = float(b.get("capacity_kwh", 19.6)); hmin = float(b.get("hard_min_soc_pct", 5.0)); hmax = float(b.get("hard_max_soc_pct", 100.0))
    pmin = float(b.get("preferred_min_soc_pct", 15.0)); pmax = float(b.get("preferred_max_soc_pct", 90.0)); critical = max(hmin, min(pmin, float(o.get("reserve_critical_soc_pct", 10.0))))
    ec = float(o.get("battery_charge_efficiency", 0.95)); ed = float(o.get("battery_discharge_efficiency", 0.95)); cmax = float(o.get("battery_max_charge_kw", 8.0)); dmax = float(o.get("battery_max_discharge_kw", 8.0))
    ilim = float(o.get("physical_grid_import_limit_kw", 13.8)); elim = float(o.get("grid_export_limit_kw", 10.0)); degradation = float(o.get("battery_degradation_ore_kwh", 5.0))
    margin = float(econf.get("minimum_arbitrage_margin_ore_kwh", 20.0)); import_overhead = float(econf.get("import_overhead_ore_kwh", 0.0)); export_overhead = float(econf.get("export_overhead_ore_kwh", 0.0))
    initial_soc = float(initial_soc_pct if initial_soc_pct is not None else (_latest_soc_pct() or hmin)); initial_soc = min(hmax, max(hmin, initial_soc)); initial_kwh = cap * initial_soc / 100.0

    lp = _LP(); n = len(rows)
    charge = lp.add_vars("charge", n, 0, cmax); discharge = lp.add_vars("discharge", n, 0, dmax); imp = lp.add_vars("import", n, 0, ilim); exp = lp.add_vars("export", n, 0, elim)
    soc = lp.add_vars("soc", n, cap * hmin / 100, cap * hmax / 100); ybat = lp.add_vars("ybat", n, 0, 1, integral=True); ygrid = lp.add_vars("ygrid", n, 0, 1, integral=True)
    ztarget = lp.add_vars("z_reserve_target", n, 0, cap); zpref = lp.add_vars("z_reserve_preferred", n, 0, cap); zcrit = lp.add_vars("z_reserve_critical", n, 0, cap); zupper = lp.add_vars("z_preferred_max", n, 0, cap)
    target_rate = max(0.0, float(o.get("reserve_target_penalty_ore_per_kwh_hour", 10.0))); pref_rate = max(target_rate, float(o.get("reserve_preferred_penalty_ore_per_kwh_hour", 100.0))); crit_rate = max(pref_rate, float(o.get("reserve_critical_penalty_ore_per_kwh_hour", 300.0))); upper_rate = max(0.0, float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))

    for t, row in enumerate(rows):
        net = float(row["load_kw"]) - float(row["pv_kw"])
        lp.constraint({imp[t]: 1, exp[t]: -1, discharge[t]: 1, charge[t]: -1}, lb=net, ub=net)
        coeff = {soc[t]: 1, charge[t]: -ec * DT_HOURS, discharge[t]: DT_HOURS / ed}; rhs = initial_kwh
        if t > 0: coeff[soc[t - 1]] = -1; rhs = 0.0
        lp.constraint(coeff, lb=rhs, ub=rhs)
        lp.constraint({charge[t]: 1, ybat[t]: -cmax}, ub=0); lp.constraint({discharge[t]: 1, ybat[t]: dmax}, ub=dmax)
        lp.constraint({imp[t]: 1, ygrid[t]: -ilim}, ub=0); lp.constraint({exp[t]: 1, ygrid[t]: elim}, ub=elim)
        buy = float(row["price_ore_kwh"]) + import_overhead; sell = max(0.0, float(row["price_ore_kwh"]) - export_overhead)
        lp.set_obj(imp[t], buy * DT_HOURS); lp.set_obj(exp[t], -sell * DT_HOURS); lp.set_obj(charge[t], degradation * DT_HOURS); lp.set_obj(discharge[t], (degradation + margin) * DT_HOURS)
        reserve_kwh, _ = _dynamic_reserve_kwh(row, cfg, cap); pref_kwh = cap * pmin / 100.0; crit_kwh = cap * critical / 100.0; pmax_kwh = cap * pmax / 100.0
        lp.constraint({ztarget[t]: 1, soc[t]: 1}, lb=reserve_kwh); lp.constraint({zpref[t]: 1, soc[t]: 1}, lb=pref_kwh); lp.constraint({zcrit[t]: 1, soc[t]: 1}, lb=crit_kwh); lp.constraint({zupper[t]: 1, soc[t]: -1}, lb=-pmax_kwh)
        lp.set_obj(ztarget[t], target_rate * DT_HOURS); lp.set_obj(zpref[t], (pref_rate - target_rate) * DT_HOURS); lp.set_obj(zcrit[t], (crit_rate - pref_rate) * DT_HOURS); lp.set_obj(zupper[t], upper_rate * DT_HOURS)

    continuation = _continuation_profile(rows_full, cfg, cap, cap * pmax / 100.0, ed)
    if continuation.get("enabled"):
        target = float(continuation.get("target_kwh") or 0.0); ref = float(continuation.get("reference_price_ore_kwh") or 0.0); risk = float(continuation.get("risk_premium_ore_kwh") or 0.0)
        zcont = lp.add_vars("continuation_shortfall", 1, 0, cap)[0]; lp.constraint({zcont: 1, soc[-1]: 1}, lb=target); lp.set_obj(soc[-1], -ref); lp.set_obj(zcont, risk)
    else:
        lp.constraint({soc[-1]: 1}, lb=initial_kwh, ub=initial_kwh)

    groups = []
    if template is not None:
        groups = _hour_groups(rows, template, force_window); rate_ore = float(template["rate_sek_per_kw"]) * 100.0; hist = [max(0.0, float(x)) for x in (historical_peaks_kw or [])]
        if template["kind"] == "import_top3_mean":
            theta = lp.add_vars("tariff_theta", 1, 0, ilim)[0]; z = lp.add_vars("tariff_excess", len(groups) + len(hist), 0, ilim); lp.set_obj(theta, rate_ore)
            if len(z): lp.set_obj(z, rate_ore / 3.0)
            for j, g in enumerate(groups):
                coeff = {z[j]: 1, theta: 1}
                for i in g["indices"]: coeff[imp[i]] = coeff.get(imp[i], 0.0) - 0.25
                lp.constraint(coeff, lb=0.0)
            for j, value in enumerate(hist, start=len(groups)): lp.constraint({z[j]: 1, theta: 1}, lb=value)
        elif template["kind"] == "export_max_hour":
            peak = lp.add_vars("tariff_export_peak", 1, 0, elim)[0]; lp.set_obj(peak, rate_ore)
            for g in groups:
                coeff = {peak: 1}
                for i in g["indices"]: coeff[exp[i]] = coeff.get(exp[i], 0.0) - 0.25
                lp.constraint(coeff, lb=0.0)
            for value in hist: lp.constraint({peak: 1}, lb=value)
        else: raise ValueError(template["kind"])

    result = lp.solve()
    if not result.success or result.x is None: raise RuntimeError(f"Tariff scenario optimization failed: status={result.status} message={result.message}")
    x = result.x; out_rows = []; energy_cost = degradation_cost = hurdle_cost = 0.0
    for t, row in enumerate(rows):
        c = float(x[charge[t]]); d = float(x[discharge[t]]); gi = float(x[imp[t]]); ge = float(x[exp[t]]); buy = float(row["price_ore_kwh"]) + import_overhead; sell = max(0.0, float(row["price_ore_kwh"]) - export_overhead)
        e_cost = (gi * buy - ge * sell) * DT_HOURS; deg = (c + d) * degradation * DT_HOURS; hurdle = d * margin * DT_HOURS
        energy_cost += e_cost; degradation_cost += deg; hurdle_cost += hurdle
        out_rows.append({"start": row["start"], "load_kw": round(float(row["load_kw"]), 4), "pv_kw": round(float(row["pv_kw"]), 4), "price_ore_kwh": round(float(row["price_ore_kwh"]), 4), "charge_kw": round(c, 4), "discharge_kw": round(d, 4), "grid_import_kw": round(gi, 4), "grid_export_kw": round(ge, 4), "expected_soc_pct": round(float(x[soc[t]]) / cap * 100.0, 2)})

    tariff_eval = evaluate_plan_tariff(out_rows, template, force_window, historical_peaks_kw) if template is not None else {"metric_kw": 0.0, "top_values_kw": [], "cost_sek": 0.0, "active_hours": []}
    terminal_kwh = float(x[soc[-1]]); cont_asset = 0.0
    if continuation.get("enabled"):
        ref = float(continuation.get("reference_price_ore_kwh") or 0.0); risk = float(continuation.get("risk_premium_ore_kwh") or 0.0); target = float(continuation.get("target_kwh") or 0.0); cont_asset = terminal_kwh * ref + min(terminal_kwh, target) * risk
    return {"engine": ENGINE_NAME, "status": "optimal" if result.status == 0 else "feasible", "solver_status": int(result.status), "solver_message": str(result.message), "known_price_intervals": n, "initial_soc_pct": round(initial_soc, 2), "terminal_soc_pct": round(terminal_kwh / cap * 100.0, 2), "tariff": tariff_eval, "economics": {"energy_cost_ore": round(energy_cost, 2), "battery_degradation_cost_ore": round(degradation_cost, 2), "discretionary_shift_hurdle_ore": round(hurdle_cost, 2), "tariff_cost_ore": round(float(tariff_eval["cost_sek"]) * 100.0, 2), "continuation_asset_value_ore": round(cont_asset, 2)}, "rows": out_rows}


def _base_known_cash(rows: list[dict[str, Any]]) -> float:
    return sum(float(r.get("cash_cost_ore") or 0.0) for r in rows if r.get("price_known"))


def run_live_scenario(cfg: dict[str, Any], name: str, *, force_window: bool = True, historical_peaks_kw: list[float] | None = None) -> dict[str, Any]:
    template = _template(cfg, name); base = build_plan(cfg); full_rows = _build_horizon(cfg)
    solved = _solve_rows(full_rows, cfg, template, force_window=force_window, historical_peaks_kw=historical_peaks_kw, initial_soc_pct=float(base["initial_soc_pct"]))
    base_tariff = evaluate_plan_tariff(base["rows"], template, force_window, historical_peaks_kw)
    return {"scenario": name, "test_only": True, "base_planner": base["planner"], "calendar_mode": "forced_test_window" if force_window else "actual_template_calendar", "template": template, "historical_peaks_kw": historical_peaks_kw or [], "base_plan": {"known_price_cash_cost_ore": round(_base_known_cash(base["rows"]), 2), "tariff": base_tariff}, "tariff_aware": solved, "comparison": {"tariff_metric_reduction_kw": round(float(base_tariff["metric_kw"]) - float(solved["tariff"]["metric_kw"]), 3), "tariff_cost_reduction_sek": round(float(base_tariff["cost_sek"]) - float(solved["tariff"]["cost_sek"]), 2)}}


def _synthetic_rows(day: date, *, base_load_kw: float = 1.0, pv_profile: dict[int, float] | None = None, load_additions: dict[int, float] | None = None, price_profile: dict[int, float] | None = None) -> list[dict[str, Any]]:
    rows = []
    for q in range(96):
        local = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ) + timedelta(minutes=15 * q); hour = local.hour
        rows.append({"start": local.astimezone(ZoneInfo("UTC")).isoformat(), "load_kw": base_load_kw + float((load_additions or {}).get(hour, 0.0)), "base_load_kw": base_load_kw, "component_forecast_kw": {}, "load_uncertainty_kw": 0.5, "pv_kw": float((pv_profile or {}).get(hour, 0.0)), "pv_uncertainty_kw": 0.5 if (pv_profile or {}).get(hour, 0.0) else 0.0, "price_known": True, "price_ore_kwh": float((price_profile or {}).get(hour, 150.0))})
    return rows


def _synthetic_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {"policy": {"battery": dict((cfg.get("policy") or {}).get("battery") or {}), "economics": dict((cfg.get("policy") or {}).get("economics") or {})}, "optimizer": dict(cfg.get("optimizer") or {}), "forecast": {"horizon_hours": 24}, "tariffs": cfg.get("tariffs") or {}}


def run_edge_cases(cfg: dict[str, Any]) -> dict[str, Any]:
    scfg = _synthetic_cfg(cfg); consumption = _template(cfg, "consumption_demand"); production = _template(cfg, "production_demand")
    flat = {h: 150.0 for h in range(24)}; cheap_night = {h: (90.0 if h >= 19 or h < 7 else 150.0) for h in range(24)}

    inside_rows = _synthetic_rows(date(2026, 11, 3), load_additions={17: 10.5, 18: 10.5}, price_profile=cheap_night)
    inside_base = _solve_rows(inside_rows, scfg, None, force_window=False, initial_soc_pct=60.0); inside_tariff = _solve_rows(inside_rows, scfg, consumption, force_window=False, initial_soc_pct=60.0)
    inside_base_eval = evaluate_plan_tariff(inside_base["rows"], consumption, False); inside_tariff_eval = inside_tariff["tariff"]

    after_rows = _synthetic_rows(date(2026, 11, 3), load_additions={19: 10.5}, price_profile=flat); control_rows = _synthetic_rows(date(2026, 11, 3), price_profile=flat)
    after_control = _solve_rows(control_rows, scfg, consumption, force_window=False, initial_soc_pct=60.0); after_tariff = _solve_rows(after_rows, scfg, consumption, force_window=False, initial_soc_pct=60.0)
    after_control_metric = float(after_control["tariff"]["metric_kw"]); after_metric = float(after_tariff["tariff"]["metric_kw"])

    established = [13.0, 12.0, 11.0]; established_rows = _synthetic_rows(date(2026, 11, 3), load_additions={17: 9.0}, price_profile=flat)
    established_tariff = _solve_rows(established_rows, scfg, consumption, force_window=False, historical_peaks_kw=established, initial_soc_pct=60.0); established_metric = float(established_tariff["tariff"]["metric_kw"]); established_expected = sum(established) / 3.0

    pv = {h: 9.0 for h in range(10, 15)}; prod_rows = _synthetic_rows(date(2026, 7, 11), pv_profile=pv, price_profile=flat)
    prod_base = _solve_rows(prod_rows, scfg, None, force_window=False, initial_soc_pct=20.0); prod_tariff = _solve_rows(prod_rows, scfg, production, force_window=False, initial_soc_pct=20.0); prod_base_eval = evaluate_plan_tariff(prod_base["rows"], production, False)

    return {"engine": ENGINE_NAME, "tests": {
        "consumption_inside_window": {"pass": inside_tariff_eval["metric_kw"] < inside_base_eval["metric_kw"] - 0.1, "base_metric_kw": round(float(inside_base_eval["metric_kw"]), 3), "tariff_metric_kw": round(float(inside_tariff_eval["metric_kw"]), 3)},
        "consumption_after_19_boundary": {"pass": abs(after_metric - after_control_metric) < 0.01, "control_metric_kw": round(after_control_metric, 3), "with_19_20_spike_metric_kw": round(after_metric, 3)},
        "established_monthly_peaks": {"pass": abs(established_metric - established_expected) < 0.01, "historical_top3_kw": established, "tariff_metric_kw": round(established_metric, 3)},
        "production_export_peak": {"pass": float(prod_tariff["tariff"]["metric_kw"]) < float(prod_base_eval["metric_kw"]) - 0.1, "base_metric_kw": round(float(prod_base_eval["metric_kw"]), 3), "tariff_metric_kw": round(float(prod_tariff["tariff"]["metric_kw"]), 3)},
    }}
