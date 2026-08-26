from __future__ import annotations

import asyncio

from fastapi import HTTPException, Query

from .historical_closed_loop_v2 import compare_closed_loop
from .runtime_entry import app, core

RUNTIME_BUILD = "1.0.68"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

# runtime_entry 1.0.67 already installed the v1 closed-loop route. Replace only
# that endpoint; all UI, lifecycle, regression and other routes remain unchanged.
app.router.routes[:] = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/optimizer/evaluation/closed-loop"
]


@app.get(
    "/optimizer/evaluation/closed-loop",
    tags=["optimizer-evaluation"],
    summary="Closed-loop v3.5 head-to-head with Solinteg grid-sign normalization and energy-balance validation",
)
async def optimizer_closed_loop_v2(
    start: str | None = Query(None, description="Optional ISO timestamp. If naive, interpreted as Europe/Stockholm. Must be paired with end."),
    end: str | None = Query(None, description="Optional exclusive ISO timestamp. If naive, interpreted as Europe/Stockholm. Must be paired with start."),
    hours: int | None = Query(None, ge=1, le=744, description="Preset rolling window. Mutually exclusive with start/end and days."),
    days: int | None = Query(None, ge=1, le=31, description="Preset rolling window. Mutually exclusive with start/end and hours."),
    min_information_coverage: float = Query(0.90, ge=0.50, le=1.0),
    min_actual_coverage: float = Query(0.90, ge=0.50, le=1.0),
    include_rows: bool = Query(True),
):
    try:
        return await asyncio.to_thread(
            compare_closed_loop,
            core.cfg,
            start=start,
            end=end,
            hours=hours,
            days=days,
            min_information_coverage=min_information_coverage,
            min_actual_coverage=min_actual_coverage,
            include_rows=include_rows,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Historical closed-loop v2 comparison failed: {exc!r}")

app.openapi_schema = None
