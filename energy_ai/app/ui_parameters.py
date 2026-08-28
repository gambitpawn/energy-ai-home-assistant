from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .settings_store import delete_setting_overrides, load_setting_overrides, set_setting_overrides

OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR = "http://supervisor"


def parameter(
    section: str,
    key: str,
    label: str,
    kind: str,
    default: Any,
    help_text: str,
    *,
    unit: str = "",
    recommended: str | None = None,
    physical: str | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
) -> dict[str, Any]:
    return {
        "section": section,
        "key": key,
        "label": label,
        "kind": kind,
        "default": default,
        "help": help_text,
        "unit": unit,
        "recommended": recommended,
        "physical": physical,
        "min": minimum,
        "max": maximum,
        "step": step,
    }


P = parameter
PARAMETERS: list[dict[str, Any]] = [
    P("Installation", "pv_capacity_kw", "PV capacity", "float", 10.0, "Installed peak DC capacity used by the PV forecast model.", unit="kW", physical="Use the physical installed panel capacity (kWp).", minimum=0.1, maximum=100, step=0.1),
    P("Installation", "pv_tilt_deg", "PV tilt", "float", 35.5, "Panel inclination from horizontal used for irradiance modelling.", unit="°", physical="Use the actual array tilt.", minimum=0, maximum=90, step=0.5),
    P("Installation", "pv_azimuth_deg", "PV azimuth", "float", -79.0, "Array orientation. Convention: 0° = south, negative = east, positive = west.", unit="°", physical="Use the actual array orientation in the model convention.", minimum=-180, maximum=180, step=1),
    P("Installation", "battery_capacity_kwh", "Battery capacity", "float", 19.6, "Usable stationary-battery energy capacity used by the optimizer.", unit="kWh", physical="Use the usable capacity of the installed battery.", minimum=0.1, maximum=200, step=0.1),
    P("Installation", "optimizer_battery_max_charge_kw", "Max battery charge", "float", 8.0, "Maximum charging power the optimizer may schedule.", unit="kW", physical="Set no higher than the inverter/battery charge limit.", minimum=0, maximum=15, step=0.1),
    P("Installation", "optimizer_battery_max_discharge_kw", "Max battery discharge", "float", 8.0, "Maximum discharge power the optimizer may schedule.", unit="kW", physical="Set no higher than the inverter/battery discharge limit.", minimum=0, maximum=15, step=0.1),
    P("Installation", "optimizer_physical_grid_import_limit_kw", "Grid import limit", "float", 13.8, "Physical maximum grid import used as a hard optimizer constraint.", unit="kW", physical="Use the actual main-fuse / connection limit.", minimum=0, maximum=30, step=0.1),
    P("Installation", "optimizer_grid_export_limit_kw", "Grid export limit", "float", 10.0, "Maximum allowed export used as a hard optimizer constraint.", unit="kW", physical="Use the actual inverter/network export limit.", minimum=0, maximum=30, step=0.1),
    P("Installation", "ev_max_power_kw", "EV max charging power", "float", 11.0, "Maximum EV charging power used by EV modelling.", unit="kW", physical="Use the charger/vehicle effective maximum.", minimum=0, maximum=22, step=0.1),

    P("Battery policy", "hard_min_soc_pct", "Hard minimum SOC", "float", 5.0, "Absolute lower SOC boundary. The optimizer must not deliberately cross it.", unit="%", recommended="Typically 5–10%, unless the battery manufacturer requires another floor.", minimum=0, maximum=50, step=1),
    P("Battery policy", "preferred_min_soc_pct", "Preferred minimum SOC", "float", 15.0, "Soft lower comfort/resilience boundary. Going below it is allowed but penalized.", unit="%", recommended="15–25% is a reasonable starting range.", minimum=5, maximum=60, step=1),
    P("Battery policy", "preferred_max_soc_pct", "Preferred maximum SOC", "float", 90.0, "Soft upper SOC boundary intended to avoid unnecessary time at very high SOC.", unit="%", recommended="85–95% is a reasonable starting range for daily operation.", minimum=50, maximum=100, step=1),
    P("Battery policy", "normal_reserve_soc_pct", "Normal reserve", "float", 20.0, "Target reserve retained under normal forecast uncertainty.", unit="%", recommended="20–30% depending on resilience preference and forecast quality.", minimum=5, maximum=70, step=1),
    P("Battery policy", "high_uncertainty_reserve_soc_pct", "High-uncertainty reserve", "float", 28.0, "Higher reserve target used when forecast uncertainty is elevated.", unit="%", recommended="Usually 5–15 percentage points above the normal reserve.", minimum=5, maximum=80, step=1),

    P("Economics", "import_fixed_including_energy_tax_ore_kwh", "Fixed import cost incl. energy tax", "float", 36.0, "Fixed per-kWh import component, including energy tax and any other fixed per-kWh component intentionally represented here.", unit="öre/kWh", recommended="2026 Swedish energy-tax default: 36.00 öre/kWh excluding VAT; adjust to the contract actually modelled.", minimum=-500, maximum=1000, step=0.01),
    P("Economics", "import_spot_percentage", "Import spot-linked grid fee", "float", 6.86, "Percentage of quarter-hour spot price added to import economics.", unit="%", physical="Use the network contract percentage applied to spot price.", minimum=-100, maximum=500, step=0.01),
    P("Economics", "export_fixed_compensation_ore_kwh", "Fixed export compensation", "float", 2.84, "Fixed per-kWh network compensation added to exported energy value.", unit="öre/kWh", physical="Use the actual network-contract compensation.", minimum=-500, maximum=1000, step=0.01),
    P("Economics", "export_spot_percentage", "Export spot-linked compensation", "float", 6.05, "Percentage of quarter-hour spot price added to export compensation.", unit="%", physical="Use the actual network-contract percentage.", minimum=-100, maximum=500, step=0.01),
    P("Economics", "economics_valid_from", "Economics valid from", "str", "", "Optional effective-date marker for the configured economics. Leave blank to use current configuration without a declared start date."),
    P("Economics", "minimum_arbitrage_margin_ore_kwh", "Minimum arbitrage margin", "float", 20.0, "Minimum extra value required before discretionary battery arbitrage is considered worthwhile.", unit="öre/kWh", recommended="15–40 öre/kWh is a practical starting range; higher values reduce cycling.", minimum=0, maximum=500, step=1),
    P("Economics", "optimizer_battery_degradation_ore_kwh", "Battery degradation cost", "float", 5.0, "External economic wear cost assigned to battery throughput.", unit="öre/kWh", recommended="5–20 öre/kWh is a useful lifecycle-cost sensitivity range.", minimum=0, maximum=100, step=1),

    P("Optimizer", "optimizer_battery_charge_efficiency", "Charge efficiency", "float", 0.95, "Fraction of charging energy retained in the battery model.", recommended="Use measured/manufacturer efficiency where available; 0.93–0.97 is a common starting range.", minimum=0.5, maximum=1, step=0.01),
    P("Optimizer", "optimizer_battery_discharge_efficiency", "Discharge efficiency", "float", 0.95, "Fraction of stored energy delivered when discharging.", recommended="Use measured/manufacturer efficiency where available; 0.93–0.97 is a common starting range.", minimum=0.5, maximum=1, step=0.01),
    P("Optimizer", "optimizer_soc_grid_step_kwh", "SOC grid step", "float", 0.5, "Energy resolution used by the deterministic dynamic-programming state grid. Smaller values increase precision and compute cost.", unit="kWh", recommended="0.25–0.5 kWh is a practical range for this installation.", minimum=0.1, maximum=2, step=0.1),
    P("Optimizer", "optimizer_reserve_critical_soc_pct", "Critical SOC", "float", 10.0, "SOC below which reserve shortfall receives the strongest penalty.", unit="%", recommended="Usually at or slightly above the hard minimum SOC.", minimum=5, maximum=30, step=1),
    P("Optimizer", "optimizer_reserve_critical_penalty_ore_per_kwh_hour", "Critical reserve penalty", "float", 300.0, "Penalty for remaining below the critical reserve threshold.", unit="öre/kWh·h", recommended="Keep materially above routine arbitrage values.", minimum=0, maximum=2000, step=10),
    P("Optimizer", "optimizer_reserve_preferred_penalty_ore_per_kwh_hour", "Preferred reserve penalty", "float", 100.0, "Penalty for reserve shortfall below the preferred region.", unit="öre/kWh·h", minimum=0, maximum=1000, step=10),
    P("Optimizer", "optimizer_reserve_target_penalty_ore_per_kwh_hour", "Reserve target penalty", "float", 10.0, "Gentle penalty for being below the active reserve target.", unit="öre/kWh·h", recommended="5–25 is a reasonable starting range.", minimum=0, maximum=1000, step=1),
    P("Optimizer", "optimizer_preferred_max_excess_penalty_ore_per_kwh_hour", "High-SOC excess penalty", "float", 2.0, "Small penalty for staying above preferred maximum SOC.", unit="öre/kWh·h", recommended="Keep low relative to reserve penalties.", minimum=0, maximum=500, step=1),
    P("Optimizer", "optimizer_reserve_uncertainty_full_scale_kw", "Uncertainty full scale", "float", 3.0, "Forecast-error scale at which uncertainty reserve adjustment reaches full strength.", unit="kW", recommended="Tune from measured forecast residuals; 2–4 kW is a practical starting range.", minimum=0.1, maximum=20, step=0.1),
    P("Optimizer", "optimizer_terminal_soc_tolerance_pct", "Terminal SOC tolerance", "float", 3.0, "Allowed deviation around terminal SOC matching at the end of the planning horizon.", unit="%", recommended="2–5% balances continuity and flexibility.", minimum=0, maximum=20, step=0.5),
    P("Optimizer", "optimizer_terminal_soc_tiebreak_ore_per_kwh", "Terminal SOC tiebreak", "float", 5.0, "Small continuation-value term discouraging arbitrary horizon-end depletion.", unit="öre/kWh", recommended="2–10 is a reasonable range.", minimum=0, maximum=500, step=1),
    P("Optimizer", "optimizer_unknown_price_energy_coverage_fraction", "Unknown-price coverage", "float", 0.35, "Fraction of future energy exposure covered conservatively when market prices are not yet published.", recommended="0.25–0.5 depending on risk tolerance.", minimum=0, maximum=1, step=0.05),
    P("Optimizer", "optimizer_unknown_price_risk_premium_ore_kwh", "Unknown-price risk premium", "float", 40.0, "Risk premium applied to intervals with unpublished future prices.", unit="öre/kWh", recommended="20–60 is a useful initial range.", minimum=0, maximum=500, step=5),
    P("Optimizer", "optimizer_unknown_price_default_continuation_value_ore_kwh", "Unknown-price continuation value", "float", 150.0, "Fallback continuation value for stored energy beyond known market prices.", unit="öre/kWh", recommended="Use sensitivity testing rather than over-fitting.", minimum=0, maximum=1000, step=5),

    P("Optimizer – live replanning", "optimizer_soc_replan_threshold_pct", "SOC replan threshold", "float", 2.0, "Recalculate a deterministic live plan between quarter boundaries when measured SOC deviates from the interpolated plan by this many percentage points.", unit="percentage points", recommended="2 percentage points.", minimum=0.1, maximum=20, step=0.1),
    P("Optimizer – live replanning", "optimizer_soc_replan_emergency_threshold_pct", "Emergency SOC deviation", "float", 5.0, "Deviation at or above this level bypasses the ordinary replan cooldown.", unit="percentage points", recommended="5 percentage points.", minimum=0.1, maximum=30, step=0.1),
    P("Optimizer – live replanning", "optimizer_soc_replan_min_interval_seconds", "Minimum replan interval", "int", 60, "Minimum time between ordinary SOC-triggered live replans.", unit="s", recommended="60 seconds.", minimum=0, maximum=900, step=15),
    P("Optimizer – live replanning", "optimizer_soc_observation_max_age_seconds", "Maximum SOC observation age", "int", 180, "Refuse to re-anchor on an SOC observation older than this limit.", unit="s", recommended="180 seconds.", minimum=15, maximum=1800, step=15),

    P("Flexible loads", "sauna_default_duration_minutes", "Sauna default duration", "int", 120, "Default run duration used by the Overview Sauna now quick control.", unit="min", recommended="120 minutes is the selected household default.", minimum=15, maximum=360, step=15),
    P("Flexible loads", "sauna_nominal_peak_kw", "Sauna nominal peak", "float", 6.0, "Nominal sauna load used by flexible-load forecasting and overrides.", unit="kW", physical="Use the heater's typical full-load power.", minimum=0, maximum=15, step=0.1),
    P("Flexible loads", "sauna_detection_step_kw", "Sauna detection step", "float", 4.0, "Load step used when detecting likely sauna operation from aggregate power.", unit="kW", recommended="Keep below the sauna nominal peak but above ordinary household fluctuations.", minimum=0, maximum=15, step=0.1),

    P("Tariffs", "tariff_enabled", "Tariff optimization enabled", "bool", False, "Master switch for demand-tariff-aware optimization."),
    P("Tariffs", "tariff_consumption_enabled", "Consumption demand tariff", "bool", False, "Enable the configured import demand tariff model."),
    P("Tariffs", "tariff_consumption_rate_sek_per_kw", "Consumption demand rate", "float", 105.0, "Price per kW used by the consumption demand tariff.", unit="SEK/kW", physical="Use the actual network tariff rate.", minimum=0, maximum=10000, step=1),
    P("Tariffs", "tariff_consumption_start_hour", "Consumption window start", "int", 7, "First clock hour included in the consumption demand-tariff window.", unit="hour", physical="Use the tariff contract definition.", minimum=0, maximum=23, step=1),
    P("Tariffs", "tariff_consumption_end_hour", "Consumption window end", "int", 19, "End of the consumption demand-tariff clock window.", unit="hour", physical="Use the tariff contract definition.", minimum=1, maximum=24, step=1),
    P("Tariffs", "tariff_consumption_active_months", "Consumption active months", "str", "1,2,11,12", "Comma-separated month numbers when the tariff applies.", physical="Use the tariff contract definition."),
    P("Tariffs", "tariff_consumption_day_rule", "Consumption day rule", "str", "workdays", "Calendar rule controlling which days the consumption tariff applies.", physical="Use the tariff contract definition."),
    P("Tariffs", "tariff_production_enabled", "Production demand tariff", "bool", False, "Enable the configured export/production demand tariff model."),
    P("Tariffs", "tariff_production_rate_sek_per_kw", "Production demand rate", "float", 10.0, "Price per kW used by the production/export demand tariff.", unit="SEK/kW", physical="Use the actual network tariff if applicable.", minimum=0, maximum=10000, step=1),
    P("Tariffs", "tariff_production_start_hour", "Production window start", "int", 8, "First clock hour included in the production tariff window.", unit="hour", physical="Use the tariff contract definition.", minimum=0, maximum=23, step=1),
    P("Tariffs", "tariff_production_end_hour", "Production window end", "int", 16, "End of the production tariff clock window.", unit="hour", physical="Use the tariff contract definition.", minimum=1, maximum=24, step=1),
    P("Tariffs", "tariff_production_active_months", "Production active months", "str", "4,5,6,7,8", "Comma-separated month numbers when the production tariff applies.", physical="Use the tariff contract definition."),
    P("Tariffs", "tariff_production_day_rule", "Production day rule", "str", "weekends_holidays_midsummer_eve", "Calendar rule controlling which days the production tariff applies.", physical="Use the tariff contract definition."),

    P("Collector", "poll_seconds", "Persistent collector interval", "int", 60, "How often the persisted collector samples Home Assistant. The live UI is WebSocket driven and separate.", unit="s", recommended="60 s is appropriate for storage/model input.", minimum=15, maximum=900, step=15),
    P("Collector", "stale_after_seconds", "Stale threshold", "int", 180, "Age after which a persisted collector sample is considered stale.", unit="s", recommended="About 2–4× the collector interval.", minimum=30, maximum=3600, step=30),

    P("Data sources", "entity_pv_power", "PV power entity", "str", "sensor.solinteg_inverter_pv_power_total", "Home Assistant entity used for live and persisted PV power.", physical="Select total instantaneous PV production."),
    P("Data sources", "entity_house_load", "House load entity", "str", "sensor.solinteg_inverter_house_total_load", "Home Assistant entity representing total house load.", physical="Use total load; EV should remain included so it is not double-counted."),
    P("Data sources", "entity_grid_power", "Grid power entity", "str", "sensor.solinteg_inverter_meter_active_power", "Home Assistant entity representing net grid power.", physical="Use the meter entity at the grid connection point."),
    P("Data sources", "entity_battery_power", "Battery power entity", "str", "sensor.solinteg_inverter_battery_power", "Home Assistant entity representing instantaneous battery power.", physical="Use the inverter/battery sensor matching the configured sign convention."),
    P("Data sources", "entity_battery_soc", "Battery SOC entity", "str", "sensor.solinteg_inverter_battery_soc", "Home Assistant entity representing stationary battery SOC.", physical="Use the stationary battery SOC sensor."),
    P("Data sources", "entity_spot_price", "Spot price entity", "str", "sensor.nord_pool_se4_aktuellt_pris", "Home Assistant entity used as current spot-price source.", physical="Use the market-price sensor matching price area and unit."),
    P("Data sources", "entity_ev_power", "EV charger power entity", "str", "sensor.zap361270_laddeffekt", "Home Assistant entity used for actual EV charging power.", physical="Zaptec total charge power is preferred."),
    P("Data sources", "entity_ev_connected", "EV charger status entity", "str", "sensor.zap361270_laddstatus", "Home Assistant entity used to classify EV charger connection/charging state.", physical="Zaptec operation mode/status is preferred."),
    P("Data sources", "entity_ev_soc", "Vehicle SOC entity", "str", "", "Home Assistant entity representing vehicle battery SOC.", physical="Polestar Battery Level is preferred."),
    P("Data sources", "entity_ev_target_soc", "Vehicle target SOC entity", "str", "", "Optional Home Assistant entity representing desired vehicle target SOC."),
    P("Data sources", "entity_ev_ready_by", "Vehicle ready-by entity", "str", "", "Optional Home Assistant entity representing a vehicle departure/ready-by time."),
    P("Data sources", "entity_sauna_power", "Sauna power entity", "str", "", "Optional Home Assistant entity for direct sauna power measurement."),
    P("Data sources", "entity_spa_power", "Spa power entity", "str", "", "Optional Home Assistant entity for spa power."),
    P("Data sources", "entity_spa_temperature", "Spa temperature entity", "str", "", "Optional Home Assistant entity for spa water temperature."),
    P("Data sources", "entity_pool_heat_pump_power", "Pool heat-pump power entity", "str", "", "Optional Home Assistant entity for pool heat-pump power."),
    P("Data sources", "entity_pool_temperature", "Pool temperature entity", "str", "", "Optional Home Assistant entity for pool temperature."),
    P("Data sources", "load_components_json", "Load components JSON", "str", "", "Optional explicit load-component configuration JSON."),

    P("Actuator – Solinteg", "entity_solinteg_working_mode", "Solinteg Working Mode entity", "str", "", "Home Assistant select entity exposing Solinteg Working Mode. Leave blank for strict auto-discovery.", physical="SolaX Modbus Solinteg Working Mode select."),
    P("Actuator – Solinteg", "entity_solinteg_battery_power_target", "Solinteg battery power target entity", "str", "", "Home Assistant number entity exposing EMS BattCtrl Charge Discharge Power Target. Leave blank for strict auto-discovery.", physical="SolaX Modbus Solinteg power-target entity."),
    P("Actuator – Solinteg", "actuator_control_working_mode", "Control working mode", "str", "EMS BattCtrl", "Working Mode option used while Energy AI controls battery power."),
    P("Actuator – Solinteg", "actuator_safe_working_mode", "Safe release working mode", "str", "General", "Working Mode restored on disarm, fault or clean shutdown."),
    P("Actuator – safety", "actuator_soc_guard_margin_pct", "Hard-SOC guard margin", "float", 1.0, "Additional margin inside hard SOC limits enforced over a full control interval.", unit="percentage points", recommended="1 percentage point.", minimum=0, maximum=10, step=0.5),
    P("Actuator – safety", "actuator_state_max_age_seconds", "Maximum actual-state age", "int", 180, "Reject physical control when SOC/load/PV state is older than this.", unit="s", recommended="180 s with 60 s collection.", minimum=15, maximum=1800, step=15),
    P("Actuator – safety", "actuator_candidate_grace_seconds", "Control candidate grace", "int", 120, "Grace after a decision interval before watchdog forces safe release.", unit="s", recommended="120 s.", minimum=0, maximum=900, step=15),
    P("Actuator – safety", "actuator_ack_timeout_seconds", "Solinteg acknowledgement timeout", "float", 8.0, "Maximum wait for mode / power-target readback after a command.", unit="s", recommended="8 s.", minimum=1, maximum=30, step=0.5),
    P("Actuator – safety", "actuator_ack_tolerance_kw", "Power-target acknowledgement tolerance", "float", 0.10, "Maximum difference between safe target and Solinteg readback.", unit="kW", recommended="0.10 kW.", minimum=0.01, maximum=2, step=0.01),
    P("Actuator – safety", "actuator_zero_deadband_kw", "Zero deadband", "float", 0.05, "Safe actions smaller than this are sent as zero.", unit="kW", minimum=0, maximum=1, step=0.01),
    P("Actuator – safety", "actuator_min_action_change_kw", "Minimum command change", "float", 0.10, "Do not rewrite the target for tiny optimizer changes if the previous target remains safe.", unit="kW", recommended="0.10 kW.", minimum=0, maximum=2, step=0.01),
    P("Actuator – safety", "actuator_watchdog_poll_seconds", "Watchdog interval", "int", 30, "How often ACTIVE verifies mode, target readback, candidate validity and safety envelope.", unit="s", recommended="30 s.", minimum=10, maximum=300, step=10),
    P("Actuator – safety", "actuator_max_physical_command_kw", "Maximum physical command", "float", 2.0, "Final symmetric downstream cap on battery charge/discharge power sent to Solinteg. It does not change optimizer/model decisions.", unit="kW", recommended="2 kW for commissioning; increase deliberately after verified operation.", minimum=0.0, maximum=8.0, step=0.5, physical="Applied after deterministic SOC/grid safety and before Solinteg dispatch."),
]

PARAM_BY_KEY = {item["key"]: item for item in PARAMETERS}


def _raw_options() -> dict[str, Any]:
    try:
        raw = json.loads(OPTIONS_PATH.read_text(encoding="utf-8")) if OPTIONS_PATH.exists() else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _coerce(meta: dict[str, Any], raw: Any) -> Any:
    kind = meta["kind"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        value = str(raw).lower()
        if value in {"true", "1", "yes", "on"}:
            return True
        if value in {"false", "0", "no", "off"}:
            return False
        raise ValueError("must be true/false")
    if kind == "str":
        return str(raw).strip()
    value: int | float
    if kind == "int":
        value = int(float(raw))
    else:
        value = float(raw)
    if meta.get("min") is not None and value < meta["min"]:
        raise ValueError(f"minimum is {meta['min']}")
    if meta.get("max") is not None and value > meta["max"]:
        raise ValueError(f"maximum is {meta['max']}")
    return value


async def _supervisor_post(path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    token = os.getenv("SUPERVISOR_TOKEN") or ""
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SUPERVISOR}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
    try:
        body = response.json()
    except Exception:
        body = response.text
    return response.status_code, body


PARAMETERS_EXTENSION = r'''
<style>
.param-edit-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.param-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.param-save-state{font-size:11px;color:var(--muted)}.param-section-edit h2{display:flex;align-items:center;justify-content:space-between}.param-edit-row{display:grid;grid-template-columns:minmax(200px,1.25fr) minmax(180px,.85fr);gap:14px;padding:9px 0;border-bottom:1px solid var(--line);align-items:center}.param-label-wrap{display:flex;align-items:center;gap:7px;min-width:0}.param-info{position:relative;display:inline-grid;place-items:center;width:17px;height:17px;border:1px solid #52687c;border-radius:50%;font-size:11px;font-weight:800;color:#a9bac9;cursor:help;flex:0 0 auto}.param-tip{display:none;position:absolute;z-index:50;left:22px;top:-8px;width:min(360px,70vw);padding:10px 11px;background:#0d151e;border:1px solid #40556a;border-radius:10px;box-shadow:0 8px 25px #0009;font-size:11px;font-weight:400;line-height:1.45;color:var(--text)}.param-info:hover .param-tip,.param-info:focus .param-tip{display:block}.param-tip b{color:#fff}.param-input-wrap{display:flex;align-items:center;gap:7px}.param-input{width:100%;min-width:0;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:8px;padding:7px 9px;font:inherit}.param-input:focus{outline:1px solid var(--blue);border-color:var(--blue)}.param-unit{font-size:10px;color:var(--muted);min-width:48px}.param-changed{border-color:var(--warn)!important}.param-section-count{font-size:10px;color:var(--muted);font-weight:400}.param-restart-note{color:var(--warn);font-size:11px}.param-error{color:var(--bad)}.param-reset{border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:7px;padding:6px 8px;font-size:10px;cursor:pointer;white-space:nowrap}.param-reset:hover{border-color:var(--blue);color:var(--text)}.param-source{font-size:9px;color:var(--muted);margin-left:5px;font-weight:400}.param-source-db{color:var(--warn)}
@media(max-width:700px){.param-edit-row{grid-template-columns:1fr}.param-tip{left:-8px;top:23px}.param-edit-head{align-items:flex-start;flex-direction:column}}
</style>
<script>
let paramMeta=null,paramOriginal={};
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function tipHtml(m){const bits=[`<div>${esc(m.help)}</div>`];if(m.physical)bits.push(`<div style="margin-top:7px"><b>Set value:</b> ${esc(m.physical)}</div>`);if(m.recommended)bits.push(`<div style="margin-top:7px"><b>Recommended:</b> ${esc(m.recommended)}</div>`);if(m.default!==undefined&&m.default!==null)bits.push(`<div style="margin-top:7px"><b>Default:</b> ${esc(m.default)}${m.unit?' '+esc(m.unit):''}</div>`);return bits.join('')}
function fieldHtml(m,v){const common=`class="param-input" data-param="${esc(m.key)}"`;if(m.kind==='bool')return `<select ${common}><option value="true" ${v===true?'selected':''}>Enabled</option><option value="false" ${v===false?'selected':''}>Disabled</option></select>`;if(m.kind==='str')return `<input ${common} type="text" value="${esc(v??'')}">`;return `<input ${common} type="number" value="${esc(v??'')}" ${m.min!=null?`min="${m.min}"`:''} ${m.max!=null?`max="${m.max}"`:''} ${m.step!=null?`step="${m.step}"`:'step="any"'}>`}
function valueOf(el,m){if(m.kind==='bool')return el.value==='true';if(m.kind==='int')return Number.parseInt(el.value,10);if(m.kind==='float')return Number(el.value);return el.value}
function changedParams(){const out={};document.querySelectorAll('.param-input').forEach(el=>{const m=paramMeta.parameters.find(x=>x.key===el.dataset.param),v=valueOf(el,m);if(String(v)!==String(paramOriginal[m.key]))out[m.key]=v});return out}
function updateParamState(){const count=Object.keys(changedParams()).length,s=$('paramSaveState');if(s)s.textContent=count?`${count} unsaved change${count===1?'':'s'}`:'No unsaved changes'}
function markParamChanged(el){const m=paramMeta.parameters.find(x=>x.key===el.dataset.param),v=valueOf(el,m);el.classList.toggle('param-changed',String(v)!==String(paramOriginal[m.key]));updateParamState()}
function renderParamEditor(d){paramMeta=d;paramOriginal={...d.values};const sections={};for(const m of d.parameters)(sections[m.section]??=[]).push(m);$('parameterGrid').innerHTML=Object.entries(sections).map(([section,items])=>`<div class="card param-section-edit"><h2>${esc(section)}<span class="param-section-count">${items.length} parameters</span></h2>${items.map(m=>{const v=d.values[m.key],src=(d.sources||{})[m.key]||'default',db=src==='db_override';return `<div class="param-edit-row"><div class="param-label-wrap"><span class="param-name">${esc(m.label)}</span>${db?'<span class="param-source param-source-db">DB override</span>':''}<span class="param-info" tabindex="0">i<span class="param-tip">${tipHtml(m)}</span></span></div><div class="param-input-wrap">${fieldHtml(m,v)}<span class="param-unit">${esc(m.unit||'')}</span>${db?`<button class="param-reset" data-reset-param="${esc(m.key)}">Use HA/default</button>`:''}</div></div>`}).join('')}</div>`).join('');const p=$('parameters');p.querySelector('.notice').innerHTML='<strong>Persistent Energy AI settings.</strong> Values saved here are stored in the Energy AI database and override Home Assistant add-on options after restart. “Use HA/default” removes that override.';$('parameterGrid').querySelectorAll('.param-input').forEach(el=>el.addEventListener('input',()=>markParamChanged(el)));$('parameterGrid').querySelectorAll('[data-reset-param]').forEach(el=>el.addEventListener('click',()=>resetParameter(el.dataset.resetParam)))}
async function loadParameterEditor(){try{renderParamEditor(await api('ui/parameters-meta'));updateParamState()}catch(e){$('parameterGrid').innerHTML=`<div class="notice param-error">Could not load editable parameters: ${esc(e.message)}</div>`}}
async function saveParameters(restart=false){const patch=changedParams(),status=$('paramSaveState');status.classList.remove('param-error');if(!Object.keys(patch).length){status.textContent='Nothing to save';return}status.textContent='Saving…';try{const r=await api('ui/parameters-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values:patch,restart})});if(r.restart_scheduled){status.textContent='Saved · restarting add-on…';setTimeout(()=>location.reload(),7000);return}status.textContent=restart&&r.manual_restart_required?'Saved · restart add-on manually':'Saved · restart required';await loadParameterEditor()}catch(e){status.textContent=`Save failed: ${e.message}`;status.classList.add('param-error')}}
async function resetParameter(key){const status=$('paramSaveState');status.textContent='Resetting…';try{await api('ui/parameters-reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys:[key],restart:false})});status.textContent='Override removed · restart required';await loadParameterEditor()}catch(e){status.textContent=`Reset failed: ${e.message}`;status.classList.add('param-error')}}
function installParameterEditor(){const p=$('parameters');if(!p||$('paramEditActions'))return;p.querySelector('.notice').insertAdjacentHTML('beforebegin','<div class="param-edit-head"><div><h2 style="margin:0 0 3px">Parameters</h2><div class="param-restart-note">Saved changes take effect after add-on restart.</div></div><div class="param-actions" id="paramEditActions"><span id="paramSaveState" class="param-save-state">Loading…</span><button class="btn" id="paramSave">Save</button><button class="btn" id="paramSaveRestart">Save & restart</button></div></div>');$('paramSave').onclick=()=>saveParameters(false);$('paramSaveRestart').onclick=()=>saveParameters(true);loadParameterEditor()}
renderParameters=function(){if(typeof loadParameterEditor==='function')loadParameterEditor()};
installParameterEditor();
$('tabs')?.addEventListener('click',e=>{if(e.target.closest('.tab')?.dataset.view==='parameters')loadParameterEditor()});
</script>
'''


def install_parameter_routes(app: FastAPI) -> None:
    @app.get("/ui/parameters-meta", include_in_schema=False)
    async def parameters_meta():
        raw = _raw_options()
        overrides = load_setting_overrides()
        effective = {**raw, **overrides}
        values = {m["key"]: effective.get(m["key"], m["default"]) for m in PARAMETERS}
        sources = {
            m["key"]: "db_override" if m["key"] in overrides else "home_assistant_options" if m["key"] in raw else "default"
            for m in PARAMETERS
        }
        return JSONResponse({"parameters": PARAMETERS, "values": values, "sources": sources, "restart_required": True, "storage_precedence": ["default", "home_assistant_options", "db_override"]})

    @app.post("/ui/parameters-save", include_in_schema=False)
    async def parameters_save(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        patch = body.get("values") or {}
        restart = bool(body.get("restart", False))
        if not isinstance(patch, dict):
            return JSONResponse({"error": "values must be an object"}, status_code=400)
        clean: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for key, raw_value in patch.items():
            meta = PARAM_BY_KEY.get(str(key))
            if not meta:
                errors[str(key)] = "parameter is not editable"
                continue
            try:
                clean[str(key)] = _coerce(meta, raw_value)
            except Exception as exc:
                errors[str(key)] = str(exc)
        if errors:
            return JSONResponse({"error": "validation failed", "fields": errors}, status_code=400)
        try:
            db_result = set_setting_overrides(clean, source="ui")
        except Exception as exc:
            return JSONResponse({"error": f"Could not persist settings in SQLite: {exc!r}"}, status_code=500)
        restart_scheduled = False
        manual_restart_required = bool(restart)
        if restart and os.getenv("SUPERVISOR_TOKEN"):
            async def delayed_restart() -> None:
                await asyncio.sleep(0.8)
                try:
                    await _supervisor_post("/addons/self/restart", {})
                except Exception:
                    pass
            asyncio.create_task(delayed_restart())
            restart_scheduled = True
            manual_restart_required = False
        return JSONResponse({"saved": sorted(clean), "persistent_store": "sqlite", "db_result": db_result, "supervisor_options_modified": False, "restart_required": not restart_scheduled, "restart_scheduled": restart_scheduled, "manual_restart_required": manual_restart_required})

    @app.post("/ui/parameters-reset", include_in_schema=False)
    async def parameters_reset(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        keys = body.get("keys") or []
        restart = bool(body.get("restart", False))
        if not isinstance(keys, list):
            return JSONResponse({"error": "keys must be an array"}, status_code=400)
        invalid = sorted(str(k) for k in keys if str(k) not in PARAM_BY_KEY)
        if invalid:
            return JSONResponse({"error": "parameter is not editable", "keys": invalid}, status_code=400)
        removed = delete_setting_overrides(str(k) for k in keys)
        restart_scheduled = False
        manual_restart_required = bool(restart)
        if restart and os.getenv("SUPERVISOR_TOKEN"):
            async def delayed_restart() -> None:
                await asyncio.sleep(0.8)
                try:
                    await _supervisor_post("/addons/self/restart", {})
                except Exception:
                    pass
            asyncio.create_task(delayed_restart())
            restart_scheduled = True
            manual_restart_required = False
        return JSONResponse({"removed_db_overrides": removed, "fallback": "home_assistant_options_then_code_default", "restart_required": not restart_scheduled, "restart_scheduled": restart_scheduled, "manual_restart_required": manual_restart_required})
