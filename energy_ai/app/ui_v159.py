from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import DASHBOARD_HTML
from .db import DB_PATH
from .overview_extension import OVERVIEW_EXTENSION
from .ui_v158 import V158_EXTENSION


V159_EXTENSION = r'''
<style>
.live-flow-card{margin:12px 0 0}.live-flow-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:4px}.live-flow-status{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:11px}.live-flow-status .lf-dot{width:7px;height:7px;border-radius:50%;background:var(--warn)}.live-flow-status.fresh .lf-dot{background:var(--good)}.live-flow-wrap{position:relative;min-height:270px}.live-flow-svg{width:100%;height:270px;display:block;overflow:visible}.lf-base{fill:none;stroke:#2d4051;stroke-width:5;stroke-linecap:round}.lf-active{fill:none;stroke-width:4;stroke-linecap:round;stroke-dasharray:3 12;opacity:.95;animation:lfDash var(--lf-speed,1.5s) linear infinite}.lf-active.reverse{animation-direction:reverse}.lf-active.idle{animation:none;stroke:#3a4c5d;stroke-dasharray:none;opacity:.45}.lf-node{fill:#172330;stroke:#314558;stroke-width:1.5}.lf-node-title{fill:#91a2b3;font-size:12px;text-anchor:middle}.lf-node-value{fill:#eef4f8;font-size:20px;font-weight:750;text-anchor:middle}.lf-node-sub{fill:#91a2b3;font-size:10px;text-anchor:middle}.lf-arrow{fill:#91a2b3;font-size:14px;font-weight:700;text-anchor:middle}.lf-pv{stroke:#ffbf5a}.lf-battery{stroke:#a78bfa}.lf-grid{stroke:#5ad5d5}.lf-ev{stroke:#ef9f63}.lf-note{color:var(--muted);font-size:10px;margin-top:-2px}.lf-off{opacity:.38}
@keyframes lfDash{to{stroke-dashoffset:-30}}
@media(prefers-reduced-motion:reduce){.lf-active{animation:none;stroke-dasharray:none}}
@media(max-width:620px){.live-flow-svg{height:245px}.live-flow-wrap{min-height:245px}.lf-node-value{font-size:17px}.lf-node-title{font-size:11px}}
</style>
<script>
let liveFlowTimer=null;

function liveFlowMarkup(){
  return `<div class="card live-flow-card" id="liveFlowCard">
    <div class="live-flow-head"><h2 style="margin:0">Live energy flow</h2><div class="live-flow-status" id="liveFlowStatus"><span class="lf-dot"></span><span>Loading live state…</span></div></div>
    <div class="live-flow-wrap">
      <svg class="live-flow-svg" viewBox="0 0 1000 270" role="img" aria-label="Live power flow between solar PV, home, battery, grid and electric vehicle">
        <defs><filter id="lfGlow"><feGaussianBlur stdDeviation="1.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
        <path class="lf-base" d="M500 72 L500 101"/><path id="lfPvPath" class="lf-active lf-pv idle" d="M500 72 L500 101"/>
        <path class="lf-base" d="M245 135 L447 135"/><path id="lfBatteryPath" class="lf-active lf-battery idle" d="M245 135 L447 135"/>
        <path class="lf-base" d="M553 135 L755 135"/><path id="lfGridPath" class="lf-active lf-grid idle" d="M553 135 L755 135"/>
        <path class="lf-base" d="M500 169 L500 209"/><path id="lfEvPath" class="lf-active lf-ev idle" d="M500 169 L500 209"/>

        <text id="lfPvArrow" class="lf-arrow" x="520" y="92">↓</text>
        <text id="lfBatteryArrow" class="lf-arrow" x="348" y="126">→</text>
        <text id="lfGridArrow" class="lf-arrow" x="652" y="126">←</text>
        <text id="lfEvArrow" class="lf-arrow" x="520" y="196">↓</text>

        <rect class="lf-node" x="432" y="12" width="136" height="60" rx="14"/><text class="lf-node-title" x="500" y="32">Solar PV</text><text id="lfPvValue" class="lf-node-value" x="500" y="56">—</text>
        <rect class="lf-node" x="447" y="101" width="106" height="68" rx="16"/><text class="lf-node-title" x="500" y="122">Home</text><text id="lfHomeValue" class="lf-node-value" x="500" y="147">—</text><text class="lf-node-sub" x="500" y="161">total load</text>
        <rect class="lf-node" x="105" y="101" width="140" height="68" rx="14"/><text class="lf-node-title" x="175" y="122">Battery</text><text id="lfBatteryValue" class="lf-node-value" x="175" y="146">—</text><text id="lfBatterySub" class="lf-node-sub" x="175" y="160">—</text>
        <rect class="lf-node" x="755" y="101" width="140" height="68" rx="14"/><text class="lf-node-title" x="825" y="122">Grid</text><text id="lfGridValue" class="lf-node-value" x="825" y="146">—</text><text id="lfGridSub" class="lf-node-sub" x="825" y="160">—</text>
        <g id="lfEvNode"><rect class="lf-node" x="430" y="209" width="140" height="56" rx="14"/><text class="lf-node-title" x="500" y="229">EV charger</text><text id="lfEvValue" class="lf-node-value" x="500" y="253">—</text></g>
      </svg>
    </div>
    <div class="lf-note">EV is shown as a branch of total house load and is not added again to the energy balance.</div>
  </div>`;
}

function lfNum(v){const x=Number(v);return Number.isFinite(x)?x:null}
function lfPower(v){const x=lfNum(v);return x==null?'—':`${n(Math.abs(x),2)} kW`}
function lfSpeed(v){const x=Math.abs(Number(v)||0);return `${Math.max(.55,Math.min(2.8,2.4/(.35+x))).toFixed(2)}s`}
function lfSetPath(id,value,forward){
  const el=$(id),x=lfNum(value);if(!el)return;
  el.style.setProperty('--lf-speed',lfSpeed(x));el.classList.toggle('idle',x==null||Math.abs(x)<.05);el.classList.toggle('reverse',x!=null&&Math.abs(x)>=.05&&!forward);
}

function renderLiveFlow(d){
  const pv=lfNum(d.pv_power_kw),load=lfNum(d.house_load_kw),grid=lfNum(d.grid_power_kw),bat=lfNum(d.battery_power_kw),soc=lfNum(d.battery_soc_pct),ev=lfNum(d.ev_power_kw);
  $('lfPvValue').textContent=lfPower(pv);$('lfHomeValue').textContent=lfPower(load);$('lfBatteryValue').textContent=lfPower(bat);$('lfGridValue').textContent=lfPower(grid);$('lfEvValue').textContent=lfPower(ev);
  $('lfBatterySub').textContent=soc==null?'SOC —':`SOC ${n(soc,1)}%`;
  $('lfGridSub').textContent=grid==null?'—':Math.abs(grid)<.05?'balanced':grid>0?'importing':'exporting';
  const batDischarge=bat!=null&&bat>0,gridImport=grid!=null&&grid>0,evToHome=ev!=null&&ev<0;
  lfSetPath('lfPvPath',pv,true);lfSetPath('lfBatteryPath',bat,batDischarge);lfSetPath('lfGridPath',grid,!gridImport);lfSetPath('lfEvPath',ev,!evToHome);
  $('lfPvArrow').textContent=pv!=null&&pv>.05?'↓':'·';$('lfBatteryArrow').textContent=bat==null||Math.abs(bat)<.05?'·':batDischarge?'→':'←';$('lfGridArrow').textContent=grid==null||Math.abs(grid)<.05?'·':gridImport?'←':'→';$('lfEvArrow').textContent=ev==null||Math.abs(ev)<.05?'·':evToHome?'↑':'↓';
  const evNode=$('lfEvNode');if(evNode)evNode.classList.toggle('lf-off',ev==null);
  const status=$('liveFlowStatus'),age=lfNum(d.age_seconds),stale=lfNum(d.stale_after_seconds)??180;status.classList.toggle('fresh',age!=null&&age<=stale);status.querySelector('span:last-child').textContent=age==null?'No live sample':age<90?`Updated ${Math.round(age)} s ago`:`Updated ${Math.round(age/60)} min ago`;
}

async function loadLiveFlow(){try{renderLiveFlow(await api('ui/live-flow'))}catch(e){const s=$('liveFlowStatus');if(s)s.querySelector('span:last-child').textContent='Live state unavailable'}}

function installLiveFlow(){
  const kpis=$('overviewKpis');if(!kpis||$('liveFlowCard'))return;kpis.insertAdjacentHTML('afterend',liveFlowMarkup());loadLiveFlow();
  if(liveFlowTimer)clearInterval(liveFlowTimer);liveFlowTimer=setInterval(()=>{if(document.querySelector('#overview.view.active'))loadLiveFlow()},15000);
}

installLiveFlow();
$('tabs').addEventListener('click',e=>{const b=e.target.closest('.tab');if(b?.dataset?.view==='overview')loadLiveFlow()});
</script>
'''


def _num(item: Any) -> float | None:
    if isinstance(item, dict):
        if not item.get("available"):
            return None
        item = item.get("state")
    try:
        return float(item)
    except (TypeError, ValueError):
        return None


def _live_flow(cfg: dict[str, Any]) -> dict[str, Any]:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT collected_at,payload_json FROM raw_state ORDER BY id DESC LIMIT 1").fetchone()
    stale_after = int((cfg.get("collector") or {}).get("stale_after_seconds", 180))
    if not row:
        return {"collected_at": None, "age_seconds": None, "stale_after_seconds": stale_after}
    collected_at, raw = row
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    try:
        stamp = datetime.fromisoformat(str(collected_at).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds())
    except Exception:
        age = None
    return {
        "collected_at": collected_at,
        "age_seconds": None if age is None else round(age, 1),
        "stale_after_seconds": stale_after,
        "pv_power_kw": _num(payload.get("pv_power_kw")),
        "house_load_kw": _num(payload.get("house_load_kw")),
        "grid_power_kw": _num(payload.get("grid_power_kw")),
        "battery_power_kw": _num(payload.get("battery_power_kw")),
        "battery_soc_pct": _num(payload.get("battery_soc_pct")),
        "ev_power_kw": _num(payload.get("ev_power_kw")),
    }


def install_ui_v159(app: FastAPI, cfg: dict[str, Any]) -> None:
    @app.middleware("http")
    async def ui_v159(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace("</body>", OVERVIEW_EXTENSION + V158_EXTENSION + V159_EXTENSION + "</body>")
            return HTMLResponse(html)
        return await call_next(request)

    @app.get("/ui/live-flow", include_in_schema=False)
    async def ui_live_flow():
        try:
            return JSONResponse(_live_flow(cfg))
        except sqlite3.OperationalError:
            return JSONResponse({
                "collected_at": None,
                "age_seconds": None,
                "stale_after_seconds": int((cfg.get("collector") or {}).get("stale_after_seconds", 180)),
            })
