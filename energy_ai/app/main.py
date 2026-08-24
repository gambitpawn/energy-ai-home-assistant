import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .collector import Collector
from .config import load_config
from .db import init_db, latest_rows, insert_llm
from .llm import LLMExplainer
from .models import ExplainRequest, ExplainResponse

cfg = load_config()
collector = Collector(cfg)
explainer = LLMExplainer(cfg)
task = None


@asynccontextmanager
async def lifespan(app):
    global task
    init_db()
    try:
        await collector.run_once()
    except Exception as exc:
        collector.last_error = repr(exc)
    task = asyncio.create_task(collector.loop())
    yield
    collector.stop()
    if task:
        task.cancel()


app = FastAPI(
    title="Energy AI",
    version="0.1.4",
    description="Read-only HA energy data + LLM analysis",
    lifespan=lifespan,
)


def _candidate_score(entity: dict, category: str) -> int:
    entity_id = entity.get("entity_id", "").lower()
    attrs = entity.get("attributes", {}) or {}
    name = str(attrs.get("friendly_name", "")).lower()
    unit = str(attrs.get("unit_of_measurement", "")).lower()
    device_class = str(attrs.get("device_class", "")).lower()
    text = f"{entity_id} {name}"

    keywords = {
        "pv_power": ["pv", "solar", "sol", "photovoltaic", "inverter power", "yield power"],
        "house_load": ["load", "house", "home consumption", "consumption power", "förbrukning"],
        "grid_power": ["grid", "meter power", "import power", "export power", "nät"],
        "battery_power": ["battery power", "batteri power", "battery charge", "battery discharge"],
        "battery_soc": ["battery soc", "state of charge", "soc", "batteri soc"],
        "spot_price": ["spot", "price", "elpris", "nordpool", "electricity price"],
    }

    score = 0
    for kw in keywords[category]:
        if kw in text:
            score += 4
    if category in {"pv_power", "house_load", "grid_power", "battery_power"}:
        if device_class == "power":
            score += 3
        if unit in {"w", "kw"}:
            score += 2
    if category == "battery_soc":
        if device_class == "battery":
            score += 3
        if unit == "%":
            score += 2
    if category == "spot_price":
        if any(x in unit for x in ["kwh", "mwh", "sek", "kr", "öre", "eur"]):
            score += 2
    if entity_id.startswith("sensor."):
        score += 1
    if entity.get("state") in (None, "unknown", "unavailable", ""):
        score -= 2
    return score


def _discover_candidates(states: list[dict]) -> dict[str, list[dict]]:
    result = {}
    for category in ["pv_power", "house_load", "grid_power", "battery_power", "battery_soc", "spot_price"]:
        ranked = []
        for entity in states:
            score = _candidate_score(entity, category)
            if score <= 0:
                continue
            attrs = entity.get("attributes", {}) or {}
            ranked.append({
                "entity_id": entity.get("entity_id"),
                "friendly_name": attrs.get("friendly_name"),
                "state": entity.get("state"),
                "unit": attrs.get("unit_of_measurement"),
                "device_class": attrs.get("device_class"),
                "score": score,
            })
        ranked.sort(key=lambda x: (-x["score"], x["entity_id"] or ""))
        result[category] = ranked[:12]
    return result


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Energy AI</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 760px; }
    .ok { color: #188038; }
    .warn { color: #b06000; }
    code { background: rgba(127,127,127,.12); padding: .15rem .3rem; border-radius: 4px; }
    li { margin: .45rem 0; }
  </style>
</head>
<body>
  <h1>Energy AI</h1>
  <p>Home Assistant-appen körs.</p>
  <p id="health">Kontrollerar status…</p>
  <ul>
    <li><a href="discover">Discover HA entities</a></li>
    <li><a href="health">Health</a></li>
    <li><a href="state">Current state</a></li>
    <li><a href="history">History</a></li>
    <li><a href="config">Configuration</a></li>
    <li><a href="docs">API docs</a></li>
  </ul>
  <p>Version <code>0.1.4</code>. Fysisk styrning är avstängd i denna version.</p>
  <script>
    fetch('health')
      .then(r => r.json())
      .then(h => {
        const el = document.getElementById('health');
        el.className = h.ok ? 'ok' : 'warn';
        el.textContent = h.ok
          ? 'HA-datainsamling fungerar.'
          : 'Appen körs, men datainsamlingen behöver konfigureras: ' + (h.last_error || 'okänt fel');
      })
      .catch(() => {
        const el = document.getElementById('health');
        el.className = 'warn';
        el.textContent = 'Appen körs, men health-status kunde inte läsas.';
      });
  </script>
</body>
</html>
"""


@app.get("/discover", response_class=HTMLResponse)
async def discover():
    try:
        states = await collector.ha.all_states()
    except Exception as exc:
        raise HTTPException(502, f"Could not read Home Assistant states: {exc!r}")
    groups = _discover_candidates(states)
    labels = {
        "pv_power": "PV power",
        "house_load": "House load",
        "grid_power": "Grid power",
        "battery_power": "Battery power",
        "battery_soc": "Battery SOC",
        "spot_price": "Spot price",
    }
    sections = []
    for key, rows in groups.items():
        items = []
        for row in rows:
            items.append(
                "<tr>"
                f"<td><code>{escape(str(row['entity_id']))}</code></td>"
                f"<td>{escape(str(row['friendly_name'] or ''))}</td>"
                f"<td>{escape(str(row['state']))}</td>"
                f"<td>{escape(str(row['unit'] or ''))}</td>"
                f"<td>{row['score']}</td>"
                "</tr>"
            )
        sections.append(
            f"<h2>{labels[key]}</h2>"
            "<table><thead><tr><th>Entity</th><th>Name</th><th>State</th><th>Unit</th><th>Score</th></tr></thead>"
            f"<tbody>{''.join(items) or '<tr><td colspan=5>No candidates</td></tr>'}</tbody></table>"
        )
    return """
<!doctype html><html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energy AI discovery</title><style>
body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px} table{border-collapse:collapse;width:100%;margin-bottom:2rem}
th,td{border-bottom:1px solid #9995;text-align:left;padding:.45rem} code{font-size:.9em} h2{margin-top:2rem}
</style></head><body><h1>Discover Home Assistant entities</h1>
<p>Automatiskt rankade kandidater. Kontrollera betydelsen innan någon entity väljs; hög score är inte samma sak som verifierad semantik.</p>
""" + "".join(sections) + "<p><a href='./'>Tillbaka</a></p></body></html>"


@app.get("/discover.json")
async def discover_json():
    try:
        states = await collector.ha.all_states()
    except Exception as exc:
        raise HTTPException(502, f"Could not read Home Assistant states: {exc!r}")
    return _discover_candidates(states)


@app.get("/health")
async def health():
    return {
        "ok": collector.last_error is None,
        "read_only": True,
        "collector_running": collector.running,
        "ha_api_authenticated": collector.ha.authenticated,
        "last_error": collector.last_error,
        "openai_configured": explainer.client is not None,
        "llm_model": explainer.model,
    }


@app.get("/config")
async def config():
    return cfg


@app.get("/state")
async def state():
    if collector.latest is None:
        raise HTTPException(503, "No HA state collected yet")
    return collector.latest


@app.post("/collect-now")
async def collect_now():
    return await collector.run_once()


@app.get("/history")
async def history(
    resolution: str = Query("15m", pattern="^(raw|15m)$"),
    limit: int = Query(96, ge=1, le=1000),
):
    return latest_rows("raw_state" if resolution == "raw" else "state_15m", limit)


@app.post("/explain", response_model=ExplainResponse)
async def explain(req: ExplainRequest):
    payload = {
        "proposed_action": req.proposed_action,
        "reason_data": req.reason_data,
        "policy": cfg.get("policy", {}),
    }
    if req.include_current_state and collector.latest is not None:
        payload["current_state"] = collector.latest.model_dump()
    try:
        text = await asyncio.to_thread(explainer.explain, payload)
    except Exception as exc:
        raise HTTPException(502, f"LLM explanation failed: {exc!r}")
    now = datetime.now(timezone.utc).isoformat()
    insert_llm(now, explainer.model, payload, text)
    return ExplainResponse(explanation_sv=text, model=explainer.model)
