from __future__ import annotations

from datetime import date

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .optimizer_evaluation import _dt, _plan_actions, evaluate_day


EVALUATION_EXTENSION = r'''
<style>
#overviewPlan{border-radius:10px;background-repeat:no-repeat}
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
const renderEvalBase=renderEval;
renderEval=function(){renderEvalBase();drawEvaluationCurrent()};
loadEval=async function(localDate){try{state.eval=await api(`ui/evaluation-day?local_date=${localDate}`);renderEval()}catch(e){$('evalMeta').textContent=e.message}};
const evalNote=document.querySelector('#evaluation .chart-note');if(evalNote)evalNote.textContent='Solid = realized · dashed = forecast/plan · hindsight = perfect-information benchmark at the same terminal SOC. Hover for exact values.';
evalPickerCurrent();
</script>
'''


def _enriched_evaluation(cfg: dict, local_date: str) -> dict:
    result = evaluate_day(cfg, local_date)
    if not result.get("rows"):
        return result
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
    return result


def install_evaluation_routes(app: FastAPI, cfg: dict) -> None:
    @app.get("/ui/evaluation-day", include_in_schema=False)
    async def ui_evaluation_day(local_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
        return JSONResponse(_enriched_evaluation(cfg, local_date))
