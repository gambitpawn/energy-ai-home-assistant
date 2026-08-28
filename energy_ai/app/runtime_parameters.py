from __future__ import annotations

from . import ui_v164


def install_runtime_parameters() -> None:
    definitions = [
        ui_v164.p("Optimizer – live replanning", "optimizer_soc_replan_threshold_pct", "SOC replan threshold", "float", 2.0, "Recalculate a deterministic live plan between quarter boundaries when measured battery SOC differs from the interpolated plan by at least this many percentage points.", unit="percentage points", recommended="2 percentage points balances responsiveness against unnecessary replans.", minimum=0.1, maximum=20, step=0.1),
        ui_v164.p("Optimizer – live replanning", "optimizer_soc_replan_emergency_threshold_pct", "Emergency SOC deviation", "float", 5.0, "A deviation at or above this level bypasses the ordinary replan cooldown.", unit="percentage points", recommended="5 percentage points.", minimum=0.1, maximum=30, step=0.1),
        ui_v164.p("Optimizer – live replanning", "optimizer_soc_replan_min_interval_seconds", "Minimum replan interval", "int", 60, "Minimum time between ordinary SOC-triggered live replans. Emergency deviations can bypass it.", unit="s", recommended="60 seconds.", minimum=0, maximum=900, step=15),
        ui_v164.p("Optimizer – live replanning", "optimizer_soc_observation_max_age_seconds", "Maximum SOC observation age", "int", 180, "The live planner refuses to re-anchor on an SOC observation older than this limit.", unit="s", recommended="180 seconds with a 60-second collector cadence.", minimum=15, maximum=1800, step=15),
        ui_v164.p("Actuator – Solinteg", "entity_solinteg_working_mode", "Solinteg Working Mode entity", "str", "", "Home Assistant select entity exposing Solinteg Working Mode. Leave blank for strict auto-discovery."),
        ui_v164.p("Actuator – Solinteg", "entity_solinteg_battery_power_target", "Solinteg battery power target entity", "str", "", "Home Assistant number entity exposing EMS BattCtrl Charge Discharge Power Target. Leave blank for strict auto-discovery."),
        ui_v164.p("Actuator – Solinteg", "actuator_control_working_mode", "Control working mode", "str", "EMS BattCtrl", "Working Mode option used while Energy AI controls battery power."),
        ui_v164.p("Actuator – Solinteg", "actuator_safe_working_mode", "Safe release working mode", "str", "General", "Working Mode restored on disarm, fault or clean shutdown."),
        ui_v164.p("Actuator – safety", "actuator_soc_guard_margin_pct", "Hard-SOC guard margin", "float", 1.0, "Additional margin inside hard SOC limits enforced over a full control interval.", unit="percentage points", recommended="1 percentage point.", minimum=0, maximum=10, step=0.5),
        ui_v164.p("Actuator – safety", "actuator_state_max_age_seconds", "Maximum actual-state age", "int", 180, "Reject physical control when SOC/load/PV state is older than this.", unit="s", recommended="180 s with 60 s collection.", minimum=15, maximum=1800, step=15),
        ui_v164.p("Actuator – safety", "actuator_candidate_grace_seconds", "Control candidate grace", "int", 120, "Grace after the end of a decision interval before watchdog forces safe release.", unit="s", recommended="120 s.", minimum=0, maximum=900, step=15),
        ui_v164.p("Actuator – safety", "actuator_ack_timeout_seconds", "Solinteg acknowledgement timeout", "float", 8.0, "Maximum wait for Working Mode / power-target readback after a command.", unit="s", recommended="8 s.", minimum=1, maximum=30, step=0.5),
        ui_v164.p("Actuator – safety", "actuator_ack_tolerance_kw", "Power-target acknowledgement tolerance", "float", 0.10, "Maximum difference between requested safe target and Solinteg readback.", unit="kW", recommended="0.10 kW.", minimum=0.01, maximum=2, step=0.01),
        ui_v164.p("Actuator – safety", "actuator_zero_deadband_kw", "Zero deadband", "float", 0.05, "Safe actions smaller than this are sent as zero.", unit="kW", minimum=0, maximum=1, step=0.01),
        ui_v164.p("Actuator – safety", "actuator_min_action_change_kw", "Minimum command change", "float", 0.10, "Do not rewrite the Solinteg target for tiny optimizer changes if the previous target remains safe.", unit="kW", recommended="0.10 kW.", minimum=0, maximum=2, step=0.01),
        ui_v164.p("Actuator – safety", "actuator_watchdog_poll_seconds", "Watchdog interval", "int", 30, "How often ACTIVE mode verifies Solinteg mode, target readback, candidate validity and safety envelope.", unit="s", recommended="30 s.", minimum=10, maximum=300, step=10),
        ui_v164.p("Actuator – safety", "actuator_max_physical_command_kw", "Maximum physical command", "float", 2.0, "Final symmetric downstream cap on battery charge/discharge power sent to Solinteg. It does not change optimizer or model decisions.", unit="kW", recommended="2 kW for commissioning; increase deliberately after verified physical operation.", minimum=0.0, maximum=8.0, step=0.5),
    ]
    existing = {item.get("key") for item in ui_v164.PARAMETERS}
    for item in definitions:
        if item["key"] not in existing:
            ui_v164.PARAMETERS.append(item)
    ui_v164.PARAM_BY_KEY.clear()
    ui_v164.PARAM_BY_KEY.update({item["key"]: item for item in ui_v164.PARAMETERS})
