from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import date
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .dashboard import _history
from .db import connect_db
from .optimizer_evaluation import _day_bounds, _dt, _plan_actions, evaluate_day
from .regret_decomposition import ENGINE_NAME as REGRET_ENGINE_NAME, regret_decomposition


_DECOMP_LOCK = threading.Lock()
_OPPORTUNITY_EPS_SEK = 0.05


EVALUATION_EXTENSION = r'''
<style>
#overviewPlan{border-radius:10px;background-repeat:no-repeat}
.eval-breakdown{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:10px}
.eval-breakdown .piece{background:#0f1720;border:1px solid var(--line);border-radius:10px;padding:11px}
.eval-breakdown .piece .name{color:var(--muted);font-size:11px}.eval-breakdown .piece .amount{font-size:20px;font-weight:750;margin-top:3px}.eval-breakdown .piece .share{font-size:11px;color:var(--muted);margin-top:2px}
.opportunity-legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:4px 0 8px}.opportunity-legend span{display:flex;align-items:center;gap:6px}.opportunity-legend i{display:inline-block;width:14px;height:8px;border-radius:2px}
#opportunityChart{height:280px}.quality-note{font-size:11px;color:var(--muted);margin-top:8px}
@media(max-width:720px){.eval-breakdown{grid-template-columns:1fr}}
</style>
<script>
const drawOverviewBeforeEvaluation=drawOverview;
drawOverview=function(){
  drawOverviewBeforeEvaluation();
  const el=$('overviewPlan'),actual=overviewRealized.rows||[],planned=pRows();
  const stamps=[...actual.map(r=>Date.parse(r.start)),...planned.map(r=>Date.parse(r.start||r.start_utc))].filter(Number.isFinite);
  const now=Date.parse(overviewRealized.now||new Date().toISOString());
  if(stamps.length&&Number.isFinite(now)){
    const lo=Math.min(...stamps),hi=Math.max(...stamps),pctNow=Math.max(0,Math.min(100,100*(now-lo)/Math.max(1,hi-lo)));
    el.style.background=`linear-gradient(to right, transparent 0%, transparent ${pctNow}%, rgba(79,179,255,.045) ${pctNow}%, rgba(79,179,255,.045) 100%)`;
  }
};

C.hindsight='#e6d36f';
Object.assign(pick.eval,{forecastLoad:false,forecastPv:false,hindsightBattery:false,plannedSoc:false,hindsightSoc:false});
function evalPickerCurrent(){
  const defs=[['load','Actual load',C.load,'solid'],['forecastLoad','Forecast load',C.load,'dashed'],['pv','Actual PV',C.pv,'solid'],['forecastPv','Forecast PV',C.pv,'dashed'],['battery','Applied battery',C.battery,'solid'],['plannedBattery','Planned battery',C.battery,'dashed'],['hindsightBattery','Hindsight battery',C.hindsight,'dotted'],['soc','Virtual SOC',C.soc,'solid'],['plannedSoc','Planned SOC',C.soc,'dashed'],['hindsightSoc','Hindsight SOC',C.hindsight,'dotted'],['price','Spot price',C.price,'solid'],['gridImport','Grid import',C.gridImport,'solid'],['gridExport','Grid export',C.gridExport,'solid']];
  const el=$('evalPicker');
  el.innerHTML=defs.map(d=>{const sw=d[3]==='dashed'?'repeating-linear-gradient(to right,currentColor 0 5px,transparent 5px 8px)':d[3]==='dotted'?'repeating-linear-gradient(to right,currentColor 0 2px,transparent 2px 5px)':'currentColor';return `<label><input type="checkbox" data-k="${d[0]}" ${pick.eval[d[0]]?'checked':''}><span class="swatch" style="color:${d[2]};background:${sw}"></span>${d[1]}</label>`}).join('');
  el.onchange=e=>{const k=e.target?.dataset?.k;if(!k)return;pick.eval[k]=e.target.checked;drawEvaluationCurrent()};
}
function drawEvaluationCurrent(){
  const e=state.eval||{},rr=e.rows||[],ps=pick.eval,ha=e.perfect_hindsight?.actions||[],hm=new Map(ha.map(x=>[Date.parse(x.start),x]));
  lineChart($('evalChart'),[
    {label:'Actual load',axis:'power',color:C.load,values:rr.map(r=>r.actual_load_kw),on:ps.load},
    {label:'Forecast load',axis:'power',color:C.load,values:rr.map(r=>r.forecast_load_kw),on:ps.forecastLoad,dashed:true},
    {label:'Actual PV',axis:'power',color:C.pv,values:rr.map(r=>r.actual_pv_kw),on:ps.pv},
    {label:'Forecast PV',axis:'power',color:C.pv,values:rr.map(r=>r.forecast_pv_kw),on:ps.forecastPv,dashed:true},
    {label:'Applied battery',axis:'power',color:C.battery,values:rr.map(r=>r.applied_action_kw),on:ps.battery},
    {label:'Planned battery',axis:'power',color:C.battery,values:rr.map(r=>r.requested_action_kw),on:ps.plannedBattery,dashed:true},
    {label:'Hindsight battery',axis:'power',color:C.hindsight,values:rr.map(r=>hm.get(Date.parse(r.start))?.action_kw??null),on:ps.hindsightBattery,dashed:true,width:1.8},
    {label:'Spot price',axis:'price',color:C.price,values:rr.map(r=>r.price_ore_kwh),on:ps.price},
    {label:'Virtual SOC',axis:'soc',color:C.soc,values:rr.map(r=>r.virtual_soc_end_pct),on:ps.soc},
    {label:'Planned SOC',axis:'soc',color:C.soc,values:rr.map(r=>r.forecast_soc_end_pct),on:ps.plannedSoc,dashed:true},
    {label:'Hindsight SOC',axis:'soc',color:C.hindsight,values:rr.map(r=>hm.get(Date.parse(r.start))?.soc_end_pct??null),on:ps.hindsightSoc,dashed:true,width:1.8},
    {label:'Grid import',axis:'power',color:C.gridImport,values:rr.map(r=>r.grid_import_kw),on:ps.gridImport},
    {label:'Grid export',axis:'power',color:C.gridExport,values:rr.map(r=>r.grid_export_kw==null?null:-Number(r.grid_export_kw)),on:ps.gridExport}
  ],rr.map(r=>r.start));
}

const evaluationGrid=document.querySelector('#evaluation .grid.two');
if(evaluationGrid&&!$('evalRegretCard'))evaluationGrid.insertAdjacentHTML('beforebegin','<div class="card" id="evalRegretCard" style="margin-top:12px"><h2>Where did the remaining opportunity go?</h2><div id="evalRegretBreakdown" class="notice">Decomposition is calculated for complete, mature days.</div></div>');
const historyCharts=document.querySelector('#history .grid.two');
if(historyCharts){historyCharts.className='';historyCharts.innerHTML='<div class="card" style="margin-top:12px"><h2>Daily opportunity captured</h2><div class="opportunity-legend"><span><i style="background:var(--good)"></i>Captured saving</span><span><i style="background:var(--warn)"></i>Remaining gap</span><span><i style="background:var(--bad)"></i>Negative saving</span></div><div id="opportunityChart"></div><div class="quality-note">Only complete days are included. Total bar height is saving + gap to hindsight.</div></div>'}

function captureText(v){return v==null?'—':`${n(100*Number(v),1)}%`}
function regretPiece(name,value,total,title){const share=value!=null&&total>0?value/total:null;return `<div class="piece" title="${title}"><div class="name">${name}</div><div class="amount">${sek(value)}</div><div class="share">${share==null?'—':`${n(100*share,1)}% of remaining gap`}</div></div>`}
function renderRegretBreakdown(){
  const e=state.eval||{},r=e.regret_decomposition_ui||{},el=$('evalRegretBreakdown');if(!el)return;
  if(!r.valid){el.className='notice';el.textContent=r.status==='insufficient_future_actual_coverage'?'Decomposition is not mature yet: future realized load/PV is still missing for part of the stored planning horizon.':`Decomposition unavailable (${r.status||'not available'}).`;return}
  const total=Number(r.total_gap_sek||0);el.className='eval-breakdown';el.innerHTML=regretPiece('PV/load forecast gap',r.forecast_gap_sek,total,'Cost of using forecast rather than realized PV and house load, with historical price information unchanged.')+regretPiece('Unpublished price horizon',r.unpublished_price_horizon_sek,total,'Value of knowing prices that had not yet been published at decision time. Already published prices do not change.')+regretPiece('Planner / policy gap',r.planner_policy_gap_sek,total,'Residual from rolling horizon, terminal value, reserve and policy choices after perfect load, PV and price information.');
}
function renderEvaluationSummary(){
  const e=state.eval||{},m=e.evaluation_summary||{},d=e.data||{},rt=e.realtime_counterfactual||{},fe=e.forecast_error_on_executed_intervals||{};
  if(!e.local_date)return;
  $('evalKpis').innerHTML=card('Saving',sek(m.saving_sek),'vs zero-battery baseline',m.saving_sek>0?'good':m.saving_sek<0?'bad':'')+card('Available opportunity',sek(m.opportunity_sek),'Saving + gap to hindsight')+card('Opportunity captured',captureText(m.capture_fraction),'Share of available opportunity',m.capture_fraction>=.7?'good':m.capture_fraction!=null&&m.capture_fraction<.4?'warn':'')+card('Remaining gap',sek(m.remaining_gap_sek),'Gap to hindsight',m.remaining_gap_sek>0?'warn':'')+card('Plan coverage',pct(d.plan_action_coverage_fraction),'≥90% for complete day',d.plan_action_coverage_fraction>=.9?'good':'warn')+card('Data quality',m.comparable?'Complete':'Partial',m.comparable?'Included in period KPIs':'Excluded from period KPIs',m.comparable?'good':'warn');
  $('diagnostics').innerHTML=rows({'Status':`<span class="pill ${e.status==='ok'?'ok':'partial'}">${e.status}</span>`,'Load MAE':fe.load?.mae_kw!=null?`${n(fe.load.mae_kw)} kW`:'—','PV MAE':fe.pv?.mae_kw!=null?`${n(fe.pv.mae_kw)} kW`:'—','Net-load MAE':fe.net_load?.mae_kw!=null?`${n(fe.net_load.mae_kw)} kW`:'—','Forecast economic gap':e.regret_decomposition_ui?.valid?sek(e.regret_decomposition_ui.forecast_gap_sek):'—','Battery throughput':rt.battery_throughput_kwh!=null?`${n(rt.battery_throughput_kwh)} kWh`:'—','Clamped intervals':n(rt.clamped_action_intervals,0),'Terminal SOC':rt.terminal_soc_pct!=null?`${n(rt.terminal_soc_pct,1)}%`:'—','Import exceedances':n(rt.import_proxy_exceedance_intervals,0)});
  renderRegretBreakdown();
}
function opportunityBars(el,days){
  if(!el)return;const data=days.filter(x=>x.status==='ok'&&x.opportunity_sek!=null);if(!data.length){el.innerHTML='<div class="empty">No complete evaluated days.</div>';return}
  const W=1000,H=260,p={l:52,r:18,t:18,b:52};const max=Math.max(1,...data.map(x=>Math.max(0,Number(x.opportunity_sek||0))),...data.map(x=>Math.max(0,-Number(x.saving_sek||0))));const base=H-p.b;const usable=base-p.t;const slot=(W-p.l-p.r)/data.length,bw=Math.max(5,Math.min(42,slot*.58));let svg=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
  for(let j=0;j<5;j++){const v=max*(4-j)/4,y=p.t+usable*j/4;svg+=`<line x1="${p.l}" y1="${y}" x2="${W-p.r}" y2="${y}" stroke="#263647"/><text x="4" y="${y+4}" fill="#91a2b3" font-size="10">${n(v,1)}</text>`}
  data.forEach((x,i)=>{const cx=p.l+slot*(i+.5),saving=Number(x.saving_sek||0),gap=Math.max(0,Number(x.remaining_gap_sek||0)),opp=Math.max(0,Number(x.opportunity_sek||0)),cap=Math.max(0,Math.min(opp,saving)),hCap=usable*cap/max,hGap=usable*Math.max(0,opp-cap)/max;const title=`${x.local_date}: opportunity ${n(x.opportunity_sek)} SEK · saving ${n(x.saving_sek)} SEK · captured ${captureText(x.capture_fraction)} · remaining ${n(x.remaining_gap_sek)} SEK`;if(hGap>0)svg+=`<rect x="${cx-bw/2}" y="${base-hCap-hGap}" width="${bw}" height="${hGap}" fill="#ffbf5a"><title>${title}</title></rect>`;if(hCap>0)svg+=`<rect x="${cx-bw/2}" y="${base-hCap}" width="${bw}" height="${hCap}" fill="#51d88a"><title>${title}</title></rect>`;if(saving<0){const hn=Math.min(usable*.3,usable*(-saving)/max);svg+=`<rect x="${cx-bw/2}" y="${base}" width="${bw}" height="${hn}" fill="#ff6b6b"><title>${title}</title></rect>`}svg+=`<text x="${cx}" y="${H-20}" fill="#91a2b3" font-size="9" text-anchor="middle">${String(x.local_date).slice(5)}</text>`});svg+=`<line x1="${p.l}" y1="${base}" x2="${W-p.r}" y2="${base}" stroke="#52687c"/></svg>`;el.innerHTML=svg;
}
const renderEvalBase=renderEval;
renderEval=function(){renderEvalBase();renderEvaluationSummary();drawEvaluationCurrent()};
const renderHistoryBase=renderHistory;
renderHistory=function(){
  const h=state.history||{},days=h.days||[],s=h.evaluation_summary||{};
  $('historyKpis').innerHTML=card('Complete days',n(h.complete_days,0),`${n(h.partial_days,0)} partial`)+card('Total saving',sek(s.total_saving_sek),'Complete days only',s.total_saving_sek>0?'good':'')+card('Available opportunity',sek(s.total_opportunity_sek),'Saving + remaining gap')+card('Opportunity captured',captureText(s.capture_fraction),'Aggregate complete days',s.capture_fraction>=.7?'good':s.capture_fraction!=null&&s.capture_fraction<.4?'warn':'')+card('Remaining gap',sek(s.total_remaining_gap_sek),'Gap to hindsight')+card('Data quality',`${n(h.complete_days,0)}/${n(h.stored_days,0)}`,'Complete / stored days');
  opportunityBars($('opportunityChart'),days);
  $('historyTable').innerHTML=days.length?`<table class="tbl"><thead><tr><th>Date</th><th>Quality</th><th>Saving SEK</th><th>Opportunity SEK</th><th>Captured</th><th>Forecast gap</th><th>Unpublished price</th><th>Planner / policy</th><th>Load MAE kW</th><th>PV MAE kW</th></tr></thead><tbody>${days.map(x=>`<tr><td>${x.local_date}</td><td><span class="pill ${x.status==='ok'?'ok':'partial'}">${x.status==='ok'?'complete':`${x.status} · ${pct(x.coverage)}`}</span></td><td>${n(x.saving_sek)}</td><td>${n(x.opportunity_sek)}</td><td>${captureText(x.capture_fraction)}</td><td>${n(x.forecast_gap_sek)}</td><td title="Prices not yet published at decision time; published prices themselves do not change.">${n(x.unpublished_price_horizon_sek)}</td><td>${n(x.planner_policy_gap_sek)}</td><td>${n(x.load_mae_kw)}</td><td>${n(x.pv_mae_kw)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No evaluated days stored yet.</div>';
};
loadEval=async function(localDate){try{state.eval=await api(`ui/evaluation-day?local_date=${localDate}`);renderEval()}catch(e){$('evalMeta').textContent=e.message}};
loadHistory=async function(){try{state.history=await api(`ui/evaluation-history?days=${$('historyDays').value}`);renderHistory()}catch(e){$('historyTable').innerHTML=`<div class="empty">${e.message}</div>`}};
const evalNote=document.querySelector('#evaluation .chart-note');if(evalNote)evalNote.textContent='Solid = realized · dashed = forecast/plan · hindsight = perfect-information benchmark at the same terminal SOC. Hover for exact values.';
evalPickerCurrent();
</script>
'''


def _config_fingerprint(cfg: dict[str, Any]) -> str:
    payload = {"policy": cfg.get("policy") or {}, "optimizer": cfg.get("optimizer") or {}, "tariffs": cfg.get("tariffs") or {}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:20]


def _init_decomposition_cache() -> None:
    with connect_db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS optimizer_regret_ui_cache(
                local_date TEXT NOT NULL,
                source_created_at TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                engine TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(local_date, source_created_at, config_fingerprint, engine)
            )"""
        )


def _source_created_at(local_date: str) -> str | None:
    try:
        with connect_db() as c:
            row = c.execute("SELECT created_at FROM optimizer_day_eval WHERE local_date=?", (local_date,)).fetchone()
        return str(row[0]) if row else None
    except Exception:
        return None


def _regret_ui(raw: dict[str, Any]) -> dict[str, Any]:
    valid = bool(raw.get("valid_decomposition")) and raw.get("status") == "valid"
    d = raw.get("decomposition") or {}
    return {
        "status": raw.get("status") or "unavailable",
        "valid": valid,
        "forecast_gap_sek": d.get("forecast_regret_sek") if valid else None,
        "unpublished_price_horizon_sek": d.get("price_information_regret_sek") if valid else None,
        "planner_policy_gap_sek": d.get("planner_horizon_policy_residual_sek") if valid else None,
        "total_gap_sek": d.get("realtime_to_hindsight_total_gap_sek") if valid else None,
        "definition": {
            "forecast_gap": "perfect realized PV/load with the same historical price information",
            "unpublished_price_horizon": "value of prices beyond what had been published at decision time",
            "planner_policy_gap": "residual between perfect-information rolling v3.5 and full hindsight",
        },
    }


def _cached_regret(cfg: dict[str, Any], local_date: str) -> dict[str, Any]:
    source_created = _source_created_at(local_date)
    if not source_created:
        return {"status": "missing_day_evaluation", "valid": False}
    fingerprint = _config_fingerprint(cfg)
    _init_decomposition_cache()
    with _DECOMP_LOCK:
        with connect_db() as c:
            row = c.execute(
                "SELECT payload_json FROM optimizer_regret_ui_cache WHERE local_date=? AND source_created_at=? AND config_fingerprint=? AND engine=?",
                (local_date, source_created, fingerprint, REGRET_ENGINE_NAME),
            ).fetchone()
        if row:
            try:
                return _regret_ui(json.loads(row[0]))
            except Exception:
                pass
        start, end = _day_bounds(date.fromisoformat(local_date))
        try:
            raw = regret_decomposition(cfg, start=start.isoformat(), end=end.isoformat())
        except Exception as exc:
            return {"status": "decomposition_failed", "valid": False, "error": repr(exc)}
        if raw.get("valid_decomposition") and raw.get("status") == "valid":
            with connect_db() as c:
                c.execute(
                    "INSERT OR REPLACE INTO optimizer_regret_ui_cache(local_date,source_created_at,config_fingerprint,engine,payload_json) VALUES (?,?,?,?,?)",
                    (local_date, source_created, fingerprint, REGRET_ENGINE_NAME, json.dumps(raw, ensure_ascii=False)),
                )
        return _regret_ui(raw)


def _summary_from_evaluation(result: dict[str, Any], regret: dict[str, Any]) -> dict[str, Any]:
    comparison = result.get("comparison") or {}
    data = result.get("data") or {}
    saving = comparison.get("realtime_economic_saving_vs_zero_battery_sek")
    gap = regret.get("total_gap_sek") if regret.get("valid") else comparison.get("perfect_information_gap_sek")
    opportunity = float(saving) + float(gap) if saving is not None and gap is not None else None
    capture = float(saving) / opportunity if opportunity is not None and opportunity > _OPPORTUNITY_EPS_SEK else None
    coverage = data.get("plan_action_coverage_fraction")
    return {
        "saving_sek": saving,
        "remaining_gap_sek": gap,
        "opportunity_sek": round(opportunity, 2) if opportunity is not None else None,
        "capture_fraction": round(capture, 4) if capture is not None else None,
        "comparable": result.get("status") == "ok" and coverage is not None and float(coverage) >= 0.90,
    }


def _decorate_history_day(cfg: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    regret = _cached_regret(cfg, str(out.get("local_date"))) if out.get("status") == "ok" else {"status": "day_not_complete", "valid": False}
    saving = out.get("saving_sek")
    remaining = regret.get("total_gap_sek") if regret.get("valid") else out.get("perfect_information_gap_sek")
    opportunity = float(saving) + float(remaining) if saving is not None and remaining is not None else None
    capture = float(saving) / opportunity if opportunity is not None and opportunity > _OPPORTUNITY_EPS_SEK else None
    out.update(
        {
            "remaining_gap_sek": remaining,
            "opportunity_sek": round(opportunity, 2) if opportunity is not None else None,
            "capture_fraction": round(capture, 4) if capture is not None else None,
            "forecast_gap_sek": regret.get("forecast_gap_sek"),
            "unpublished_price_horizon_sek": regret.get("unpublished_price_horizon_sek"),
            "planner_policy_gap_sek": regret.get("planner_policy_gap_sek"),
            "decomposition_status": regret.get("status"),
        }
    )
    return out


def _enriched_history(cfg: dict[str, Any], days: int) -> dict[str, Any]:
    base = _history(days)
    decorated = [_decorate_history_day(cfg, item) for item in (base.get("days") or [])]
    good = [x for x in decorated if x.get("status") == "ok" and x.get("opportunity_sek") is not None]
    total_saving = sum(float(x.get("saving_sek") or 0.0) for x in good)
    total_gap = sum(float(x.get("remaining_gap_sek") or 0.0) for x in good)
    total_opportunity = sum(float(x.get("opportunity_sek") or 0.0) for x in good)
    capture = total_saving / total_opportunity if total_opportunity > _OPPORTUNITY_EPS_SEK else None
    return {
        **base,
        "days": decorated,
        "evaluation_summary": {
            "total_saving_sek": round(total_saving, 2) if good else None,
            "total_remaining_gap_sek": round(total_gap, 2) if good else None,
            "total_opportunity_sek": round(total_opportunity, 2) if good else None,
            "capture_fraction": round(capture, 4) if capture is not None else None,
            "included_complete_days": len(good),
        },
    }


def _enriched_evaluation(cfg: dict[str, Any], local_date: str) -> dict[str, Any]:
    result = evaluate_day(cfg, local_date)
    if result.get("rows"):
        decisions = _plan_actions(date.fromisoformat(local_date))
        for row in result.get("rows") or []:
            try:
                stamp = _dt(row["start"]).replace(second=0, microsecond=0)
            except Exception:
                continue
            decision = decisions.get(stamp)
            if decision is None:
                row.update({"forecast_load_kw": None, "forecast_pv_kw": None, "forecast_soc_end_pct": None, "forecast_price_ore_kwh": None, "plan_reason": None})
                continue
            row.update({
                "forecast_load_kw": decision.get("forecast_load_kw"),
                "forecast_pv_kw": decision.get("forecast_pv_kw"),
                "forecast_soc_end_pct": decision.get("forecast_soc_end_pct"),
                "forecast_price_ore_kwh": decision.get("forecast_price_ore_kwh"),
                "plan_reason": decision.get("reason"),
            })
    regret = _cached_regret(cfg, local_date) if result.get("status") == "ok" else {"status": "day_not_complete", "valid": False}
    result["regret_decomposition_ui"] = regret
    result["evaluation_summary"] = _summary_from_evaluation(result, regret)
    return result


def install_evaluation_routes(app: FastAPI, cfg: dict[str, Any]) -> None:
    @app.get("/ui/evaluation-day", include_in_schema=False)
    async def ui_evaluation_day(local_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
        return JSONResponse(await asyncio.to_thread(_enriched_evaluation, cfg, local_date))

    @app.get("/ui/evaluation-history", include_in_schema=False)
    async def ui_evaluation_history(days: int = Query(30, ge=1, le=180)):
        return JSONResponse(await asyncio.to_thread(_enriched_history, cfg, days))
