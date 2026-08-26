from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from html import escape
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .db import DB_PATH


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Energy AI</title>
<style>
:root{--bg:#0b1118;--panel:#121b25;--panel2:#172330;--line:#263647;--text:#eef4f8;--muted:#91a2b3;--accent:#4fb3ff;--good:#51d88a;--warn:#ffbf5a;--bad:#ff6b6b;--purple:#a78bfa;--radius:16px}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif} a{color:inherit}.shell{max-width:1500px;margin:auto;padding:18px}.top{display:flex;gap:18px;align-items:center;justify-content:space-between;margin-bottom:14px}.brand{display:flex;gap:12px;align-items:center}.logo{width:38px;height:38px;border-radius:12px;background:linear-gradient(135deg,#2375d8,#4fb3ff);display:grid;place-items:center;font-weight:800}.brand h1{font-size:20px;margin:0}.brand .sub{color:var(--muted);font-size:12px}.status{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}.dot{width:8px;height:8px;border-radius:50%;background:var(--warn)}.dot.ok{background:var(--good)}
.tabs{display:flex;gap:6px;overflow:auto;padding:4px;background:#0f1720;border:1px solid var(--line);border-radius:14px;margin-bottom:16px}.tab{border:0;background:transparent;color:var(--muted);padding:10px 14px;border-radius:10px;cursor:pointer;white-space:nowrap;font-weight:650}.tab.active{background:var(--panel2);color:var(--text)}
.view{display:none}.view.active{display:block}.grid{display:grid;gap:12px}.kpis{grid-template-columns:repeat(6,minmax(130px,1fr))}.two{grid-template-columns:1.6fr 1fr}.three{grid-template-columns:repeat(3,1fr)}.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:15px;min-width:0}.card h2,.card h3{margin:0 0 12px}.card h2{font-size:15px}.card h3{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}.kpi .label{color:var(--muted);font-size:12px}.kpi .value{font-size:24px;font-weight:750;margin-top:3px}.kpi .hint{font-size:11px;color:var(--muted);margin-top:4px}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.chart{height:330px;position:relative}.chart.small{height:250px} svg{width:100%;height:100%;overflow:visible}.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:3px 0 10px}.legend span:before{content:"";display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;background:var(--c)}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.btn,select,input{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:9px;padding:8px 10px}.btn{cursor:pointer;font-weight:650}.btn:hover{border-color:#3d5b76}.muted{color:var(--muted)}.notice{padding:10px 12px;border:1px solid var(--line);border-radius:10px;color:var(--muted);background:#0f1720}.table-wrap{overflow:auto}.tbl{width:100%;border-collapse:collapse}.tbl th,.tbl td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}.tbl th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}.pill{display:inline-block;padding:3px 8px;border-radius:999px;background:var(--panel2);font-size:11px}.pill.ok{color:var(--good)}.pill.partial{color:var(--warn)}
.param-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.param-section .row{display:grid;grid-template-columns:minmax(170px,1.3fr) minmax(90px,.7fr) minmax(90px,.8fr);gap:8px;padding:8px 0;border-bottom:1px solid var(--line);align-items:center}.param-section .row:last-child{border-bottom:0}.param-name{font-weight:600}.param-value{text-align:right;font-variant-numeric:tabular-nums}.source{font-size:11px;color:var(--muted);text-align:right}.advanced{opacity:.9}.empty{height:220px;display:grid;place-items:center;color:var(--muted);text-align:center}.footer{color:var(--muted);font-size:11px;margin-top:14px;text-align:right}
@media(max-width:1100px){.kpis{grid-template-columns:repeat(3,1fr)}.two,.three,.param-grid{grid-template-columns:1fr}}@media(max-width:620px){.shell{padding:10px}.kpis{grid-template-columns:repeat(2,1fr)}.kpi .value{font-size:20px}.param-section .row{grid-template-columns:1fr auto}.source{grid-column:1/-1;text-align:left}.chart{height:280px}}
</style>
</head>
<body>
<div class="shell">
  <div class="top">
    <div class="brand"><div class="logo">AI</div><div><h1>Energy AI</h1><div class="sub">Optimizer evaluation · shadow mode</div></div></div>
    <div class="status"><span id="statusDot" class="dot"></span><span id="runtimeText">Loading…</span></div>
  </div>
  <div class="tabs" id="tabs">
    <button class="tab active" data-view="overview">Overview</button>
    <button class="tab" data-view="plan">Plan</button>
    <button class="tab" data-view="evaluation">Evaluation</button>
    <button class="tab" data-view="history">History</button>
    <button class="tab" data-view="parameters">Parameters</button>
  </div>

  <section id="overview" class="view active">
    <div class="grid kpis" id="overviewKpis"></div>
    <div class="grid two" style="margin-top:12px">
      <div class="card"><h2>Current optimizer plan</h2><div class="legend"><span style="--c:#4fb3ff">Load</span><span style="--c:#ffbf5a">PV</span><span style="--c:#a78bfa">Battery action</span></div><div id="overviewPlan" class="chart"></div></div>
      <div class="card"><h2>System state</h2><div id="systemState"></div></div>
    </div>
  </section>

  <section id="plan" class="view">
    <div class="toolbar"><button class="btn" id="refreshPlan">Refresh plan</button><span class="muted" id="planMeta"></span></div>
    <div class="card"><h2>Forecast → optimizer decision</h2><div class="legend"><span style="--c:#4fb3ff">Load</span><span style="--c:#ffbf5a">PV</span><span style="--c:#a78bfa">Battery +discharge / −charge</span><span style="--c:#51d88a">SOC</span></div><div id="planChart" class="chart"></div></div>
    <div class="card" style="margin-top:12px"><h2>Planned intervals</h2><div id="planTable" class="table-wrap"></div></div>
  </section>

  <section id="evaluation" class="view">
    <div class="toolbar"><label>Date <input type="date" id="evalDate"></label><button class="btn" id="loadEvalDay">Load day</button><button class="btn" id="evaluateNow">Evaluate matured days</button><span class="muted" id="evalMeta"></span></div>
    <div class="grid kpis" id="evalKpis"></div>
    <div class="grid two" style="margin-top:12px">
      <div class="card"><h2>Realized day replay</h2><div class="legend"><span style="--c:#4fb3ff">Actual load</span><span style="--c:#ffbf5a">Actual PV</span><span style="--c:#a78bfa">Applied battery</span></div><div id="evalChart" class="chart"></div></div>
      <div class="card"><h2>Forecast & execution diagnostics</h2><div id="diagnostics"></div></div>
    </div>
  </section>

  <section id="history" class="view">
    <div class="toolbar"><select id="historyDays"><option value="7">7 days</option><option value="14">14 days</option><option value="30" selected>30 days</option><option value="90">90 days</option></select><button class="btn" id="reloadHistory">Reload</button></div>
    <div class="grid kpis" id="historyKpis"></div>
    <div class="grid two" style="margin-top:12px">
      <div class="card"><h2>Daily saving vs zero-battery baseline</h2><div id="savingChart" class="chart small"></div></div>
      <div class="card"><h2>Perfect-information gap</h2><div id="regretChart" class="chart small"></div></div>
    </div>
    <div class="card" style="margin-top:12px"><h2>Evaluated days</h2><div id="historyTable" class="table-wrap"></div></div>
  </section>

  <section id="parameters" class="view">
    <div class="notice" style="margin-bottom:12px">Values are the effective runtime configuration used by the optimizer. This first UI version is intentionally read-only; changes remain in the Home Assistant add-on configuration.</div>
    <div id="parameterGrid" class="param-grid"></div>
  </section>
  <div class="footer">Energy AI · evaluation UI</div>
</div>
<script>
const state={plan:null,eval:null,history:null,config:null,health:null};
const $=id=>document.getElementById(id); const n=(v,d=2)=>v==null||Number.isNaN(+v)?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:d});
const sek=v=>v==null?'—':`${n(v,2)} SEK`; const pct=v=>v==null?'—':`${n(100*Number(v),1)}%`;
async function api(path,opt){const r=await fetch(path,opt);if(!r.ok)throw new Error(`${r.status} ${await r.text()}`);return r.json()}
function card(label,value,hint='',cls=''){return `<div class="card kpi"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="hint">${hint}</div></div>`}
function rows(obj){return Object.entries(obj).map(([k,v])=>`<div style="display:flex;justify-content:space-between;gap:15px;padding:8px 0;border-bottom:1px solid var(--line)"><span class="muted">${k}</span><strong style="text-align:right">${v}</strong></div>`).join('')}
function color(cls){return getComputedStyle(document.documentElement).getPropertyValue(cls).trim()}
function lineChart(el,series,opts={}){if(!el)return; const valid=series.flatMap(s=>s.values||[]).filter(v=>v!=null&&isFinite(v)); if(!valid.length){el.innerHTML='<div class="empty">No data available yet.</div>';return} const W=1000,H=300,p={l:48,r:20,t:15,b:30}; const min=opts.min!=null?opts.min:Math.min(0,...valid),max=opts.max!=null?opts.max:Math.max(...valid); const span=max-min||1; const count=Math.max(...series.map(s=>s.values.length)); const x=i=>p.l+(W-p.l-p.r)*(i/Math.max(1,count-1)),y=v=>p.t+(H-p.t-p.b)*(1-(v-min)/span); let svg=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`; for(let j=0;j<5;j++){const yy=p.t+(H-p.t-p.b)*j/4;const vv=max-span*j/4;svg+=`<line x1="${p.l}" y1="${yy}" x2="${W-p.r}" y2="${yy}" stroke="#263647" stroke-width="1"/><text x="5" y="${yy+4}" fill="#91a2b3" font-size="12">${n(vv,1)}</text>`} series.forEach(s=>{let d='';s.values.forEach((v,i)=>{if(v==null||!isFinite(v))return;d+=(d?'L':'M')+x(i)+','+y(v)});svg+=`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width||2.2}" vector-effect="non-scaling-stroke"/>`}); svg+='</svg>';el.innerHTML=svg}
function barChart(el,values,colorPos='#51d88a',colorNeg='#ff6b6b'){if(!values.length){el.innerHTML='<div class="empty">No complete evaluated days yet.</div>';return}const W=1000,H=230,p={l:42,r:12,t:10,b:30};const vals=values.map(x=>Number(x.value)||0),mx=Math.max(...vals.map(Math.abs),1);const y0=(H-p.b+p.t)/2;const bh=(H-p.t-p.b)/2;const bw=(W-p.l-p.r)/values.length*.68;let svg=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="${p.l}" y1="${y0}" x2="${W-p.r}" y2="${y0}" stroke="#52687c"/>`;values.forEach((d,i)=>{const v=Number(d.value)||0,x=p.l+(i+.5)*(W-p.l-p.r)/values.length-bw/2,h=Math.abs(v)/mx*bh,y=v>=0?y0-h:y0;svg+=`<rect x="${x}" y="${y}" width="${bw}" height="${Math.max(1,h)}" rx="2" fill="${v>=0?colorPos:colorNeg}"/><title>${d.label}: ${n(v,2)}</title>`});svg+='</svg>';el.innerHTML=svg}

function renderPlan(){const p=state.plan||{}, rs=p.rows||p.plan||p.intervals||[]; $('planMeta').textContent=p.generated_at?`Generated ${new Date(p.generated_at).toLocaleString()} · ${p.planner||''}`:'No plan'; const load=rs.map(r=>r.load_kw??r.forecast_load_kw),pv=rs.map(r=>r.pv_kw??r.forecast_pv_kw),act=rs.map(r=>r.battery_action_kw??r.action_kw),soc=rs.map(r=>r.expected_soc_pct??r.soc_end_pct); lineChart($('planChart'),[{values:load,color:'#4fb3ff'},{values:pv,color:'#ffbf5a'},{values:act,color:'#a78bfa'},{values:soc.map(v=>v==null?null:Number(v)/10),color:'#51d88a'}]); lineChart($('overviewPlan'),[{values:load,color:'#4fb3ff'},{values:pv,color:'#ffbf5a'},{values:act,color:'#a78bfa'}]); $('planTable').innerHTML=rs.length?`<table class="tbl"><thead><tr><th>Start</th><th>Load kW</th><th>PV kW</th><th>Price öre/kWh</th><th>Battery kW</th><th>SOC %</th><th>Reason</th></tr></thead><tbody>${rs.slice(0,144).map(r=>`<tr><td>${r.start?new Date(r.start).toLocaleString():''}</td><td>${n(r.load_kw)}</td><td>${n(r.pv_kw)}</td><td>${n(r.price_ore_kwh)}</td><td>${n(r.battery_action_kw??r.action_kw)}</td><td>${n(r.expected_soc_pct??r.soc_end_pct,1)}</td><td>${r.reason||''}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No planned intervals.</div>';
 const s=p.summary||{}; $('overviewKpis').innerHTML=card('Initial SOC',s.initial_soc_pct!=null?`${n(s.initial_soc_pct,1)}%`:p.initial_soc_pct!=null?`${n(p.initial_soc_pct,1)}%`:'—','Plan start')+card('Planned charge',s.charge_kwh!=null?`${n(s.charge_kwh)} kWh`:'—','Current horizon')+card('Planned discharge',s.discharge_kwh!=null?`${n(s.discharge_kwh)} kWh`:'—','Current horizon')+card('Import',s.grid_import_kwh!=null?`${n(s.grid_import_kwh)} kWh`:'—','Planned')+card('Export',s.grid_export_kwh!=null?`${n(s.grid_export_kwh)} kWh`:'—','Planned')+card('Planner',p.planner||'—',p.mode||'shadow'); }
function renderHealth(){const h=state.health||{}; const latest=h.latest||h.snapshot||{};$('systemState').innerHTML=rows({'Runtime':h.runtime_build||state.config?.runtime_build||'—','Collector':h.collector_running===false?'Stopped':'Running','Last error':h.last_error||'None','PV':latest.pv_power_kw!=null?`${n(latest.pv_power_kw)} kW`:'—','House load':latest.house_load_kw!=null?`${n(latest.house_load_kw)} kW`:'—','Grid':latest.grid_power_kw!=null?`${n(latest.grid_power_kw)} kW`:'—','Battery':latest.battery_power_kw!=null?`${n(latest.battery_power_kw)} kW`:'—','SOC':latest.battery_soc_pct!=null?`${n(latest.battery_soc_pct,1)}%`:'—'});}
function renderEval(){const e=state.eval||{};$('evalMeta').textContent=e.local_date?`${e.local_date} · ${e.status||''}`:'';if(!e.local_date){$('evalKpis').innerHTML='';return}const c=e.comparison||{},rt=e.realtime_counterfactual||{},d=e.data||{},ph=e.perfect_hindsight||{};const sv=c.realtime_economic_saving_vs_zero_battery_sek; $('evalKpis').innerHTML=card('Saving vs baseline',sek(sv),'Zero-battery baseline',sv>0?'good':sv<0?'bad':'')+card('Perfect-info gap',sek(c.perfect_information_gap_sek),'Positive = hindsight better',c.perfect_information_gap_sek>0?'warn':'')+card('Plan coverage',pct(d.plan_action_coverage_fraction),'≥90% required for KPI',d.plan_action_coverage_fraction>=.9?'good':'warn')+card('Throughput',`${n(rt.battery_throughput_kwh)} kWh`,'Battery throughput')+card('Clamped intervals',n(rt.clamped_action_intervals,0),'Physical constraints')+card('Terminal SOC',`${n(rt.terminal_soc_pct,1)}%`,`${n(rt.terminal_soc_delta_pct,1)} pp change`);const rr=e.rows||[];lineChart($('evalChart'),[{values:rr.map(r=>r.actual_load_kw),color:'#4fb3ff'},{values:rr.map(r=>r.actual_pv_kw),color:'#ffbf5a'},{values:rr.map(r=>r.applied_action_kw),color:'#a78bfa'}]);const fe=e.forecast_error_on_executed_intervals||{};$('diagnostics').innerHTML=rows({'Status':`<span class="pill ${e.status==='ok'?'ok':'partial'}">${e.status}</span>`,'Load MAE':fe.load?.mae_kw!=null?`${n(fe.load.mae_kw)} kW`:'—','Load bias':fe.load?.bias_kw!=null?`${n(fe.load.bias_kw)} kW`:'—','PV MAE':fe.pv?.mae_kw!=null?`${n(fe.pv.mae_kw)} kW`:'—','PV bias':fe.pv?.bias_kw!=null?`${n(fe.pv.bias_kw)} kW`:'—','Net-load MAE':fe.net_load?.mae_kw!=null?`${n(fe.net_load.mae_kw)} kW`:'—','Hindsight':ph.status||'—','Import exceedances':n(rt.import_proxy_exceedance_intervals,0)});}
function renderHistory(){const h=state.history||{},days=h.days||[];$('historyKpis').innerHTML=card('Complete days',n(h.complete_days,0),`${n(h.partial_days,0)} partial`)+card('Total saving',sek(h.total_realtime_economic_saving_vs_zero_battery_sek),'Complete days only',h.total_realtime_economic_saving_vs_zero_battery_sek>0?'good':'')+card('Mean daily saving',sek(h.mean_daily_realtime_economic_saving_sek),'Complete days')+card('Perfect-info gap',sek(h.total_perfect_information_gap_sek),'Total regret')+card('Mean coverage',pct(h.mean_plan_action_coverage_fraction),'All stored days')+card('Clamped intervals',n(h.total_clamped_action_intervals,0),'All stored days');barChart($('savingChart'),days.filter(x=>x.status==='ok').map(x=>({label:x.local_date,value:x.saving_sek})));barChart($('regretChart'),days.filter(x=>x.status==='ok'&&x.perfect_information_gap_sek!=null).map(x=>({label:x.local_date,value:x.perfect_information_gap_sek})),'#ffbf5a','#51d88a');$('historyTable').innerHTML=days.length?`<table class="tbl"><thead><tr><th>Date</th><th>Status</th><th>Coverage</th><th>Saving SEK</th><th>Perfect-info gap SEK</th><th>Load MAE kW</th><th>PV MAE kW</th><th>Throughput kWh</th><th>Clamps</th></tr></thead><tbody>${days.map(x=>`<tr><td>${x.local_date}</td><td><span class="pill ${x.status==='ok'?'ok':'partial'}">${x.status}</span></td><td>${pct(x.coverage)}</td><td>${n(x.saving_sek)}</td><td>${n(x.perfect_information_gap_sek)}</td><td>${n(x.load_mae_kw)}</td><td>${n(x.pv_mae_kw)}</td><td>${n(x.throughput_kwh)}</td><td>${n(x.clamped_intervals,0)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No evaluated days stored yet.</div>'}
function paramSection(title,items,advanced=false){return `<div class="card param-section ${advanced?'advanced':''}"><h2>${title}</h2>${items.map(x=>`<div class="row"><div class="param-name">${x[0]}</div><div class="param-value">${x[1]}</div><div class="source">${x[2]||'Runtime config'}</div></div>`).join('')}</div>`}
function renderParameters(){const c=state.config||{},b=c.policy?.battery||{},e=c.policy?.economics||{},o=c.optimizer||{},t=c.tariffs||{},col=c.collector||{},ent=c.entities||{};const yes=v=>v?'Enabled':'Disabled';$('parameterGrid').innerHTML=[paramSection('Battery',[['Capacity',`${n(b.capacity_kwh)} kWh`],['Hard minimum SOC',`${n(b.hard_min_soc_pct,1)}%`],['Hard maximum SOC',`${n(b.hard_max_soc_pct,1)}%`],['Preferred SOC range',`${n(b.preferred_min_soc_pct,1)}–${n(b.preferred_max_soc_pct,1)}%`],['Normal reserve',`${n(b.normal_reserve_soc_pct,1)}%`],['High-uncertainty reserve',`${n(b.high_uncertainty_reserve_soc_pct,1)}%`],['Max charge',`${n(o.battery_max_charge_kw)} kW`],['Max discharge',`${n(o.battery_max_discharge_kw)} kW`],['Charge efficiency',`${n(100*o.battery_charge_efficiency,1)}%`],['Discharge efficiency',`${n(100*o.battery_discharge_efficiency,1)}%`]]),paramSection('Grid & connection',[['Physical import limit',`${n(o.physical_grid_import_limit_kw)} kW`],['Export limit',`${n(o.grid_export_limit_kw)} kW`],['Import entity',ent.grid_power||'—','Home Assistant'],['Battery entity',ent.battery_power||'—','Home Assistant'],['SOC entity',ent.battery_soc||'—','Home Assistant']]),paramSection('Economics',[['Import overhead',`${n(e.import_overhead_ore_kwh)} öre/kWh`],['Export overhead',`${n(e.export_overhead_ore_kwh)} öre/kWh`],['Minimum arbitrage margin',`${n(e.minimum_arbitrage_margin_ore_kwh)} öre/kWh`],['Battery degradation',`${n(o.battery_degradation_ore_kwh)} öre/kWh throughput`],['Spot-price entity',ent.spot_price||'—','Home Assistant']]),paramSection('Optimizer behaviour',[['Mode',o.mode||'—'],['Planner',o.planner||'—'],['SOC grid step',`${n(o.soc_grid_step_kwh)} kWh`],['Critical reserve SOC',`${n(o.reserve_critical_soc_pct,1)}%`],['Terminal SOC tolerance',`${n(o.terminal_soc_tolerance_pct,1)}%`],['Terminal SOC tiebreak',`${n(o.terminal_soc_tiebreak_ore_per_kwh)} öre/kWh`],['Unknown-price coverage',pct(o.unknown_price_energy_coverage_fraction)],['Unknown-price risk premium',`${n(o.unknown_price_risk_premium_ore_kwh)} öre/kWh`],['Continuation value',`${n(o.unknown_price_default_continuation_value_ore_kwh)} öre/kWh`]]),paramSection('Reserve penalties',[['Critical shortfall',`${n(o.reserve_critical_penalty_ore_per_kwh_hour)} öre/(kWh·h)`],['Preferred shortfall',`${n(o.reserve_preferred_penalty_ore_per_kwh_hour)} öre/(kWh·h)`],['Target shortfall',`${n(o.reserve_target_penalty_ore_per_kwh_hour)} öre/(kWh·h)`],['Above preferred max',`${n(o.preferred_max_excess_penalty_ore_per_kwh_hour)} öre/(kWh·h)`],['Uncertainty full scale',`${n(o.reserve_uncertainty_full_scale_kw)} kW`]]),paramSection('Tariffs',[['Tariff framework',yes(t.enabled)],['Consumption demand',yes(t.consumption_demand?.enabled)],['Consumption rate',`${n(t.consumption_demand?.rate_sek_per_kw)} SEK/kW`],['Consumption window',`${n(t.consumption_demand?.start_hour,0)}–${n(t.consumption_demand?.end_hour,0)}`],['Production demand',yes(t.production_demand?.enabled)],['Production rate',`${n(t.production_demand?.rate_sek_per_kw)} SEK/kW`],['Production window',`${n(t.production_demand?.start_hour,0)}–${n(t.production_demand?.end_hour,0)}`]]),paramSection('Forecast & data',[['Collector interval',`${n(col.poll_seconds,0)} s`],['Stale after',`${n(col.stale_after_seconds,0)} s`],['PV entity',ent.pv_power||'—','Home Assistant'],['House-load entity',ent.house_load||'—','Home Assistant']],true),paramSection('System',[['Runtime build',c.runtime_build||'—'],['Evaluation threshold','90% plan coverage','Evaluation policy'],['Physical writes','Disabled','Safety'],['Configuration source','Home Assistant add-on options','Runtime']],true)].join('')}
async function loadPlan(){try{state.plan=await api('optimizer/plan?limit=144');renderPlan()}catch(e){$('planMeta').textContent=e.message}}
async function loadEval(date){try{state.eval=await api(`optimizer/evaluation/day?local_date=${date}`);renderEval()}catch(e){$('evalMeta').textContent=e.message}}
async function loadHistory(){try{state.history=await api(`ui/history?days=${$('historyDays').value}`);renderHistory()}catch(e){$('historyTable').innerHTML=`<div class="empty">${e.message}</div>`}}
async function init(){const y=new Date(Date.now()-86400000);$('evalDate').value=y.toISOString().slice(0,10);try{[state.config,state.health]=await Promise.all([api('ui/config'),api('health')]);$('statusDot').classList.add('ok');$('runtimeText').textContent=`Runtime ${state.config.runtime_build||'unknown'}`;renderHealth();renderParameters()}catch(e){$('runtimeText').textContent='Backend connection error'}await Promise.all([loadPlan(),loadHistory(),loadEval($('evalDate').value)])}
$('tabs').addEventListener('click',e=>{const b=e.target.closest('.tab');if(!b)return;document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.view').forEach(x=>x.classList.toggle('active',x.id===b.dataset.view));});$('refreshPlan').onclick=async()=>{try{await api('optimizer/refresh');await loadPlan()}catch(e){$('planMeta').textContent=e.message}};$('loadEvalDay').onclick=()=>loadEval($('evalDate').value);$('evaluateNow').onclick=async()=>{try{$('evalMeta').textContent='Evaluating…';await api('optimizer/evaluation/evaluate-now?lookback_days=7');await loadHistory();await loadEval($('evalDate').value)}catch(e){$('evalMeta').textContent=e.message}};$('reloadHistory').onclick=loadHistory;$('historyDays').onchange=loadHistory;init();
</script>
</body></html>'''


def _safe_config(cfg: dict[str, Any]) -> dict[str, Any]:
    # cfg is already the normalized runtime config and contains no API keys/passwords.
    allowed = {
        "runtime_build": cfg.get("runtime_build"),
        "collector": cfg.get("collector") or {},
        "entities": cfg.get("entities") or {},
        "policy": cfg.get("policy") or {},
        "optimizer": cfg.get("optimizer") or {},
        "tariffs": cfg.get("tariffs") or {},
    }
    return allowed


def _history(days: int) -> dict[str, Any]:
    cutoff = (datetime.now().date()).isoformat()
    with sqlite3.connect(DB_PATH) as c:
        raw = c.execute(
            "SELECT local_date,payload_json FROM optimizer_day_eval ORDER BY local_date DESC LIMIT ?",
            (max(1, min(180, int(days))),),
        ).fetchall()
    payloads: list[dict[str, Any]] = []
    for local_date, payload_raw in reversed(raw):
        try:
            p = json.loads(payload_raw)
        except Exception:
            continue
        comp = p.get("comparison") or {}
        data = p.get("data") or {}
        rt = p.get("realtime_counterfactual") or {}
        fe = p.get("forecast_error_on_executed_intervals") or {}
        payloads.append({
            "local_date": local_date,
            "status": p.get("status"),
            "coverage": data.get("plan_action_coverage_fraction"),
            "saving_sek": comp.get("realtime_economic_saving_vs_zero_battery_sek"),
            "perfect_information_gap_sek": comp.get("perfect_information_gap_sek"),
            "load_mae_kw": (fe.get("load") or {}).get("mae_kw"),
            "pv_mae_kw": (fe.get("pv") or {}).get("mae_kw"),
            "throughput_kwh": rt.get("battery_throughput_kwh"),
            "clamped_intervals": rt.get("clamped_action_intervals"),
        })
    good = [p for p in payloads if p.get("status") == "ok"]
    partial = [p for p in payloads if p.get("status") == "partial_plan_coverage"]
    savings = [float(p["saving_sek"]) for p in good if p.get("saving_sek") is not None]
    regrets = [float(p["perfect_information_gap_sek"]) for p in good if p.get("perfect_information_gap_sek") is not None]
    coverages = [float(p["coverage"]) for p in payloads if p.get("coverage") is not None]
    return {
        "window_days": days,
        "stored_days": len(payloads),
        "complete_days": len(good),
        "partial_days": len(partial),
        "mean_plan_action_coverage_fraction": round(sum(coverages) / len(coverages), 4) if coverages else None,
        "total_realtime_economic_saving_vs_zero_battery_sek": round(sum(savings), 2) if savings else None,
        "mean_daily_realtime_economic_saving_sek": round(sum(savings) / len(savings), 2) if savings else None,
        "total_perfect_information_gap_sek": round(sum(regrets), 2) if regrets else None,
        "mean_daily_perfect_information_gap_sek": round(sum(regrets) / len(regrets), 2) if regrets else None,
        "total_clamped_action_intervals": sum(int(p.get("clamped_intervals") or 0) for p in payloads),
        "days": payloads,
    }


def install_dashboard(app: FastAPI, cfg: dict[str, Any]) -> None:
    @app.middleware("http")
    async def dashboard_root(request: Request, call_next):
        if request.url.path in {"", "/"}:
            return RedirectResponse(url="ui", status_code=307)
        return await call_next(request)

    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard_ui():
        return DASHBOARD_HTML

    @app.get("/ui/config", include_in_schema=False)
    async def dashboard_config():
        return JSONResponse(_safe_config(cfg))

    @app.get("/ui/history", include_in_schema=False)
    async def dashboard_history(days: int = Query(30, ge=1, le=180)):
        try:
            return JSONResponse(_history(days))
        except sqlite3.OperationalError:
            return JSONResponse({
                "window_days": days,
                "stored_days": 0,
                "complete_days": 0,
                "partial_days": 0,
                "days": [],
            })
