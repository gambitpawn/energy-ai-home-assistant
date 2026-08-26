from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import DASHBOARD_HTML
from .overview_extension import OVERVIEW_EXTENSION
from .ui_v158 import V158_EXTENSION
from .ui_v159 import V159_EXTENSION
from .ui_v160 import V160_EXTENSION
from .ui_v161 import V161_EXTENSION
from .ui_v161_fix import V161_FIX_EXTENSION


V163_EXTENSION = r'''
<style>
.ev-awareness-card{margin:12px 0 0}.ev-awareness-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:9px}.ev-observe-pill{display:inline-flex;align-items:center;padding:3px 8px;border:1px solid #344c5d;border-radius:999px;color:var(--muted);font-size:10px;font-weight:700}.ev-awareness-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:0}.ev-awareness-stat{padding:2px 15px;min-width:0}.ev-awareness-stat:first-child{padding-left:0}.ev-awareness-stat+.ev-awareness-stat{border-left:1px solid var(--line)}.ev-awareness-label{font-size:10px;color:var(--muted);margin-bottom:3px}.ev-awareness-value{font-size:17px;font-weight:720;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ev-awareness-sub{font-size:9px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ev-awareness-source{margin-top:10px;padding-top:8px;border-top:1px solid var(--line);font-size:9px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}
@media(max-width:900px){.ev-awareness-grid{grid-template-columns:1fr 1fr}.ev-awareness-stat{padding:9px 12px}.ev-awareness-stat:nth-child(odd){padding-left:0}.ev-awareness-stat+.ev-awareness-stat{border-left:0}.ev-awareness-stat:nth-child(even){border-left:1px solid var(--line)}}
</style>
<script>
function evAwarenessMarkup(){return `<div class="card ev-awareness-card" id="evAwarenessCard">
  <div class="ev-awareness-head"><h2 style="margin:0">EV awareness</h2><span class="ev-observe-pill">OBSERVATION ONLY</span></div>
  <div class="ev-awareness-grid">
    <div class="ev-awareness-stat"><div class="ev-awareness-label">Vehicle SOC</div><div class="ev-awareness-value" id="evAwareSoc">—</div><div class="ev-awareness-sub" id="evAwareSocSub">Polestar</div></div>
    <div class="ev-awareness-stat"><div class="ev-awareness-label">Charging status</div><div class="ev-awareness-value" id="evAwareStatus">—</div><div class="ev-awareness-sub" id="evAwareStatusSub">Zaptec / vehicle</div></div>
    <div class="ev-awareness-stat"><div class="ev-awareness-label">Charge power</div><div class="ev-awareness-value" id="evAwarePower">—</div><div class="ev-awareness-sub" id="evAwareImpact">—</div></div>
    <div class="ev-awareness-stat"><div class="ev-awareness-label">Target SOC</div><div class="ev-awareness-value" id="evAwareTarget">—</div><div class="ev-awareness-sub">when configured</div></div>
    <div class="ev-awareness-stat"><div class="ev-awareness-label">Ready by</div><div class="ev-awareness-value" id="evAwareReady">—</div><div class="ev-awareness-sub">when configured</div></div>
  </div>
  <div class="ev-awareness-source" id="evAwareSources">Waiting for EV entity mapping…</div>
</div>`}

function installEvAwareness(){
  const flow=$('liveFlowCard');if(!flow||$('evAwarenessCard'))return;
  flow.insertAdjacentHTML('afterend',evAwarenessMarkup());
  const exec=$('execStatus')?.closest('.decision-col');
  if(exec&&!$('decisionEvImpact'))exec.insertAdjacentHTML('beforeend','<div class="decision-row"><span>EV load</span><strong id="decisionEvImpact">—</strong></div>');
}
function evPretty(raw){
  if(raw==null||raw==='')return null;
  const s=String(raw).trim().replaceAll('_',' ');
  return s.replace(/^./,c=>c.toUpperCase());
}
function evReadyText(raw){
  if(raw==null||raw==='')return '—';
  const ms=Date.parse(String(raw));
  if(!Number.isFinite(ms))return String(raw);
  return new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Stockholm',weekday:'short',hour:'2-digit',minute:'2-digit'}).format(new Date(ms));
}
function evSourceShort(entity){
  if(!entity)return '—';
  const s=String(entity);if(s.includes('polestar'))return 'Polestar';if(s.includes('zap'))return 'Zaptec';return s;
}
function renderEvAwareness(d){
  installEvAwareness();
  const power=lfNum(d.ev_power_kw),soc=lfNum(d.ev_soc_pct),target=lfNum(d.ev_target_soc_pct),load=lfNum(d.house_load_kw),connected=evConnectedState(d.ev_connected,power),vehicleStatus=evPretty(d.ev_charging_status);
  let status=power!=null&&Math.abs(power)>.05?'Charging':vehicleStatus||(connected===true?'Connected · idle':connected===false?'Disconnected':'Unknown');
  $('evAwareSoc').textContent=soc==null?'—':`${n(soc,1)}%`;
  $('evAwareStatus').textContent=status;
  $('evAwarePower').textContent=power==null?'—':`${n(Math.abs(power),2)} kW`;
  $('evAwareTarget').textContent=target==null?'—':`${n(target,1)}%`;
  $('evAwareReady').textContent=evReadyText(d.ev_ready_by);
  const share=power!=null&&load!=null&&load>.05?Math.max(0,Math.min(999,Math.abs(power)/load*100)):null;
  $('evAwareImpact').textContent=power!=null&&Math.abs(power)>.05?(share==null?'contributes to house load':`${n(share,0)}% of current house load`):'no current EV load';
  const entities=d.field_entities||{},origins=d.mapping_origin||{};
  $('evAwareSocSub').textContent=entities.ev_soc_pct?`${evSourceShort(entities.ev_soc_pct)} · ${origins.ev_soc_pct||'mapped'}`:'Polestar SOC not mapped';
  $('evAwareStatusSub').textContent=entities.ev_connected?`${evSourceShort(entities.ev_connected)} charger status`:(entities.ev_charging_status?`${evSourceShort(entities.ev_charging_status)} vehicle status`:'status not mapped');
  const parts=[];
  for(const [label,key] of [['power','ev_power_kw'],['charger','ev_connected'],['vehicle SOC','ev_soc_pct'],['vehicle status','ev_charging_status']]){
    if(entities[key])parts.push(`${label}: ${entities[key]} (${origins[key]||'mapped'})`);
  }
  $('evAwareSources').textContent=parts.length?parts.join(' · '):'No EV entities mapped.';
  const impact=$('decisionEvImpact');if(impact)impact.textContent=power!=null&&Math.abs(power)>.05?`${n(Math.abs(power),2)} kW${share==null?'':` · ${n(share,0)}% house load`}`:'No current EV load';
}

const renderLiveFlow162=renderLiveFlow;
renderLiveFlow=function(d){renderLiveFlow162(d);renderEvAwareness(d)};
installEvAwareness();

setTimeout(()=>{
  const cards=[...document.querySelectorAll('#developer .dev-card')],card=cards[cards.length-1];
  if(card&&!card.querySelector('a[href="ui/ev-awareness"]'))card.insertAdjacentHTML('beforeend',devLink('EV awareness JSON','ui/ev-awareness','Resolved Zaptec / Polestar mapping and current EV values'));
},0);
loadLiveFlow();
</script>
'''


def install_ui_v163(app: FastAPI, live_cache: Any) -> None:
    @app.middleware("http")
    async def ui_v163(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace(
                "</body>",
                OVERVIEW_EXTENSION
                + V158_EXTENSION
                + V159_EXTENSION
                + V160_EXTENSION
                + V161_EXTENSION
                + V161_FIX_EXTENSION
                + V163_EXTENSION
                + "</body>",
            )
            return HTMLResponse(html)
        return await call_next(request)

    @app.get("/ui/ev-awareness", include_in_schema=False)
    async def ui_ev_awareness():
        snap = live_cache.snapshot()
        values = snap.get("values") or {}
        fields = ("ev_power_kw", "ev_connected", "ev_soc_pct", "ev_target_soc_pct", "ev_ready_by", "ev_charging_status")
        return JSONResponse({
            "values": {k: values.get(k) for k in fields},
            "entities": {k: (snap.get("field_entities") or {}).get(k) for k in fields},
            "mapping_origin": {k: (snap.get("mapping_origin") or {}).get(k) for k in fields},
            "source_updated": {k: (snap.get("source_updated") or {}).get(k) for k in fields},
            "websocket_connected": bool(snap.get("connected")),
            "bootstrap_at": snap.get("bootstrap_at"),
            "policy": "Explicit add-on configuration wins. Unique Zaptec candidates may fill charger power/status; unique Polestar candidates may fill vehicle SOC/status. Ambiguous candidates are not selected.",
        })
