from __future__ import annotations

import asyncio
from html import escape

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse

from .config import load_config
from .ha import HomeAssistantClient
from .load_calibration import model_status as load_model_status, train_load_model
from .load_forecast import LoadForecaster
from .pv_calibration import model_status, train_pv_model
from .training import build_dataset, dataset_preview, fetch_historical_irradiance, fetch_historical_weather, save_upload, training_status

router = APIRouter(prefix="/training", tags=["training"])
cfg = load_config()
ha_client = HomeAssistantClient(cfg)
load_forecaster = LoadForecaster(cfg)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def training_page(request: Request):
    status = training_status(); pv_status = model_status(); load_status = load_model_status(); files = status["files"]
    rows = "".join(f"<tr><td>{escape(str(f.get('name','')))}</td><td>{escape(str(f.get('kind','')))}</td><td>{f.get('rows','')}</td><td>{f.get('bytes','')}</td></tr>" for f in files) or "<tr><td colspan='4'>Inga träningsfiler ännu.</td></tr>"
    slash_form = request.url.path.endswith("/"); child = "" if slash_form else "training/"; back = "../" if slash_form else "./"
    pv_text = "Tränad modell finns." if pv_status["model_exists"] else "Ingen tränad modell ännu."
    load_text = "Tränad modell finns." if load_status["model_exists"] else "Ingen tränad modell ännu."
    return f"""<!doctype html><html lang='sv'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Energy AI training</title><style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:900px}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}th,td{{border-bottom:1px solid #9995;text-align:left;padding:.45rem}}input,button{{margin:.4rem 0}}.box{{padding:1rem;border:1px solid #9995;border-radius:8px;margin:1rem 0}}</style></head><body><h1>Training data</h1><p>Filer sparas persistent i <code>{escape(status['training_dir'])}</code>.</p><div class='box'><h2>Ladda upp historik</h2><form action='{child}upload' method='post' enctype='multipart/form-data'><input type='file' name='file' accept='.csv,text/csv' required><br><button type='submit'>Ladda upp CSV</button></form></div><div class='box'><h2>Historiskt väder</h2><p>Hämtar temperatur och molnighet för Solinteg-perioden och bygger om datasetet.</p><form action='{child}fetch-weather' method='post'><button type='submit'>Hämta historiskt väder</button></form></div><div class='box'><h2>Historisk solinstrålning</h2><form action='{child}fetch-irradiance' method='post'><button type='submit'>Hämta historisk GTI</button></form></div><div class='box'><h2>PV calibration</h2><p>{pv_text}</p><form action='{child}pv/train' method='post'><button type='submit'>Träna PV-modell</button></form><p><a href='{child}pv/status'>PV-modellstatus och rapport</a></p></div><div class='box'><h2>Lastprognos v2</h2><p>{load_text} Adaptiv 28-dagars veckoprofil + 7-dagars nivåkorrektion + temperatur + residual-ML. Träning hämtar historiskt väder automatiskt.</p><form action='{child}load/train' method='post'><button type='submit'>Träna lastmodell v2</button></form><p><a href='{child}load/status'>Lastmodellstatus och rapport</a> · <a href='{child}load/forecast'>36 h lastprognos</a></p></div><h2>Filer</h2><table><thead><tr><th>Fil</th><th>Identifierad typ</th><th>Rader</th><th>Bytes</th></tr></thead><tbody>{rows}</tbody></table><p><a href='{child}build'>Bygg/bygg om 15-minutersdataset</a></p><p><a href='{child}preview?limit=20'>Dataset preview</a> · <a href='{child}status'>JSON status</a> · <a href='{back}'>Tillbaka</a></p></body></html>"""


@router.post("/upload")
async def training_upload(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".csv"): raise HTTPException(400, "Only CSV files are accepted")
    data = await file.read()
    if not data: raise HTTPException(400, "Uploaded file is empty")
    if len(data) > 50 * 1024 * 1024: raise HTTPException(413, "Training CSV exceeds 50 MB")
    path = save_upload(file.filename or "upload.csv", data)
    info = next((x for x in training_status()["files"] if x.get("name") == path.name), None)
    return {"ok": True, "saved_to": str(path), "detected": info}


@router.post("/fetch-irradiance")
async def training_fetch_irradiance():
    try: return await fetch_historical_irradiance(cfg, ha_client)
    except Exception as exc: raise HTTPException(502, f"Historical irradiance fetch failed: {exc!r}")


@router.post("/fetch-weather")
async def training_fetch_weather():
    try: return await fetch_historical_weather(ha_client)
    except Exception as exc: raise HTTPException(502, f"Historical weather fetch failed: {exc!r}")


@router.get("/status")
async def training_status_route(): return training_status()

@router.get("/build")
async def training_build():
    try: return build_dataset()
    except Exception as exc: raise HTTPException(500, f"Training dataset build failed: {exc!r}")

@router.get("/preview")
async def training_preview(limit: int = Query(20, ge=1, le=200)): return dataset_preview(limit)

@router.post("/pv/train")
async def training_pv_train():
    capacity_kw = float(cfg.get("forecast", {}).get("pv", {}).get("capacity_kw", 10.0))
    try:
        ha_cfg = await ha_client.system_config(); lat = ha_cfg.get("latitude"); lon = ha_cfg.get("longitude")
        if lat is None or lon is None: raise RuntimeError("Home Assistant config does not expose latitude/longitude")
        return await asyncio.to_thread(train_pv_model, capacity_kw, float(lat), float(lon))
    except Exception as exc: raise HTTPException(500, f"PV calibration training failed: {exc!r}")

@router.get("/pv/status")
async def training_pv_status(): return model_status()

@router.post("/load/train")
async def training_load_train():
    try:
        weather = await fetch_historical_weather(ha_client)
        report = await asyncio.to_thread(train_load_model)
        report["weather_enrichment"] = {"source": weather.get("source"), "interpolated_15m_rows": weather.get("interpolated_15m_rows")}
        return report
    except Exception as exc: raise HTTPException(500, f"Load forecast v2 training failed: {exc!r}")

@router.get("/load/status")
async def training_load_status(): return load_model_status()

@router.get("/load/forecast")
async def training_load_forecast():
    try: return await asyncio.to_thread(load_forecaster.refresh)
    except Exception as exc: raise HTTPException(500, f"Load forecast failed: {exc!r}")
