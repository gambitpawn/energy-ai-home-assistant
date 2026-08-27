from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from .price_economics import (
    CURRENT_ECONOMICS,
    economics_payload,
    economics_signature,
    effective_prices,
    effective_prices_for_row,
)

ECONOMICS_EPOCH_PATH = Path("/data/economics_model_epoch.json")


def _price(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, float]:
    return effective_prices(float(row["price_ore_kwh"]), cfg)


def _optimizer_continuation_profile(rows, cfg, cap, preferred_max_kwh, eta_discharge):
    from . import optimizer as m

    o = cfg.get("optimizer") or {}
    unknown = [r for r in rows if not r["price_known"]]
    known = [r for r in rows if r["price_known"]]
    if not unknown:
        return {
            "enabled": False, "target_kwh": None, "target_soc_pct": None,
            "value_ore_per_kwh": None, "unknown_net_deficit_kwh": 0.0,
            "unknown_peak_support_kwh": 0.0, "reference_price_ore_kwh": None,
        }
    lim = float(o.get("physical_grid_import_limit_kw", 13.8))
    frac = max(0.0, min(1.0, float(o.get("unknown_price_energy_coverage_fraction", 0.35))))
    riskmax = max(0.0, float(o.get("unknown_price_risk_premium_ore_kwh", 40.0)))
    default = max(0.0, float(o.get("unknown_price_default_continuation_value_ore_kwh", 150.0)))
    scale = max(0.01, float(o.get("reserve_uncertainty_full_scale_kw", 3.0)))
    reserve = max((m._dynamic_reserve_kwh(r, cfg, cap)[0] for r in unknown), default=0.0)
    deficit = sum(max(0.0, r["load_kw"] - r["pv_kw"]) * m.DT_HOURS for r in unknown)
    peak = sum(max(0.0, r["load_kw"] - r["pv_kw"] - lim) * m.DT_HOURS / max(0.01, eta_discharge) for r in unknown)
    covered = deficit * frac / max(0.01, eta_discharge)
    target = min(preferred_max_kwh, max(reserve + covered, reserve + peak))
    buys = [
        effective_prices(float(r["price_ore_kwh"]), cfg)["effective_import_price_ore_kwh"]
        for r in known if r.get("price_ore_kwh") is not None
    ]
    ref = float(median(buys)) if buys else default
    avgunc = sum(
        max(0.0, float(r.get("load_uncertainty_kw") or 0.0))
        + max(0.0, float(r.get("pv_uncertainty_kw") or 0.0))
        for r in unknown
    ) / len(unknown)
    risk = riskmax * (0.6 * min(1.0, deficit / max(0.01, cap)) + 0.4 * min(1.0, avgunc / scale))
    return {
        "enabled": True,
        "target_kwh": target,
        "target_soc_pct": target / cap * 100.0,
        "value_ore_per_kwh": ref + risk,
        "unknown_net_deficit_kwh": deficit,
        "unknown_peak_support_kwh": peak,
        "reference_price_ore_kwh": ref,
        "risk_premium_ore_kwh": risk,
        "coverage_fraction": frac,
        "price_semantics": CURRENT_ECONOMICS,
    }


def _optimizer_interval_result(row, action, cfg):
    from . import optimizer as m

    o = cfg.get("optimizer") or {}
    e = (cfg.get("policy") or {}).get("economics") or {}
    ilim = float(o.get("physical_grid_import_limit_kw", 13.8))
    elim = float(o.get("grid_export_limit_kw", 10.0))
    load, pv = float(row["load_kw"]), float(row["pv_kw"])
    net = load - pv
    grid = net - action
    imp, rawexp = max(0.0, grid), max(0.0, -grid)
    exp, curt = min(rawexp, elim), max(0.0, rawexp - elim)
    pv_surplus, charge = max(0.0, pv - load), max(0.0, -action)
    pv_charge = min(charge, pv_surplus)
    grid_charge = max(0.0, charge - pv_charge)
    batt_export = min(exp, max(0.0, action - max(0.0, net))) if action > 0 and exp > 0 else 0.0
    required = max(0.0, net - ilim)
    discretionary = max(0.0, action - required) if action > 0 else 0.0
    feasible = imp <= ilim + 1e-9
    degr = abs(action) * m.DT_HOURS * float(o.get("battery_degradation_ore_kwh", 5.0))
    buy = sell = None
    if not row["price_known"]:
        if grid_charge > 1e-6 or batt_export > 1e-6 or (required <= 1e-6 and action > 1e-6):
            feasible = False
        energy, hurdle = 0.0, 0.0
    else:
        prices = _price(row, cfg)
        buy = prices["effective_import_price_ore_kwh"]
        sell = prices["effective_export_price_ore_kwh"]
        energy = imp * m.DT_HOURS * buy - exp * m.DT_HOURS * sell
        hurdle = discretionary * m.DT_HOURS * float(e.get("minimum_arbitrage_margin_ore_kwh", 20.0))
    cash = energy + degr
    return {
        "feasible": 1.0 if feasible else 0.0,
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "battery_export_kw": batt_export,
        "pv_charge_kw": pv_charge,
        "grid_charge_kw": grid_charge,
        "curtailed_kw": curt,
        "required_physical_discharge_kw": required,
        "discretionary_discharge_kw": discretionary,
        "effective_import_price_ore_kwh": buy,
        "effective_export_price_ore_kwh": sell,
        "energy_cost_ore": energy,
        "degradation_cost_ore": degr,
        "cash_cost_ore": cash,
        "hurdle_cost_ore": hurdle,
        "interval_cost_ore": cash + hurdle,
    }


def _adaptive_interval_result(row, action, cfg, params):
    from . import optimizer as opt

    p = params.bounded()
    o = cfg.get("optimizer") or {}
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
    cycling = abs(action) * opt.DT_HOURS * p.cycling_penalty_ore_kwh
    discharge_hurdle = 0.0
    charge_hurdle = 0.0
    buy = sell = None
    if not row["price_known"]:
        if grid_charge > 1e-6 or batt_export > 1e-6 or (required <= 1e-6 and action > 1e-6):
            feasible = False
        energy = 0.0
    else:
        prices = _price(row, cfg)
        buy = prices["effective_import_price_ore_kwh"]
        sell = prices["effective_export_price_ore_kwh"]
        energy = imp * opt.DT_HOURS * buy - exp * opt.DT_HOURS * sell
        discharge_hurdle = discretionary * opt.DT_HOURS * p.discharge_hurdle_ore_kwh
        charge_hurdle = grid_charge * opt.DT_HOURS * p.charge_hurdle_ore_kwh
    return {
        "feasible": 1.0 if feasible else 0.0,
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "grid_charge_kw": grid_charge,
        "battery_export_kw": batt_export,
        "required_physical_discharge_kw": required,
        "discretionary_discharge_kw": discretionary,
        "effective_import_price_ore_kwh": buy,
        "effective_export_price_ore_kwh": sell,
        "energy_cost_ore": energy,
        "cycling_penalty_ore": cycling,
        "discharge_hurdle_cost_ore": discharge_hurdle,
        "charge_hurdle_cost_ore": charge_hurdle,
        "interval_cost_ore": energy + cycling + discharge_hurdle + charge_hurdle,
    }


def _evaluation_baseline_interval(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, float]:
    from . import optimizer as optm

    o = cfg.get("optimizer") or {}
    net = float(row["load_kw"]) - float(row["pv_kw"])
    imp = max(0.0, net)
    raw_export = max(0.0, -net)
    export_limit = float(o.get("grid_export_limit_kw", 10.0))
    exp = min(raw_export, export_limit)
    prices = _price(row, cfg)
    buy = prices["effective_import_price_ore_kwh"]
    sell = prices["effective_export_price_ore_kwh"]
    return {
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "curtailed_kw": max(0.0, raw_export - export_limit),
        "effective_import_price_ore_kwh": buy,
        "effective_export_price_ore_kwh": sell,
        "cash_cost_ore": (imp * buy - exp * sell) * optm.DT_HOURS,
    }


def _evaluation_apply_action(row, requested_action_kw, energy_kwh, cfg, reserve_soc_pct):
    from . import optimizer_evaluation as m

    battery = (cfg.get("policy") or {}).get("battery") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    opt = cfg.get("optimizer") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    pmin = float(battery.get("preferred_min_soc_pct", 15.0))
    pmax = float(battery.get("preferred_max_soc_pct", 90.0))
    ec = float(opt.get("battery_charge_efficiency", 0.95))
    ed = float(opt.get("battery_discharge_efficiency", 0.95))
    cmax = float(opt.get("battery_max_charge_kw", 8.0))
    dmax = float(opt.get("battery_max_discharge_kw", 8.0))
    ilim = float(opt.get("physical_grid_import_limit_kw", 13.8))
    elim = float(opt.get("grid_export_limit_kw", 10.0))
    min_e = cap * hmin / 100.0
    max_e = cap * hmax / 100.0
    net = float(row["load_kw"]) - float(row["pv_kw"])
    requested = float(requested_action_kw)
    if requested >= 0.0:
        by_soc = max(0.0, energy_kwh - min_e) * ed / m.DT_HOURS
        by_export = max(0.0, net + elim)
        action = min(requested, dmax, by_soc, by_export)
    else:
        by_soc = max(0.0, max_e - energy_kwh) / max(1e-9, ec * m.DT_HOURS)
        by_import = max(0.0, ilim - net)
        action = -min(-requested, cmax, by_soc, by_import)
    clamped = abs(action - requested) > 1e-6
    if action >= 0.0:
        end_e = energy_kwh - action * m.DT_HOURS / max(1e-9, ed)
    else:
        end_e = energy_kwh + (-action) * ec * m.DT_HOURS
    end_e = min(max_e, max(min_e, end_e))
    grid = net - action
    imp = max(0.0, grid)
    raw_export = max(0.0, -grid)
    exp = min(raw_export, elim)
    curtailed = max(0.0, raw_export - elim)
    prices = _price(row, cfg)
    buy = prices["effective_import_price_ore_kwh"]
    sell = prices["effective_export_price_ore_kwh"]
    degradation = abs(action) * m.DT_HOURS * float(opt.get("battery_degradation_ore_kwh", 5.0))
    energy_cost = (imp * buy - exp * sell) * m.DT_HOURS
    required = max(0.0, net - ilim)
    discretionary = max(0.0, action - required) if action > 0 else 0.0
    hurdle = discretionary * m.DT_HOURS * float(econ.get("minimum_arbitrage_margin_ore_kwh", 20.0))
    target_pct = float(reserve_soc_pct) if reserve_soc_pct is not None else float(battery.get("normal_reserve_soc_pct", 20.0))
    reserve_kwh = cap * max(hmin, min(hmax, target_pct)) / 100.0
    reserve_penalty = m._reserve_policy_penalty_ore(end_e, reserve_kwh, cfg, cap, hmin, pmin)
    upper_penalty = max(0.0, end_e - cap * pmax / 100.0) * float(opt.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)) * m.DT_HOURS
    cash = energy_cost + degradation
    return {
        "requested_action_kw": requested,
        "applied_action_kw": action,
        "clamped": clamped,
        "energy_end_kwh": end_e,
        "soc_end_pct": end_e / cap * 100.0,
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "curtailed_kw": curtailed,
        "import_limit_exceedance_kw": max(0.0, imp - ilim),
        "effective_import_price_ore_kwh": buy,
        "effective_export_price_ore_kwh": sell,
        "energy_cost_ore": energy_cost,
        "degradation_cost_ore": degradation,
        "cash_cost_ore": cash,
        "hurdle_cost_ore": hurdle,
        "reserve_policy_penalty_ore": reserve_penalty,
        "preferred_max_excess_penalty_ore": upper_penalty,
        "policy_objective_cost_ore": cash + hurdle + reserve_penalty + upper_penalty,
        "throughput_kwh": abs(action) * m.DT_HOURS,
    }


def _evaluation_hindsight(rows, cfg, initial_soc_pct, terminal_soc_pct):
    from . import optimizer_evaluation as m

    if not rows:
        return {"status": "no_rows"}
    battery = (cfg.get("policy") or {}).get("battery") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    opt = cfg.get("optimizer") or {}
    cap = float(battery.get("capacity_kwh", 19.6)); hmin = float(battery.get("hard_min_soc_pct", 5.0)); hmax = float(battery.get("hard_max_soc_pct", 100.0))
    pmin = float(battery.get("preferred_min_soc_pct", 15.0)); pmax = float(battery.get("preferred_max_soc_pct", 90.0)); normal = float(battery.get("normal_reserve_soc_pct", 20.0))
    critical = max(hmin, min(pmin, float(opt.get("reserve_critical_soc_pct", 10.0))))
    initial = max(hmin, min(hmax, float(initial_soc_pct))); terminal = max(hmin, min(hmax, float(terminal_soc_pct)))
    e0 = cap * initial / 100.0; eterm = cap * terminal / 100.0
    ec = float(opt.get("battery_charge_efficiency", 0.95)); ed = float(opt.get("battery_discharge_efficiency", 0.95))
    cmax = float(opt.get("battery_max_charge_kw", 8.0)); dmax = float(opt.get("battery_max_discharge_kw", 8.0)); ilim = float(opt.get("physical_grid_import_limit_kw", 13.8)); elim = float(opt.get("grid_export_limit_kw", 10.0))
    deg = float(opt.get("battery_degradation_ore_kwh", 5.0)); margin = float(econ.get("minimum_arbitrage_margin_ore_kwh", 20.0))
    critical_rate = max(0.0, float(opt.get("reserve_critical_penalty_ore_per_kwh_hour", 300.0))); preferred_rate = max(0.0, min(critical_rate, float(opt.get("reserve_preferred_penalty_ore_per_kwh_hour", 100.0)))); target_rate = max(0.0, min(preferred_rate, float(opt.get("reserve_target_penalty_ore_per_kwh_hour", 10.0)))); upper_rate = max(0.0, float(opt.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))
    n = len(rows); lp = m._LP()
    charge = lp.add_vars("charge", n, 0, cmax); discharge = lp.add_vars("discharge", n, 0, dmax); imp = lp.add_vars("import", n, 0, ilim); exp = lp.add_vars("export", n, 0, elim); soc = lp.add_vars("soc", n, cap*hmin/100.0, cap*hmax/100.0)
    ybat = lp.add_vars("ybat", n, 0, 1, integral=True); ygrid = lp.add_vars("ygrid", n, 0, 1, integral=True); discretionary = lp.add_vars("discretionary", n, 0, dmax)
    ztarget = lp.add_vars("ztarget", n, 0, cap); zpreferred = lp.add_vars("zpreferred", n, 0, cap); zcritical = lp.add_vars("zcritical", n, 0, cap); zupper = lp.add_vars("zupper", n, 0, cap)
    target_kwh=cap*normal/100.0; preferred_kwh=cap*pmin/100.0; critical_kwh=cap*critical/100.0; upper_kwh=cap*pmax/100.0
    for t,row in enumerate(rows):
        net=float(row["load_kw"])-float(row["pv_kw"]); lp.constraint({int(imp[t]):1,int(exp[t]):-1,int(discharge[t]):1,int(charge[t]):-1},lb=net,ub=net)
        coeff={int(soc[t]):1,int(charge[t]):-ec*m.DT_HOURS,int(discharge[t]):m.DT_HOURS/ed}
        if t: coeff[int(soc[t-1])]=-1; lp.constraint(coeff,lb=0,ub=0)
        else: lp.constraint(coeff,lb=e0,ub=e0)
        lp.constraint({int(charge[t]):1,int(ybat[t]):-cmax},ub=0); lp.constraint({int(discharge[t]):1,int(ybat[t]):dmax},ub=dmax); lp.constraint({int(imp[t]):1,int(ygrid[t]):-ilim},ub=0); lp.constraint({int(exp[t]):1,int(ygrid[t]):elim},ub=elim)
        required=max(0.0,net-ilim); lp.constraint({int(discretionary[t]):1,int(discharge[t]):-1},lb=-required)
        prices=_price(row,cfg); buy=prices["effective_import_price_ore_kwh"]; sell=prices["effective_export_price_ore_kwh"]
        lp.set_obj(imp[t],buy*m.DT_HOURS); lp.set_obj(exp[t],-sell*m.DT_HOURS); lp.set_obj(charge[t],deg*m.DT_HOURS); lp.set_obj(discharge[t],deg*m.DT_HOURS); lp.set_obj(discretionary[t],margin*m.DT_HOURS)
        lp.constraint({int(ztarget[t]):1,int(soc[t]):1},lb=target_kwh); lp.constraint({int(zpreferred[t]):1,int(soc[t]):1},lb=preferred_kwh); lp.constraint({int(zcritical[t]):1,int(soc[t]):1},lb=critical_kwh); lp.constraint({int(zupper[t]):1,int(soc[t]):-1},lb=-upper_kwh)
        lp.set_obj(ztarget[t],target_rate*m.DT_HOURS); lp.set_obj(zpreferred[t],(preferred_rate-target_rate)*m.DT_HOURS); lp.set_obj(zcritical[t],(critical_rate-preferred_rate)*m.DT_HOURS); lp.set_obj(zupper[t],upper_rate*m.DT_HOURS)
    lp.constraint({int(soc[-1]):1},lb=eterm,ub=eterm)
    result=lp.solve(time_limit=30)
    if not result.success or result.x is None: return {"status":"infeasible_or_timeout","solver_status":int(result.status),"solver_message":str(result.message)}
    x=result.x; energy=degradation=hurdle=reserve=upper=throughput=0.0; actions=[]
    for t,row in enumerate(rows):
        c=max(0.0,float(x[charge[t]])); d=max(0.0,float(x[discharge[t]])); gi=max(0.0,float(x[imp[t]])); ge=max(0.0,float(x[exp[t]])); dd=max(0.0,float(x[discretionary[t]])); prices=_price(row,cfg)
        energy+=(gi*prices["effective_import_price_ore_kwh"]-ge*prices["effective_export_price_ore_kwh"])*m.DT_HOURS; degradation+=(c+d)*deg*m.DT_HOURS; hurdle+=dd*margin*m.DT_HOURS
        reserve+=(float(x[ztarget[t]])*target_rate+float(x[zpreferred[t]])*(preferred_rate-target_rate)+float(x[zcritical[t]])*(critical_rate-preferred_rate))*m.DT_HOURS; upper+=float(x[zupper[t]])*upper_rate*m.DT_HOURS; throughput+=(c+d)*m.DT_HOURS
        actions.append({"start":row["start"],"action_kw":round(d-c,4),"soc_end_pct":round(float(x[soc[t]])/cap*100.0,2),"grid_import_kw":round(gi,4),"grid_export_kw":round(ge,4)})
    cash=energy+degradation
    return {"status":"optimal" if result.status==0 else "feasible","solver_status":int(result.status),"solver_message":str(result.message),"initial_soc_pct":round(initial,3),"terminal_soc_pct":round(terminal,3),"cash_cost_ore":round(cash,2),"policy_objective_cost_ore":round(cash+hurdle+reserve+upper,2),"battery_throughput_kwh":round(throughput,3),"actions":actions,"economics_mode":CURRENT_ECONOMICS}


def _tariff_solve_rows(rows_full, cfg, template, *, force_window=True, historical_peaks_kw=None, initial_soc_pct=None):
    from . import tariff_scenarios as m

    rows=[]
    for row in rows_full:
        if not row.get("price_known"): break
        rows.append(dict(row))
    if not rows: raise RuntimeError("No contiguous published-price intervals for tariff test")
    o=cfg.get("optimizer") or {}; b=(cfg.get("policy") or {}).get("battery") or {}; econf=(cfg.get("policy") or {}).get("economics") or {}
    cap=float(b.get("capacity_kwh",19.6)); hmin=float(b.get("hard_min_soc_pct",5.0)); hmax=float(b.get("hard_max_soc_pct",100.0)); pmin=float(b.get("preferred_min_soc_pct",15.0)); pmax=float(b.get("preferred_max_soc_pct",90.0)); critical=max(hmin,min(pmin,float(o.get("reserve_critical_soc_pct",10.0))))
    ec=float(o.get("battery_charge_efficiency",0.95)); ed=float(o.get("battery_discharge_efficiency",0.95)); cmax=float(o.get("battery_max_charge_kw",8.0)); dmax=float(o.get("battery_max_discharge_kw",8.0)); ilim=float(o.get("physical_grid_import_limit_kw",13.8)); elim=float(o.get("grid_export_limit_kw",10.0)); degradation=float(o.get("battery_degradation_ore_kwh",5.0)); margin=float(econf.get("minimum_arbitrage_margin_ore_kwh",20.0))
    initial_soc=float(initial_soc_pct if initial_soc_pct is not None else (m._latest_soc_pct() or hmin)); initial_soc=min(hmax,max(hmin,initial_soc)); initial_kwh=cap*initial_soc/100.0
    lp=m._LP(); n=len(rows); charge=lp.add_vars("charge",n,0,cmax); discharge=lp.add_vars("discharge",n,0,dmax); imp=lp.add_vars("import",n,0,ilim); exp=lp.add_vars("export",n,0,elim); soc=lp.add_vars("soc",n,cap*hmin/100,cap*hmax/100); ybat=lp.add_vars("ybat",n,0,1,integral=True); ygrid=lp.add_vars("ygrid",n,0,1,integral=True)
    ztarget=lp.add_vars("z_reserve_target",n,0,cap); zpref=lp.add_vars("z_reserve_preferred",n,0,cap); zcrit=lp.add_vars("z_reserve_critical",n,0,cap); zupper=lp.add_vars("z_preferred_max",n,0,cap)
    target_rate=max(0.0,float(o.get("reserve_target_penalty_ore_per_kwh_hour",10.0))); pref_rate=max(target_rate,float(o.get("reserve_preferred_penalty_ore_per_kwh_hour",100.0))); crit_rate=max(pref_rate,float(o.get("reserve_critical_penalty_ore_per_kwh_hour",300.0))); upper_rate=max(0.0,float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour",2.0)))
    for t,row in enumerate(rows):
        net=float(row["load_kw"])-float(row["pv_kw"]); lp.constraint({imp[t]:1,exp[t]:-1,discharge[t]:1,charge[t]:-1},lb=net,ub=net); coeff={soc[t]:1,charge[t]:-ec*m.DT_HOURS,discharge[t]:m.DT_HOURS/ed}; rhs=initial_kwh
        if t>0: coeff[soc[t-1]]=-1; rhs=0.0
        lp.constraint(coeff,lb=rhs,ub=rhs); lp.constraint({charge[t]:1,ybat[t]:-cmax},ub=0); lp.constraint({discharge[t]:1,ybat[t]:dmax},ub=dmax); lp.constraint({imp[t]:1,ygrid[t]:-ilim},ub=0); lp.constraint({exp[t]:1,ygrid[t]:elim},ub=elim)
        prices=_price(row,cfg); lp.set_obj(imp[t],prices["effective_import_price_ore_kwh"]*m.DT_HOURS); lp.set_obj(exp[t],-prices["effective_export_price_ore_kwh"]*m.DT_HOURS); lp.set_obj(charge[t],degradation*m.DT_HOURS); lp.set_obj(discharge[t],(degradation+margin)*m.DT_HOURS)
        reserve_kwh,_=m._dynamic_reserve_kwh(row,cfg,cap); pref_kwh=cap*pmin/100.0; crit_kwh=cap*critical/100.0; pmax_kwh=cap*pmax/100.0
        lp.constraint({ztarget[t]:1,soc[t]:1},lb=reserve_kwh); lp.constraint({zpref[t]:1,soc[t]:1},lb=pref_kwh); lp.constraint({zcrit[t]:1,soc[t]:1},lb=crit_kwh); lp.constraint({zupper[t]:1,soc[t]:-1},lb=-pmax_kwh)
        lp.set_obj(ztarget[t],target_rate*m.DT_HOURS); lp.set_obj(zpref[t],(pref_rate-target_rate)*m.DT_HOURS); lp.set_obj(zcrit[t],(crit_rate-pref_rate)*m.DT_HOURS); lp.set_obj(zupper[t],upper_rate*m.DT_HOURS)
    continuation=m._continuation_profile(rows_full,cfg,cap,cap*pmax/100.0,ed)
    if continuation.get("enabled"):
        target=float(continuation.get("target_kwh") or 0.0); ref=float(continuation.get("reference_price_ore_kwh") or 0.0); risk=float(continuation.get("risk_premium_ore_kwh") or 0.0); zcont=lp.add_vars("continuation_shortfall",1,0,cap)[0]; lp.constraint({zcont:1,soc[-1]:1},lb=target); lp.set_obj(soc[-1],-ref); lp.set_obj(zcont,risk)
    else: lp.constraint({soc[-1]:1},lb=initial_kwh,ub=initial_kwh)
    groups=[]
    if template is not None:
        groups=m._hour_groups(rows,template,force_window); rate_ore=float(template["rate_sek_per_kw"])*100.0; hist=[max(0.0,float(x)) for x in (historical_peaks_kw or [])]
        if template["kind"]=="import_top3_mean":
            theta=lp.add_vars("tariff_theta",1,0,ilim)[0]; z=lp.add_vars("tariff_excess",len(groups)+len(hist),0,ilim); lp.set_obj(theta,rate_ore)
            if len(z): lp.set_obj(z,rate_ore/3.0)
            for j,g in enumerate(groups):
                coeff={z[j]:1,theta:1}
                for i in g["indices"]: coeff[imp[i]]=coeff.get(imp[i],0.0)-0.25
                lp.constraint(coeff,lb=0.0)
            for j,value in enumerate(hist,start=len(groups)): lp.constraint({z[j]:1,theta:1},lb=value)
        elif template["kind"]=="export_max_hour":
            peak=lp.add_vars("tariff_export_peak",1,0,elim)[0]; lp.set_obj(peak,rate_ore)
            for g in groups:
                coeff={peak:1}
                for i in g["indices"]: coeff[exp[i]]=coeff.get(exp[i],0.0)-0.25
                lp.constraint(coeff,lb=0.0)
            for value in hist: lp.constraint({peak:1},lb=value)
        else: raise ValueError(template["kind"])
    result=lp.solve()
    if not result.success or result.x is None: raise RuntimeError(f"Tariff scenario optimization failed: status={result.status} message={result.message}")
    x=result.x; out_rows=[]; energy_cost=degradation_cost=hurdle_cost=0.0
    for t,row in enumerate(rows):
        c=float(x[charge[t]]); d=float(x[discharge[t]]); gi=float(x[imp[t]]); ge=float(x[exp[t]]); prices=_price(row,cfg); e_cost=(gi*prices["effective_import_price_ore_kwh"]-ge*prices["effective_export_price_ore_kwh"])*m.DT_HOURS; deg=(c+d)*degradation*m.DT_HOURS; hurdle=d*margin*m.DT_HOURS
        energy_cost+=e_cost; degradation_cost+=deg; hurdle_cost+=hurdle
        out_rows.append({"start":row["start"],"load_kw":round(float(row["load_kw"]),4),"pv_kw":round(float(row["pv_kw"]),4),"price_ore_kwh":round(float(row["price_ore_kwh"]),4),"effective_import_price_ore_kwh":round(prices["effective_import_price_ore_kwh"],4),"effective_export_price_ore_kwh":round(prices["effective_export_price_ore_kwh"],4),"charge_kw":round(c,4),"discharge_kw":round(d,4),"grid_import_kw":round(gi,4),"grid_export_kw":round(ge,4),"expected_soc_pct":round(float(x[soc[t]])/cap*100.0,2)})
    tariff_eval=m.evaluate_plan_tariff(out_rows,template,force_window,historical_peaks_kw) if template is not None else {"metric_kw":0.0,"top_values_kw":[],"cost_sek":0.0,"active_hours":[]}
    terminal_kwh=float(x[soc[-1]]); cont_asset=0.0
    if continuation.get("enabled"):
        ref=float(continuation.get("reference_price_ore_kwh") or 0.0); risk=float(continuation.get("risk_premium_ore_kwh") or 0.0); target=float(continuation.get("target_kwh") or 0.0); cont_asset=terminal_kwh*ref+min(terminal_kwh,target)*risk
    return {"engine":m.ENGINE_NAME,"status":"optimal" if result.status==0 else "feasible","solver_status":int(result.status),"solver_message":str(result.message),"known_price_intervals":n,"initial_soc_pct":round(initial_soc,2),"terminal_soc_pct":round(terminal_kwh/cap*100.0,2),"tariff":tariff_eval,"economics":{"mode":CURRENT_ECONOMICS,"energy_cost_ore":round(energy_cost,2),"battery_degradation_cost_ore":round(degradation_cost,2),"discretionary_shift_hurdle_ore":round(hurdle_cost,2),"tariff_cost_ore":round(float(tariff_eval["cost_sek"])*100.0,2),"continuation_asset_value_ore":round(cont_asset,2)},"rows":out_rows}


def _monthly_solve(rows,cfg,*,tariff_enabled:bool,hourly_cap_kw=None,initial_soc_pct:float=50.0):
    from . import monthly_replay as m

    if not rows: raise RuntimeError("No monthly replay rows")
    b=(cfg.get("policy") or {}).get("battery") or {}; econ=(cfg.get("policy") or {}).get("economics") or {}; o=cfg.get("optimizer") or {}
    cap=float(b.get("capacity_kwh",19.6)); hmin=float(b.get("hard_min_soc_pct",5)); hmax=float(b.get("hard_max_soc_pct",100)); pmin=float(b.get("preferred_min_soc_pct",15)); pmax=float(b.get("preferred_max_soc_pct",90)); reserve_pct=float(b.get("normal_reserve_soc_pct",20)); initial=max(hmin,min(hmax,float(initial_soc_pct))); e0=cap*initial/100
    ec=float(o.get("battery_charge_efficiency",.95)); ed=float(o.get("battery_discharge_efficiency",.95)); cmax=float(o.get("battery_max_charge_kw",8)); dmax=float(o.get("battery_max_discharge_kw",8)); ilim=float(o.get("physical_grid_import_limit_kw",13.8)); elim=float(o.get("grid_export_limit_kw",10)); deg=float(o.get("battery_degradation_ore_kwh",5)); margin=float(econ.get("minimum_arbitrage_margin_ore_kwh",20))
    critical=max(hmin,min(pmin,float(o.get("reserve_critical_soc_pct",10)))); cr=max(0.,float(o.get("reserve_critical_penalty_ore_per_kwh_hour",300))); pr=max(0.,min(cr,float(o.get("reserve_preferred_penalty_ore_per_kwh_hour",100)))); tr=max(0.,min(pr,float(o.get("reserve_target_penalty_ore_per_kwh_hour",10)))); ur=max(0.,float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour",2)))
    n=len(rows); lp=m._LP(); charge=lp.add_vars("charge",n,0,cmax); discharge=lp.add_vars("discharge",n,0,dmax); imp=lp.add_vars("import",n,0,ilim); exp=lp.add_vars("export",n,0,elim); soc=lp.add_vars("soc",n,cap*hmin/100,cap*hmax/100); ddisc=lp.add_vars("disc_discharge",n,0,dmax); zt=lp.add_vars("z_target",n,0,cap); zp=lp.add_vars("z_preferred",n,0,cap); zc=lp.add_vars("z_critical",n,0,cap); zu=lp.add_vars("z_upper",n,0,cap)
    neg=[i for i,r in enumerate(rows) if _price(r,cfg)["effective_import_price_ore_kwh"]<0]; yb=lp.add_vars("yb",len(neg),0,1,integral=True) if neg else m.np.array([],dtype=int); yg=lp.add_vars("yg",len(neg),0,1,integral=True) if neg else m.np.array([],dtype=int); negpos={t:j for j,t in enumerate(neg)}
    rk=cap*reserve_pct/100; pk=cap*pmin/100; ck=cap*critical/100; uk=cap*pmax/100
    for t,r in enumerate(rows):
        net=float(r["load_kw"])-float(r["pv_kw"]); lp.constraint({int(imp[t]):1,int(exp[t]):-1,int(discharge[t]):1,int(charge[t]):-1},lb=net,ub=net); coeff={int(soc[t]):1,int(charge[t]):-ec*m.DT_HOURS,int(discharge[t]):m.DT_HOURS/ed}
        if t: coeff[int(soc[t-1])]=-1; lp.constraint(coeff,lb=0,ub=0)
        else: lp.constraint(coeff,lb=e0,ub=e0)
        required=max(0.,net-ilim); lp.constraint({int(ddisc[t]):1,int(discharge[t]):-1},lb=-required)
        if t in negpos:
            j=negpos[t]; lp.constraint({int(charge[t]):1,int(yb[j]):-cmax},ub=0); lp.constraint({int(discharge[t]):1,int(yb[j]):dmax},ub=dmax); lp.constraint({int(imp[t]):1,int(yg[j]):-ilim},ub=0); lp.constraint({int(exp[t]):1,int(yg[j]):elim},ub=elim)
        prices=_price(r,cfg); lp.set_obj(imp[t],(prices["effective_import_price_ore_kwh"]+.001)*m.DT_HOURS); lp.set_obj(exp[t],(-prices["effective_export_price_ore_kwh"]+.001)*m.DT_HOURS); lp.set_obj(charge[t],deg*m.DT_HOURS); lp.set_obj(discharge[t],deg*m.DT_HOURS); lp.set_obj(ddisc[t],margin*m.DT_HOURS)
        lp.constraint({int(zt[t]):1,int(soc[t]):1},lb=rk); lp.constraint({int(zp[t]):1,int(soc[t]):1},lb=pk); lp.constraint({int(zc[t]):1,int(soc[t]):1},lb=ck); lp.constraint({int(zu[t]):1,int(soc[t]):-1},lb=-uk); lp.set_obj(zt[t],tr*m.DT_HOURS); lp.set_obj(zp[t],(pr-tr)*m.DT_HOURS); lp.set_obj(zc[t],(cr-pr)*m.DT_HOURS); lp.set_obj(zu[t],ur*m.DT_HOURS)
    lp.constraint({int(soc[-1]):1},lb=e0,ub=e0); tariff=m._template(cfg,"consumption_demand"); groups=m._hour_groups(rows,tariff,False)
    if hourly_cap_kw is not None:
        for g in groups: lp.constraint({int(imp[i]):.25 for i in g["indices"]},ub=max(0.,float(hourly_cap_kw)))
    if tariff_enabled:
        rate=float(tariff["rate_sek_per_kw"])*100; theta=lp.add_vars("theta",1,0,ilim)[0]; z=lp.add_vars("top3_excess",len(groups),0,ilim); lp.set_obj(theta,rate); lp.set_obj(z,rate/3)
        for j,g in enumerate(groups):
            c={int(z[j]):1,int(theta):1}
            for i in g["indices"]: c[int(imp[i])]=c.get(int(imp[i]),0)-.25
            lp.constraint(c,lb=0)
    res=lp.solve(time_limit=90)
    if not res.success or res.x is None: return {"status":"infeasible_or_timeout","solver_status":int(res.status),"solver_message":str(res.message),"hourly_cap_kw":hourly_cap_kw}
    x=res.x; out=[]; energy=degradation=hurdle=reserve=upper=0.0
    for t,r in enumerate(rows):
        c=max(0.,float(x[charge[t]])); d=max(0.,float(x[discharge[t]])); gi=max(0.,float(x[imp[t]])); ge=max(0.,float(x[exp[t]])); dd=max(0.,float(x[ddisc[t]])); prices=_price(r,cfg); energy+=(gi*prices["effective_import_price_ore_kwh"]-ge*prices["effective_export_price_ore_kwh"])*m.DT_HOURS; degradation+=(c+d)*deg*m.DT_HOURS; hurdle+=dd*margin*m.DT_HOURS; reserve+=(float(x[zt[t]])*tr+float(x[zp[t]])*(pr-tr)+float(x[zc[t]])*(cr-pr))*m.DT_HOURS; upper+=float(x[zu[t]])*ur*m.DT_HOURS; out.append({"start":r["start"],"grid_import_kw":gi,"grid_export_kw":ge,"charge_kw":c,"discharge_kw":d,"soc_pct":float(x[soc[t]])/cap*100,"price_ore_kwh":float(r["price_ore_kwh"]),"effective_import_price_ore_kwh":prices["effective_import_price_ore_kwh"],"effective_export_price_ore_kwh":prices["effective_export_price_ore_kwh"]})
    te=m._evaluate(out,tariff); tc=float(te["cost_sek"])*100; cash=energy+degradation+tc; objective=cash+hurdle+reserve+upper
    return {"status":"optimal" if res.status==0 else "feasible","solver_status":int(res.status),"solver_message":str(res.message),"hourly_cap_kw":hourly_cap_kw,"tariff_enabled_in_objective":tariff_enabled,"initial_soc_pct":initial,"terminal_soc_pct":round(float(x[soc[-1]])/cap*100,3),"tariff":te,"economics":{"mode":CURRENT_ECONOMICS,"energy_cost_ore":round(energy,2),"battery_degradation_cost_ore":round(degradation,2),"tariff_cost_ore":round(tc,2),"cash_plus_tariff_ore":round(cash,2),"cash_plus_tariff_sek":round(cash/100,2),"discretionary_shift_hurdle_ore":round(hurdle,2),"reserve_policy_penalty_ore":round(reserve,2),"preferred_max_excess_penalty_ore":round(upper,2),"objective_cost_ore":round(objective,2)},"diagnostics":{"intervals":n,"active_tariff_clock_hours":len(groups),"negative_effective_import_price_intervals":len(neg)}}


def _common_objective_from_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    if not cfg: return {}
    from .price_economics import economics_payload
    optimizer=dict(cfg.get("optimizer") or {}); ep=economics_payload(cfg)
    return {"economics":{**ep,"battery_degradation_ore_kwh":float(optimizer.get("battery_degradation_ore_kwh",5.0))},"tariffs":dict(cfg.get("tariffs") or {}),"evaluation_semantics":"common_realized_economic_cost_current_economics","replay_economics_default":CURRENT_ECONOMICS}


def _reprice_evaluate_day_result(result: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    from . import optimizer_evaluation as m
    if result.get("status") not in {"ok","partial_plan_coverage"}: return result
    day=result.get("local_date"); rows,_=m._actual_rows(datetime.fromisoformat(str(day)).date())
    if not rows: return result
    ref=float(median([_price(r,cfg)["effective_import_price_ore_kwh"] for r in rows])); rt=result.get("realtime_counterfactual") or {}; base=result.get("zero_battery_baseline") or {}; comp=result.get("comparison") or {}; hindsight=result.get("perfect_hindsight") or {}
    battery=(cfg.get("policy") or {}).get("battery") or {}; cap=float(battery.get("capacity_kwh",19.6)); first=float(rt.get("initial_soc_pct") or 0.0); terminal=float(rt.get("terminal_soc_pct") or first); terminal_delta=cap*(terminal-first)/100.0; adj=terminal_delta*ref
    rt["terminal_asset_adjustment_ore"]=round(adj,2); rt["economic_cost_ore"]=round(float(rt.get("cash_cost_ore") or 0.0)-adj,2)
    baseline_cost=float(base.get("cash_cost_ore") or 0.0); base["economic_cost_ore"]=round(baseline_cost,2); saving=baseline_cost-float(rt["economic_cost_ore"]); comp["realtime_economic_saving_vs_zero_battery_ore"]=round(saving,2); comp["realtime_economic_saving_vs_zero_battery_sek"]=round(saving/100.0,2)
    if hindsight.get("status") in {"optimal","feasible"}:
        hcost=float(hindsight.get("cash_cost_ore") or 0.0)-adj; gap=float(rt["economic_cost_ore"])-hcost; comp["perfect_information_gap_ore"]=round(gap,2); comp["perfect_information_gap_sek"]=round(gap/100.0,2)
    result["reference_price_ore_kwh_for_terminal_energy"]=round(ref,3); result["economics"]={"mode":CURRENT_ECONOMICS,"pricing":economics_payload(cfg),"terminal_reference":"median_effective_import_price"}
    return result


def _wrap_app_comparison(original, cfg):
    def wrapped(*args, **kwargs):
        from . import app_comparison as m
        result=original(*args, **kwargs)
        if not isinstance(result,dict) or result.get("status") in {"unsupported_active_tariffs","no_actual_data","missing_soc"}: return result
        a,b=m.resolve_window(start=kwargs.get("start"),end=kwargs.get("end"),hours=kwargs.get("hours"),days=kwargs.get("days")); rows,_=m._actual_rows(a,b)
        if not rows: return result
        ref=float(median([_price(r,cfg)["effective_import_price_ore_kwh"] for r in rows])); actual=result.get("actual_app") or {}; planner=result.get("shadow_planner") or {}; comparison=result.get("comparison") or {}; battery=(cfg.get("policy") or {}).get("battery") or {}; cap=float(battery.get("capacity_kwh",19.6)); init=float(actual.get("initial_soc_pct") or 0.0)
        aa=(cap*(float(actual.get("terminal_soc_pct") or init)-init)/100.0)*ref; pa=(cap*(float(planner.get("terminal_soc_pct") or init)-init)/100.0)*ref; ac=float(actual.get("cash_cost_ore") or 0.0)-aa; pc=float(planner.get("cash_cost_ore") or 0.0)-pa; advantage=ac-pc
        actual.update({"terminal_asset_adjustment_ore":round(aa,2),"economic_cost_ore":round(ac,2),"economic_cost_sek":round(ac/100.0,2)}); planner.update({"terminal_asset_adjustment_ore":round(pa,2),"economic_cost_ore":round(pc,2),"economic_cost_sek":round(pc/100.0,2)}); comparison.update({"planner_advantage_ore":round(advantage,2),"planner_advantage_sek":round(advantage/100.0,2),"cash_cost_difference_ore":round(float(actual.get("cash_cost_ore") or 0.0)-float(planner.get("cash_cost_ore") or 0.0),2)})
        if result.get("valid_comparison"): result["winner"]="shadow_planner" if advantage>0.005 else "actual_app" if advantage<-0.005 else "tie"
        result["valuation"]={**(result.get("valuation") or {}),"reference_price_ore_kwh":round(ref,3),"economic_cost_definition":"effective import cost minus effective export revenue plus battery degradation minus terminal battery asset adjustment","economics_mode":CURRENT_ECONOMICS,"pricing":economics_payload(cfg)}
        return result
    return wrapped


def _app_actual_interval(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, float]:
    from . import app_comparison as m
    opt=cfg.get("optimizer") or {}; grid=float(row["grid_power_kw"]); batt=float(row["battery_power_kw"]); imp=max(0.0,grid); exp=max(0.0,-grid); prices=_price(row,cfg); energy_cost=(imp*prices["effective_import_price_ore_kwh"]-exp*prices["effective_export_price_ore_kwh"])*m.DT_HOURS; degradation=abs(batt)*m.DT_HOURS*float(opt.get("battery_degradation_ore_kwh",5.0))
    return {"grid_import_kw":imp,"grid_export_kw":exp,"effective_import_price_ore_kwh":prices["effective_import_price_ore_kwh"],"effective_export_price_ore_kwh":prices["effective_export_price_ore_kwh"],"energy_cost_ore":energy_cost,"degradation_cost_ore":degradation,"cash_cost_ore":energy_cost+degradation,"throughput_kwh":abs(batt)*m.DT_HOURS,"charge_kwh":max(0.0,-batt)*m.DT_HOURS,"discharge_kwh":max(0.0,batt)*m.DT_HOURS}


def _reset_learning_epoch_if_needed(cfg: dict[str, Any]) -> dict[str, Any]:
    sig=economics_signature(cfg); old={}
    if ECONOMICS_EPOCH_PATH.exists():
        try: old=json.loads(ECONOMICS_EPOCH_PATH.read_text(encoding="utf-8"))
        except Exception: old={}
    if old.get("signature")==sig: return {"changed":False,"signature":sig,"previous_signature":old.get("signature")}
    neural_samples=adaptive_runs=0
    try:
        from . import neural_training as nt
        nt._init_tables()
        with sqlite3.connect(nt.DB_PATH) as c:
            neural_samples=int(c.execute("SELECT COUNT(*) FROM neural_training_sample").fetchone()[0]); c.execute("DELETE FROM neural_training_sample")
        for path in (nt.MODEL_PATH,nt.MODEL_META_PATH):
            try: path.unlink(missing_ok=True)
            except Exception: pass
    except Exception: pass
    try:
        from . import adaptive_learning as al
        al.init_adaptive_learning_store()
        with sqlite3.connect(al.DB_PATH) as c:
            adaptive_runs=int(c.execute("SELECT COUNT(*) FROM adaptive_learning_run WHERE status='complete'").fetchone()[0]); c.execute("UPDATE adaptive_learning_run SET status='superseded_economics' WHERE status='complete'")
        al.persist_parameters(al.DEFAULT_PARAMETERS,"candidate",score_ore=None,source_run_id=None)
    except Exception: pass
    state={"signature":sig,"previous_signature":old.get("signature"),"changed_at":datetime.now(timezone.utc).isoformat(),"neural_samples_invalidated":neural_samples,"adaptive_complete_runs_superseded":adaptive_runs,"semantics":"historical physical/forecast/spot data retained; learned labels/parameters are rebuilt under current economics"}
    ECONOMICS_EPOCH_PATH.parent.mkdir(parents=True,exist_ok=True); ECONOMICS_EPOCH_PATH.write_text(json.dumps(state,indent=2),encoding="utf-8")
    return {"changed":True,**state}


def _patch_neural_candidates(cfg: dict[str, Any]):
    from . import neural_training as nt
    from .engine_contract import EngineInput
    from .engine_input_v2 import enriched_common_objective
    original=nt._candidate_inputs
    def wrapped(local_cfg: dict[str, Any], limit: int = 1000):
        candidates,diag=original(local_cfg,limit); out=[]
        for item in candidates:
            objective=enriched_common_objective(local_cfg,item.decision_start,item.generated_at)
            out.append(EngineInput(generated_at=item.generated_at,decision_start=item.decision_start,initial_soc_pct=item.initial_soc_pct,interval_minutes=item.interval_minutes,horizon_rows=item.horizon_rows,constraints=item.constraints,objective=objective,source={**item.source,"economics_repriced":CURRENT_ECONOMICS}))
        diag={**diag,"economics_repricing":CURRENT_ECONOMICS,"economics_signature":economics_signature(local_cfg)}
        return out,diag
    nt._candidate_inputs=wrapped


def install_economics_patches(cfg: dict[str, Any]) -> dict[str, Any]:
    from . import adaptive_deterministic as ad
    from . import adaptive_replay as ar
    from . import app_comparison as ac
    from . import engine_contract as ec
    from . import engine_input_v2 as ei
    from . import historical_closed_loop as hc
    from . import monthly_replay as mr
    from . import optimizer as op
    from . import optimizer_evaluation as oe
    from . import optimizer_v35_replay as ov
    from . import tariff_scenarios as ts

    op._continuation_profile=_optimizer_continuation_profile; op._interval_result=_optimizer_interval_result
    ov._continuation_profile=_optimizer_continuation_profile; ov._interval_result=_optimizer_interval_result
    ad._interval_result_adaptive=_adaptive_interval_result
    oe._baseline_interval=_evaluation_baseline_interval; oe._apply_action=_evaluation_apply_action; oe._hindsight=_evaluation_hindsight
    hc._apply_action=_evaluation_apply_action
    ar._apply_action=_evaluation_apply_action
    ac._apply_action=_evaluation_apply_action; ac._actual_interval=_app_actual_interval
    ts._continuation_profile=_optimizer_continuation_profile; ts._solve_rows=_tariff_solve_rows
    mr._solve=_monthly_solve
    ec.common_objective_from_cfg=_common_objective_from_cfg; ei.common_objective_from_cfg=_common_objective_from_cfg

    original_eval=oe.evaluate_day
    def eval_day(*args,**kwargs): return _reprice_evaluate_day_result(original_eval(*args,**kwargs),cfg)
    oe.evaluate_day=eval_day

    original_compare=ac.compare_app_vs_planner
    ac.compare_app_vs_planner=_wrap_app_comparison(original_compare,cfg)

    original_post=ar.DailyReplayEvaluator.__post_init__
    def replay_post(self):
        original_post(self)
        self.reference_price_ore_kwh=float(median([_price(r,self.cfg)["effective_import_price_ore_kwh"] for r in self.rows]))
    ar.DailyReplayEvaluator.__post_init__=replay_post

    _patch_neural_candidates(cfg)
    epoch=_reset_learning_epoch_if_needed(cfg)
    return {"installed":True,"pricing_model":economics_payload(cfg).get("pricing_model"),"economics_signature":economics_signature(cfg),"learning_epoch":epoch,"patched_paths":["optimizer","optimizer_v35_replay","adaptive_deterministic","adaptive_replay","optimizer_evaluation","historical_closed_loop","tariff_scenarios","monthly_replay","app_comparison","engine_contract","engine_input_v2","neural_training"]}
