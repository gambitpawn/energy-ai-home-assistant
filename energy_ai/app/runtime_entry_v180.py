from __future__ import annotations

import asyncio

from .runtime_entry_v179 import app, core
from .settings_store import settings_status
from .ui_v180 import install_ui_v180

RUNTIME_BUILD = "1.0.80"

install_ui_v180(app)

core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD


@app.get(
    "/settings/status",
    tags=["settings"],
    summary="Persistent Energy AI UI settings and storage precedence",
)
async def persistent_settings_status():
    return await asyncio.to_thread(settings_status)


app.openapi_schema = None
