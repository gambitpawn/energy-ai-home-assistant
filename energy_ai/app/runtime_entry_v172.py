from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import Query

from . import neural_training as neural_training_module
from .db import DB_PATH
from .neural_training import training_maturity_status
from .runtime_entry_v171 import app, core

RUNTIME_BUILD = "1.0.72"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD


def _latest_completed_actual_start():
    """Return the latest fully completed 15-minute bucket, never the live partial bucket."""
    now = datetime.now(timezone.utc)
    current_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    latest_allowed = current_start - timedelta(minutes=15)
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute("SELECT MAX(bucket_start) FROM state_15m WHERE bucket_start<=?", (latest_allowed.isoformat(),)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        return neural_training_module._utc(str(row[0]))
    except Exception:
        return None


# build_training_samples resolves this helper through its module globals at call time,
# so the same completed-bucket rule applies to bootstrap and explicit sample builds.
neural_training_module._latest_actual_start = _latest_completed_actual_start


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
