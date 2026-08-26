from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import DASHBOARD_HTML
from .db import DB_PATH


OVERVIEW_EXTENSION = r'''
<style>
.chart{cursor:crosshair}.chart-tooltip{position:absolute;z-index:20;pointer-events:none;min-width:180px;max-width:270px;padding:9px 11px;border:1px solid #3a5064;border-radius:9px;background:rgba(11,17,24,.96);box-shadow:0 8px 24px rgba(0,0,0,.35);font-size:11px;color:#eef4f8;display:none}.chart-tooltip .tt-time{font-weight:750;margin-bottom:5px}.chart-tooltip .tt-row{display:flex;justify-content:space-between;gap:14px;padding:2px 0}.chart-tooltip .tt-name{display:flex;align-items:center;gap:6px;color:#b8c6d2}.chart-tooltip .tt-dot{width:7px;height:7px;border-radius:50%;display:inline-block}.chart-tooltip .tt-value{font-variant-numeric:tabular-nums;font-weight:650}.chart-tooltip .tt-kind{color:#91a2b3;font-size:10px;margin-left:4px}.now-badge{font-size:10px}
</style>
<script>
let overviewRealized={rows:[],now:null};

function overviewSeriesPicker(){
  const defs=[['load','Load',C.load],['pv','PV',C.pv],['battery','Battery action',C.battery],['price','Spot price',C.price],['soc','SOC',C.soc]];
  const el=$('overviewPicker');
  el.innerHTML=defs.map(d=>`<label><input type="checkbox" data-k="${d[0]}" ${pick.overview[d[0]]?'checked':''}><span class="swatch solid" style="color:${d[2]};background:${d[2]}"></span>${d[1]}</label>`).join('');
  el.onchange=e=>{const k=e.target?.dataset?.k;if(!k)return;pick.overview[k]=e.target.checked;drawOverview()};
}

function unitFor(axis){return axis==='power'?'kW':axis==='price'?'öre/kWh':'%'}
function fmtChart(v,axis){return `${n(v,axis==='soc'?1:2)} ${unitFor(axis)}`}
function timeMs(v){const x=Date.parse(v||'');return Number.isFinite(x)?x:null}

function interactiveTimeChart(el,series,times,opts={}){
  const active=series.filter(s=>s.on&&s.values.some(v=>v!=null&&isFinite(v)));
  if(!active.length){el.innerHTML='<div class="empty">No selected data available.</div>';return}
  const parsed=(times||[]).map(timeMs),good=parsed.filter(v=>v!=null);
  if(!good.length){el.innerHTML='<div class="empty">No timestamped data available.</div>';return}
  const W=1000,H=320,p={l:58,r:92,t:20,b:48},t0=Math.min(...good),t1=Math.max(...good),tr=Math.max(1,t1-t0),axes={};
  for(const k of ['power','price','soc']){
    const vals=active.filter(s=>s.axis===k).flatMap(s=>s.values).filter(v=>v!=null&&isFinite(v)).map(Number);
    if(!vals.length)continue;
    if(k==='soc')axes[k]={min:0,max:100};
    else{let mn=Math.min(...vals),mx=Math.max(...vals);mn=Math.min(0,mn);if(mx===mn)mx=mn+1;const pad=(mx-mn)*.08;axes[k]={min:mn-pad,max:mx+pad}}
  }
  const xms=ms=>p.l+(W-p.l-p.r)*(ms-t0)/tr,y=(v,k)=>{const q=axes[k];return p.t+(H-p.t-p.b)*(1-(Number(v)-q.min)/(q.max-q.min||1))};
  let svg=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-chart-svg="1">`;
  const primary=axes.power?'power':axes.price?'price':'soc',pr=axes[primary];
  for(let j=0;j<5;j++){const yy=p.t+(H-p.t-p.b)*j/4,vv=pr.max-(pr.max-pr.min)*j/4;svg+=`<line x1="${p.l}" y1="${yy}" x2="${W-p.r}" y2="${yy}" stroke="#263647"/><text x="5" y="${yy+4}" fill="#91a2b3" font-size="11">${n(vv,1)}</text>`}
  svg+=`<text x="5" y="12" fill="#91a2b3" font-size="10">${unitFor(primary)}</text>`;
  let off=0;for(const k of ['price','soc']){if(!axes[k]||k===primary)continue;const q=axes[k],col=k==='price'?C.price:C.soc;for(let j=0;j<5;j++){const yy=p.t+(H-p.t-p.b)*j/4,vv=q.max-(q.max-q.min)*j/4;svg+=`<text x="${W-p.r+8+off}" y="${yy+4}" fill="${col}" font-size="10">${n(vv,k==='soc'?0:1)}</text>`}svg+=`<text x="${W-p.r+8+off}" y="12" fill="${col}" font-size="9">${unitFor(k)}</text>`;off+=40}
  const tickCount=9;for(let j=0;j<tickCount;j++){const ms=t0+tr*j/(tickCount-1),xx=xms(ms);svg+=`<line x1="${xx}" y1="${H-p.b}" x2="${xx}" y2="${H-p.b+5}" stroke="#52687c"/><text x="${xx}" y="${H-12}" fill="#91a2b3" font-size="10" text-anchor="middle">${tlabel(new Date(ms).toISOString(),tr>30*3600000&&j===0)}</text>`}
  const nowMs=timeMs(opts.now);if(nowMs!=null&&nowMs>=t0&&nowMs<=t1){const nx=xms(nowMs);svg+=`<line x1="${nx}" y1="${p.t}" x2="${nx}" y2="${H-p.b}" stroke="#eef4f8" stroke-width="1.2" stroke-dasharray="3 4" opacity=".75" vector-effect="non-scaling-stroke"/><text class="now-badge" x="${nx+5}" y="${p.t+12}" fill="#eef4f8">Now</text>`}
  for(const s of active){let d='',started=false;s.values.forEach((v,i)=>{const ms=parsed[i];if(v==null||!isFinite(v)||ms==null){started=false;return}d+=(started?'L':'M')+xms(ms)+','+y(v,s.axis);started=true});svg+=`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width||2.2}" ${s.dashed?'stroke-dasharray="7 5"':''} vector-effect="non-scaling-stroke"/>`}
  svg+=`<line id="crosshair-${el.id}" x1="0" y1="${p.t}" x2="0" y2="${H-p.b}" stroke="#d7e3ec" stroke-width="1" opacity="0" vector-effect="non-scaling-stroke"/></svg><div class="chart-tooltip"></div>`;el.innerHTML=svg;
  const svgEl=el.querySelector('svg'),tip=el.querySelector('.chart-tooltip'),cross=el.querySelector(`#crosshair-${el.id}`);
  svgEl.onmousemove=ev=>{
    const rect=svgEl.getBoundingClientRect(),rel=(ev.clientX-rect.left)/Math.max(1,rect.width),vx=rel*W,plotX=Math.max(p.l,Math.min(W-p.r,vx)),ms=t0+(plotX-p.l)/(W-p.l-p.r)*tr;
    let idx=-1,dist=Infinity;parsed.forEach((tm,i)=>{if(tm==null)return;const z=Math.abs(tm-ms);if(z<dist){dist=z;idx=i}});if(idx<0)return;
    const xx=xms(parsed[idx]);cross.setAttribute('x1',xx);cross.setAttribute('x2',xx);cross.setAttribute('opacity','.75');
    const rows=active.filter(s=>s.values[idx]!=null&&isFinite(s.values[idx])).map(s=>`<div class="tt-row"><span class="tt-name"><span class="tt-dot" style="background:${s.color}"></span>${s.label||'Series'}${s.kind?`<span class="tt-kind">${s.kind}</span>`:''}</span><span class="tt-value">${fmtChart(s.values[idx],s.axis)}</span></div>`).join('');
    tip.innerHTML=`<div class="tt-time">${tlabel(times[idx],true)}</div>${rows}`;tip.style.display='block';const localX=ev.clientX-el.getBoundingClientRect().left,localY=ev.clientY-el.getBoundingClientRect().top;tip.style.left=`${Math.min(el.clientWidth-280,Math.max(8,localX+14))}px`;tip.style.top=`${Math.max(8,localY-20)}px`;
  };
  svgEl.onmouseleave=()=>{cross.setAttribute('opacity','0');tip.style.display='none'};
}

// Replace the base chart renderer so Plan and Evaluation also get crosshair/tooltips.
lineChart=function(el,series,times){interactiveTimeChart(el,series.map((s,i)=>({...s,label:s.label||`Series ${i+1}`})),times)};

// Attach semantic labels to the forward plan data used by Plan.
planData=function(which){
  const rs=pRows(),ps=pick[which];return {times:rs.map(r=>r.start||r.start_utc),series:[
    {label:'Load forecast',kind:'plan',axis:'power',color:C.load,values:rs.map(r=>r.load_kw??r.forecast_load_kw),on:ps.load,dashed:true},
    {label:'PV forecast',kind:'plan',axis:'power',color:C.pv,values:rs.map(r=>r.pv_kw??r.forecast_pv_kw),on:ps.pv,dashed:true},
    {label:'Battery action',kind:'plan',axis:'power',color:C.battery,values:rs.map(r=>r.battery_action_kw??r.action_kw),on:ps.battery,dashed:true},
    {label:'Spot price',kind:'forward',axis:'price',color:C.price,values:rs.map(r=>r.price_ore_kwh??r.forecast_price_ore_kwh),on:ps.price,dashed:true},
    {label:'SOC',kind:'plan',axis:'soc',color:C.soc,values:rs.map(r=>r.expected_soc_pct??r.soc_end_pct),on:ps.soc,dashed:true}
  ]}
};

function planTotals(){
  const rs=pRows(),dt=.25;let charge=0,discharge=0,imp=0,exp=0;
  for(const r of rs){const a=Number(r.battery_action_kw??r.action_kw);if(Number.isFinite(a)){if(a<0)charge+=-a*dt;else discharge+=a*dt}const gi=Number(r.grid_import_kw),ge=Number(r.grid_export_kw);if(Number.isFinite(gi))imp+=Math.max(0,gi)*dt;if(Number.isFinite(ge))exp+=Math.max(0,ge)*dt}
  return {charge,discharge,imp,exp};
}

function renderOverviewKpis(){
  const p=state.plan||{},s=p.summary||{},z=planTotals();
  $('overviewKpis').innerHTML=card('Initial SOC',p.initial_soc_pct!=null?`${n(p.initial_soc_pct,1)}%`:s.initial_soc_pct!=null?`${n(s.initial_soc_pct,1)}%`:'—','Plan start')+card('Planned charge',`${n(z.charge,2)} kWh`,'Current horizon')+card('Planned discharge',`${n(z.discharge,2)} kWh`,'Current horizon')+card('Import',`${n(z.imp,2)} kWh`,'Planned')+card('Export',`${n(z.exp,2)} kWh`,'Planned')+card('Planner',p.planner||'—',p.mode||'shadow','planner');
}

const baseRenderPlan=renderPlan;
renderPlan=function(){baseRenderPlan();renderOverviewKpis();const d=planData('plan');interactiveTimeChart($('planChart'),d.series,d.times);drawOverview()};

function latestActual(){const a=overviewRealized.rows||[];return a.length?a[a.length-1]:null}
renderHealth=function(){
  const h=state.health||{},x=latestActual();
  $('systemState').innerHTML=rows({'Runtime':h.runtime_build||state.config?.runtime_build||'—','Collector':h.collector_running===false?'Stopped':'Running','Last error':h.last_error||'None','PV':x?.pv_kw!=null?`${n(x.pv_kw)} kW`:'—','House load':x?.load_kw!=null?`${n(x.load_kw)} kW`:'—','Grid':x?.grid_kw!=null?`${n(x.grid_kw)} kW`:'—','Battery':x?.battery_kw!=null?`${n(x.battery_kw)} kW`:'—','SOC':x?.soc_pct!=null?`${n(x.soc_pct,1)}%`:'—'});
};

function drawOverview(){
  const actual=overviewRealized.rows||[],nowMs=timeMs(overviewRealized.now)||Date.now(),planned=pRows().filter(r=>{const ms=timeMs(r.start||r.start_utc);return ms!=null&&ms>=nowMs-15*60*1000});
  const points=[];actual.forEach(r=>points.push({kind:'actual',start:r.start,...r}));planned.forEach(r=>points.push({kind:'plan',start:r.start||r.start_utc,...r}));points.sort((a,b)=>Date.parse(a.start)-Date.parse(b.start)||(a.kind==='actual'?-1:1));
  const times=points.map(r=>r.start),ps=pick.overview,vals=(kind,fn)=>points.map(r=>r.kind===kind?fn(r):null);
  const series=[
    {label:'Load',kind:'actual',axis:'power',color:C.load,values:vals('actual',r=>r.load_kw),on:ps.load},{label:'Load',kind:'plan',axis:'power',color:C.load,values:vals('plan',r=>r.load_kw??r.forecast_load_kw),on:ps.load,dashed:true},
    {label:'PV',kind:'actual',axis:'power',color:C.pv,values:vals('actual',r=>r.pv_kw),on:ps.pv},{label:'PV',kind:'plan',axis:'power',color:C.pv,values:vals('plan',r=>r.pv_kw??r.forecast_pv_kw),on:ps.pv,dashed:true},
    {label:'Battery',kind:'actual',axis:'power',color:C.battery,values:vals('actual',r=>r.battery_kw),on:ps.battery},{label:'Battery',kind:'plan',axis:'power',color:C.battery,values:vals('plan',r=>r.battery_action_kw??r.action_kw),on:ps.battery,dashed:true},
    {label:'Spot price',kind:'actual',axis:'price',color:C.price,values:vals('actual',r=>r.price_ore_kwh),on:ps.price},{label:'Spot price',kind:'forward',axis:'price',color:C.price,values:vals('plan',r=>r.price_ore_kwh??r.forecast_price_ore_kwh),on:ps.price,dashed:true},
    {label:'SOC',kind:'actual',axis:'soc',color:C.soc,values:vals('actual',r=>r.soc_pct),on:ps.soc},{label:'SOC',kind:'plan',axis:'soc',color:C.soc,values:vals('plan',r=>r.expected_soc_pct??r.soc_end_pct),on:ps.soc,dashed:true}
  ];interactiveTimeChart($('overviewPlan'),series,times,{now:overviewRealized.now});
}

// Rebuild Evaluation with named series for tooltips.
drawEval=function(){
  const rr=state.eval?.rows||[],ps=pick.eval;interactiveTimeChart($('evalChart'),[
    {label:'Load',kind:'actual',axis:'power',color:C.load,values:rr.map(r=>r.actual_load_kw),on:ps.load},
    {label:'PV',kind:'actual',axis:'power',color:C.pv,values:rr.map(r=>r.actual_pv_kw),on:ps.pv},
    {label:'Battery',kind:'applied',axis:'power',color:C.battery,values:rr.map(r=>r.applied_action_kw),on:ps.battery},
    {label:'Battery',kind:'plan',axis:'power',color:C.battery,values:rr.map(r=>r.requested_action_kw),on:ps.plannedBattery,dashed:true},
    {label:'Spot price',kind:'actual',axis:'price',color:C.price,values:rr.map(r=>r.price_ore_kwh),on:ps.price},
    {label:'SOC',kind:'replay',axis:'soc',color:C.soc,values:rr.map(r=>r.virtual_soc_end_pct),on:ps.soc},
    {label:'Grid import',kind:'actual',axis:'power',color:C.gridImport,values:rr.map(r=>r.grid_import_kw),on:ps.gridImport},
    {label:'Grid export',kind:'actual',axis:'power',color:C.gridExport,values:rr.map(r=>r.grid_export_kw==null?null:-Number(r.grid_export_kw)),on:ps.gridExport}
  ],rr.map(r=>r.start));
};

async function loadOverviewHistory(){try{overviewRealized=await api('ui/overview-history?hours=24');drawOverview();renderHealth()}catch(e){console.warn('Overview history unavailable',e);drawOverview()}}

const overviewTitle=document.querySelector('#overview .card h2');if(overviewTitle)overviewTitle.textContent='Realized → optimizer plan';
const overviewNote=document.querySelector('#overview .chart-note');if(overviewNote)overviewNote.textContent='Solid = last 24 h realized · dashed = current forecast/plan · vertical line = now. Hover for exact values. Time is Europe/Stockholm.';
const planNote=document.querySelector('#plan .chart-note');if(planNote)planNote.textContent='Dashed = forecast/planned values. Hover for exact values. Time is Europe/Stockholm.';
const evalNote=document.querySelector('#evaluation .chart-note');if(evalNote)evalNote.textContent='Solid = realized/applied · dashed = planned. Hover for exact values. Time is Europe/Stockholm.';
overviewSeriesPicker();

$('planPicker').onchange=e=>{const k=e.target?.dataset?.k;if(!k)return;pick.plan[k]=e.target.checked;const d=planData('plan');interactiveTimeChart($('planChart'),d.series,d.times)};
$('refreshPlan').onclick=async()=>{try{await api('optimizer/refresh');await loadPlan();await loadOverviewHistory()}catch(e){$('planMeta').textContent=e.message}};
$('tabs').addEventListener('click',e=>{const b=e.target.closest('.tab');if(b?.dataset?.view==='overview')loadOverviewHistory()});
loadOverviewHistory();
</script>
'''


def _history_rows(hours: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(1, min(72, int(hours))))
    with sqlite3.connect(DB_PATH) as c:
        raw = c.execute(
            "SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? AND bucket_start<=? ORDER BY bucket_start",
            (cutoff.isoformat(), now.isoformat()),
        ).fetchall()
    rows: list[dict[str, Any]] = []
    for bucket_start, payload_raw in raw:
        try:
            payload = json.loads(payload_raw)
        except Exception:
            continue
        means = payload.get("mean") or {}
        rows.append({
            "start": bucket_start,
            "load_kw": means.get("house_load_kw"),
            "pv_kw": means.get("pv_power_kw"),
            "battery_kw": means.get("battery_power_kw"),
            "grid_kw": means.get("grid_power_kw"),
            "price_ore_kwh": means.get("spot_price_ore_kwh"),
            "soc_pct": payload.get("battery_soc_end_pct"),
            "completeness": payload.get("completeness"),
        })
    return {"hours": hours, "now": now.isoformat(), "rows": rows}


def install_overview_extension(app: FastAPI) -> None:
    @app.middleware("http")
    async def extended_ui(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace("</body>", OVERVIEW_EXTENSION + "</body>")
            return HTMLResponse(html)
        return await call_next(request)

    @app.get("/ui/overview-history", include_in_schema=False)
    async def overview_history(hours: int = Query(24, ge=1, le=72)):
        try:
            return JSONResponse(_history_rows(hours))
        except sqlite3.OperationalError:
            return JSONResponse({"hours": hours, "now": datetime.now(timezone.utc).isoformat(), "rows": []})
