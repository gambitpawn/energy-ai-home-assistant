from __future__ import annotations

from .adaptive_learning import mark_orphaned_running_runs
from .runtime_entry_v176 import app, core

RUNTIME_BUILD = "1.0.77"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

# A persisted 'running' row cannot belong to this newly started process.
# Mark it interrupted so the completed-day learner can restart cleanly.
ORPHANED_ADAPTIVE_RUNS_ON_STARTUP = mark_orphaned_running_runs()

app.openapi_schema = None
