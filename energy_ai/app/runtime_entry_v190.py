from __future__ import annotations

from . import runtime_entry_v189 as v189
from . import ui_v164
from .actuator_config import effective_actuator_config_report
from .actuator_physical_cap_v190 import install_physical_command_cap_patch

app = v189.app
core = v189.core
RUNTIME_BUILD = "1.0.90"

# The cap is installed only after the complete v1.0.89 runtime has been wired.
# Optimizer, model selector, shared-vintage comparison and frozen v3.5 remain
# unchanged; only DeterministicActuator's final safety result is capped before
# dry-run reporting or Solinteg dispatch.
install_physical_command_cap_patch()


def _install_physical_cap_parameter() -> None:
    key = "actuator_max_physical_command_kw"
    if key not in {item.get("key") for item in ui_v164.PARAMETERS}:
        ui_v164.PARAMETERS.append(
            ui_v164.p(
                "Actuator – safety",
                key,
                "Maximum physical command",
                "float",
                2.0,
                "Final symmetric downstream cap on battery charge/discharge power sent to Solinteg. It does not change optimizer or model decisions.",
                unit="kW",
                recommended="2 kW for commissioning; increase deliberately after verified physical operation.",
                minimum=0.0,
                maximum=8.0,
                step=0.5,
                physical="Applies after the deterministic SOC/grid safety envelope and before Solinteg dispatch.",
            )
        )
    ui_v164.PARAM_BY_KEY.clear()
    ui_v164.PARAM_BY_KEY.update({item["key"]: item for item in ui_v164.PARAMETERS})


_install_physical_cap_parameter()


@app.get(
    "/actuator/physical-cap/status",
    tags=["actuator"],
    summary="Downstream physical command cap used for commissioning and staged rollout",
)
async def actuator_physical_cap_status_v190():
    report = effective_actuator_config_report(core.cfg)
    runtime = report.get("runtime") or {}
    return {
        "runtime_build": RUNTIME_BUILD,
        "max_physical_command_kw": float(runtime.get("max_physical_command_kw", 2.0)),
        "applies_to": ["charge", "discharge"],
        "position_in_control_chain": "after_deterministic_safety_before_solinteg_dispatch",
        "affects_optimizer_or_selector": False,
        "restart_required": bool(report.get("restart_required")),
        "runtime_matches_persisted": bool(report.get("runtime_matches_persisted")),
    }


core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
