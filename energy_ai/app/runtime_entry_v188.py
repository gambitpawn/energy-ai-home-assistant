from __future__ import annotations

from fastapi import HTTPException

from . import runtime_entry_v187_final as v187
from .actuator_diagnostics_v188 import install_actuator_diagnostics_patch

app = v187.app
core = v187.core
RUNTIME_BUILD = "1.0.88"

# Patch the already-created v1.87 actuator instance through its class methods.
# This preserves the tested physical command path while adding configuration
# freshness/mode gates around preflight, arm, ACTIVE routing and watchdog use.
install_actuator_diagnostics_patch()

# Capture and replace the v1.87 ACTIVE transition route. A previous successful
# arm is not sufficient if actuator parameters were changed afterwards or the
# configured safe release mode no longer matches the inverter's normal mode.
_previous_control_endpoint = None
for _route in app.router.routes:
    if getattr(_route, "path", None) == "/control/mode/{mode}":
        _previous_control_endpoint = getattr(_route, "endpoint", None)
        break

app.router.routes[:] = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/control/mode/{mode}"
]


@app.post("/control/mode/{mode}", tags=["control"], summary="Set production mode with v1.0.88 actuator configuration gate")
async def production_control_mode_v188(mode: str):
    normalized = str(mode).strip().lower()
    if normalized == "active":
        preflight = await v187.ACTUATOR.preflight()
        if not preflight.get("ok"):
            raise HTTPException(
                409,
                {
                    "error": "actuator_preflight_required_before_active",
                    "preflight": preflight,
                },
            )
    if _previous_control_endpoint is None:
        raise HTTPException(500, "v1.87 control transition endpoint is unavailable")
    return await _previous_control_endpoint(mode)


core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
