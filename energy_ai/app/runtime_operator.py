from __future__ import annotations

from . import engine_operator_selection as engine_operator_selection_module
from . import runtime as base
from . import ui_models as ui_models_module
from . import ui_parameters
from .actuator_arm_control_mode import install_arm_control_mode_patch
from .actuator_watchdog import install_actuator_watchdog_patch
from .battery_health_routes import install_battery_health_routes
from .deterministic_refined_runtime import refined_runtime_status, install_refined_runtime_patch
from .engine_operator_selection import install_operator_engine_routing
from .gradient_qualification import (
    install_qualification_candidate_runtime as install_gradient_qualification_runtime,
    qualification_status as gradient_qualification_status,
)
from .gradient_runtime import gradient_runtime_status, install_gradient_runtime_patch
from .gradient_selector_qualification import install_gradient_selector_qualification
from .hybrid_runtime import hybrid_runtime_status, install_hybrid_runtime_patch
from .neural_qualification import (
    install_qualification_candidate_runtime,
    qualification_status,
)
from .operator_mode_control import install_operator_mode_control
from .pool import install_pool_routes
from .pool_installation_profile import install_pool_installation_profile
from .settings_store import delete_setting_overrides
from .stochastic_runtime import stochastic_runtime_status, install_stochastic_runtime_patch
from .ui_control_truth import decision_summary as control_truth_decision_summary

RELEASE_BUILD = "1.0.122"
base.RUNTIME_BUILD = RELEASE_BUILD
app = base.app
engine_operator_selection_module.DISPLAY_NAMES["deterministic_refined_v1"] = "Refined deterministic"
engine_operator_selection_module.DISPLAY_NAMES["stochastic_deterministic_v1"] = "Stochastic deterministic"
engine_operator_selection_module.DISPLAY_NAMES["gradient_v1"] = "Gradient boost"

# The production overview must describe the routed/actuated control path rather
# than the frozen base optimizer plan. install_model_routes() has already created
# its endpoint during base-runtime import, but the endpoint resolves this module
# global at request time, so replacing it here changes the data source without
# duplicating the route.
ui_models_module.decision_summary = control_truth_decision_summary

# Successful arming must leave the inverter in EMS control mode at zero power.
# Safe release belongs only to Shadow/fault transitions. The base actuator is
# already instantiated by runtime.py, but patching the class method here affects
# that instance before any operator activation can call it.
install_arm_control_mode_patch()

# Runtime.py has already installed the canonical watchdog before the
# decision-start scheduler captured actuator methods. Repeating the idempotent
# install here documents the operator runtime dependency without layering a second
# watchdog implementation.
install_actuator_watchdog_patch()

# Continuous learned-model training and race qualification are separate. Neural
# + hybrid share one frozen neural candidate; gradient_v1 has its own independently
# frozen candidate and can retrain in the background without resetting robust10.
QUALIFICATION_RUNTIME = install_qualification_candidate_runtime()
GRADIENT_QUALIFICATION_RUNTIME = install_gradient_qualification_runtime()
install_gradient_selector_qualification()
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
# challenger preparation wrappers so all challengers are written for the same
# information vintage before either Auto or a manual engine is routed.
install_operator_engine_routing()

# Challenger wrappers are stacked around the same selector gateway. Gradient is
# installed last, so the call order is gradient -> refined deterministic ->
# stochastic -> hybrid -> operator routing -> robust selector. All decisions
# still share one information vintage.
install_hybrid_runtime_patch(base.core.cfg)
install_stochastic_runtime_patch(base.core.cfg)
install_refined_runtime_patch(base.core.cfg)
install_gradient_runtime_patch(base.core.cfg)

# Pool integration starts read-only. Apply mappings verified on the installed
# AquaTemp entity surface before installing routes: O08 is actual compressor
# current, while the binary Power entity is status only and must not be treated
# as W/kW telemetry.
POOL_INSTALLATION_PROFILE = install_pool_installation_profile()
install_pool_routes(app, base.core.cfg, base.core.collector.ha)

# Standalone battery-health economics diagnostics. These routes evaluate the
# canonical cost helper and a parallel perfect-information hindsight comparison;
# they do not alter planner, selector or actuator state and perform no writes.
install_battery_health_routes(app, base.core.cfg)

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


@app.get("/engines/gradient/qualification", tags=["engines"])
async def gradient_qualification_status_route():
    return {"runtime_build": RELEASE_BUILD, **gradient_qualification_status()}


@app.get("/engines/gradient/status", tags=["engines"])
async def gradient_status():
    return {"runtime_build": RELEASE_BUILD, **gradient_runtime_status()}


@app.get("/engines/refined-deterministic/status", tags=["engines"])
async def refined_deterministic_status():
    return {"runtime_build": RELEASE_BUILD, **refined_runtime_status()}


@app.get("/engines/hybrid/status", tags=["engines"])
async def hybrid_status():
    return {"runtime_build": RELEASE_BUILD, **hybrid_runtime_status()}


@app.get("/engines/stochastic/status", tags=["engines"])
async def stochastic_status():
    return {"runtime_build": RELEASE_BUILD, **stochastic_runtime_status()}


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