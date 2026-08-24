import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

from .collector import Collector
from .config import load_config
from .db import get_prices, init_db, insert_llm, insert_pv_forecast, latest_pv_forecast, latest_rows, rebuild_recent_15m, upsert_prices
from .flexible_loads import discover_flexible_load_entities, ev_state, sauna_state
from .forecast import PVForecaster
from .llm import LLMExplainer
from .load_forecast import LoadForecaster
from .models import ExplainRequest, ExplainResponse
from .pv_evaluation import evaluate_matured_forecasts, evaluation_report
from .training_routes import router as training_router

cfg = load_config()
collector = Collector(cfg)
explainer = LLMExplainer(cfg)
pv_forecaster = PVForecaster(cfg, collector.ha)
load_forecaster = LoadForecaster(cfg)
collector_task = None
maintenance_task = None
PRICE_AREA = "SE4"
PRICE_TZ = ZoneInfo("Europe/Stockholm")


async def _refresh_price_horizon():
    now = datetime.now(PRICE_TZ)
    dates = [now.date(), now.date() + timedelta(days=1)]
    result = {"area": PRICE_AREA, "dates": {}, "total_intervals": 0}
    fetched_at = datetime.now(timezone.utc).isoformat()
    for day in dates:
        try:
            rows = await collector.ha.nordpool_prices_15m(day.isoformat(), PRICE_AREA, "SEK")
            upsert_prices(PRICE_AREA, rows, fetched_at)
            result["dates"][day.isoformat()] = {"ok": True, "intervals": len(rows)}
            result["total_intervals"] += len(rows)
        except Exception as exc:
            result["dates"][day.isoformat()] = {"ok": False, "error": repr(exc)}
    return result


async def _refresh_pv_forecast():
    forecast = await pv_forecaster.refresh()
    insert_pv_forecast(forecast)
    return {
        "ok": True,
        "generated_at": forecast["generated_at"],
        "intervals": len(forecast["rows"]),
        "interval_minutes": forecast["interval_minutes"],
        "horizon_hours": forecast["horizon_hours"],
        "capacity_kw": forecast["capacity_kw"],
        "radiation_feature": forecast["radiation_feature"],
        "orientation_configured": forecast["orientation_configured"],
        "model": forecast["model"],
        "calibrated_model_active": forecast.get("calibrated_model_active", False),
    }


def _seconds_to_next_quarter() -> float:
    now = datetime.now(timezone.utc)
    next_minute = ((now.minute // 15) + 1) * 15
    if next_minute >= 60:
        target = now.replace(minute=0, second=20, microsecond=0) + timedelta(hours=1)
    else:
        target = now.replace(minute=next_minute, second=20, microsecond=0)
    return max(5.0, (target - now).total_seconds())


async def _pv_maintenance_loop():
    while True:
        await asyncio.sleep(_seconds_to_next_quarter())
        try:
            await asyncio.to_thread(evaluate_matured_forecasts, 7)
        except Exception:
            pass
        try:
            await _refresh_pv_forecast()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app):
    global collector_task, maintenance_task
    init_db()
    try:
        await collector.run_once()
        rebuild_recent_15m(collector.poll_seconds, lookback_hours=48)
    except Exception as exc:
        collector.last_error = repr(exc)
    try:
        await _refresh_price_horizon()
    except Exception:
        pass
    try:
        await _refresh_pv_forecast()
    except Exception:
        pass
    try:
        await asyncio.to_thread(evaluate_matured_forecasts, 7)
    except Exception:
        pass
    collector_task = asyncio.create_task(collector.loop())
    maintenance_task = asyncio.create_task(_pv_maintenance_loop())
    yield
    collector.stop()
    for task in (collector_task, maintenance_task):
        if task:
            task.cancel()


app = FastAPI(title="Energy AI",version="1.0.34",description="Read-only HA energy data, forecasts, continuous evaluation, flexible loads, training data and LLM analysis",lifespan=lifespan,docs_url=None,redoc_url=None)
app.include_router(training_router)

POWER_CATEGORIES = {"pv_power", "house_load", "grid_power", "battery_power"}
KEYWORDS = {
    "pv_power": ["pv", "solar", "solinteg", "photovoltaic", "yield", "inverter"],
    "house_load": ["load", "house", "home", "consumption", "förbrukning", "load power"],
    "grid_power": ["grid", "meter", "import", "export", "nät", "feed in", "feed-in"],
    "battery_power": ["battery", "batteri", "charge", "discharge", "bat power"],
    "battery_soc": ["battery", "batteri", "soc", "state of charge"],
    "spot_price": ["spot", "price", "elpris", "nordpool", "electricity price", "tibber"],
}


def _row(entity: dict, score: int = 0) -> dict:
    attrs = entity.get("attributes", {}) or {}
    return {"entity_id": entity.get("entity_id"),"friendly_name": attrs.get("friendly_name"),"state": entity.get("state"),"unit": attrs.get("unit_of_measurement"),"device_class": attrs.get("device_class"),"score": score}


def _candidate_score(entity: dict, category: str) -> int:
    entity_id = str(entity.get("entity_id", "")).lower(); attrs = entity.get("attributes", {}) or {}; name = str(attrs.get("friendly_name", "")).lower(); unit = str(attrs.get("unit_of_measurement", "")).lower(); device_class = str(attrs.get("device_class", "")).lower(); text = f"{entity_id} {name}"
    score = sum(5 for kw in KEYWORDS[category] if kw in text)
    if category in POWER_CATEGORIES:
        if device_class == "power": score += 5
        if unit in {"w", "kw"}: score += 4
    elif category == "battery_soc":
        if device_class == "battery": score += 5
        if unit == "%": score += 3
    elif category == "spot_price" and any(x in unit for x in ["kwh", "mwh", "sek", "kr", "öre", "eur"]): score += 4
    if entity.get("state") in (None, "unknown", "unavailable", ""): score -= 3
    return score


def _discover_candidates(states: list[dict]) -> dict[str, list[dict]]:
    result = {}
    for category in ["pv_power", "house_load", "grid_power", "battery_power", "battery_soc", "spot_price"]:
        ranked = []
        for entity in states:
            score = _candidate_score(entity, category)
            if score >= 4: ranked.append(_row(entity, score))
        ranked.sort(key=lambda x: (-x["score"], x["entity_id"] or "")); result[category] = ranked[:30]
    return result


def _generic_energy_candidates(states: list[dict]) -> dict[str, list[dict]]:
    power=[]; percent=[]; price=[]; keyword=[]; search_words=["solinteg","inverter","battery","batter","grid","meter","solar","pv","power","nordpool","tibber","price"]
    for entity in states:
        entity_id=str(entity.get("entity_id", "")); attrs=entity.get("attributes", {}) or {}; name=str(attrs.get("friendly_name", "")); unit=str(attrs.get("unit_of_measurement", "")).lower(); device_class=str(attrs.get("device_class", "")).lower(); text=f"{entity_id} {name}".lower(); row=_row(entity)
        if device_class == "power" or unit in {"w", "kw"}: power.append(row)
        if unit == "%" or device_class == "battery": percent.append(row)
        if any(x in unit for x in ["kwh","mwh","sek","kr","öre","eur"]) and any(x in text for x in ["price","spot","nordpool","tibber","elpris"]): price.append(row)
        if any(word in text for word in search_words): keyword.append(row)
    key=lambda x:(x["friendly_name"] or x["entity_id"] or "").lower()
    return {"all_power_sensors":sorted(power,key=key)[:100],"battery_percent_candidates":sorted(percent,key=key)[:100],"price_candidates":sorted(price,key=key)[:100],"energy_keyword_matches":sorted(keyword,key=key)[:150]}


def _table(rows: list[dict], show_score: bool = True) -> str:
    header="<th>Score</th>" if show_score else ""; body=[]
    for row in rows:
        score=f"<td>{row.get('score','')}</td>" if show_score else ""; body.append("<tr>"+f"<td><code>{escape(str(row.get('entity_id') or ''))}</code></td>"+f"<td>{escape(str(row.get('friendly_name') or ''))}</td>"+f"<td>{escape(str(row.get('state') or ''))}</td>"+f"<td>{escape(str(row.get('unit') or ''))}</td>"+f"<td>{escape(str(row.get('device_class') or ''))}</td>{score}</tr>")
    if not body: body.append(f"<tr><td colspan='{6 if show_score else 5}'>No candidates</td></tr>")
    return "<table><thead><tr><th>Entity</th><th>Name</th><th>State</th><th>Unit</th><th>Device class</th>"+header+"</tr></thead><tbody>"+"".join(body)+"</tbody></table>"


@app.get("/", response_class=HTMLResponse)
async def root():
    return """<!doctype html><html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Energy AI</title><style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:760px}.ok{color:#188038}.warn{color:#b06000}code{background:#8882;padding:.15rem .3rem;border-radius:4px}li{margin:.45rem 0}</style></head><body><h1>Energy AI</h1><p>Home Assistant-appen körs.</p><p id="health">Kontrollerar status…</p><ul><li><a href="training">Training data</a></li><li><a href="forecast/pv/refresh">Refresh PV forecast</a></li><li><a href="forecast/pv">PV forecast</a></li><li><a href="forecast/pv/evaluation">PV online evaluation</a></li><li><a href="forecast/load">Load forecast</a></li><li><a href="flexible-loads">EV / sauna state</a></li><li><a href="discover/flexible">Discover Zaptec / sauna entities</a></li><li><a href="prices/refresh">Refresh 15-minute prices</a></li><li><a href="prices">15-minute prices</a></li><li><a href="ha-diagnostics">HA connection diagnostics</a></li><li><a href="discover">Discover HA entities</a></li><li><a href="health">Health</a></li><li><a href="state">Current state</a></li><li><a href="history?resolution=15m&limit=96">15-minute history</a></li><li><a href="history?resolution=raw&limit=100">Raw history</a></li><li><a href="config">Configuration</a></li><li><a href="docs">API docs</a></li></ul><p>Version <code>1.0.34</code>. Fysisk styrning är avstängd.</p><script>fetch('health').then(r=>r.json()).then(h=>{const e=document.getElementById('health');e.className=h.ok?'ok':'warn';e.textContent=h.ok?'HA-datainsamling fungerar.':'Appen körs, men datainsamlingen behöver konfigureras: '+(h.last_error||'okänt fel')})</script></body></html>"""


@app.get("/docs", include_in_schema=False)
async def custom_docs(): return get_swagger_ui_html(openapi_url="openapi.json", title="Energy AI API docs")


@app.get("/forecast/pv/refresh")
async def forecast_pv_refresh():
    try: return await _refresh_pv_forecast()
    except Exception as exc: raise HTTPException(502, f"PV forecast refresh failed: {exc!r}")


@app.get("/forecast/pv")
async def forecast_pv(limit: int = Query(144, ge=1, le=500)):
    result = latest_pv_forecast(limit); return {"unit":"kW","interval_minutes":15,**result}


@app.get("/forecast/pv/evaluation")
async def forecast_pv_evaluation(days: int = Query(30, ge=1, le=180)):
    return await asyncio.to_thread(evaluation_report, days)


@app.post("/forecast/pv/evaluate-now")
async def forecast_pv_evaluate_now():
    return await asyncio.to_thread(evaluate_matured_forecasts, 30)


@app.get("/forecast/load")
async def forecast_load():
    try: return await asyncio.to_thread(load_forecaster.refresh)
    except Exception as exc: raise HTTPException(500, f"Load forecast failed: {exc!r}")


@app.get("/flexible-loads")
async def flexible_loads_state():
    return {"ev": ev_state(cfg), "sauna": sauna_state(cfg), "read_only": True}


@app.get("/discover/flexible")
async def discover_flexible():
    try:
        ranked = await discover_flexible_load_entities(collector.ha)
    except Exception as exc:
        raise HTTPException(502, f"Could not discover flexible-load entities: {exc!r}")
    return {"configured": {k: v for k, v in (cfg.get("entities") or {}).items() if k.startswith("ev_") or k.startswith("sauna_")}, "ranked": ranked}


@app.get("/prices/refresh")
async def prices_refresh():
    result = await _refresh_price_horizon()
    if not any(v.get("ok") for v in result["dates"].values()): raise HTTPException(502, result)
    return result


@app.get("/prices")
async def prices(limit: int = Query(192, ge=1, le=500)): return {"area": PRICE_AREA,"unit":"öre/kWh","interval_minutes":15,"prices":get_prices(PRICE_AREA,limit)}


@app.get("/ha-diagnostics")
async def ha_diagnostics(): return await collector.ha.diagnostics()


@app.get("/discover", response_class=HTMLResponse)
async def discover():
    try: states=await collector.ha.all_states()
    except Exception as exc: raise HTTPException(502,f"Could not read Home Assistant states: {exc!r}")
    groups=_discover_candidates(states); generic=_generic_energy_candidates(states); labels={"pv_power":"PV power","house_load":"House load","grid_power":"Grid power","battery_power":"Battery power","battery_soc":"Battery SOC","spot_price":"Spot price"}; sections=[f"<h2>{labels[k]}</h2>{_table(v)}" for k,v in groups.items()]
    sections += ["<h2>All power sensors</h2>"+_table(generic["all_power_sensors"],False),"<h2>Battery / percentage candidates</h2>"+_table(generic["battery_percent_candidates"],False),"<h2>Price candidates</h2>"+_table(generic["price_candidates"],False),"<h2>Energy keyword matches</h2>"+_table(generic["energy_keyword_matches"],False)]
    return "<!doctype html><html lang='sv'><head><meta charset='utf-8'><title>Energy AI discovery</title><style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:1200px}table{border-collapse:collapse;width:100%;margin-bottom:2rem}th,td{border-bottom:1px solid #9995;text-align:left;padding:.42rem}</style></head><body><h1>Discover Home Assistant entities</h1>"+"".join(sections)+"<p><a href='./'>Tillbaka</a></p></body></html>"


@app.get("/discover.json")
async def discover_json():
    try: states=await collector.ha.all_states()
    except Exception as exc: raise HTTPException(502,f"Could not read Home Assistant states: {exc!r}")
    return {"ranked":_discover_candidates(states),"generic":_generic_energy_candidates(states)}


@app.get("/health")
async def health(): return {"ok":collector.last_error is None,"read_only":True,"collector_running":collector.running,"pv_evaluation_running":maintenance_task is not None and not maintenance_task.done(),"ha_api_authenticated":collector.ha.authenticated,"last_error":collector.last_error,"openai_configured":explainer.client is not None,"llm_model":explainer.model}


@app.get("/config")
async def config(): return cfg


@app.get("/state")
async def state():
    return collector.latest


@app.post("/collect-now")
async def collect_now(): return await collector.run_once()


@app.get("/history")
async def history(resolution:str=Query("15m",pattern="^(raw|15m)$"),limit:int=Query(96,ge=1,le=1000)): return latest_rows("raw_state" if resolution=="raw" else "state_15m",limit)


@app.post("/explain",response_model=ExplainResponse)
async def explain(req:ExplainRequest):
    payload={"proposed_action":req.proposed_action,"reason_data":req.reason_data,"policy":cfg.get("policy",{})}
    if req.include_current_state and collector.latest is not None: payload["current_state"]=collector.latest.model_dump()
    try: text=await asyncio.to_thread(explainer.explain,payload)
    except Exception as exc: raise HTTPException(502,f"LLM explanation failed: {exc!r}")
    now=datetime.now(timezone.utc).isoformat(); insert_llm(now,explainer.model,payload,text)
    return ExplainResponse(explanation_sv=text,model=explainer.model)
