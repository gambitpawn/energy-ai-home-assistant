from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException, Query

from . import main as core
from .app_comparison_v2 import compare_app_vs_planner
from .dashboard import install_dashboard
from .live_state import LiveStateCache
from .optimizer_evaluation import evaluate_matured_optimizer_days
from .overview_extension import install_overview_extension
from .tariff_entry import app
from .ui_v158 import install_ui_v158
from .ui_v159 import install_ui_v159
from .ui_v160 import install_ui_v160
from .ui_v161 import install_ui_v161
from .ui_v161_fix import install_ui_v161_fix
from .ui_v163 import install_ui_v163
from .ui_v164 import install_ui_v164
from .ui_v165 import install_ui_v165

RUNTIME_BUILD = "1.0.66"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

# Home Assistant Ingress strips its external prefix before forwarding requests.
# A relative OpenAPI server URL makes Swagger resolve "Try it out" calls against
# the same ingress path from which openapi.json was loaded, instead of HA root.
app.servers = [{"url": ".", "description": "Current Home Assistant Ingress path"}]
app.openapi_schema = None

# Live UI state is deliberately separate from the 60-second persisted collector.
# The cache is seeded from the collector's first snapshot, then maintained through
# Home Assistant state_changed events over one websocket connection.
live_state_cache = LiveStateCache(core.cfg, core.collector.ha)
_base_lifespan = app.router.lifespan_context


@asynccontextmanager
async def runtime_lifespan(app_instance):
    async with _base_lifespan(app_instance) as lifespan_state:
        live_state_cache.seed(core.collector.latest)
        live_task = asyncio.create_task(live_state_cache.run(), name="energy-ai-live-state")
        try:
            yield lifespan_state
        finally:
            live_state_cache.stop()
            live_task.cancel()
            try:
                await live_task
            except asyncio.CancelledError:
                pass


app.router.lifespan_context = runtime_lifespan

# Layer the dashboard extensions. The latest UI middleware serves the combined
# HTML while earlier extensions keep their supporting endpoints available.
install_dashboard(app, core.cfg)
install_overview_extension(app)
install_ui_v158(app, core.cfg)
install_ui_v159(app, core.cfg)
install_ui_v160(app, live_state_cache)
install_ui_v161(app, core.cfg, core.collector.ha)
install_ui_v161_fix(app)
install_ui_v163(app, live_state_cache)
install_ui_v164(app)
install_ui_v165(app)


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


@app.get(
    "/optimizer/evaluation/app-comparison",
    tags=["optimizer-evaluation"],
    summary="Compare actual app/inverter operation with the stored realtime shadow planner",
)
async def optimizer_app_comparison(
    start: str | None = Query(None, description="Optional ISO timestamp. If naive, interpreted as Europe/Stockholm. Must be paired with end."),
    end: str | None = Query(None, description="Optional exclusive ISO timestamp. If naive, interpreted as Europe/Stockholm. Must be paired with start."),
    hours: int | None = Query(None, ge=1, le=744, description="Preset rolling window. Mutually exclusive with start/end and days."),
    days: int | None = Query(None, ge=1, le=31, description="Preset rolling window. Mutually exclusive with start/end and hours."),
    min_plan_coverage: float = Query(0.90, ge=0.50, le=1.0),
    min_actual_coverage: float = Query(0.90, ge=0.50, le=1.0),
    include_rows: bool = Query(True),
):
    try:
        return await asyncio.to_thread(
            compare_app_vs_planner,
            core.cfg,
            start=start,
            end=end,
            hours=hours,
            days=days,
            min_plan_coverage=min_plan_coverage,
            min_actual_coverage=min_actual_coverage,
            include_rows=include_rows,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"App-vs-planner comparison failed: {exc!r}")
