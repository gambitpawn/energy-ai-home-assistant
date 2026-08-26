from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import DASHBOARD_HTML
from .overview_extension import OVERVIEW_EXTENSION
from .ui_v158 import V158_EXTENSION
from .ui_v159 import V159_EXTENSION
from .ui_v160 import V160_EXTENSION


V161_EXTENSION = r'''
<style>
.decision-card{margin:12px 0 0}.decision-grid{display:grid;grid-template-columns:1.15fr 1fr 1fr;gap:0}.decision-col{padding:2px 18px;min-width:0}.decision-col:first-child{padding-left:0}.decision-col+.decision-col{border-left:1px solid var(--line)}.decision-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}.shadow-pill{display:inline-flex;align-items:center;padding:3px 8px;border:1px solid #5a4d2c;border-radius:999px;color:var(--warn);font-size:10px;font-weight:750;letter-spacing:.03em}.decision-action{font-size:27px;font-weight:780;margin:2px 0 5px}.decision-reason{color:var(--muted);font-size:12px;min-height:34px}.decision-row{display:flex;justify-content:space-between;gap:14px;padding:6px 0;border-bottom:1px solid var(--line);font-size:12px}.decision-row:last-child{border-bottom:0}.decision-row span{color:var(--muted)}.decision-row strong{text-align:right;font-variant-numeric:tabular-nums}.exec-diff.ok{color:var(--good)}.exec-diff.warn{color:var(--warn)}.exec-diff.bad{color:var(--bad)}
@media(max-width:900px){.decision-grid{grid-template-columns:1fr}.decision-col{padding:12px 0}.decision-col+.decision-col{border-left:0;border-top:1px solid var(--line)}}
</style>
<script>
function decisionMarkup(){return `<div class="card decision-card" id="currentDecisionCard">
  <div class="decision-head"><h2 style="margin:0">Current decision</h2><span class="shadow-pill">SHADOW · READ-ONLY</span></div>
  <div class="decision-grid">
    <div class="decision-col"><div class="muted" style="font-size:11px">Optimizer action</div><div id="decisionAction" class="decision-action">—</div><div id="decisionReason" class="decision-reason">Waiting for current plan…</div><div class="decision-row"><span>Interval</span><strong id="decisionInterval">—</strong></div><div class="decision-row"><span>Plan age</span><strong id="decisionAge">—</strong></div></div>
    <div class="decision-col"><div class="decision-row"><span>Expected SOC end</span><strong id="decisionSoc">—</strong></div><div class="decision-row"><span>Spot price</span><strong id="decisionPrice">—</strong></div><div class="decision-row"><span>Reserve target</span><strong id="decisionReserve">—</strong></div><div class="decision-row"><span>Next planned change</span><strong id="decisionNext">—</strong></div></div>
    <div class="decision-col"><div class="muted" style="font-size:11px;margin-bottom:4px">Execution status</div><div class="decision-row"><span>Planned battery</span><strong id="execPlanned">—</strong></div><div class="decision-row"><span>Actual battery</span><strong id="execActual">—</strong></div><div class="decision-row"><span>Difference</span><strong id="execDiff" class="exec-diff">—</strong></div><div class="decision-row"><span>Status</span><strong id="execStatus">Observation only</strong></div></div>
  </div>
</div>`}

function installCurrentDecision(){const flow=$('liveFlowCard');if(!flow||$('currentDecisionCard'))return;flow.insertAdjacentHTML('afterend',decisionMarkup())}
function rowStartMs(r){const x=Date.parse(r?.start||r?.start_utc||'');return Number.isFinite(x)?x:null}
function actionText(v){const x=Number(v);if(!Number.isFinite(x))return '—';if(Math.abs(x)<.05)return 'Idle';return x<0?`Charge ${n(Math.abs(x),2)} kW`:`Discharge ${n(x,2)} kW`}
function reasonText(raw){
  const s=String(raw||'').trim();if(!s)return 'No planner reason recorded for this interval.';
  const map={mixed_charge:'Charge to prepare for expected demand / economics',mixed_discharge:'Discharge according to current economic plan',price_charge:'Charge while energy is comparatively cheap',price_discharge:'Discharge while energy is comparatively valuable',pv_charge:'Store available solar production',pv_surplus_charge:'Store surplus solar production',reserve_charge:'Charge to restore battery reserve',reserve_hold:'Hold energy to preserve reserve',idle:'No battery action is currently preferred',hold:'Hold battery at current state',import_cap_discharge:'Discharge to reduce grid import',export_limit_charge:'Charge to reduce grid export'};
  return map[s]||s.replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
}
function clockRange(startMs){if(startMs==null)return '—';const a=new Date(startMs),b=new Date(startMs+15*60000),fmt=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Stockholm',hour:'2-digit',minute:'2-digit'});return `${fmt.format(a)}–${fmt.format(b)}`}
function ageText(ts){const ms=Date.parse(ts||'');if(!Number.isFinite(ms))return '—';const sec=Math.max(0,(Date.now()-ms)/1000);if(sec<90)return `${Math.round(sec)} s`;if(sec<5400)return `${Math.round(sec/60)} min`;return `${n(sec/3600,1)} h`}
function decisionRows(){return pRows().map(r=>({...r,_ms:rowStartMs(r)})).filter(r=>r._ms!=null).sort((a,b)=>a._ms-b._ms)}
function currentDecisionRow(){const rs=decisionRows(),now=Date.now();return rs.find(r=>r._ms<=now&&now<r._ms+15*60000)||rs.find(r=>r._ms>now)||null}
function nextDecisionChange(current){if(!current)return null;const rs=decisionRows(),idx=rs.findIndex(r=>r._ms===current._ms),a=Number(current.battery_action_kw??current.action_kw??0),reason=current.reason||'';for(let i=idx+1;i<rs.length;i++){const b=Number(rs[i].battery_action_kw??rs[i].action_kw??0);if(Math.abs(b-a)>.25||(rs[i].reason||'')!==reason)return rs[i]}return null}
function executionClass(diff){const x=Math.abs(Number(diff));return x<=.35?'ok':x<=1.0?'warn':'bad'}
function renderCurrentDecision(live){
  installCurrentDecision();const r=currentDecisionRow();if(!r)return;
  const action=Number(r.battery_action_kw??r.action_kw),actual=Number(live?.battery_power_kw),diff=Number.isFinite(action)&&Number.isFinite(actual)?actual-action:null,next=nextDecisionChange(r);
  $('decisionAction').textContent=actionText(action);$('decisionReason').textContent=reasonText(r.reason);$('decisionInterval').textContent=clockRange(r._ms);$('decisionAge').textContent=ageText(state.plan?.generated_at);
  const soc=r.expected_soc_pct??r.soc_end_pct;$('decisionSoc').textContent=soc==null?'—':`${n(soc,1)}%`;const price=r.price_ore_kwh??r.forecast_price_ore_kwh;$('decisionPrice').textContent=price==null?'—':`${n(price,1)} öre/kWh`;const reserve=r.reserve_soc_pct??state.plan?.reserve_soc_pct;$('decisionReserve').textContent=reserve==null?'—':`${n(reserve,1)}%`;
  $('decisionNext').textContent=next?`${tlabel(new Date(next._ms).toISOString())} · ${actionText(next.battery_action_kw??next.action_kw)}`:'No change in current horizon';$('execPlanned').textContent=actionText(action);$('execActual').textContent=Number.isFinite(actual)?actionText(actual):'—';
  const de=$('execDiff');de.textContent=diff==null?'—':`${n(Math.abs(diff),2)} kW`;de.className=`exec-diff ${diff==null?'':executionClass(diff)}`;$('execStatus').textContent=diff==null?'No live battery value':Math.abs(diff)<=.35?'Tracking plan':Math.abs(diff)<=1.0?'Actual differs from plan':'Large difference from plan';
}

const renderLiveFlow160=renderLiveFlow;
renderLiveFlow=function(d){renderLiveFlow160(d);renderCurrentDecision(d)};
installCurrentDecision();renderCurrentDecision(null);

setTimeout(()=>{const cards=[...document.querySelectorAll('#developer .dev-card')],card=cards[cards.length-1];if(card&&!card.querySelector('a[href="ui/ev-candidates"]'))card.insertAdjacentHTML('beforeend',devLink('EV integration candidates','ui/ev-candidates','Zaptec charger + Polestar vehicle entity suggestions'))},0);
</script>
'''


def _candidate_row(entity: dict[str, Any], score: int, role: str, source: str) -> dict[str, Any]:
    attrs = entity.get("attributes") or {}
    return {
        "role": role,
        "source": source,
        "score": score,
        "entity_id": entity.get("entity_id"),
        "friendly_name": attrs.get("friendly_name"),
        "state": entity.get("state"),
        "unit": attrs.get("unit_of_measurement"),
        "device_class": attrs.get("device_class"),
    }


def _ev_candidates(states: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    ranked: dict[str, list[dict[str, Any]]] = {"ev_power": [], "ev_connected": [], "ev_soc": [], "ev_charging_status": []}
    for entity in states:
        eid = str(entity.get("entity_id") or "")
        attrs = entity.get("attributes") or {}
        name = str(attrs.get("friendly_name") or "")
        unit = str(attrs.get("unit_of_measurement") or "").lower()
        dc = str(attrs.get("device_class") or "").lower()
        text = f"{eid} {name}".lower()
        zaptec = "zaptec" in text or "zap" in text
        polestar = "polestar" in text

        if zaptec:
            score = 0
            if any(k in text for k in ("total_charge_power", "total charge power", "laddeffekt", "charge power")): score += 40
            if dc == "power" or unit in {"w", "kw"}: score += 10
            if score >= 40: ranked["ev_power"].append(_candidate_row(entity, score, "ev_power", "zaptec"))
            score = 0
            if any(k in text for k in ("charger_operation_mode", "charger operation mode", "laddstatus", "operation mode")): score += 40
            if any(k in str(entity.get("state") or "").lower() for k in ("connected", "charging", "disconnected")): score += 10
            if score >= 40: ranked["ev_connected"].append(_candidate_row(entity, score, "ev_connected", "zaptec"))

        if polestar:
            score = 0
            if any(k in text for k in ("battery_charge_level", "battery level")): score += 45
            if dc == "battery" or unit == "%": score += 10
            if score >= 45: ranked["ev_soc"].append(_candidate_row(entity, score, "ev_soc", "polestar"))
            score = 0
            if any(k in text for k in ("charging_status", "charging status")): score += 45
            if score >= 45: ranked["ev_charging_status"].append(_candidate_row(entity, score, "ev_charging_status", "polestar"))
            score = 0
            if any(k in text for k in ("charger_connection_status", "charging connection status")): score += 45
            if score >= 45: ranked["ev_connected"].append(_candidate_row(entity, score, "ev_connected", "polestar"))

    for key in ranked:
        ranked[key] = sorted(ranked[key], key=lambda x: (-x["score"], str(x["entity_id"])))[:20]
    return {"configured": {k: (cfg.get("entities") or {}).get(k) for k in ("ev_power", "ev_connected", "ev_soc", "ev_target_soc", "ev_ready_by")}, "candidates": ranked, "selection_policy": "Zaptec is preferred for charger power/status; Polestar is preferred for vehicle SOC. Candidates are not auto-selected when multiple vehicles or chargers may exist."}


def install_ui_v161(app: FastAPI, cfg: dict[str, Any], ha: Any) -> None:
    @app.middleware("http")
    async def ui_v161(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace("</body>", OVERVIEW_EXTENSION + V158_EXTENSION + V159_EXTENSION + V160_EXTENSION + V161_EXTENSION + "</body>")
            return HTMLResponse(html)
        return await call_next(request)

    @app.get("/ui/ev-candidates", include_in_schema=False)
    async def ui_ev_candidates():
        try:
            return JSONResponse(_ev_candidates(await ha.all_states(), cfg))
        except Exception as exc:
            return JSONResponse({"error": repr(exc), "configured": (cfg.get("entities") or {})}, status_code=500)
