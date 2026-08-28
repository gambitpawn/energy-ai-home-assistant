from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .db import DB_PATH


LIVE_EXTENSION = r'''
<style>
.live-flow-card{margin:12px 0 0}.live-flow-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:4px}.live-flow-status{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:11px}.live-flow-status .lf-dot{width:7px;height:7px;border-radius:50%;background:var(--warn)}.live-flow-status.fresh .lf-dot{background:var(--good)}.live-flow-wrap{position:relative;min-height:270px}.live-flow-svg{width:100%;height:270px;display:block;overflow:visible}.lf-base{fill:none;stroke:#2d4051;stroke-width:5;stroke-linecap:round}.lf-active{fill:none;stroke-width:4;stroke-linecap:round;stroke-dasharray:3 12;opacity:.95;animation:lfDash var(--lf-speed,1.5s) linear infinite}.lf-active.reverse{animation-direction:reverse}.lf-active.idle{animation:none;stroke:#3a4c5d;stroke-dasharray:none;opacity:.45}.lf-node{fill:#172330;stroke:#314558;stroke-width:1.5}.lf-node-title{fill:#91a2b3;font-size:12px;text-anchor:middle}.lf-node-value{fill:#eef4f8;font-size:20px;font-weight:750;text-anchor:middle}.lf-node-sub{fill:#91a2b3;font-size:10px;text-anchor:middle}.lf-arrow{fill:#91a2b3;font-size:14px;font-weight:700;text-anchor:middle}.lf-pv{stroke:#ffbf5a}.lf-battery{stroke:#a78bfa}.lf-grid{stroke:#5ad5d5}.lf-ev{stroke:#ef9f63}.lf-note{color:var(--muted);font-size:10px;margin-top:-2px}.lf-off{opacity:.38}.lf-live-pill{display:inline-flex;align-items:center;gap:5px;padding:2px 7px;border-radius:999px;background:#14231d;color:var(--good);font-size:10px;font-weight:700}.lf-live-pill.off{background:#2a2020;color:var(--warn)}
.dev-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.dev-card a{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);color:var(--text);text-decoration:none}.dev-card a:last-child{border-bottom:0}.dev-card a:hover{color:var(--blue)}.dev-path{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;color:var(--muted);overflow-wrap:anywhere}.dev-arrow{color:var(--muted);font-size:12px}.dev-note{margin-bottom:12px}.dev-tab{opacity:.82}
@keyframes lfDash{to{stroke-dashoffset:-30}}@media(prefers-reduced-motion:reduce){.lf-active{animation:none;stroke-dasharray:none}}@media(max-width:1000px){.dev-grid{grid-template-columns:1fr 1fr}}@media(max-width:620px){.live-flow-svg{height:245px}.live-flow-wrap{min-height:245px}.lf-node-value{font-size:17px}.lf-node-title{font-size:11px}.dev-grid{grid-template-columns:1fr}}
</style>
<script>
let liveFlowTimer=null;
function liveFlowMarkup(){return `<div class="card live-flow-card" id="liveFlowCard"><div class="live-flow-head"><h2 style="margin:0">Live energy flow</h2><div class="live-flow-status" id="liveFlowStatus"><span class="lf-dot"></span><span>Loading live state…</span></div></div><div class="live-flow-wrap"><svg class="live-flow-svg" viewBox="0 0 1000 270" role="img" aria-label="Live power flow between solar PV, home, battery, grid and electric vehicle"><path class="lf-base" d="M500 72 L500 101"/><path id="lfPvPath" class="lf-active lf-pv idle" d="M500 72 L500 101"/><path class="lf-base" d="M245 135 L447 135"/><path id="lfBatteryPath" class="lf-active lf-battery idle" d="M245 135 L447 135"/><path class="lf-base" d="M553 135 L755 135"/><path id="lfGridPath" class="lf-active lf-grid idle" d="M553 135 L755 135"/><path class="lf-base" d="M500 169 L500 199"/><path id="lfEvPath" class="lf-active lf-ev idle" d="M500 169 L500 199"/><text id="lfPvArrow" class="lf-arrow" x="520" y="92">↓</text><text id="lfBatteryArrow" class="lf-arrow" x="348" y="126">→</text><text id="lfGridArrow" class="lf-arrow" x="652" y="126">←</text><text id="lfEvArrow" class="lf-arrow" x="520" y="192">↓</text><rect class="lf-node" x="432" y="12" width="136" height="60" rx="14"/><text class="lf-node-title" x="500" y="32">Solar PV</text><text id="lfPvValue" class="lf-node-value" x="500" y="56">—</text><rect class="lf-node" x="447" y="101" width="106" height="68" rx="16"/><text class="lf-node-title" x="500" y="122">Home</text><text id="lfHomeValue" class="lf-node-value" x="500" y="147">—</text><text class="lf-node-sub" x="500" y="161">total load</text><rect class="lf-node" x="105" y="101" width="140" height="68" rx="14"/><text class="lf-node-title" x="175" y="122">Battery</text><text id="lfBatteryValue" class="lf-node-value" x="175" y="146">—</text><text id="lfBatterySub" class="lf-node-sub" x="175" y="160">—</text><rect class="lf-node" x="755" y="101" width="140" height="68" rx="14"/><text class="lf-node-title" x="825" y="122">Grid</text><text id="lfGridValue" class="lf-node-value" x="825" y="146">—</text><text id="lfGridSub" class="lf-node-sub" x="825" y="160">—</text><g id="lfEvNode"><rect class="lf-node" x="430" y="199" width="140" height="68" rx="14"/><text class="lf-node-title" x="500" y="217">EV charger</text><text id="lfEvValue" class="lf-node-value" x="500" y="239">—</text><text id="lfEvSub" class="lf-node-sub" x="500" y="257">—</text></g></svg></div><div class="lf-note">EV is shown as a branch of total house load and is not added again to the energy balance.</div></div>`}
function lfNum(v){if(v===null||v===undefined||v==='')return null;const x=Number(v);return Number.isFinite(x)?x:null}
function lfPower(v){const x=lfNum(v);return x==null?'—':`${n(Math.abs(x),2)} kW`}
function lfSpeed(v){const x=Math.abs(Number(v)||0);return `${Math.max(.55,Math.min(2.8,2.4/(.35+x))).toFixed(2)}s`}
function lfSetPath(id,value,forward){const el=$(id),x=lfNum(value);if(!el)return;el.style.setProperty('--lf-speed',lfSpeed(x));el.classList.toggle('idle',x==null||Math.abs(x)<.05);el.classList.toggle('reverse',x!=null&&Math.abs(x)>=.05&&!forward)}
function evConnectedState(raw,power){if(Number(power)>.05)return true;if(raw==null)return null;if(typeof raw==='boolean')return raw;const s=String(raw).trim().toLowerCase();if(['off','false','0','disconnected','not_connected','unplugged','frånkopplad'].includes(s))return false;if(['on','true','1','connected','charging','ready','paused','waiting','ansluten','laddar','connected_charging','connected_requesting','connected_finished'].includes(s))return true;return null}
function renderLiveFlow(d){const pv=lfNum(d.pv_power_kw),load=lfNum(d.house_load_kw),grid=lfNum(d.grid_power_kw),bat=lfNum(d.battery_power_kw),soc=lfNum(d.battery_soc_pct),ev=lfNum(d.ev_power_kw),evSoc=lfNum(d.ev_soc_pct),connected=evConnectedState(d.ev_connected,ev);$('lfPvValue').textContent=lfPower(pv);$('lfHomeValue').textContent=lfPower(load);$('lfBatteryValue').textContent=lfPower(bat);$('lfGridValue').textContent=lfPower(grid);$('lfEvValue').textContent=lfPower(ev);$('lfBatterySub').textContent=soc==null?'SOC —':`SOC ${n(soc,1)}%`;const gridImport=grid!=null&&grid<-.05,gridExport=grid!=null&&grid>.05;$('lfGridSub').textContent=grid==null?'—':Math.abs(grid)<.05?'balanced':gridImport?'importing':'exporting';const batDischarge=bat!=null&&bat>0;lfSetPath('lfPvPath',pv,true);lfSetPath('lfBatteryPath',bat,batDischarge);lfSetPath('lfGridPath',grid,gridExport);lfSetPath('lfEvPath',ev,true);$('lfPvArrow').textContent=pv!=null&&pv>.05?'↓':'·';$('lfBatteryArrow').textContent=bat==null||Math.abs(bat)<.05?'·':batDischarge?'→':'←';$('lfGridArrow').textContent=grid==null||Math.abs(grid)<.05?'·':gridImport?'←':'→';$('lfEvArrow').textContent=ev==null||Math.abs(ev)<.05?'·':'↓';let evStatus=ev!=null&&Math.abs(ev)>.05?'Charging':connected===true?'Connected · idle':connected===false?'Disconnected':'Connection unknown';if(evSoc!=null)evStatus+=` · SOC ${n(evSoc,1)}%`;$('lfEvSub').textContent=evStatus;$('lfEvNode').classList.toggle('lf-off',connected===false&&!(ev!=null&&Math.abs(ev)>.05));const status=$('liveFlowStatus');status.classList.toggle('fresh',!!d.websocket_connected);status.querySelector('span:last-child').innerHTML=d.websocket_connected?'<span class="lf-live-pill">LIVE · WebSocket</span>':`<span class="lf-live-pill off">Reconnecting</span>${d.websocket_error?' · '+String(d.websocket_error).slice(0,90):''}`;const note=document.querySelector('#liveFlowCard .lf-note');if(note&&pv!=null&&load!=null&&bat!=null&&grid!=null){const residual=Math.abs(grid-(pv+bat-load));note.textContent=residual>.6?`EV is part of total house load and is not double-counted. Live balance residual ${n(residual,2)} kW — check source/sign conventions.`:'EV is shown as a branch of total house load and is not added again to the energy balance.'}}
async function loadLiveFlow(){try{renderLiveFlow(await api('ui/live-state'))}catch(e){const s=$('liveFlowStatus');if(s)s.querySelector('span:last-child').textContent='Live state unavailable'}}
function installLiveFlow(){const kpis=$('overviewKpis');if(!kpis||$('liveFlowCard'))return;kpis.insertAdjacentHTML('afterend',liveFlowMarkup());loadLiveFlow();if(liveFlowTimer)clearInterval(liveFlowTimer);liveFlowTimer=setInterval(()=>{if(document.querySelector('#overview.view.active'))loadLiveFlow()},1000)}
function devLink(label,path,description=''){return `<a href="${path}" target="_blank" rel="noopener"><span><strong>${label}</strong>${description?`<div class="dev-path">${description}</div>`:''}<div class="dev-path">${path}</div></span><span class="dev-arrow">↗</span></a>`}
function installDeveloperTab(){const tabs=$('tabs');if(!tabs||$('developer'))return;tabs.insertAdjacentHTML('beforeend','<button class="tab dev-tab" data-view="developer">Developer</button>');const footer=document.querySelector('.footer');if(!footer)return;footer.insertAdjacentHTML('beforebegin',`<section id="developer" class="view"><div class="notice dev-note"><strong>Developer tools.</strong> Raw diagnostics and API surfaces retained for commissioning and troubleshooting.</div><div class="dev-grid"><div class="card dev-card"><h2>API & health</h2>${devLink('Swagger / API docs','docs')}${devLink('OpenAPI JSON','openapi.json')}${devLink('Health','health')}${devLink('Configuration','config')}${devLink('HA diagnostics','ha-diagnostics')}</div><div class="card dev-card"><h2>Optimizer & models</h2>${devLink('Optimizer status','optimizer/status')}${devLink('Current optimizer plan','optimizer/plan')}${devLink('Optimizer history','optimizer/history')}${devLink('Engine registry','engines')}${devLink('Selector status','engines/selector/status')}${devLink('Selector scores','engines/selector/scores')}${devLink('Actuator status','actuator/status')}</div><div class="card dev-card"><h2>Forecasts & live state</h2>${devLink('Load forecast','forecast/load')}${devLink('PV forecast','forecast/pv')}${devLink('Prices','prices')}${devLink('Flexible loads','flexible-loads')}${devLink('Live-state cache','ui/live-state')}${devLink('EV awareness JSON','ui/ev-awareness')}${devLink('EV integration candidates','ui/ev-candidates')}</div></div></section>`)}
installLiveFlow();installDeveloperTab();$('tabs')?.addEventListener('click',e=>{if(e.target.closest('.tab')?.dataset.view==='overview')loadLiveFlow()});
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
    return {"collected_at": collected_at, "age_seconds": None if age is None else round(age, 1), "stale_after_seconds": stale_after, "pv_power_kw": _num(payload.get("pv_power_kw")), "house_load_kw": _num(payload.get("house_load_kw")), "grid_power_kw": _num(payload.get("grid_power_kw")), "battery_power_kw": _num(payload.get("battery_power_kw")), "battery_soc_pct": _num(payload.get("battery_soc_pct")), "ev_power_kw": _num(payload.get("ev_power_kw"))}


def _candidate_row(entity: dict[str, Any], score: int, role: str, source: str) -> dict[str, Any]:
    attrs = entity.get("attributes") or {}
    return {"role": role, "source": source, "score": score, "entity_id": entity.get("entity_id"), "friendly_name": attrs.get("friendly_name"), "state": entity.get("state"), "unit": attrs.get("unit_of_measurement"), "device_class": attrs.get("device_class")}


def _ev_candidates(states: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    ranked: dict[str, list[dict[str, Any]]] = {"ev_power": [], "ev_connected": [], "ev_soc": [], "ev_charging_status": []}
    for entity in states:
        eid = str(entity.get("entity_id") or ""); attrs = entity.get("attributes") or {}; name = str(attrs.get("friendly_name") or ""); unit = str(attrs.get("unit_of_measurement") or "").lower(); dc = str(attrs.get("device_class") or "").lower(); text = f"{eid} {name}".lower(); zaptec = "zaptec" in text or "zap" in text; polestar = "polestar" in text
        if zaptec:
            score = (40 if any(k in text for k in ("total_charge_power", "total charge power", "laddeffekt", "charge power")) else 0) + (10 if dc == "power" or unit in {"w", "kw"} else 0)
            if score >= 40: ranked["ev_power"].append(_candidate_row(entity, score, "ev_power", "zaptec"))
            score = (40 if any(k in text for k in ("charger_operation_mode", "charger operation mode", "laddstatus", "operation mode")) else 0) + (10 if any(k in str(entity.get("state") or "").lower() for k in ("connected", "charging", "disconnected")) else 0)
            if score >= 40: ranked["ev_connected"].append(_candidate_row(entity, score, "ev_connected", "zaptec"))
        if polestar:
            score = (45 if any(k in text for k in ("battery_charge_level", "battery level")) else 0) + (10 if dc == "battery" or unit == "%" else 0)
            if score >= 45: ranked["ev_soc"].append(_candidate_row(entity, score, "ev_soc", "polestar"))
            if any(k in text for k in ("charging_status", "charging status")): ranked["ev_charging_status"].append(_candidate_row(entity, 45, "ev_charging_status", "polestar"))
            if any(k in text for k in ("charger_connection_status", "charging connection status")): ranked["ev_connected"].append(_candidate_row(entity, 45, "ev_connected", "polestar"))
    for key in ranked:
        ranked[key] = sorted(ranked[key], key=lambda x: (-x["score"], str(x["entity_id"])))[:20]
    return {"configured": {k: (cfg.get("entities") or {}).get(k) for k in ("ev_power", "ev_connected", "ev_soc", "ev_target_soc", "ev_ready_by")}, "candidates": ranked, "selection_policy": "Zaptec is preferred for charger power/status; Polestar is preferred for vehicle SOC. Ambiguous candidates are not auto-selected."}


def install_live_routes(app: FastAPI, cfg: dict[str, Any], live_cache: Any, ha: Any) -> None:
    @app.get("/ui/live-flow", include_in_schema=False)
    async def ui_live_flow():
        try:
            return JSONResponse(_live_flow(cfg))
        except sqlite3.OperationalError:
            return JSONResponse({"collected_at": None, "age_seconds": None, "stale_after_seconds": int((cfg.get("collector") or {}).get("stale_after_seconds", 180))})

    @app.get("/ui/live-state", include_in_schema=False)
    async def ui_live_state():
        snap = live_cache.snapshot(); values = snap.get("values") or {}
        return JSONResponse({"pv_power_kw": values.get("pv_power_kw"), "house_load_kw": values.get("house_load_kw"), "grid_power_kw": values.get("grid_power_kw"), "battery_power_kw": values.get("battery_power_kw"), "battery_soc_pct": values.get("battery_soc_pct"), "ev_power_kw": values.get("ev_power_kw"), "ev_connected": values.get("ev_connected"), "ev_soc_pct": values.get("ev_soc_pct"), "ev_target_soc_pct": values.get("ev_target_soc_pct"), "ev_ready_by": values.get("ev_ready_by"), "ev_charging_status": values.get("ev_charging_status"), "websocket_connected": bool(snap.get("connected")), "websocket_connected_at": snap.get("connected_at"), "websocket_last_event_at": snap.get("last_event_at"), "websocket_error": snap.get("last_error"), "websocket_reconnects": snap.get("reconnects"), "websocket_bootstrap_at": snap.get("bootstrap_at"), "websocket_bootstrap_matched": snap.get("bootstrap_matched"), "transport": snap.get("transport"), "field_entities": snap.get("field_entities"), "mapping_origin": snap.get("mapping_origin"), "configured_entities": snap.get("configured_entities"), "source_updated": snap.get("source_updated"), "served_at": snap.get("served_at")})

    @app.get("/ui/ev-candidates", include_in_schema=False)
    async def ui_ev_candidates():
        try:
            return JSONResponse(_ev_candidates(await ha.all_states(), cfg))
        except Exception as exc:
            return JSONResponse({"error": repr(exc), "configured": (cfg.get("entities") or {})}, status_code=500)

    @app.get("/ui/ev-awareness", include_in_schema=False)
    async def ui_ev_awareness():
        snap = live_cache.snapshot(); values = snap.get("values") or {}; fields = ("ev_power_kw", "ev_connected", "ev_soc_pct", "ev_target_soc_pct", "ev_ready_by", "ev_charging_status")
        return JSONResponse({"values": {k: values.get(k) for k in fields}, "entities": {k: (snap.get("field_entities") or {}).get(k) for k in fields}, "mapping_origin": {k: (snap.get("mapping_origin") or {}).get(k) for k in fields}, "source_updated": {k: (snap.get("source_updated") or {}).get(k) for k in fields}, "websocket_connected": bool(snap.get("connected")), "bootstrap_at": snap.get("bootstrap_at"), "policy": "Explicit configuration wins. Unique Zaptec candidates may fill charger power/status; unique Polestar candidates may fill vehicle SOC/status. Ambiguous candidates are not selected."})
