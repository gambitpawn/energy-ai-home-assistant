from __future__ import annotations

from contextlib import asynccontextmanager

from . import runtime_entry_v187 as v187

app = v187.app
core = v187.core
RUNTIME_BUILD = "1.0.87"

# If the previous process was ACTIVE, attempt inverter-side zero + General before
# entering the inherited lifespan. That places the release before collector /
# optimizer startup refreshes. A failed attempt remains flagged and v187's
# watchdog retries after startup; application-side writes are already disabled.
_previous_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_v187_final(application):
    if v187._NEEDS_STARTUP_RELEASE:
        try:
            await v187.ADAPTER.safe_release()
            v187._NEEDS_STARTUP_RELEASE = False
        except Exception:
            pass
    async with _previous_lifespan(application):
        yield


app.router.lifespan_context = _lifespan_v187_final
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
