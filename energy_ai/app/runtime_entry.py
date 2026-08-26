from __future__ import annotations

import asyncio

from fastapi import HTTPException, Query

from . import main as core
from .dashboard import install_dashboard
from .optimizer_evaluation import evaluate_matured_optimizer_days
from .overview_extension import install_overview_extension
from .tariff_entry import app
from .ui_v158 import install_ui_v158

RUNTIME_BUILD = "1.0.58"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

# Home Assistant Ingress strips its external prefix before forwarding requests.
# A relative OpenAPI server URL makes Swagger resolve "Try it out" calls against
# the same ingress path from which openapi.json was loaded, instead of HA root.
app.servers = [{"url": ".", "description": "Current Home Assistant Ingress path"}]
app.openapi_schema = None

# Layer the dashboard extensions. The latest UI middleware serves the combined
# HTML while the earlier extension keeps its history endpoint available.
install_dashboard(app, core.cfg)
install_overview_extension(app)
install_ui_v158(app, core.cfg)


@app.get(
    "/optimizer/evaluation/evaluate-now",
    tags=["optimizer-evaluation"],
    summary="Evaluate matured optimizer days (browser-friendly GET alias)",
)
async def optimizer_evaluation_now_get(
    lookback_days: int = Query(7, ge=1, le=90),
):
    try:
        return await asyncio.to_thread(evaluate_matured_optimizer_days, core.cfg, lookback_days)
    except Exception as exc:
        raise HTTPException(500, f"Optimizer hindsight evaluation failed: {exc!r}")
