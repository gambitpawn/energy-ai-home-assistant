from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import DASHBOARD_HTML
from .overview_extension import OVERVIEW_EXTENSION
from .ui_v158 import V158_EXTENSION
from .ui_v159 import V159_EXTENSION


V160_EXTENSION = r'''
<style>
.lf-live-pill{display:inline-flex;align-items:center;gap:5px;padding:2px 7px;border-radius:999px;background:#14231d;color:var(--good);font-size:10px;font-weight:700}.lf-live-pill.off{background:#2a2020;color:var(--warn)}
</style>
<script>
function ensureEvSub(){
  const group=$('lfEvNode');if(!group||$('lfEvSub'))return;
  const rect=group.querySelector('rect'),title=group.querySelector('.lf-node-title'),value=$('lfEvValue');
  if(rect){rect.setAttribute('y','199');rect.setAttribute('height','68')}
  if(title)title.setAttribute('y','217');
  if(value)value.setAttribute('y','239');
  const ns='http://www.w3.org/2000/svg',sub=document.createElementNS(ns,'text');
  sub.setAttribute('id','lfEvSub');sub.setAttribute('class','lf-node-sub');sub.setAttribute('x','500');sub.setAttribute('y','257');sub.textContent='—';group.appendChild(sub);
}

function evConnectedState(raw,power){
  if(Number(power)>.05)return true;
  if(raw==null)return null;
  if(typeof raw==='boolean')return raw;
  const s=String(raw).trim().toLowerCase();
  if(['off','false','0','disconnected','not_connected','unplugged','frånkopplad'].includes(s))return false;
  if(['on','true','1','connected','charging','ready','paused','waiting','ansluten','laddar'].includes(s))return true;
  return null;
}

const renderLiveFlow159=renderLiveFlow;
renderLiveFlow=function(d){
  renderLiveFlow159(d);ensureEvSub();
  const ev=lfNum(d.ev_power_kw),evSoc=lfNum(d.ev_soc_pct),connected=evConnectedState(d.ev_connected,ev),sub=$('lfEvSub');
  let status=ev!=null&&Math.abs(ev)>.05?'Charging':connected===true?'Connected · idle':connected===false?'Disconnected':'Connection unknown';
  if(evSoc!=null)status+=` · SOC ${n(evSoc,1)}%`;
  if(sub)sub.textContent=status;
  const node=$('lfEvNode');if(node)node.classList.toggle('lf-off',connected===false&&!(ev!=null&&Math.abs(ev)>.05));
  const live=$('liveFlowStatus');
  if(live){
    live.classList.toggle('fresh',!!d.websocket_connected);
    live.querySelector('span:last-child').innerHTML=d.websocket_connected?'<span class="lf-live-pill">LIVE · WebSocket</span>':`<span class="lf-live-pill off">Reconnecting</span>${d.websocket_error?' · '+String(d.websocket_error).slice(0,90):''}`;
  }
};

loadLiveFlow=async function(){
  try{renderLiveFlow(await api('ui/live-state'))}
  catch(e){const s=$('liveFlowStatus');if(s)s.querySelector('span:last-child').textContent='Live state unavailable'}
};

// Browser polls only the add-on's in-memory cache. Home Assistant itself is event-driven over WebSocket.
if(liveFlowTimer)clearInterval(liveFlowTimer);
liveFlowTimer=setInterval(()=>{if(document.querySelector('#overview.view.active'))loadLiveFlow()},1000);
ensureEvSub();loadLiveFlow();

// Add the cache endpoint to the temporary Developer tab when present.
setTimeout(()=>{
  const cards=[...document.querySelectorAll('#developer .dev-card')];
  const card=cards[cards.length-1];
  if(card&&!card.querySelector('a[href="ui/live-state"]'))card.insertAdjacentHTML('beforeend',devLink('Live-state cache','ui/live-state','WebSocket-backed in-memory values used by Overview'));
},0);
</script>
'''


def install_ui_v160(app: FastAPI, live_cache: Any) -> None:
    @app.middleware("http")
    async def ui_v160(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace(
                "</body>",
                OVERVIEW_EXTENSION + V158_EXTENSION + V159_EXTENSION + V160_EXTENSION + "</body>",
            )
            return HTMLResponse(html)
        return await call_next(request)

    @app.get("/ui/live-state", include_in_schema=False)
    async def ui_live_state():
        snap = live_cache.snapshot()
        values = snap.get("values") or {}
        return JSONResponse({
            "pv_power_kw": values.get("pv_power_kw"),
            "house_load_kw": values.get("house_load_kw"),
            "grid_power_kw": values.get("grid_power_kw"),
            "battery_power_kw": values.get("battery_power_kw"),
            "battery_soc_pct": values.get("battery_soc_pct"),
            "ev_power_kw": values.get("ev_power_kw"),
            "ev_connected": values.get("ev_connected"),
            "ev_soc_pct": values.get("ev_soc_pct"),
            "websocket_connected": bool(snap.get("connected")),
            "websocket_connected_at": snap.get("connected_at"),
            "websocket_last_event_at": snap.get("last_event_at"),
            "websocket_error": snap.get("last_error"),
            "websocket_reconnects": snap.get("reconnects"),
            "websocket_bootstrap_at": snap.get("bootstrap_at"),
            "websocket_bootstrap_matched": snap.get("bootstrap_matched"),
            "transport": snap.get("transport"),
            "field_entities": snap.get("field_entities"),
            "configured_entities": snap.get("configured_entities"),
            "source_updated": snap.get("source_updated"),
            "served_at": snap.get("served_at"),
        })
