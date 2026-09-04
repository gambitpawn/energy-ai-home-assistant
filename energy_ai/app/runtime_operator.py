from __future__ import annotations

from .persistent_operating_mode import install_persistent_operating_mode, prepare_startup

# Capture the persisted operator intent before runtime.py performs its mandatory
# startup disarm. This ordering is critical: runtime.py intentionally stages the
# physical actuator in Shadow on every new process.
_STARTUP_MODE_STATE = prepare_startup()

from . import runtime as base
from . import operator_mode_control as operator_mode_control_module
from . import ui_models as ui_models_module
from . import ui_parameters
from .actuator_arm_control_mode import install_arm_control_mode_patch
from .actuator_watchdog import install_actuator_watchdog_patch
from .battery_health_routes import install_battery_health_routes
from .deterministic_refined_runtime import refined_runtime_status, install_refined_runtime_patch
from .engine_operator_selection import install_operator_engine_routing
from .maintenance_coordination import install_process_worker
from .operator_mode_control import install_operator_mode_control
from .pool import install_pool_routes
from .pool_installation_profile import install_pool_installation_profile
from .release_version import RELEASE_VERSION
from .retired_ml_cleanup import cleanup_retired_ml
from .settings_store import delete_setting_overrides
from .stochastic_runtime import stochastic_runtime_status, install_stochastic_runtime_patch
from .ui_control_truth import decision_summary as control_truth_decision_summary

RELEASE_BUILD = RELEASE_VERSION
base.RUNTIME_BUILD = RELEASE_BUILD
# dashboard.py renders state.config.runtime_build from the already-loaded core
# configuration. Synchronize it with the canonical add-on build before serving
# any UI request; this prevents the UI from exposing stale base-runtime literals.
base.core.cfg["runtime_build"] = RELEASE_BUILD
app = base.app

# Remove artifacts and selector state left by the retired learned-model
# architecture before selector routing is installed. The frozen deterministic
# baseline and adaptive learning state are explicitly outside the cleanup scope.
RETIRED_ML_CLEANUP = cleanup_retired_ml()

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

# Operator engine selection is a routing override only. Install it before the
# remaining deterministic challenger preparation wrappers so all active engines
# are written for the same information vintage before Auto/manual routing.
install_operator_engine_routing()

install_stochastic_runtime_patch(base.core.cfg)
install_refined_runtime_patch(base.core.cfg)

# Fork the single maintenance process only after every active model/selector
# runtime patch above is installed, but before FastAPI lifespan tasks or worker
# threads start.
MAINTENANCE_PROCESS = install_process_worker(app=app)

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

# Fault notifications use an existing Home Assistant notify service rather than
# embedding SMTP credentials in Energy AI. The target may be an email address
# when the configured notify service supports explicit targets.
_NOTIFICATION_PARAMETERS = [
    ui_parameters.parameter(
        "Notifications",
        "fault_notification_enabled",
        "Fault email enabled",
        "bool",
        False,
        "Send an email/notification when Energy AI enters PAUSED after an actuator or runtime fault.",
        recommended="Enable after configuring and testing the Home Assistant notify service below.",
    ),
    ui_parameters.parameter(
        "Notifications",
        "fault_notification_service",
        "Home Assistant notify service",
        "str",
        "",
        "Existing Home Assistant notify service used for fault mail, for example notify.email_supervisor.",
        physical="Use a notify.* service that is already configured and verified in Home Assistant.",
    ),
    ui_parameters.parameter(
        "Notifications",
        "fault_notification_target",
        "Supervisor email / target",
        "str",
        "",
        "Optional notify target. For an SMTP notify service this can be the supervisor email address; leave blank if the service has a fixed recipient.",
    ),
]
for item in _NOTIFICATION_PARAMETERS:
    key = str(item["key"])
    if key not in ui_parameters.PARAM_BY_KEY:
        ui_parameters.PARAMETERS.append(item)
        ui_parameters.PARAM_BY_KEY[key] = item

install_operator_mode_control(
    app=app,
    core=base.core,
    actuator=base.ACTUATOR,
    adapter=base.ADAPTER,
    timing_scheduler=base.ACTUATOR_TIMING,
    selector_module=base.selector,
    candidate_from_selection=base._candidate_from_selection,
)

PERSISTENT_OPERATING_MODE = install_persistent_operating_mode(
    app=app,
    actuator=base.ACTUATOR,
    ha=base.core.collector.ha,
    startup_state=_STARTUP_MODE_STATE,
)


def _remove_route(path: str) -> None:
    app.router.routes[:] = [
        route for route in app.router.routes
        if getattr(route, "path", None) != path
    ]


@app.get("/engines/refined-deterministic/status", tags=["engines"])
async def refined_deterministic_status():
    return {"runtime_build": RELEASE_BUILD, **refined_runtime_status()}


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
