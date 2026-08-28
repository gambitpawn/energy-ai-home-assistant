from __future__ import annotations

from . import runtime as base
from . import ui_parameters
from .engine_operator_selection import install_operator_engine_routing
from .hybrid_runtime import hybrid_runtime_status, install_hybrid_runtime_patch
from .neural_qualification import (
    install_qualification_candidate_runtime,
    qualification_status,
)
from .operator_mode_control import install_operator_mode_control
from .settings_store import delete_setting_overrides

RELEASE_BUILD = "1.0.98"
base.RUNTIME_BUILD = RELEASE_BUILD
app = base.app

# Continuous neural training and race qualification are separate. Freeze one
# neural revision for neural_v1 + hybrid_v1 while new latest models may continue
# to train in the background.
QUALIFICATION_RUNTIME = install_qualification_candidate_runtime()
_ORIGINAL_NEURAL_RUNTIME_STATUS = base.neural_runtime_status


def _qualification_aware_neural_runtime_status():
    status = _ORIGINAL_NEURAL_RUNTIME_STATUS()
    candidate = qualification_status()
    status["latest_model_shadow_ready"] = bool(status.get("shadow_ready"))
    status["qualification_candidate"] = candidate
    status["shadow_ready"] = bool(candidate.get("candidate_ready"))
    return status


base.neural_runtime_status = _qualification_aware_neural_runtime_status

# Operator engine selection is a routing override only. Install it before the
# hybrid wrapper so hybrid_v1 is still prepared for the shared information
# vintage before either Auto or a manual engine is routed.
install_operator_engine_routing()

# hybrid_v1 must be prepared for the shared information vintage before the
# existing selector gateway chooses which engine to route.
install_hybrid_runtime_patch(base.core.cfg)

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


@app.get("/engines/neural/qualification", tags=["engines"])
async def neural_qualification_status():
    return {"runtime_build": RELEASE_BUILD, **qualification_status()}


@app.get("/engines/hybrid/status", tags=["engines"])
async def hybrid_status():
    return {"runtime_build": RELEASE_BUILD, **hybrid_runtime_status()}


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
