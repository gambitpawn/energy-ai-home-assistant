from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import ui_v164
from .dashboard import DASHBOARD_HTML
from .db import DB_PATH
from .engine_store import competition_rows
from .optimizer_store import latest_plan
from .overview_extension import OVERVIEW_EXTENSION
from .production_state import active_overrides, cancel_override, create_override, scheduled_overrides, set_mode, status
from .settings_store import load_setting_overrides
from .ui_v158 import V158_EXTENSION
from .ui_v159 import V159_EXTENSION
from .ui_v160 import V160_EXTENSION
from .ui_v161 import V161_EXTENSION
from .ui_v161_fix import V161_FIX_EXTENSION
from .ui_v163 import V163_EXTENSION
from .ui_v164 import V164_EXTENSION
from .ui_v165 import V165_EXTENSION
from .ui_v180 import V180_EXTENSION

OPTIONS_PATH = Path('/data/options.json')
WINDOW_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}


def _install_production_parameters() -> None:
    if any(p.get("key") == "sauna_default_duration_minutes" for p in ui_v164.PARAMETERS):
        return
    meta = ui_v164.p(
        "Flexible loads",
        "sauna_default_duration_minutes",
        "Sauna default duration",
        "int",
        120,
        "Default run duration used by the Overview Sauna now quick control.",
        unit="min",
        recommended="120 minutes is the selected household default; adjust if normal sauna sessions change.",
        minimum=15,
        maximum=360,
        step=15,
    )
    ui_v164.PARAMETERS.append(meta)
    ui_v164.PARAM_BY_KEY[meta["key"]] = meta


_install_production_parameters()


def _raw_options() -> dict[str, Any]:
    try:
        raw = json.loads(OPTIONS_PATH.read_text(encoding="utf-8")) if OPTIONS_PATH.exists() else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _effective_setting(key: str, default: Any) -> Any:
    overrides = load_setting_overrides()
    if key in overrides:
        return overrides[key]
    return _raw_options().get(key, default)


def _dt(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _reason_text(raw: Any) -> str:
    s = str(raw or "").strip()
    mapping = {
        "mixed_charge": "Charge now to prepare for expected demand and later energy value.",
        "mixed_discharge": "Discharge now because stored energy has higher value in this interval.",
        "price_charge": "Charge while imported energy is comparatively cheap.",
        "price_discharge": "Discharge while grid energy is comparatively expensive.",
        "pv_charge": "Store available solar production.",
        "pv_surplus_charge": "Store surplus solar production instead of exporting it.",
        "reserve_charge": "Charge to restore the required battery reserve.",
        "reserve_hold": "Preserve battery energy to maintain reserve.",
        "idle": "No battery action currently improves the plan enough to justify cycling.",
        "hold": "Hold the battery at its current state.",
        "import_cap_discharge": "Discharge to reduce grid import near the configured limit.",
        "export_limit_charge": "Charge to avoid exceeding the configured export limit.",
    }
    return mapping.get(s, s.replace("_", " ").capitalize() if s else "No structured reason recorded.")


def _decision_summary() -> dict[str, Any]:
    plan = latest_plan(500)
    rows = list(plan.get("rows") or [])
    now = datetime.now(timezone.utc)
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        try:
            parsed.append((_dt(row.get("start") or row.get("start_utc")), row))
        except Exception:
            continue
    parsed.sort(key=lambda x: x[0])
    current = None
    for stamp, row in parsed:
        if stamp <= now < stamp + timedelta(minutes=15):
            current = (stamp, row)
            break
    if current is None:
        current = next(((s, r) for s, r in parsed if s > now), None)
    nxt = None
    if current:
        cur_action = float(current[1].get("battery_action_kw", current[1].get("action_kw", 0.0)) or 0.0)
        cur_reason = str(current[1].get("reason") or "")
        passed = False
        for stamp, row in parsed:
            if stamp == current[0]:
                passed = True
                continue
            if not passed:
                continue
            action = float(row.get("battery_action_kw", row.get("action_kw", 0.0)) or 0.0)
            reason = str(row.get("reason") or "")
            if abs(action - cur_action) > 0.25 or reason != cur_reason:
                nxt = (stamp, row)
                break

    def item(pair):
        if not pair:
            return None
        stamp, row = pair
        action = float(row.get("battery_action_kw", row.get("action_kw", 0.0)) or 0.0)
        return {
            "start": stamp.isoformat(),
            "action_kw": action,
            "action": "idle" if abs(action) < 0.05 else "charge" if action < 0 else "discharge",
            "reason_code": row.get("reason"),
            "reason": _reason_text(row.get("reason")),
        }

    return {"generated_at": plan.get("generated_at"), "current": item(current), "next": item(nxt)}


def _model_comparison(window: str) -> dict[str, Any]:
    days = WINDOW_DAYS.get(window, 7)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    competitions = competition_rows(start.isoformat(), end.isoformat())
    behaviour: dict[str, list[dict[str, Any]]] = {}
    for item in competitions:
        stamp = item.get("decision_start")
        for engine_id, decision in (item.get("decisions") or {}).items():
            behaviour.setdefault(engine_id, []).append({
                "start": stamp,
                "requested_action_kw": decision.get("requested_action_kw"),
                "expected_soc_pct": decision.get("expected_soc_pct"),
                "status": decision.get("status"),
            })

    cutoff_date = (datetime.now().date() - timedelta(days=days)).isoformat()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT local_date,engine_id,intervals,mean_regret_ore,p90_regret_ore,clamp_rate
               FROM engine_daily_score WHERE local_date>=? ORDER BY local_date,engine_id''',
            (cutoff_date,),
        ).fetchall()
    economics: dict[str, list[dict[str, Any]]] = {}
    cumulative: dict[str, float] = {}
    for local_date, engine_id, intervals, mean_regret, p90_regret, clamp_rate in rows:
        daily_regret_sek = float(mean_regret) * int(intervals) / 100.0
        cumulative[str(engine_id)] = cumulative.get(str(engine_id), 0.0) + daily_regret_sek
        economics.setdefault(str(engine_id), []).append({
            "date": local_date,
            "intervals": int(intervals),
            "mean_regret_ore": float(mean_regret),
            "p90_regret_ore": float(p90_regret),
            "clamp_rate": float(clamp_rate),
            "daily_oracle_regret_sek": round(daily_regret_sek, 4),
            "cumulative_oracle_regret_sek": round(cumulative[str(engine_id)], 4),
        })
    return {
        "window": window,
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "economics": economics,
        "behaviour": behaviour,
        "metric_note": "Economic comparison is realized oracle regret on shared historical conditions; lower is better.",
    }


V183_EXTENSION = r'''
<style>
.prod-strip{display:grid;grid-template-columns:1fr 1.4fr 1.4fr;gap:10px;margin:0 0 12px}.prod-cell{background:#0f1720;border:1px solid var(--line);border-radius:12px;padding:10px 12px}.prod-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.prod-value{font-size:15px;font-weight:720;margin-top:2px}.prod-reason{font-size:11px;color:var(--muted);margin-top:3px}.prod-mode{display:inline-flex;padding:3px 8px;border-radius:999px;background:var(--panel2);font-size:11px}.quick-card{margin:0 0 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.quick-group{display:flex;gap:7px;align-items:center;padding-right:12px;border-right:1px solid var(--line)}.quick-group:last-child{border-right:0}.quick-label{font-size:11px;color:var(--muted);font-weight:700}.quick-time{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:8px;padding:7px 8px}.quick-status{font-size:10px;color:var(--muted)}
.models-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}.seg button{border:0;border-right:1px solid var(--line);background:var(--panel2);color:var(--muted);padding:8px 10px;cursor:pointer}.seg button:last-child{border-right:0}.seg button.active{background:#24435d;color:var(--text)}.model-picker{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:11px}.model-chart{height:370px}.model-note{font-size:11px;color:var(--muted);margin-top:7px}
@media(max-width:900px){.prod-strip{grid-template-columns:1fr}.quick-group{border-right:0;border-bottom:1px solid var(--line);padding-bottom:8px;width:100%}}
</style>
<script>
let modelWindow='7d',modelMode='economic',modelData=null,modelEnabled={};
function actionLabel(d){if(!d)return '—';const x=Number(d.action_kw||0);return Math.abs(x)<.05?'Idle':x<0?`Charge ${n(Math.abs(x),2)} kW`:`Discharge ${n(x,2)} kW`}
function installProductionOverview(){
 const ov=$('overview');if(!ov||$('prodStrip'))return;
 const k=$('overviewKpis');if(k)k.style.display='none';
 const sys=$('systemState')?.closest('.card');if(sys)sys.style.display='none';
 const decision=$('currentDecisionCard');if(decision)decision.style.display='none';
 const ev=$('evAwarenessCard');if(ev)ev.style.display='none';
 const html=`<div id="prodStrip" class="prod-strip"><div class="prod-cell"><div class="prod-label">Operating mode</div><div class="prod-value"><span id="prodMode" class="prod-mode">—</span></div><div id="prodModeSub" class="prod-reason">—</div></div><div class="prod-cell"><div class="prod-label">Current / latest decision</div><div id="prodCurrent" class="prod-value">—</div><div id="prodCurrentReason" class="prod-reason">—</div></div><div class="prod-cell"><div class="prod-label">Next planned change</div><div id="prodNext" class="prod-value">—</div><div id="prodNextReason" class="prod-reason">—</div></div></div><div class="card quick-card"><div class="quick-group"><span class="quick-label">Sauna</span><button class="btn" id="saunaNow">Now · 2h</button><input id="saunaTime" class="quick-time" type="datetime-local"><button class="btn" id="saunaSchedule">Schedule</button></div><div class="quick-group"><span class="quick-label">EV</span><button class="btn" id="evChargeNow">Charge now</button></div><span id="quickStatus" class="quick-status"></span></div>`;
 ov.insertAdjacentHTML('afterbegin',html);$('saunaNow').onclick=()=>quickOverride('sauna',{now:true});$('saunaSchedule').onclick=()=>quickOverride('sauna',{starts_at:$('saunaTime').value});$('evChargeNow').onclick=()=>quickOverride('ev_charge_now',{now:true});loadProductionOverview();
}
async function quickOverride(kind,opt){const st=$('quickStatus');st.textContent='Applying…';try{const r=await api('control/override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,...opt})});st.textContent=r.operating_mode==='active'?'Control request applied':'Shadow request added to optimizer plan';setTimeout(loadProductionOverview,800)}catch(e){st.textContent=`Failed: ${e.message}`}}
async function loadProductionOverview(){try{const d=await api('ui/production-overview');$('prodMode').textContent=String(d.production.operating_mode||'—').toUpperCase();$('prodModeSub').textContent=d.production.actuator_ready?'Actuator safety ready':'Physical writes disabled · shadow planning';$('prodCurrent').textContent=actionLabel(d.decision.current);$('prodCurrentReason').textContent=d.decision.current?.reason||'—';$('prodNext').textContent=d.decision.next?`${tlabel(d.decision.next.start)} · ${actionLabel(d.decision.next)}`:'No change in current horizon';$('prodNextReason').textContent=d.decision.next?.reason||'—';const a=d.overrides||[];$('quickStatus').textContent=a.length?`${a.length} active/scheduled user override${a.length===1?'':'s'}`:''}catch(e){}}
function installModels(){const tabs=$('tabs');if(!tabs||$('models'))return;const dev=[...tabs.querySelectorAll('.tab')].find(x=>x.dataset.view==='developer');const b='<button class="tab" data-view="models">Models</button>';if(dev)dev.insertAdjacentHTML('beforebegin',b);else tabs.insertAdjacentHTML('beforeend',b);const footer=document.querySelector('.footer');footer.insertAdjacentHTML('beforebegin',`<section id="models" class="view"><div class="models-toolbar"><div class="seg" id="modelWindow"><button data-w="1d">24 h</button><button data-w="7d" class="active">7 days</button><button data-w="30d">30 days</button><button data-w="90d">90 days</button></div><div class="seg" id="modelMode"><button data-m="economic" class="active">Economic</button><button data-m="behaviour">Behaviour</button></div><div id="modelPicker" class="model-picker"></div></div><div class="card"><h2 id="modelChartTitle">Economic performance</h2><div id="modelChart" class="chart model-chart"></div><div id="modelNote" class="model-note"></div></div></section>`);$('modelWindow').onclick=e=>{const x=e.target.closest('button');if(!x)return;modelWindow=x.dataset.w;[...$('modelWindow').querySelectorAll('button')].forEach(y=>y.classList.toggle('active',y===x));loadModels()};$('modelMode').onclick=e=>{const x=e.target.closest('button');if(!x)return;modelMode=x.dataset.m;[...$('modelMode').querySelectorAll('button')].forEach(y=>y.classList.toggle('active',y===x));renderModels()};}
async function loadModels(){try{modelData=await api(`ui/models-comparison?window=${modelWindow}`);const ids=[...new Set([...Object.keys(modelData.economics||{}),...Object.keys(modelData.behaviour||{})])];for(const id of ids)if(modelEnabled[id]===undefined)modelEnabled[id]=true;$('modelPicker').innerHTML=ids.map(id=>`<label><input type="checkbox" data-engine="${id}" ${modelEnabled[id]?'checked':''}> ${id}</label>`).join('');$('modelPicker').onchange=e=>{if(e.target.dataset.engine){modelEnabled[e.target.dataset.engine]=e.target.checked;renderModels()}};renderModels()}catch(e){$('modelChart').innerHTML=`<div class="empty">Could not load model comparison: ${e.message}</div>`}}
function renderModels(){if(!modelData)return;if(modelMode==='economic'){const ids=Object.keys(modelData.economics||{}).filter(id=>modelEnabled[id]);const allDates=[...new Set(ids.flatMap(id=>(modelData.economics[id]||[]).map(r=>r.date)))].sort();const series=ids.map((id,ix)=>{const map=Object.fromEntries((modelData.economics[id]||[]).map(r=>[r.date,r.cumulative_oracle_regret_sek]));let last=0;return {name:id,axis:'power',color:[C.load,C.pv,C.battery,C.price,C.gridImport][ix%5],on:true,values:allDates.map(d=>map[d]??last),width:2.4}});for(const s of series)s.values=s.values.map(v=>(last=v,last));$('modelChartTitle').textContent='Cumulative realized oracle regret';$('modelNote').textContent='SEK relative to the perfect-information oracle on comparable realized days. Lower is better.';lineChart($('modelChart'),series,allDates.map(d=>`${d}T12:00:00Z`))}else{const ids=Object.keys(modelData.behaviour||{}).filter(id=>modelEnabled[id]);const all=[...new Set(ids.flatMap(id=>(modelData.behaviour[id]||[]).map(r=>r.start)))].sort();const series=ids.map((id,ix)=>{const map=Object.fromEntries((modelData.behaviour[id]||[]).map(r=>[r.start,r.requested_action_kw]));return {name:id,axis:'power',color:[C.load,C.pv,C.battery,C.price,C.gridImport][ix%5],on:true,values:all.map(t=>map[t]??null),width:2.1}});$('modelChartTitle').textContent='Battery action by model';$('modelNote').textContent='Positive = discharge, negative = charge. Decisions share the same stored information vintages.';lineChart($('modelChart'),series,all)}}
installProductionOverview();installModels();setInterval(()=>{if(document.querySelector('#overview.view.active'))loadProductionOverview()},15000);setTimeout(()=>{const tabs=$('tabs');tabs?.addEventListener('click',e=>{if(e.target.closest('.tab')?.dataset.view==='models')loadModels()})},0);
</script>
'''


def install_ui_v183(app: FastAPI) -> None:
    @app.middleware("http")
    async def ui_v183(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace(
                "</body>",
                OVERVIEW_EXTENSION + V158_EXTENSION + V159_EXTENSION + V160_EXTENSION + V161_EXTENSION + V161_FIX_EXTENSION + V163_EXTENSION + V164_EXTENSION + V165_EXTENSION + V180_EXTENSION + V183_EXTENSION + "</body>",
            )
            return HTMLResponse(html)
        return await call_next(request)

    @app.get("/ui/production-overview", include_in_schema=False)
    async def production_overview():
        return JSONResponse({"production": status(), "decision": _decision_summary(), "overrides": scheduled_overrides()})

    @app.get("/ui/models-comparison", include_in_schema=False)
    async def models_comparison(window: str = "7d"):
        if window not in WINDOW_DAYS:
            return JSONResponse({"error": "window must be one of 1d,7d,30d,90d"}, status_code=400)
        return JSONResponse(_model_comparison(window))
