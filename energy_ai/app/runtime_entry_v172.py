from __future__ import annotations

import asyncio

from fastapi import Query

from .neural_training import training_maturity_status
from .runtime_entry_v171 import app, core

RUNTIME_BUILD = "1.0.72"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD


@app.get(
    "/engines/neural/maturity",
    tags=["engines-neural"],
    summary="Canonical decision-vintage and teacher-label maturity diagnostics",
)
async def neural_maturity(
    candidate_limit: int = Query(2000, ge=10, le=10000),
):
    return await asyncio.to_thread(training_maturity_status, core.cfg, candidate_limit)


app.openapi_schema = None
