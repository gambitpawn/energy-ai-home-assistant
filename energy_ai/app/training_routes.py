from __future__ import annotations

from html import escape

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse

from .config import load_config
from .ha import HomeAssistantClient
from .training import build_dataset, dataset_preview, fetch_historical_irradiance, save_upload, training_status

router = APIRouter(prefix="/training", tags=["training"])
cfg = load_config()
ha_client = HomeAssistantClient(cfg)


@router.get("/", response_class=HTMLResponse)
async def training_page():
    status = training_status()
    files = status["files"]
    rows = "".join(
        f"<tr><td>{escape(str(f.get('name','')))}</td><td>{escape(str(f.get('kind','')))}</td><td>{f.get('rows','')}</td><td>{f.get('bytes','')}</td></tr>"
        for f in files
    ) or "<tr><td colspan='4'>Inga träningsfiler ännu.</td></tr>"
    return f"""<!doctype html><html lang='sv'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Energy AI training</title><style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:900px}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}th,td{{border-bottom:1px solid #9995;text-align:left;padding:.45rem}}input,button{{margin:.4rem 0}}.box{{padding:1rem;border:1px solid #9995;border-radius:8px;margin:1rem 0}}</style></head><body><h1>Training data</h1><p>Filer sparas persistent i <code>{escape(status['training_dir'])}</code>.</p><div class='box'><h2>Ladda upp historik</h2><form action='upload' method='post' enctype='multipart/form-data'><input type='file' name='file' accept='.csv,text/csv' required><br><button type='submit'>Ladda upp CSV</button></form></div><div class='box'><h2>Historisk solinstrålning</h2><p>Hämtar automatiskt satellitbaserad GTI för Solinteg-filens hela datumintervall med konfigurerad taklutning och azimut. Efter hämtningen byggs träningsdatasetet om automatiskt.</p><form action='fetch-irradiance' method='post'><button type='submit'>Hämta historisk GTI</button></form></div><h2>Filer</h2><table><thead><tr><th>Fil</th><th>Identifierad typ</th><th>Rader</th><th>Bytes</th></tr></thead><tbody>{rows}</tbody></table><p><a href='build'>Bygg/bygg om 15-minutersdataset</a></p><p><a href='preview?limit=20'>Dataset preview</a> · <a href='status'>JSON status</a> · <a href='../'>Tillbaka</a></p></body></html>"""


@router.post("/upload")
async def training_upload(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV files are accepted")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "Training CSV exceeds 50 MB")
    path = save_upload(file.filename or "upload.csv", data)
    info = next((x for x in training_status()["files"] if x.get("name") == path.name), None)
    return {"ok": True, "saved_to": str(path), "detected": info}


@router.post("/fetch-irradiance")
async def training_fetch_irradiance():
    try:
        return await fetch_historical_irradiance(cfg, ha_client)
    except Exception as exc:
        raise HTTPException(502, f"Historical irradiance fetch failed: {exc!r}")


@router.get("/status")
async def training_status_route():
    return training_status()


@router.get("/build")
async def training_build():
    try:
        return build_dataset()
    except Exception as exc:
        raise HTTPException(500, f"Training dataset build failed: {exc!r}")


@router.get("/preview")
async def training_preview(limit: int = Query(20, ge=1, le=200)):
    return dataset_preview(limit)
