from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .db import DB_PATH
from .engine_registry import registry_status
from .engine_store import competition_rows
from .model_selector import ensure_selector_state
from .neural_engine import neural_runtime_status
from .optimizer_store import latest_plan
from .production_state import scheduled_overrides, status as production_status

WINDOW_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}


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


def decision_summary() -> dict[str, Any]:
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
    current = next(((s, r) for s, r in parsed if s <= now < s + timedelta(minutes=15)), None)
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
        return {"start": stamp.isoformat(), "action_kw": action, "action": "idle" if abs(action) < 0.05 else "charge" if action < 0 else "discharge", "reason_code": row.get("reason"), "reason": _reason_text(row.get("reason"))}

    return {"generated_at": plan.get("generated_at"), "current": item(current), "next": item(nxt)}


def model_comparison(cfg: dict[str, Any], window: str) -> dict[str, Any]:
    days = WINDOW_DAYS.get(window, 7)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    competitions = competition_rows(start.isoformat(), end.isoformat())
    behaviour: dict[str, list[dict[str, Any]]] = {}
    for item in competitions:
        stamp = item.get("decision_start")
        for engine_id, decision in (item.get("decisions") or {}).items():
            behaviour.setdefault(str(engine_id), []).append({"start": stamp, "requested_action_kw": decision.get("requested_action_kw"), "expected_soc_pct": decision.get("expected_soc_pct"), "status": decision.get("status")})

    state = ensure_selector_state(cfg)
    context = state["context_signature"]
    score_days = days
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        dates = [str(r[0]) for r in c.execute("SELECT DISTINCT local_date FROM engine_daily_score WHERE context_signature=? ORDER BY local_date DESC LIMIT ?", (context, score_days)).fetchall()]
        dates.reverse()
        rows = []
        if dates:
            placeholders = ",".join("?" for _ in dates)
            rows = c.execute(f"SELECT local_date,engine_id,intervals,mean_regret_ore,p90_regret_ore,clamp_rate,payload_json FROM engine_daily_score WHERE context_signature=? AND local_date IN ({placeholders}) ORDER BY local_date,engine_id", (context, *dates)).fetchall()

    economics: dict[str, list[dict[str, Any]]] = {}
    cumulative: dict[str, float] = {}
    for local_date, engine_id, intervals, mean_regret, p90_regret, clamp_rate, raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except Exception:
            payload = {}
        total_regret_ore = payload.get("total_regret_ore")
        if total_regret_ore is None:
            total_regret_ore = float(mean_regret) * int(intervals)
        daily_regret_sek = float(total_regret_ore) / 100.0
        eid = str(engine_id)
        cumulative[eid] = cumulative.get(eid, 0.0) + daily_regret_sek
        economics.setdefault(eid, []).append({"date": str(local_date), "intervals": int(intervals), "mean_regret_ore": float(mean_regret), "p90_regret_ore": float(p90_regret), "clamp_rate": float(clamp_rate), "daily_oracle_regret_sek": round(daily_regret_sek, 4), "cumulative_oracle_regret_sek": round(cumulative[eid], 4)})

    registry = registry_status()
    neural = neural_runtime_status()
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in registry.get("engines") or []:
        engine_id = str(entry.get("engine_id") or "")
        if not engine_id:
            continue
        seen.add(engine_id)
        model_state = "available" if entry.get("available") else "unavailable"
        note = ""
        if engine_id == "neural_v1":
            if neural.get("shadow_ready"):
                model_state = "shadow ready"
                note = "Neural model is trained and produces comparable shadow decisions."
            elif neural.get("model_exists"):
                model_state = "learning"
                note = f"Neural model is still learning; {neural.get('samples') or 0} samples available."
            else:
                model_state = "learning"
                note = f"Neural training data is accumulating; {neural.get('samples') or 0} samples available."
        models.append({"engine_id": engine_id, "state": model_state, "note": note})
    for engine_id in sorted(set(economics) | set(behaviour)):
        if engine_id not in seen:
            models.append({"engine_id": engine_id, "state": "historical", "note": "Stored historical model data."})

    return {
        "window": window,
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "economics": economics,
        "behaviour": behaviour,
        "models": models,
        "neural_runtime": neural,
        "economic_score_dates": dates,
        "economic_window_semantics": f"latest {score_days} mature scored day(s); oracle scoring has an inherent realization lag",
        "metric_note": "Economic comparison uses the latest mature selector-score days. Lower realized oracle regret is better. Behaviour remains a trailing wall-clock window.",
    }


MODELS_EXTENSION = r'''
<style>
.prod-strip{display:grid;grid-template-columns:1fr 1.4fr 1.4fr;gap:10px;margin:0 0 12px}.prod-cell{background:#0f1720;border:1px solid var(--line);border-radius:12px;padding:10px 12px}.prod-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.prod-value{font-size:15px;font-weight:720;margin-top:2px}.prod-reason{font-size:11px;color:var(--muted);margin-top:3px}.prod-mode{display:inline-flex;padding:3px 8px;border-radius:999px;background:var(--panel2);font-size:11px}.quick-card{margin:0 0 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.quick-group{display:flex;gap:7px;align-items:center;padding-right:12px;border-right:1px solid var(--line)}.quick-group:last-child{border-right:0}.quick-label{font-size:11px;color:var(--muted);font-weight:700}.quick-time{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:8px;padding:7px 8px}.quick-status{font-size:10px;color:var(--muted)}.models-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}.seg button{border:0;border-right:1px solid var(--line);background:var(--panel2);color:var(--muted);padding:8px 10px;cursor:pointer}.seg button:last-child{border-right:0}.seg button.active{background:#24435d;color:var(--text)}.model-picker{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:11px}.model-chart{height:370px}.model-note,.history-note{font-size:11px;color:var(--muted);margin-top:7px}.model-metric{display:none;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:9px;padding:7px 9px}.model-metric.show{display:inline-block}.model-state{font-size:10px;color:var(--muted);margin-left:3px}.model-state.learning{color:var(--warn)}
@media(max-width:900px){.prod-strip{grid-template-columns:1fr}.quick-group{border-right:0;border-bottom:1px solid var(--line);padding-bottom:8px;width:100%}}
</style>
<script>
let modelWindow='7d',modelMode='economic',modelBehaviourMetric='battery',modelData=null,modelEnabled={};
function actionLabel(d){if(!d)return '—';const x=Number(d.action_kw||0);return Math.abs(x)<.05?'Idle':x<0?`Charge ${n(Math.abs(x),2)} kW`:`Discharge ${n(x,2)} kW`}
function installProductionOverview(){const ov=$('overview');if(!ov||$('prodStrip'))return;const k=$('overviewKpis');if(k)k.style.display='none';const sys=$('systemState')?.closest('.card');if(sys)sys.style.display='none';const html=`<div id="prodStrip" class="prod-strip"><div class="prod-cell"><div class="prod-label">Operating mode</div><div class="prod-value"><span id="prodMode" class="prod-mode">—</span></div><div id="prodModeSub" class="prod-reason">—</div></div><div class="prod-cell"><div class="prod-label">Current / latest decision</div><div id="prodCurrent" class="prod-value">—</div><div id="prodCurrentReason" class="prod-reason">—</div></div><div class="prod-cell"><div class="prod-label">Next planned change</div><div id="prodNext" class="prod-value">—</div><div id="prodNextReason" class="prod-reason">—</div></div></div><div class="card quick-card"><div class="quick-group"><span class="quick-label">Sauna</span><button class="btn" id="saunaNow">Now · 2h</button><input id="saunaTime" class="quick-time" type="datetime-local"><button class="btn" id="saunaSchedule">Schedule</button></div><div class="quick-group"><span class="quick-label">EV</span><button class="btn" id="evChargeNow">Charge now</button></div><span id="quickStatus" class="quick-status"></span></div>`;ov.insertAdjacentHTML('afterbegin',html);$('saunaNow').onclick=()=>quickOverride('sauna',{now:true});$('saunaSchedule').onclick=()=>quickOverride('sauna',{starts_at:$('saunaTime').value});$('evChargeNow').onclick=()=>quickOverride('ev_charge_now',{now:true});loadProductionOverview()}
async function quickOverride(kind,opt){const st=$('quickStatus');st.textContent='Applying…';try{const r=await api('control/override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,...opt})});st.textContent=r.operating_mode==='active'?'Control request applied':'Shadow request added to optimizer plan';setTimeout(loadProductionOverview,800)}catch(e){st.textContent=`Failed: ${e.message}`}}
async function loadProductionOverview(){try{const d=await api('ui/production-overview');$('prodMode').textContent=String(d.production.operating_mode||'—').toUpperCase();$('prodModeSub').textContent=d.production.actuator_ready?'Actuator safety ready':'Physical writes disabled · shadow planning';$('prodCurrent').textContent=actionLabel(d.decision.current);$('prodCurrentReason').textContent=d.decision.current?.reason||'—';$('prodNext').textContent=d.decision.next?`${tlabel(d.decision.next.start)} · ${actionLabel(d.decision.next)}`:'No change in current horizon';$('prodNextReason').textContent=d.decision.next?.reason||'—';const a=d.overrides||[];$('quickStatus').textContent=a.length?`${a.length} active/scheduled user override${a.length===1?'':'s'}`:''}catch(e){}}
function installModels(){const tabs=$('tabs');if(!tabs||$('models'))return;const dev=[...tabs.querySelectorAll('.tab')].find(x=>x.dataset.view==='developer');const button='<button class="tab" data-view="models">Models</button>';if(dev)dev.insertAdjacentHTML('beforebegin',button);else tabs.insertAdjacentHTML('beforeend',button);const footer=document.querySelector('.footer');footer.insertAdjacentHTML('beforebegin',`<section id="models" class="view"><div class="models-toolbar"><div class="seg" id="modelWindow"><button data-w="1d">24 h</button><button data-w="7d" class="active">7 days</button><button data-w="30d">30 days</button><button data-w="90d">90 days</button></div><div class="seg" id="modelMode"><button data-m="economic" class="active">Economic</button><button data-m="behaviour">Behaviour</button></div><select id="modelBehaviourMetric" class="model-metric"><option value="battery">Battery action</option><option value="soc">Expected SOC</option></select><div id="modelPicker" class="model-picker"></div></div><div class="card"><h2 id="modelChartTitle">Economic performance</h2><div id="modelChart" class="chart model-chart"></div><div id="modelNote" class="model-note"></div></div></section>`);$('modelWindow').onclick=e=>{const x=e.target.closest('button');if(!x)return;modelWindow=x.dataset.w;[...$('modelWindow').querySelectorAll('button')].forEach(y=>y.classList.toggle('active',y===x));loadModels()};$('modelMode').onclick=e=>{const x=e.target.closest('button');if(!x)return;modelMode=x.dataset.m;[...$('modelMode').querySelectorAll('button')].forEach(y=>y.classList.toggle('active',y===x));$('modelBehaviourMetric').classList.toggle('show',modelMode==='behaviour');renderModels()};$('modelBehaviourMetric').onchange=e=>{modelBehaviourMetric=e.target.value;renderModels()}}
async function loadModels(){try{modelData=await api(`ui/models-comparison?window=${modelWindow}`);const models=modelData.models||[];for(const m of models)if(modelEnabled[m.engine_id]===undefined)modelEnabled[m.engine_id]=true;$('modelPicker').innerHTML=models.map(m=>`<label title="${esc(m.note||'')}"><input type="checkbox" data-engine="${esc(m.engine_id)}" ${modelEnabled[m.engine_id]?'checked':''}> ${esc(m.engine_id)} <span class="model-state ${m.state==='learning'?'learning':''}">${esc(m.state||'')}</span></label>`).join('');$('modelPicker').onchange=e=>{if(e.target.dataset.engine){modelEnabled[e.target.dataset.engine]=e.target.checked;renderModels()}};renderModels()}catch(e){$('modelChart').innerHTML=`<div class="empty">Could not load model comparison: ${e.message}</div>`}}
function renderModels(){if(!modelData)return;if(modelMode==='economic'){const ids=Object.keys(modelData.economics||{}).filter(id=>modelEnabled[id]),allDates=[...new Set(ids.flatMap(id=>(modelData.economics[id]||[]).map(r=>r.date)))].sort(),palette=[C.load,C.pv,C.battery,C.price,C.gridImport],series=ids.map((id,ix)=>{const map=Object.fromEntries((modelData.economics[id]||[]).map(r=>[r.date,r.cumulative_oracle_regret_sek]));let last=0;return {name:id,axis:'power',color:palette[ix%palette.length],on:true,values:allDates.map(d=>{if(map[d]!=null)last=Number(map[d]);return last}),width:2.4}});$('modelChartTitle').textContent='Cumulative realized oracle regret';$('modelNote').textContent='SEK relative to the perfect-information oracle on the latest mature scored days. Lower is better.';lineChart($('modelChart'),series,allDates.map(d=>`${d}T12:00:00Z`));return}const ids=Object.keys(modelData.behaviour||{}).filter(id=>modelEnabled[id]),all=[...new Set(ids.flatMap(id=>(modelData.behaviour[id]||[]).map(r=>r.start)))].sort(),palette=[C.load,C.pv,C.battery,C.price,C.gridImport],isSoc=modelBehaviourMetric==='soc',field=isSoc?'expected_soc_pct':'requested_action_kw',series=ids.map((id,ix)=>{const map=Object.fromEntries((modelData.behaviour[id]||[]).map(r=>[r.start,r[field]]));return {name:id,axis:isSoc?'soc':'power',color:palette[ix%palette.length],on:true,values:all.map(t=>map[t]??null),width:2.1}});$('modelChartTitle').textContent=isSoc?'Expected SOC by model':'Battery action by model';$('modelNote').textContent=isSoc?'Expected battery SOC after each model decision on the same stored information vintages.':'Positive = discharge, negative = charge. Decisions share the same stored information vintages.';lineChart($('modelChart'),series,all)}
renderHistory=function(){const h=state.history||{},days=h.days||[];$('historyKpis').innerHTML=card('Fully evaluated',n(h.complete_days,0),`${n(h.partial_days,0)} partial`)+card('Stored mature days',n(h.stored_days,0),'Automatic backfill')+card('Total saving',sek(h.total_realtime_economic_saving_vs_zero_battery_sek),'Fully evaluated days',h.total_realtime_economic_saving_vs_zero_battery_sek>0?'good':'')+card('Mean daily saving',sek(h.mean_daily_realtime_economic_saving_sek),'Fully evaluated days')+card('Perfect-info gap',sek(h.total_perfect_information_gap_sek),'Fully evaluated days')+card('Mean coverage',pct(h.mean_plan_action_coverage_fraction),'Stored evaluated days');const withSaving=days.filter(x=>x.saving_sek!=null),withRegret=days.filter(x=>x.perfect_information_gap_sek!=null);bars($('savingChart'),withSaving.map(x=>({label:`${x.local_date}${x.status==='ok'?'':' · partial'}`,value:x.saving_sek})));bars($('regretChart'),withRegret.map(x=>({label:`${x.local_date}${x.status==='ok'?'':' · partial'}`,value:x.perfect_information_gap_sek})),'#ffbf5a','#51d88a');if(!$('historyPartialNote'))$('historyTable').parentElement.insertAdjacentHTML('beforebegin','<div id="historyPartialNote" class="history-note">Partial days are shown when they contain usable metrics; they are excluded from aggregate saving/regret KPIs until plan coverage reaches the evaluation threshold.</div>');$('historyTable').innerHTML=days.length?`<table class="tbl"><thead><tr><th>Date</th><th>Status</th><th>Coverage</th><th>Saving SEK</th><th>Perfect-info gap SEK</th><th>Load MAE kW</th><th>PV MAE kW</th><th>Throughput kWh</th><th>Clamps</th></tr></thead><tbody>${days.map(x=>`<tr><td>${x.local_date}</td><td><span class="pill ${x.status==='ok'?'ok':'partial'}">${x.status}</span></td><td>${pct(x.coverage)}</td><td>${n(x.saving_sek)}</td><td>${n(x.perfect_information_gap_sek)}</td><td>${n(x.load_mae_kw)}</td><td>${n(x.pv_mae_kw)}</td><td>${n(x.throughput_kwh)}</td><td>${n(x.clamped_intervals,0)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No evaluated mature days stored yet.</div>'}
installProductionOverview();installModels();setInterval(()=>{if(document.querySelector('#overview.view.active'))loadProductionOverview()},15000);$('tabs')?.addEventListener('click',e=>{if(e.target.closest('.tab')?.dataset.view==='models')loadModels()});
</script>
'''


def install_model_routes(app: FastAPI, cfg: dict[str, Any]) -> None:
    @app.get("/ui/production-overview", include_in_schema=False)
    async def production_overview():
        return JSONResponse({"production": production_status(), "decision": decision_summary(), "overrides": scheduled_overrides()})

    @app.get("/ui/models-comparison", include_in_schema=False)
    async def models_comparison(window: str = "7d"):
        if window not in WINDOW_DAYS:
            return JSONResponse({"error": "window must be one of 1d,7d,30d,90d"}, status_code=400)
        return JSONResponse(model_comparison(cfg, window))

    # Compatibility alias for browser bookmarks from the pre-consolidation UI.
    @app.get("/ui/models-comparison-v184", include_in_schema=False)
    async def models_comparison_legacy(window: str = "7d"):
        if window not in WINDOW_DAYS:
            return JSONResponse({"error": "window must be one of 1d,7d,30d,90d"}, status_code=400)
        return JSONResponse(model_comparison(cfg, window))
