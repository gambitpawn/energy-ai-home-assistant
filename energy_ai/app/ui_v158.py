from __future__ import annotations

from datetime import date

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import DASHBOARD_HTML
from .optimizer_evaluation import _dt, _plan_actions, evaluate_day
from .overview_extension import OVERVIEW_EXTENSION


V158_EXTENSION = r'''
<style>
#overviewPlan{border-radius:10px;background-repeat:no-repeat}
</style>
<script>
// 1.0.58: stronger actual/forward distinction on Overview, without changing the chart layout.
const drawOverview157=drawOverview;
drawOverview=function(){
  drawOverview157();
  const el=$('overviewPlan'),actual=overviewRealized.rows||[],planned=pRows();
  const stamps=[...actual.map(r=>Date.parse(r.start)),...planned.map(r=>Date.parse(r.start||r.start_utc))].filter(Number.isFinite);
  const now=Date.parse(overviewRealized.now||new Date().toISOString());
  if(stamps.length&&Number.isFinite(now)){
    const lo=Math.min(...stamps),hi=Math.max(...stamps),pctNow=Math.max(0,Math.min(100,100*(now-lo)/Math.max(1,hi-lo)));
    el.style.background=`linear-gradient(to right, transparent 0%, transparent ${pctNow}%, rgba(79,179,255,.045) ${pctNow}%, rgba(79,179,255,.045) 100%)`;
  }
};

// Evaluation 2.0 series. Actual = solid, forecast/plan = dashed, hindsight = dotted.
C.hindsight='#e6d36f';
Object.assign(pick.eval,{
  forecastLoad:false,forecastPv:false,hindsightBattery:false,plannedSoc:false,hindsightSoc:false
});

function evalPicker158(){
  const defs=[
    ['load','Actual load',C.load,'solid'],['forecastLoad','Forecast load',C.load,'dashed'],
    ['pv','Actual PV',C.pv,'solid'],['forecastPv','Forecast PV',C.pv,'dashed'],
    ['battery','Applied battery',C.battery,'solid'],['plannedBattery','Planned battery',C.battery,'dashed'],
    ['hindsightBattery','Hindsight battery',C.hindsight,'dotted'],
    ['soc','Virtual SOC',C.soc,'solid'],['plannedSoc','Planned SOC',C.soc,'dashed'],
    ['hindsightSoc','Hindsight SOC',C.hindsight,'dotted'],
    ['price','Spot price',C.price,'solid'],['gridImport','Grid import',C.gridImport,'solid'],['gridExport','Grid export',C.gridExport,'solid']
  ];
  const el=$('evalPicker');
  el.innerHTML=defs.map(d=>{
    const sw=d[3]==='dashed'?'repeating-linear-gradient(to right,currentColor 0 5px,transparent 5px 8px)':d[3]==='dotted'?'repeating-linear-gradient(to right,currentColor 0 2px,transparent 2px 5px)':'currentColor';
    return `<label><input type="checkbox" data-k="${d[0]}" ${pick.eval[d[0]]?'checked':''}><span class="swatch" style="color:${d[2]};background:${sw}"></span>${d[1]}</label>`;
  }).join('');
  el.onchange=e=>{const k=e.target?.dataset?.k;if(!k)return;pick.eval[k]=e.target.checked;drawEval158()};
}

function drawEval158(){
  const e=state.eval||{},rr=e.rows||[],ps=pick.eval,ha=e.perfect_hindsight?.actions||[];
  const hm=new Map(ha.map(x=>[Date.parse(x.start),x]));
  const hAction=rr.map(r=>hm.get(Date.parse(r.start))?.action_kw??null);
  const hSoc=rr.map(r=>hm.get(Date.parse(r.start))?.soc_end_pct??null);
  lineChart($('evalChart'),[
    {name:'Actual load',axis:'power',color:C.load,values:rr.map(r=>r.actual_load_kw),on:ps.load},
    {name:'Forecast load',axis:'power',color:C.load,values:rr.map(r=>r.forecast_load_kw),on:ps.forecastLoad,dashed:true},
    {name:'Actual PV',axis:'power',color:C.pv,values:rr.map(r=>r.actual_pv_kw),on:ps.pv},
    {name:'Forecast PV',axis:'power',color:C.pv,values:rr.map(r=>r.forecast_pv_kw),on:ps.forecastPv,dashed:true},
    {name:'Applied battery',axis:'power',color:C.battery,values:rr.map(r=>r.applied_action_kw),on:ps.battery},
    {name:'Planned battery',axis:'power',color:C.battery,values:rr.map(r=>r.requested_action_kw),on:ps.plannedBattery,dashed:true},
    {name:'Hindsight battery',axis:'power',color:C.hindsight,values:hAction,on:ps.hindsightBattery,dashed:true,width:1.8},
    {name:'Spot price',axis:'price',color:C.price,values:rr.map(r=>r.price_ore_kwh),on:ps.price},
    {name:'Virtual SOC',axis:'soc',color:C.soc,values:rr.map(r=>r.virtual_soc_end_pct),on:ps.soc},
    {name:'Planned SOC',axis:'soc',color:C.soc,values:rr.map(r=>r.forecast_soc_end_pct),on:ps.plannedSoc,dashed:true},
    {name:'Hindsight SOC',axis:'soc',color:C.hindsight,values:hSoc,on:ps.hindsightSoc,dashed:true,width:1.8},
    {name:'Grid import',axis:'power',color:C.gridImport,values:rr.map(r=>r.grid_import_kw),on:ps.gridImport},
    {name:'Grid export',axis:'power',color:C.gridExport,values:rr.map(r=>r.grid_export_kw==null?null:-Number(r.grid_export_kw)),on:ps.gridExport}
  ],rr.map(r=>r.start));
}

// Preserve KPI/diagnostics rendering, then replace only the chart drawing.
const renderEval157=renderEval;
renderEval=function(){renderEval157();drawEval158()};

loadEval=async function(localDate){
  try{state.eval=await api(`ui/evaluation-day?local_date=${localDate}`);renderEval()}
  catch(e){$('evalMeta').textContent=e.message}
};

const evalNote=document.querySelector('#evaluation .chart-note');
if(evalNote)evalNote.textContent='Solid = realized · dashed = forecast/plan · hindsight = perfect-information benchmark at the same terminal SOC. Hover for exact values.';
evalPicker158();
</script>
'''


def _enriched_evaluation(cfg: dict, local_date: str) -> dict:
    result = evaluate_day(cfg, local_date)
    if not result.get("rows"):
        return result
    day = date.fromisoformat(local_date)
    decisions = _plan_actions(day)
    for row in result.get("rows") or []:
        try:
            stamp = _dt(row["start"]).replace(second=0, microsecond=0)
        except Exception:
            continue
        decision = decisions.get(stamp)
        if decision is None:
            row.update({
                "forecast_load_kw": None,
                "forecast_pv_kw": None,
                "forecast_soc_end_pct": None,
                "forecast_price_ore_kwh": None,
                "plan_reason": None,
            })
            continue
        row.update({
            "forecast_load_kw": decision.get("forecast_load_kw"),
            "forecast_pv_kw": decision.get("forecast_pv_kw"),
            "forecast_soc_end_pct": decision.get("forecast_soc_end_pct"),
            "forecast_price_ore_kwh": decision.get("forecast_price_ore_kwh"),
            "plan_reason": decision.get("reason"),
        })
    return result


def install_ui_v158(app: FastAPI, cfg: dict) -> None:
    @app.middleware("http")
    async def ui_v158(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace("</body>", OVERVIEW_EXTENSION + V158_EXTENSION + "</body>")
            return HTMLResponse(html)
        return await call_next(request)

    @app.get("/ui/evaluation-day", include_in_schema=False)
    async def ui_evaluation_day(local_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
        return JSONResponse(_enriched_evaluation(cfg, local_date))
