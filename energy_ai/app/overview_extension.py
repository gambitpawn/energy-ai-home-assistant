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
<script>
let overviewRealized={rows:[],now:null};

function overviewSeriesPicker(){
  const defs=[
    ['load','Load',C.load],['pv','PV',C.pv],['battery','Battery action',C.battery],
    ['price','Spot price',C.price],['soc','SOC',C.soc]
  ];
  const el=$('overviewPicker');
  el.innerHTML=defs.map(d=>`<label><input type="checkbox" data-k="${d[0]}" ${pick.overview[d[0]]?'checked':''}><span style="display:inline-flex;gap:2px;align-items:center"><span class="swatch solid" style="color:${d[2]};background:${d[2]};width:8px"></span><span class="swatch dashed" style="color:${d[2]};width:8px"></span></span>${d[1]}</label>`).join('');
  el.onchange=e=>{const k=e.target?.dataset?.k;if(!k)return;pick.overview[k]=e.target.checked;drawOverview()};
}

function overviewLineChart(el,series,times,nowIso){
  const active=series.filter(s=>s.on&&s.values.some(v=>v!=null&&isFinite(v)));
  if(!active.length){el.innerHTML='<div class="empty">No selected data available.</div>';return}
  const parsed=(times||[]).map(t=>Date.parse(t));
  const goodTimes=parsed.filter(Number.isFinite);
  if(!goodTimes.length){el.innerHTML='<div class="empty">No timestamped data available.</div>';return}
  const W=1000,H=320,p={l:58,r:88,t:20,b:48},t0=Math.min(...goodTimes),t1=Math.max(...goodTimes),tr=Math.max(1,t1-t0);
  const axes={};
  for(const k of ['power','price','soc']){
    const vals=active.filter(s=>s.axis===k).flatMap(s=>s.values).filter(v=>v!=null&&isFinite(v)).map(Number);
    if(!vals.length)continue;
    if(k==='soc')axes[k]={min:0,max:100};
    else{let mn=Math.min(...vals),mx=Math.max(...vals);mn=Math.min(0,mn);if(mx===mn)mx=mn+1;const pad=(mx-mn)*.08;axes[k]={min:mn-pad,max:mx+pad}}
  }
  const xms=ms=>p.l+(W-p.l-p.r)*(ms-t0)/tr;
  const y=(v,k)=>{const q=axes[k];return p.t+(H-p.t-p.b)*(1-(Number(v)-q.min)/(q.max-q.min||1))};
  let svg=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
  const primary=axes.power?'power':axes.price?'price':'soc',pr=axes[primary];
  for(let j=0;j<5;j++){
    const yy=p.t+(H-p.t-p.b)*j/4,vv=pr.max-(pr.max-pr.min)*j/4;
    svg+=`<line x1="${p.l}" y1="${yy}" x2="${W-p.r}" y2="${yy}" stroke="#263647"/><text x="5" y="${yy+4}" fill="#91a2b3" font-size="11">${n(vv,1)}</text>`;
  }
  svg+=`<text x="5" y="12" fill="#91a2b3" font-size="10">${primary==='power'?'kW':primary==='price'?'öre/kWh':'SOC %'}</text>`;
  let off=0;
  for(const k of ['price','soc']){
    if(!axes[k]||k===primary)continue;
    const q=axes[k],col=k==='price'?C.price:C.soc;
    for(let j=0;j<5;j++){
      const yy=p.t+(H-p.t-p.b)*j/4,vv=q.max-(q.max-q.min)*j/4;
      svg+=`<text x="${W-p.r+8+off}" y="${yy+4}" fill="${col}" font-size="10">${n(vv,k==='soc'?0:1)}</text>`;
    }
    svg+=`<text x="${W-p.r+8+off}" y="12" fill="${col}" font-size="9">${k==='price'?'öre/kWh':'SOC %'}</text>`;off+=38;
  }
  const tickCount=9;
  for(let j=0;j<tickCount;j++){
    const ms=t0+tr*j/(tickCount-1),xx=xms(ms);
    svg+=`<line x1="${xx}" y1="${H-p.b}" x2="${xx}" y2="${H-p.b+5}" stroke="#52687c"/><text x="${xx}" y="${H-12}" fill="#91a2b3" font-size="10" text-anchor="middle">${tlabel(new Date(ms).toISOString(),tr>30*3600000&&j===0)}</text>`;
  }
  const nowMs=Date.parse(nowIso||'');
  if(Number.isFinite(nowMs)&&nowMs>=t0&&nowMs<=t1){
    const nx=xms(nowMs);
    svg+=`<line x1="${nx}" y1="${p.t}" x2="${nx}" y2="${H-p.b}" stroke="#eef4f8" stroke-width="1.2" stroke-dasharray="3 4" opacity=".7" vector-effect="non-scaling-stroke"/><text x="${nx+5}" y="${p.t+12}" fill="#eef4f8" font-size="10">Now</text>`;
  }
  for(const s of active){
    let d='',started=false;
    s.values.forEach((v,i)=>{
      const ms=parsed[i];
      if(v==null||!isFinite(v)||!Number.isFinite(ms)){started=false;return}
      d+=(started?'L':'M')+xms(ms)+','+y(v,s.axis);started=true;
    });
    svg+=`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width||2.2}" ${s.dashed?'stroke-dasharray="7 5"':''} vector-effect="non-scaling-stroke"/>`;
  }
  svg+='</svg>';el.innerHTML=svg;
}

function drawOverview(){
  const actual=overviewRealized.rows||[], nowMs=Date.parse(overviewRealized.now||new Date().toISOString());
  const planned=pRows().filter(r=>{const ms=Date.parse(r.start||r.start_utc);return Number.isFinite(ms)&&ms>=nowMs-15*60*1000});
  const points=[];
  actual.forEach(r=>points.push({kind:'actual',start:r.start,...r}));
  planned.forEach(r=>points.push({kind:'plan',start:r.start||r.start_utc,...r}));
  points.sort((a,b)=>Date.parse(a.start)-Date.parse(b.start)||(a.kind==='actual'?-1:1));
  const times=points.map(r=>r.start), ps=pick.overview;
  const vals=(kind,fn)=>points.map(r=>r.kind===kind?fn(r):null);
  const series=[
    {axis:'power',color:C.load,values:vals('actual',r=>r.load_kw),on:ps.load},
    {axis:'power',color:C.load,values:vals('plan',r=>r.load_kw??r.forecast_load_kw),on:ps.load,dashed:true},
    {axis:'power',color:C.pv,values:vals('actual',r=>r.pv_kw),on:ps.pv},
    {axis:'power',color:C.pv,values:vals('plan',r=>r.pv_kw??r.forecast_pv_kw),on:ps.pv,dashed:true},
    {axis:'power',color:C.battery,values:vals('actual',r=>r.battery_kw),on:ps.battery},
    {axis:'power',color:C.battery,values:vals('plan',r=>r.battery_action_kw??r.action_kw),on:ps.battery,dashed:true},
    {axis:'price',color:C.price,values:vals('actual',r=>r.price_ore_kwh),on:ps.price},
    {axis:'price',color:C.price,values:vals('plan',r=>r.price_ore_kwh??r.forecast_price_ore_kwh),on:ps.price,dashed:true},
    {axis:'soc',color:C.soc,values:vals('actual',r=>r.soc_pct),on:ps.soc},
    {axis:'soc',color:C.soc,values:vals('plan',r=>r.expected_soc_pct??r.soc_end_pct),on:ps.soc,dashed:true}
  ];
  overviewLineChart($('overviewPlan'),series,times,overviewRealized.now);
}

async function loadOverviewHistory(){
  try{overviewRealized=await api('ui/overview-history?hours=24');drawOverview()}
  catch(e){console.warn('Overview history unavailable',e);drawOverview()}
}

// Keep the Plan chart purely forward-looking while Overview joins realized and planned data.
drawPlan=function(){const d=planData('plan');lineChart($('planChart'),d.series,d.times);drawOverview()};

const overviewTitle=document.querySelector('#overview .card h2');if(overviewTitle)overviewTitle.textContent='Realized → optimizer plan';
const overviewNote=document.querySelector('#overview .chart-note');if(overviewNote)overviewNote.textContent='Solid = last 24 h realized · dashed = current forecast/plan · vertical line = now. Time is Europe/Stockholm.';
overviewSeriesPicker();

// Rebind Plan picker so changing Plan-series does not overwrite the Overview chart.
$('planPicker').onchange=e=>{const k=e.target?.dataset?.k;if(!k)return;pick.plan[k]=e.target.checked;const d=planData('plan');lineChart($('planChart'),d.series,d.times)};

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
