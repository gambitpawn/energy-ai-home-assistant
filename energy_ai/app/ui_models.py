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
        return {
            "start": stamp.isoformat(),
            "action_kw": action,
            "action": "idle" if abs(action) < 0.05 else "charge" if action < 0 else "discharge",
            "reason_code": row.get("reason"),
            "reason": _reason_text(row.get("reason")),
        }

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
            behaviour.setdefault(str(engine_id), []).append(
                {
                    "start": stamp,
                    "requested_action_kw": decision.get("requested_action_kw"),
                    "expected_soc_pct": decision.get("expected_soc_pct"),
                    "status": decision.get("status"),
                }
            )

    state = ensure_selector_state(cfg)
    context = state["context_signature"]
    score_days = days
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            dates = [
                str(r[0])
                for r in c.execute(
                    "SELECT DISTINCT local_date FROM engine_daily_score "
                    "WHERE context_signature=? ORDER BY local_date DESC LIMIT ?",
                    (context, score_days),
                ).fetchall()
            ]
            dates.reverse()
            rows = []
            if dates:
                placeholders = ",".join("?" for _ in dates)
                rows = c.execute(
                    f"SELECT local_date,engine_id,intervals,mean_regret_ore,p90_regret_ore,"
                    f"clamp_rate,payload_json FROM engine_daily_score WHERE context_signature=? "
                    f"AND local_date IN ({placeholders}) ORDER BY local_date,engine_id",
                    (context, *dates),
                ).fetchall()
    except sqlite3.OperationalError:
        dates = []
        rows = []

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
        economics.setdefault(eid, []).append(
            {
                "date": str(local_date),
                "intervals": int(intervals),
                "mean_regret_ore": float(mean_regret),
                "p90_regret_ore": float(p90_regret),
                "clamp_rate": float(clamp_rate),
                "daily_oracle_regret_sek": round(daily_regret_sek, 4),
                "cumulative_oracle_regret_sek": round(cumulative[eid], 4),
            }
        )

    registry = registry_status()
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in registry.get("engines") or []:
        engine_id = str(entry.get("engine_id") or "")
        if not engine_id:
            continue
        seen.add(engine_id)
        models.append(
            {
                "engine_id": engine_id,
                "display_name": entry.get("display_name") or engine_id,
                "family": entry.get("family"),
                "state": "available" if entry.get("available") else "unavailable",
                "available": bool(entry.get("available")),
                "baseline": bool(entry.get("baseline")),
                "trainable": bool(entry.get("trainable")),
                "learning_enabled": bool(entry.get("learning_enabled")),
                "note": entry.get("description") or "",
            }
        )

    # Historical rows are exposed only when they belong to an engine that still
    # exists in the registry. Retirement cleanup removes obsolete engine rows;
    # this guard prevents a stale DB row from resurrecting a removed model in UI.
    economics = {engine_id: values for engine_id, values in economics.items() if engine_id in seen}
    behaviour = {engine_id: values for engine_id, values in behaviour.items() if engine_id in seen}

    return {
        "window": window,
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "economics": economics,
        "behaviour": behaviour,
        "models": models,
        "economic_score_dates": dates,
        "economic_window_semantics": f"latest {score_days} mature scored day(s); oracle scoring has an inherent realization lag",
        "metric_note": "Economic comparison uses the latest mature selector-score days. Lower realized oracle regret is better. Behaviour remains a trailing wall-clock window.",
    }


MODELS_EXTENSION = r'''
<style>
.models-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.models-seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
.models-seg button{border:0;border-right:1px solid var(--line);background:var(--panel2);color:var(--muted);padding:8px 10px;cursor:pointer}
.models-seg button:last-child{border-right:0}.models-seg button.active{background:#24435d;color:var(--text)}
.model-picker{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:11px}
.models-table{width:100%;border-collapse:collapse;font-size:12px}.models-table th,.models-table td{padding:8px;border-bottom:1px solid var(--line);text-align:left}
.model-state{font-size:10px;color:var(--muted)}.model-note{font-size:11px;color:var(--muted);margin-top:8px}
</style>
<script>
let modelsWindow='7d',modelsMode='economic',modelsData=null,modelsEnabled={};
function modelsName(m){return m.display_name||m.engine_id||'—'}
function installModels(){
  const tabs=$('tabs'); if(!tabs||$('models')) return;
  const dev=[...tabs.querySelectorAll('.tab')].find(x=>x.dataset.view==='developer');
  const button='<button class="tab" data-view="models">Models</button>';
  if(dev) dev.insertAdjacentHTML('beforebegin',button); else tabs.insertAdjacentHTML('beforeend',button);
  const footer=document.querySelector('.footer');
  footer.insertAdjacentHTML('beforebegin',`<section id="models" class="view"><div class="models-toolbar"><div class="models-seg" id="modelsWindow"><button data-w="1d">24 h</button><button data-w="7d" class="active">7 days</button><button data-w="30d">30 days</button><button data-w="90d">90 days</button></div><div class="models-seg" id="modelsMode"><button data-m="economic" class="active">Economic</button><button data-m="behaviour">Behaviour</button></div><div id="modelPicker" class="model-picker"></div></div><div class="card"><h2 id="modelsTitle">Model comparison</h2><div id="modelsBody"></div><div id="modelsNote" class="model-note"></div></div></section>`);
  $('modelsWindow').onclick=e=>{const b=e.target.closest('button');if(!b)return;modelsWindow=b.dataset.w;[...$('modelsWindow').querySelectorAll('button')].forEach(x=>x.classList.toggle('active',x===b));loadModels()};
  $('modelsMode').onclick=e=>{const b=e.target.closest('button');if(!b)return;modelsMode=b.dataset.m;[...$('modelsMode').querySelectorAll('button')].forEach(x=>x.classList.toggle('active',x===b));renderModels()};
  $('tabs')?.addEventListener('click',e=>{if(e.target.closest('.tab')?.dataset.view==='models')loadModels()});
}
async function loadModels(){try{modelsData=await api(`ui/models-comparison?window=${modelsWindow}`);const picker=$('modelPicker');const models=modelsData.models||[];models.forEach(m=>{if(modelsEnabled[m.engine_id]===undefined)modelsEnabled[m.engine_id]=true});picker.innerHTML=models.map(m=>`<label><input type="checkbox" data-e="${m.engine_id}" ${modelsEnabled[m.engine_id]?'checked':''}> ${modelsName(m)} <span class="model-state">${m.state}</span></label>`).join('');picker.querySelectorAll('input').forEach(x=>x.onchange=()=>{modelsEnabled[x.dataset.e]=x.checked;renderModels()});renderModels()}catch(e){$('modelsBody').innerHTML=`<div class="empty">Failed to load model comparison: ${e.message}</div>`}}
function renderModels(){if(!modelsData)return;const active=(modelsData.models||[]).filter(m=>modelsEnabled[m.engine_id]);$('modelsTitle').textContent=modelsMode==='economic'?'Economic model comparison':'Battery behaviour comparison';let rows=[];if(modelsMode==='economic'){rows=active.map(m=>{const s=(modelsData.economics?.[m.engine_id]||[]);const last=s.length?s[s.length-1]:null;return `<tr><td>${modelsName(m)}</td><td>${m.family||'—'}</td><td>${m.state}</td><td>${last?Number(last.mean_regret_ore).toFixed(2):'—'}</td><td>${last?Number(last.p90_regret_ore).toFixed(2):'—'}</td><td>${last?Number(last.cumulative_oracle_regret_sek).toFixed(2):'—'}</td></tr>`});$('modelsBody').innerHTML=`<table class="models-table"><thead><tr><th>Model</th><th>Family</th><th>State</th><th>Mean regret öre</th><th>P90 regret öre</th><th>Cumulative regret SEK</th></tr></thead><tbody>${rows.join('')}</tbody></table>`}else{rows=active.map(m=>{const s=(modelsData.behaviour?.[m.engine_id]||[]);const last=s.length?s[s.length-1]:null;return `<tr><td>${modelsName(m)}</td><td>${m.family||'—'}</td><td>${m.state}</td><td>${last&&last.requested_action_kw!=null?Number(last.requested_action_kw).toFixed(2):'—'}</td><td>${last&&last.expected_soc_pct!=null?Number(last.expected_soc_pct).toFixed(1):'—'}</td><td>${last?.start||'—'}</td></tr>`});$('modelsBody').innerHTML=`<table class="models-table"><thead><tr><th>Model</th><th>Family</th><th>State</th><th>Latest action kW</th><th>Expected SOC %</th><th>Decision start</th></tr></thead><tbody>${rows.join('')}</tbody></table>`}$('modelsNote').textContent=modelsData.metric_note||''}
installModels();
</script>
'''


def install_model_routes(app: FastAPI, cfg: dict[str, Any]) -> None:
    @app.get("/ui/production-overview", include_in_schema=False)
    async def production_overview():
        return JSONResponse(
            {
                "production": production_status(),
                "decision": decision_summary(),
                "overrides": scheduled_overrides(),
            }
        )

    @app.get("/ui/models-comparison", include_in_schema=False)
    async def models_comparison(window: str = "7d"):
        if window not in WINDOW_DAYS:
            return JSONResponse({"error": "window must be one of 1d,7d,30d,90d"}, status_code=400)
        return JSONResponse(model_comparison(cfg, window))

    @app.get("/ui/models-comparison-v184", include_in_schema=False)
    async def models_comparison_legacy(window: str = "7d"):
        if window not in WINDOW_DAYS:
            return JSONResponse({"error": "window must be one of 1d,7d,30d,90d"}, status_code=400)
        return JSONResponse(model_comparison(cfg, window))
