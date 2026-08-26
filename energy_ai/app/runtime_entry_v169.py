from __future__ import annotations

import asyncio

from fastapi import HTTPException, Query

from .regret_decomposition import regret_decomposition
from .runtime_entry_v168 import app, core

RUNTIME_BUILD = "1.0.69"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD


@app.get(
    "/optimizer/evaluation/regret-decomposition",
    tags=["optimizer-evaluation"],
    summary="Decompose realtime v3.5 gap into forecast, price-information and planner/horizon/policy components",
)
async def optimizer_regret_decomposition(
    start: str | None = Query(None, description="Optional ISO timestamp. If naive, interpreted as Europe/Stockholm. Must be paired with end."),
    end: str | None = Query(None, description="Optional exclusive ISO timestamp. If naive, interpreted as Europe/Stockholm. Must be paired with start."),
    hours: int | None = Query(None, ge=1, le=744, description="Preset rolling window. Mutually exclusive with start/end and days."),
    days: int | None = Query(None, ge=1, le=31, description="Preset rolling window. Mutually exclusive with start/end and hours."),
    include_rows: bool = Query(False),
):
    try:
        return await asyncio.to_thread(
            regret_decomposition,
            core.cfg,
            start=start,
            end=end,
            hours=hours,
            days=days,
            include_rows=include_rows,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Optimizer regret decomposition failed: {exc!r}")

app.openapi_schema = None
