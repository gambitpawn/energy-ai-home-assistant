from __future__ import annotations

import csv, io, math
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from .tariff_scenarios import LOCAL_TZ, _LP, _hour_groups, _tariff_metric_from_hourly, _template
from .training import DATASET_PATH

ENGINE_NAME="monthly_tariff_replay_milp_v1"
CACHE_DIR=Path("/data/training/tariff_replay")
ENERGY_CHARTS_PRICE_URL="https://api.energy-charts.info/price"
ECB_FX_URL="https://data-api.ecb.europa.eu/service/data/EXR/D.SEK.EUR.SP00.A"
DEFAULT_FIXED_CAPS_KW=[0.0,0.5,0.75,1.0,1.5,2.0]
DT_HOURS=0.25


def _bounds(month:str)->tuple[date,date]:
    try: first=datetime.strptime(month,"%Y-%m").date().replace(day=1)
    except ValueError as exc: raise ValueError("month must be YYYY-MM") from exc
    return first,first.replace(day=monthrange(first.year,first.month)[1])


def _ts(raw:str)->datetime:
    d=datetime.fromisoformat(str(raw).replace("Z","+00:00"))
    if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _bucket(d:datetime)->datetime:
    d=d.astimezone(timezone.utc); return d.replace(minute=(d.minute//15)*15,second=0,microsecond=0)


def _num(v:Any)->float|None:
    if v in (None,"","null","None","nan"): return None
    try: return float(str(v).replace(",","."))
    except (TypeError,ValueError): return None


def _dataset_month(month:str)->tuple[list[dict[str,Any]],dict[str,Any]]:
    if not DATASET_PATH.exists(): raise RuntimeError(f"Training dataset not found: {DATASET_PATH}")
    first,last=_bounds(month); rows=[]; expected=((last-first).days+1)*96
    with DATASET_PATH.open("r",encoding="utf-8",newline="") as f:
        for raw in csv.DictReader(f):
            try: stamp=_ts(raw.get("timestamp_utc") or "")
            except Exception: continue
            local=stamp.astimezone(LOCAL_TZ)
            if not first<=local.date()<=last: continue
            load,pv=_num(raw.get("load_power_kw")),_num(raw.get("pv_power_kw"))
            if load is None or pv is None: continue
            rows.append({"start":_bucket(stamp).isoformat(),"load_kw":max(0.0,load),"pv_kw":max(0.0,pv)})
    dedup={r["start"]:r for r in rows}; rows=[dedup[k] for k in sorted(dedup,key=_ts)]
    return rows,{"month":month,"expected_intervals":expected,"usable_intervals":len(rows),"coverage_fraction":round(len(rows)/max(1,expected),4),"first":rows[0]["start"] if rows else None,"last":rows[-1]["start"] if rows else None,"source":str(DATASET_PATH)}


def _cache(month:str)->Path:
    CACHE_DIR.mkdir(parents=True,exist_ok=True); return CACHE_DIR/f"market_{month}_se4.csv"


def _energy_prices(data:dict[str,Any])->dict[datetime,float]:
    prices=data.get("price") or data.get("prices") or []
    stamps=data.get("unix_seconds") or data.get("timestamp") or data.get("timestamps") or data.get("time") or []
    if not isinstance(prices,list) or not isinstance(stamps,list) or len(prices)!=len(stamps): raise RuntimeError("Unexpected Energy-Charts /price response shape")
    points=[]
    for raw,praw in zip(stamps,prices):
        p=_num(praw)
        if p is None: continue
        try: stamp=datetime.fromtimestamp(float(raw),tz=timezone.utc) if isinstance(raw,(int,float)) or str(raw).isdigit() else _ts(str(raw))
        except Exception: continue
        points.append((_bucket(stamp),p))
    points.sort(key=lambda x:x[0])
    if not points: raise RuntimeError("Energy-Charts returned no usable SE4 prices")
    out={}
    for i,(stamp,p) in enumerate(points):
        nxt=points[i+1][0] if i+1<len(points) else stamp+timedelta(minutes=15)
        span=max(1,min(4,int(round((nxt-stamp).total_seconds()/900))))
        for q in range(span): out[stamp+timedelta(minutes=15*q)]=p
    return out


def _fx_csv(text:str)->dict[date,float]:
    out={}
    for row in csv.DictReader(io.StringIO(text)):
        try: out[date.fromisoformat(str(row.get("TIME_PERIOD") or row.get("Time period")))]=float(row.get("OBS_VALUE") or row.get("Obs value"))
        except Exception: pass
    if not out: raise RuntimeError("ECB returned no usable SEK/EUR observations")
    return out


def _fx(day:date,values:dict[date,float])->float:
    d=day
    for _ in range(14):
        if d in values: return float(values[d])
        d-=timedelta(days=1)
    raise RuntimeError(f"No ECB SEK/EUR reference rate available for {day}")


async def _fetch_market(month:str)->tuple[list[dict[str,Any]],dict[str,Any]]:
    first,last=_bounds(month)
    async with httpx.AsyncClient(timeout=45.0,follow_redirects=True) as client:
        rp=await client.get(ENERGY_CHARTS_PRICE_URL,params={"bzn":"SE4","start":first.isoformat(),"end":last.isoformat()}); rp.raise_for_status(); prices=_energy_prices(rp.json())
        rf=await client.get(ECB_FX_URL,params={"startPeriod":(first-timedelta(days=10)).isoformat(),"endPeriod":last.isoformat(),"format":"csvdata"}); rf.raise_for_status(); fx=_fx_csv(rf.text)
    rows=[]
    for stamp,eur_mwh in sorted(prices.items()):
        local=stamp.astimezone(LOCAL_TZ)
        if first<=local.date()<=last:
            sek=_fx(local.date(),fx); rows.append({"start":stamp.isoformat(),"price_eur_mwh":eur_mwh,"sek_per_eur":sek,"price_ore_kwh":eur_mwh*sek/10.0})
    path=_cache(month)
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["start","price_eur_mwh","sek_per_eur","price_ore_kwh"]); w.writeheader(); w.writerows(rows)
    return rows,{"source":"Energy-Charts /price (ENTSO-E) + ECB EXR.D.SEK.EUR.SP00.A","cache":str(path),"rows":len(rows),"refreshed":True}


def _cached_market(month:str):
    path=_cache(month)
    if not path.exists(): return None
    rows=[]
    with path.open("r",encoding="utf-8",newline="") as f:
        for r in csv.DictReader(f):
            try: rows.append({"start":_bucket(_ts(r["start"])).isoformat(),"price_eur_mwh":float(r["price_eur_mwh"]),"sek_per_eur":float(r["sek_per_eur"]),"price_ore_kwh":float(r["price_ore_kwh"])})
            except Exception: pass
    return (rows,{"source":"cached Energy-Charts + ECB","cache":str(path),"rows":len(rows),"refreshed":False}) if rows else None


async def market_month(month:str,refresh:bool=False):
    if not refresh:
        cached=_cached_market(month)
        if cached: return cached
    return await _fetch_market(month)


def _join(data,market):
    p={r["start"]:r for r in market}; rows=[]
    for d in data:
        if d["start"] in p:
            m=p[d["start"]]; rows.append({**d,"price_ore_kwh":m["price_ore_kwh"],"price_eur_mwh":m["price_eur_mwh"],"sek_per_eur":m["sek_per_eur"]})
    return rows,{"dataset_intervals":len(data),"market_intervals":len(market),"joined_intervals":len(rows),"join_fraction_of_dataset":round(len(rows)/max(1,len(data)),4)}


def _evaluate(rows,tariff):
    groups=_hour_groups(rows,tariff,False); hourly=[]; details=[]
    for g in groups:
        v=sum(float(rows[i].get("grid_import_kw") or 0.0) for i in g["indices"])/4.0; hourly.append(v); details.append({"date":g["date"],"hour":g["hour"],"kw":round(v,4)})
    metric=_tariff_metric_from_hourly(hourly,tariff,[])
    return {**metric,"active_hour_count":len(hourly),"hours_above_0_5_kw":sum(x>0.500001 for x in hourly),"hours_above_1_0_kw":sum(x>1.000001 for x in hourly),"max_hour_kw":round(max(hourly,default=0.0),4),"top_hours":sorted(details,key=lambda x:x["kw"],reverse=True)[:10]}


def _solve(rows,cfg,*,tariff_enabled:bool,hourly_cap_kw:float|None=None,initial_soc_pct:float=50.0):
    if not rows: raise RuntimeError("No monthly replay rows")
    b=(cfg.get("policy") or {}).get("battery") or {}; econ=(cfg.get("policy") or {}).get("economics") or {}; o=cfg.get("optimizer") or {}
    cap=float(b.get("capacity_kwh",19.6)); hmin=float(b.get("hard_min_soc_pct",5)); hmax=float(b.get("hard_max_soc_pct",100)); pmin=float(b.get("preferred_min_soc_pct",15)); pmax=float(b.get("preferred_max_soc_pct",90)); reserve_pct=float(b.get("normal_reserve_soc_pct",20)); initial=max(hmin,min(hmax,float(initial_soc_pct))); e0=cap*initial/100
    ec=float(o.get("battery_charge_efficiency",.95)); ed=float(o.get("battery_discharge_efficiency",.95)); cmax=float(o.get("battery_max_charge_kw",8)); dmax=float(o.get("battery_max_discharge_kw",8)); ilim=float(o.get("physical_grid_import_limit_kw",13.8)); elim=float(o.get("grid_export_limit_kw",10)); deg=float(o.get("battery_degradation_ore_kwh",5)); margin=float(econ.get("minimum_arbitrage_margin_ore_kwh",20)); ioh=float(econ.get("import_overhead_ore_kwh",0)); eoh=float(econ.get("export_overhead_ore_kwh",0))
    critical=max(hmin,min(pmin,float(o.get("reserve_critical_soc_pct",10)))); cr=max(0.,float(o.get("reserve_critical_penalty_ore_per_kwh_hour",300))); pr=max(0.,min(cr,float(o.get("reserve_preferred_penalty_ore_per_kwh_hour",100)))); tr=max(0.,min(pr,float(o.get("reserve_target_penalty_ore_per_kwh_hour",10)))); ur=max(0.,float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour",2)))
    n=len(rows); lp=_LP(); charge=lp.add_vars("charge",n,0,cmax); discharge=lp.add_vars("discharge",n,0,dmax); imp=lp.add_vars("import",n,0,ilim); exp=lp.add_vars("export",n,0,elim); soc=lp.add_vars("soc",n,cap*hmin/100,cap*hmax/100); ddisc=lp.add_vars("disc_discharge",n,0,dmax); zt=lp.add_vars("z_target",n,0,cap); zp=lp.add_vars("z_preferred",n,0,cap); zc=lp.add_vars("z_critical",n,0,cap); zu=lp.add_vars("z_upper",n,0,cap)
    neg=[i for i,r in enumerate(rows) if float(r["price_ore_kwh"])+ioh<0]; yb=lp.add_vars("yb",len(neg),0,1,integral=True) if neg else np.array([],dtype=int); yg=lp.add_vars("yg",len(neg),0,1,integral=True) if neg else np.array([],dtype=int); negpos={t:j for j,t in enumerate(neg)}
    rk=cap*reserve_pct/100; pk=cap*pmin/100; ck=cap*critical/100; uk=cap*pmax/100
    for t,r in enumerate(rows):
        net=float(r["load_kw"])-float(r["pv_kw"]); lp.constraint({int(imp[t]):1,int(exp[t]):-1,int(discharge[t]):1,int(charge[t]):-1},lb=net,ub=net); coeff={int(soc[t]):1,int(charge[t]):-ec*DT_HOURS,int(discharge[t]):DT_HOURS/ed}
        if t: coeff[int(soc[t-1])]=-1; lp.constraint(coeff,lb=0,ub=0)
        else: lp.constraint(coeff,lb=e0,ub=e0)
        required=max(0.,net-ilim); lp.constraint({int(ddisc[t]):1,int(discharge[t]):-1},lb=-required)
        if t in negpos:
            j=negpos[t]; lp.constraint({int(charge[t]):1,int(yb[j]):-cmax},ub=0); lp.constraint({int(discharge[t]):1,int(yb[j]):dmax},ub=dmax); lp.constraint({int(imp[t]):1,int(yg[j]):-ilim},ub=0); lp.constraint({int(exp[t]):1,int(yg[j]):elim},ub=elim)
        buy=float(r["price_ore_kwh"])+ioh; sell=max(0.,float(r["price_ore_kwh"])-eoh); lp.set_obj(imp[t],(buy+.001)*DT_HOURS); lp.set_obj(exp[t],(-sell+.001)*DT_HOURS); lp.set_obj(charge[t],deg*DT_HOURS); lp.set_obj(discharge[t],deg*DT_HOURS); lp.set_obj(ddisc[t],margin*DT_HOURS)
        lp.constraint({int(zt[t]):1,int(soc[t]):1},lb=rk); lp.constraint({int(zp[t]):1,int(soc[t]):1},lb=pk); lp.constraint({int(zc[t]):1,int(soc[t]):1},lb=ck); lp.constraint({int(zu[t]):1,int(soc[t]):-1},lb=-uk); lp.set_obj(zt[t],tr*DT_HOURS); lp.set_obj(zp[t],(pr-tr)*DT_HOURS); lp.set_obj(zc[t],(cr-pr)*DT_HOURS); lp.set_obj(zu[t],ur*DT_HOURS)
    lp.constraint({int(soc[-1]):1},lb=e0,ub=e0)
    tariff=_template(cfg,"consumption_demand"); groups=_hour_groups(rows,tariff,False)
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
    x=res.x; out=[]; energy=degradation=hurdle=reserve=upper=0.
    for t,r in enumerate(rows):
        c=max(0.,float(x[charge[t]])); d=max(0.,float(x[discharge[t]])); gi=max(0.,float(x[imp[t]])); ge=max(0.,float(x[exp[t]])); dd=max(0.,float(x[ddisc[t]])); buy=float(r["price_ore_kwh"])+ioh; sell=max(0.,float(r["price_ore_kwh"])-eoh); energy+=(gi*buy-ge*sell)*DT_HOURS; degradation+=(c+d)*deg*DT_HOURS; hurdle+=dd*margin*DT_HOURS; reserve+=(float(x[zt[t]])*tr+float(x[zp[t]])*(pr-tr)+float(x[zc[t]])*(cr-pr))*DT_HOURS; upper+=float(x[zu[t]])*ur*DT_HOURS; out.append({"start":r["start"],"grid_import_kw":gi,"grid_export_kw":ge,"charge_kw":c,"discharge_kw":d,"soc_pct":float(x[soc[t]])/cap*100})
    te=_evaluate(out,tariff); tc=float(te["cost_sek"])*100; cash=energy+degradation+tc; objective=cash+hurdle+reserve+upper
    return {"status":"optimal" if res.status==0 else "feasible","solver_status":int(res.status),"solver_message":str(res.message),"hourly_cap_kw":hourly_cap_kw,"tariff_enabled_in_objective":tariff_enabled,"initial_soc_pct":initial,"terminal_soc_pct":round(float(x[soc[-1]])/cap*100,3),"tariff":te,"economics":{"energy_cost_ore":round(energy,2),"battery_degradation_cost_ore":round(degradation,2),"tariff_cost_ore":round(tc,2),"cash_plus_tariff_ore":round(cash,2),"cash_plus_tariff_sek":round(cash/100,2),"discretionary_shift_hurdle_ore":round(hurdle,2),"reserve_policy_penalty_ore":round(reserve,2),"preferred_max_excess_penalty_ore":round(upper,2),"objective_cost_ore":round(objective,2)},"diagnostics":{"intervals":n,"active_tariff_clock_hours":len(groups),"negative_price_intervals":len(neg)}}


def replay_status():
    coverage=[]
    if DATASET_PATH.exists():
        seen={}
        with DATASET_PATH.open("r",encoding="utf-8",newline="") as f:
            for r in csv.DictReader(f):
                try: local=_ts(r.get("timestamp_utc") or "").astimezone(LOCAL_TZ); load=_num(r.get("load_power_kw")); pv=_num(r.get("pv_power_kw"))
                except Exception: continue
                if load is None or pv is None: continue
                k=f"{local.year:04d}-{local.month:02d}"; seen[k]=seen.get(k,0)+1
        for m,count in sorted(seen.items()):
            first,last=_bounds(m); expected=((last-first).days+1)*96; coverage.append({"month":m,"usable_intervals":count,"expected_intervals":expected,"coverage_fraction":round(count/expected,4),"consumption_tariff_active_month":first.month in {1,2,11,12}})
    caches=[p.stem.replace("market_","").replace("_se4","") for p in sorted(CACHE_DIR.glob("market_*_se4.csv"))] if CACHE_DIR.exists() else []
    return {"engine":ENGINE_NAME,"dataset":str(DATASET_PATH),"dataset_exists":DATASET_PATH.exists(),"coverage":coverage,"cached_market_months":caches,"fixed_cap_benchmarks_kw":DEFAULT_FIXED_CAPS_KW}


async def run_month_replay(cfg,month:str,*,refresh_market:bool=False,initial_soc_pct:float=50.0,fixed_caps_kw:list[float]|None=None):
    data,dd=_dataset_month(month)
    if dd["coverage_fraction"]<.90: raise RuntimeError(f"Historical load/PV coverage for {month} is only {dd['coverage_fraction']:.1%}; require at least 90%")
    market,md=await market_month(month,refresh_market); rows,jd=_join(data,market)
    if jd["join_fraction_of_dataset"]<.90: raise RuntimeError(f"Historical market-data join for {month} is only {jd['join_fraction_of_dataset']:.1%}; require at least 90%")
    contiguous=[rows[0]] if rows else []
    for r in rows[1:]:
        if _ts(r["start"])-_ts(contiguous[-1]["start"])!=timedelta(minutes=15): break
        contiguous.append(r)
    if len(contiguous)/max(1,len(rows))<.95: raise RuntimeError("Monthly replay has a material internal timestamp gap; refusing to optimize across missing history")
    rows=contiguous; base=_solve(rows,cfg,tariff_enabled=False,initial_soc_pct=initial_soc_pct); optimal=_solve(rows,cfg,tariff_enabled=True,initial_soc_pct=initial_soc_pct); caps=DEFAULT_FIXED_CAPS_KW if fixed_caps_kw is None else sorted(set(max(0.,float(x)) for x in fixed_caps_kw)); fixed=[_solve(rows,cfg,tariff_enabled=True,hourly_cap_kw=x,initial_soc_pct=initial_soc_pct) for x in caps]; feasible=[x for x in fixed if x.get("status") in {"optimal","feasible"}]; best=min(feasible,key=lambda x:float((x.get("economics") or {}).get("objective_cost_ore",math.inf))) if feasible else None
    return {"engine":ENGINE_NAME,"month":month,"test_only":True,"base_planner_unchanged":(cfg.get("optimizer") or {}).get("planner"),"data":{"training":dd,"market":md,"join":jd,"optimized_contiguous_intervals":len(rows)},"assumptions":{"initial_and_terminal_soc_pct":initial_soc_pct,"replay_forecast_mode":"perfect_hindsight_load_pv_prices","reserve_mode":"normal_reserve_piecewise_policy_cost","tariff":_template(cfg,"consumption_demand"),"fixed_hourly_cap_benchmarks_kw":caps},"no_tariff_optimizer":base,"tariff_optimal":optimal,"fixed_cap_benchmarks":fixed,"decision":{"emergent_top3_kw":(optimal.get("tariff") or {}).get("metric_kw"),"emergent_max_hour_kw":(optimal.get("tariff") or {}).get("max_hour_kw"),"best_fixed_cap_kw_by_objective":None if best is None else best.get("hourly_cap_kw"),"best_fixed_cap_objective_cost_ore":None if best is None else (best.get("economics") or {}).get("objective_cost_ore")}}


async def run_winter_replay(cfg,months:list[str],*,refresh_market:bool=False,initial_soc_pct:float=50.0):
    results=[]
    for month in months:
        try: results.append(await run_month_replay(cfg,month,refresh_market=refresh_market,initial_soc_pct=initial_soc_pct))
        except Exception as exc: results.append({"month":month,"error":repr(exc)})
    good=[r for r in results if "error" not in r]
    return {"engine":ENGINE_NAME,"months":results,"summary":{"successful_months":len(good),"failed_months":len(results)-len(good),"emergent_top3_kw":{r["month"]:r["decision"]["emergent_top3_kw"] for r in good},"best_fixed_cap_kw":{r["month"]:r["decision"]["best_fixed_cap_kw_by_objective"] for r in good}}}
