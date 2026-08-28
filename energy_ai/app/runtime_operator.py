from __future__ import annotations

from . import runtime as base
from . import ui_parameters
from .operator_mode_control import install_operator_mode_control
from .settings_store import delete_setting_overrides

RELEASE_BUILD = "1.0.95"
base.RUNTIME_BUILD = RELEASE_BUILD
app = base.app

# The temporary commissioning power cap has been retired. Remove its old
# Parameters entry and any DB override so it cannot accidentally look like an
# active control limit after upgrade. Older /data/options.json files may still
# contain the key, but actuator configuration no longer reads it.
_RETIRED_PARAMETER_KEYS = {"actuator_max_physical_command_kw"}
ui_parameters.PARAMETERS[:] = [
    item for item in ui_parameters.PARAMETERS
    if item.get("key") not in _RETIRED_PARAMETER_KEYS
]
for key in _RETIRED_PARAMETER_KEYS:
    ui_parameters.PARAM_BY_KEY.pop(key, None)
try:
    delete_setting_overrides(_RETIRED_PARAMETER_KEYS)
except Exception:
    pass

install_operator_mode_control(
    app=app,
    core=base.core,
    actuator=base.ACTUATOR,
    adapter=base.ADAPTER,
    timing_scheduler=base.ACTUATOR_TIMING,
    selector_module=base.selector,
    candidate_from_selection=base._candidate_from_selection,
)


def _remove_route(path: str) -> None:
    app.router.routes[:] = [
        route for route in app.router.routes
        if getattr(route, "path", None) != path
    ]


# Keep the historical diagnostics URL, but make its semantics explicit: there
# is no longer a downstream commissioning cap in the command chain.
_remove_route("/actuator/physical-cap/status")


@app.get("/actuator/physical-cap/status", tags=["actuator"])
async def retired_physical_cap_status():
    optimizer = base.core.cfg.get("optimizer") or {}
    battery = (base.core.cfg.get("policy") or {}).get("battery") or {}
    return {
        "runtime_build": RELEASE_BUILD,
        "enabled": False,
        "retired": True,
        "reason": "temporary_commissioning_cap_removed",
        "physical_limit_source": "deterministic_actuator_safety",
        "hard_limits": {
            "battery_max_charge_kw": float(optimizer.get("battery_max_charge_kw", 8.0)),
            "battery_max_discharge_kw": float(optimizer.get("battery_max_discharge_kw", 8.0)),
            "physical_grid_import_limit_kw": float(optimizer.get("physical_grid_import_limit_kw", 13.8)),
            "grid_export_limit_kw": float(optimizer.get("grid_export_limit_kw", 10.0)),
            "hard_min_soc_pct": float(battery.get("hard_min_soc_pct", 5.0)),
            "hard_max_soc_pct": float(battery.get("hard_max_soc_pct", 100.0)),
        },
    }


app.version = RELEASE_BUILD
app.openapi_schema = None
