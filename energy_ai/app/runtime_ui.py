from __future__ import annotations

from datetime import datetime, timezone

import sqlite3
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import DASHBOARD_HTML, install_dashboard
from .overview_extension import OVERVIEW_EXTENSION, _history_rows
from .ui_charts import CHARTS_EXTENSION
from .ui_evaluation import EVALUATION_EXTENSION, install_evaluation_routes
from .ui_gradient import GRADIENT_UI_EXTENSION
from .ui_live import LIVE_EXTENSION, install_live_routes
from .ui_model_control import MODELS_CONTROL_EXTENSION, install_model_control_routes
from .ui_models import MODELS_EXTENSION, install_model_routes
from .ui_parameters import PARAMETERS_EXTENSION, install_parameter_routes
from .ui_stochastic import STOCHASTIC_UI_EXTENSION


CURRENT_UI_EXTENSION = (
    OVERVIEW_EXTENSION
    + EVALUATION_EXTENSION
    + LIVE_EXTENSION
    + PARAMETERS_EXTENSION
    + MODELS_EXTENSION
    + MODELS_CONTROL_EXTENSION
    + STOCHASTIC_UI_EXTENSION
    + GRADIENT_UI_EXTENSION
    + CHARTS_EXTENSION
)


def _remove_route(app: FastAPI, path: str, methods: set[str] | None = None) -> None:
    methods = {m.upper() for m in methods} if methods else None
    kept = []
    for route in app.router.routes:
        if getattr(route, "path", None) != path:
            kept.append(route)
            continue
        route_methods = {str(m).upper() for m in (getattr(route, "methods", None) or set())}
        if methods is not None and not (route_methods & methods):
            kept.append(route)
    app.router.routes[:] = kept


def install_runtime_ui(app: FastAPI, core, live_state_cache) -> None:
    # dashboard.py remains the source of the base HTML and its data endpoints,
    # but its bare /ui route is replaced by exactly one consolidated renderer.
    install_dashboard(app, core.cfg)
    _remove_route(app, "/ui", {"GET"})

    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    async def current_ui():
        return DASHBOARD_HTML.replace("</body>", CURRENT_UI_EXTENSION + "</body>")

    # overview_extension previously installed its own /ui middleware only to add
    # OVERVIEW_EXTENSION. Keep the data endpoint, not the middleware.
    @app.get("/ui/overview-history", include_in_schema=False)
    async def overview_history(hours: int = Query(24, ge=1, le=72)):
        try:
            return JSONResponse(_history_rows(hours))
        except sqlite3.OperationalError:
            return JSONResponse({"hours": hours, "now": datetime.now(timezone.utc).isoformat(), "rows": []})

    install_evaluation_routes(app, core.cfg)
    install_live_routes(app, core.cfg, live_state_cache, core.collector.ha)
    install_parameter_routes(app)
    install_model_routes(app, core.cfg)
    install_model_control_routes(app, core.cfg)
