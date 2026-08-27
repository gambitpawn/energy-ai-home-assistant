from __future__ import annotations

from .runtime_entry_v172 import app, core

RUNTIME_BUILD = "1.0.73"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
