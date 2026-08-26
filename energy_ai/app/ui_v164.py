from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import DASHBOARD_HTML
from .overview_extension import OVERVIEW_EXTENSION
from .ui_v158 import V158_EXTENSION
from .ui_v159 import V159_EXTENSION
from .ui_v160 import V160_EXTENSION
from .ui_v161 import V161_EXTENSION
from .ui_v161_fix import V161_FIX_EXTENSION
from .ui_v163 import V163_EXTENSION

OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR = "http://supervisor"


def p(section: str, key: str, label: str, kind: str, default: Any, help_text: str, *, unit: str = "", recommended: str | None = None, physical: str | None = None, minimum: float | None = None, maximum: float | None = None, step: float | None = None) -> dict[str, Any]:
    return {"section": section, "key": key, "label": label, "kind": kind, "default": default, "help": help_text, "unit": unit, "recommended": recommended, "physical": physical, "min": minimum, "max": maximum, "step": step}


PARAMETERS = [
    p("Installation", "pv_capacity_kw", "PV capacity", "float", 10.0, "Installed peak DC capacity used by the PV forecast model.", unit="kW", physical="Use the physical installed panel capacity (kWp).", minimum=0.1, maximum=100, step=0.1),
    p("Installation", "pv_tilt_deg", "PV tilt", "float", 35.5, "Panel inclination from horizontal used for irradiance modelling.", unit="°", physical="Use the actual array tilt.", minimum=0, maximum=90, step=0.5),
    p("Installation", "pv_azimuth_deg", "PV azimuth", "float", -79.0, "Array orientation used by the PV model. Current convention: 0° = south, negative = east, positive = west.", unit="°", physical="Use the actual array orientation in the model convention.", minimum=-180, maximum=180, step=1),
    p("Installation", "battery_capacity_kwh", "Battery capacity", "float", 19.6, "Usable battery energy capacity used by the optimizer.", unit="kWh", physical="Use the usable capacity of the installed battery.", minimum=0.1, maximum=200, step=0.1),
    p("Installation", "optimizer_battery_max_charge_kw", "Max battery charge", "float", 8.0, "Maximum charging power the optimizer may schedule.", unit="kW", physical="Set no higher than the inverter/battery charge limit.", minimum=0, maximum=15, step=0.1),
    p("Installation", "optimizer_battery_max_discharge_kw", "Max battery discharge", "float", 8.0, "Maximum discharge power the optimizer may schedule.", unit="kW", physical="Set no higher than the inverter/battery discharge limit.", minimum=0, maximum=15, step=0.1),
    p("Installation", "optimizer_physical_grid_import_limit_kw", "Grid import limit", "float", 13.8, "Physical maximum grid import used as a hard optimizer constraint.", unit="kW", physical="Use the actual main-fuse / connection limit.", minimum=0, maximum=30, step=0.1),
    p("Installation", "optimizer_grid_export_limit_kw", "Grid export limit", "float", 10.0, "Maximum allowed export used as a hard optimizer constraint.", unit="kW", physical="Use the actual inverter/network export limit.", minimum=0, maximum=30, step=0.1),
    p("Installation", "ev_max_power_kw", "EV max charging power", "float", 11.0, "Maximum EV charging power used by EV modelling.", unit="kW", physical="Use the charger/vehicle effective maximum.", minimum=0, maximum=22, step=0.1),

    p("Battery policy", "hard_min_soc_pct", "Hard minimum SOC", "float", 5.0, "Absolute lower SOC boundary. The optimizer must not deliberately cross it.", unit="%", recommended="Typically 5–10%, unless the battery manufacturer requires another floor.", minimum=0, maximum=50, step=1),
    p("Battery policy", "preferred_min_soc_pct", "Preferred minimum SOC", "float", 15.0, "Soft lower comfort/resilience boundary. Going below it is allowed but penalized.", unit="%", recommended="15–25% is a reasonable starting range.", minimum=5, maximum=60, step=1),
    p("Battery policy", "preferred_max_soc_pct", "Preferred maximum SOC", "float", 90.0, "Soft upper SOC boundary intended to avoid unnecessary time at very high SOC.", unit="%", recommended="85–95% is a reasonable starting range for daily operation.", minimum=50, maximum=100, step=1),
    p("Battery policy", "normal_reserve_soc_pct", "Normal reserve", "float", 20.0, "Target reserve retained under normal forecast uncertainty.", unit="%", recommended="20–30% depending on resilience preference and forecast quality.", minimum=5, maximum=70, step=1),
    p("Battery policy", "high_uncertainty_reserve_soc_pct", "High-uncertainty reserve", "float", 28.0, "Higher reserve target used when forecast uncertainty is elevated.", unit="%", recommended="Usually 5–15 percentage points above the normal reserve.", minimum=5, maximum=80, step=1),

    p("Economics", "import_overhead_ore_kwh", "Import overhead", "float", 0.0, "Variable import cost added on top of spot price, e.g. energy tax/network variable charge if not already represented.", unit="öre/kWh", recommended="Enter only costs that actually vary per imported kWh and are not already included elsewhere.", minimum=-500, maximum=1000, step=1),
    p("Economics", "export_overhead_ore_kwh", "Export adjustment", "float", 0.0, "Adjustment to export economics relative to spot price.", unit="öre/kWh", recommended="Use the actual variable export compensation/cost convention used by the model.", minimum=-500, maximum=1000, step=1),
    p("Economics", "minimum_arbitrage_margin_ore_kwh", "Minimum arbitrage margin", "float", 20.0, "Minimum price spread required before pure battery arbitrage is considered worthwhile.", unit="öre/kWh", recommended="15–40 öre/kWh is a practical starting range; higher values reduce cycling.", minimum=0, maximum=500, step=1),
    p("Economics", "optimizer_battery_degradation_ore_kwh", "Battery degradation cost", "float", 5.0, "Economic wear cost assigned to battery throughput.", unit="öre/kWh", recommended="Use a conservative lifecycle-cost estimate; 5–20 öre/kWh is a useful sensitivity range.", minimum=0, maximum=100, step=1),

    p("Optimizer", "optimizer_battery_charge_efficiency", "Charge efficiency", "float", 0.95, "Fraction of charging energy retained in the battery model.", recommended="Use measured/manufacturer efficiency where available; 0.93–0.97 is a common starting range.", minimum=0.5, maximum=1, step=0.01),
    p("Optimizer", "optimizer_battery_discharge_efficiency", "Discharge efficiency", "float", 0.95, "Fraction of stored energy delivered when discharging.", recommended="Use measured/manufacturer efficiency where available; 0.93–0.97 is a common starting range.", minimum=0.5, maximum=1, step=0.01),
    p("Optimizer", "optimizer_soc_grid_step_kwh", "SOC grid step", "float", 0.5, "Energy resolution used by the deterministic dynamic-programming state grid. Smaller is more precise but increases compute cost.", unit="kWh", recommended="0.25–0.5 kWh for this battery size; reduce only if finer decisions materially improve results.", minimum=0.1, maximum=2, step=0.1),
    p("Optimizer", "optimizer_reserve_critical_soc_pct", "Critical SOC", "float", 10.0, "SOC below which reserve shortfall receives the strongest penalty.", unit="%", recommended="Usually at or slightly above the hard minimum SOC.", minimum=5, maximum=30, step=1),
    p("Optimizer", "optimizer_reserve_critical_penalty_ore_per_kwh_hour", "Critical reserve penalty", "float", 300.0, "Penalty for remaining below the critical reserve threshold.", unit="öre/kWh·h", recommended="Keep materially higher than normal arbitrage values so critical reserve dominates routine trading.", minimum=0, maximum=2000, step=10),
    p("Optimizer", "optimizer_reserve_preferred_penalty_ore_per_kwh_hour", "Preferred reserve penalty", "float", 100.0, "Penalty for reserve shortfall below the preferred region.", unit="öre/kWh·h", recommended="Typically lower than the critical penalty but high enough to matter in normal price spreads.", minimum=0, maximum=1000, step=10),
    p("Optimizer", "optimizer_reserve_target_penalty_ore_per_kwh_hour", "Reserve target penalty", "float", 10.0, "Gentle penalty for being below the active reserve target.", unit="öre/kWh·h", recommended="5–25 is a reasonable starting range.", minimum=0, maximum=1000, step=1),
    p("Optimizer", "optimizer_preferred_max_excess_penalty_ore_per_kwh_hour", "High-SOC excess penalty", "float", 2.0, "Small penalty for staying above preferred maximum SOC.", unit="öre/kWh·h", recommended="Keep low relative to reserve penalties; 0–5 is a useful starting range.", minimum=0, maximum=500, step=1),
    p("Optimizer", "optimizer_reserve_uncertainty_full_scale_kw", "Uncertainty full scale", "float", 3.0, "Forecast-error scale at which the uncertainty reserve adjustment reaches full strength.", unit="kW", recommended="Tune from measured forecast residuals; 2–4 kW is a reasonable initial range for this installation.", minimum=0.1, maximum=20, step=0.1),
    p("Optimizer", "optimizer_terminal_soc_tolerance_pct", "Terminal SOC tolerance", "float", 3.0, "Allowed deviation around terminal SOC matching at the end of the planning horizon.", unit="%", recommended="2–5% balances continuity and optimizer flexibility.", minimum=0, maximum=20, step=0.5),
    p("Optimizer", "optimizer_terminal_soc_tiebreak_ore_per_kwh", "Terminal SOC tiebreak", "float", 5.0, "Small continuation-value/tiebreak term discouraging arbitrary depletion at the horizon boundary.", unit="öre/kWh", recommended="Keep small relative to real energy price spreads; 2–10 is a reasonable range.", minimum=0, maximum=500, step=1),
    p("Optimizer", "optimizer_unknown_price_energy_coverage_fraction", "Unknown-price coverage", "float", 0.35, "Fraction of future energy exposure covered conservatively when market prices are not yet published.", recommended="0.25–0.5 depending on risk tolerance.", minimum=0, maximum=1, step=0.05),
    p("Optimizer", "optimizer_unknown_price_risk_premium_ore_kwh", "Unknown-price risk premium", "float", 40.0, "Risk premium applied to intervals with unpublished future prices.", unit="öre/kWh", recommended="20–60 is a useful initial sensitivity range.", minimum=0, maximum=500, step=5),
    p("Optimizer", "optimizer_unknown_price_default_continuation_value_ore_kwh", "Unknown-price continuation value", "float", 150.0, "Fallback continuation value for stored energy beyond known market prices.", unit="öre/kWh", recommended="Set near a conservative medium-term marginal energy value; test sensitivity rather than over-fitting.", minimum=0, maximum=1000, step=5),

    p("Tariffs", "tariff_enabled", "Tariff optimization enabled", "bool", False, "Master switch for demand-tariff-aware optimization."),
    p("Tariffs", "tariff_consumption_enabled", "Consumption demand tariff", "bool", False, "Enable the configured import demand tariff model."),
    p("Tariffs", "tariff_consumption_rate_sek_per_kw", "Consumption demand rate", "float", 105.0, "Price per kW used by the configured consumption-demand tariff.", unit="SEK/kW", physical="Use the actual network tariff rate.", minimum=0, maximum=10000, step=1),
    p("Tariffs", "tariff_consumption_start_hour", "Consumption window start", "int", 7, "First clock hour included in the consumption demand-tariff window.", unit="hour", physical="Use the tariff contract definition.", minimum=0, maximum=23, step=1),
    p("Tariffs", "tariff_consumption_end_hour", "Consumption window end", "int", 19, "End of the consumption demand-tariff clock window.", unit="hour", physical="Use the tariff contract definition.", minimum=1, maximum=24, step=1),
    p("Tariffs", "tariff_consumption_active_months", "Consumption active months", "str", "1,2,11,12", "Comma-separated month numbers when the demand tariff applies.", physical="Use the tariff contract definition."),
    p("Tariffs", "tariff_production_enabled", "Production demand tariff", "bool", False, "Enable the configured export/production demand tariff model."),
    p("Tariffs", "tariff_production_rate_sek_per_kw", "Production demand rate", "float", 10.0, "Price per kW used by the production/export demand tariff.", unit="SEK/kW", physical="Use the actual network tariff if applicable.", minimum=0, maximum=10000, step=1),

    p("Collector", "poll_seconds", "Persistent collector interval", "int", 60, "How often the persisted collector samples Home Assistant. This is separate from the WebSocket live UI.", unit="s", recommended="60 s is appropriate for storage/model input; do not reduce merely to make the live UI faster.", minimum=15, maximum=900, step=15),
    p("Collector", "stale_after_seconds", "Stale threshold", "int", 180, "Age after which a persisted collector sample is considered stale.", unit="s", recommended="About 2–4× the collector interval.", minimum=30, maximum=3600, step=30),

    p("Data sources", "entity_pv_power", "PV power entity", "str", "sensor.solinteg_inverter_pv_power_total", "Home Assistant entity used for live and persisted PV power.", physical="Select the entity representing total instantaneous PV production."),
    p("Data sources", "entity_house_load", "House load entity", "str", "sensor.solinteg_inverter_house_total_load", "Home Assistant entity representing total house load.", physical="Use the total load entity; EV must remain included so it is not double-counted."),
    p("Data sources", "entity_grid_power", "Grid power entity", "str", "sensor.solinteg_inverter_meter_active_power", "Home Assistant entity representing net grid power.", physical="Use the meter entity at the grid connection point."),
    p("Data sources", "entity_battery_power", "Battery power entity", "str", "sensor.solinteg_inverter_battery_power", "Home Assistant entity representing instantaneous battery power.", physical="Use the inverter/battery power sensor matching the configured sign convention."),
    p("Data sources", "entity_battery_soc", "Battery SOC entity", "str", "sensor.solinteg_inverter_battery_soc", "Home Assistant entity representing stationary battery SOC.", physical="Use the battery/inverter SOC percentage sensor."),
    p("Data sources", "entity_spot_price", "Spot price entity", "str", "sensor.nord_pool_se4_aktuellt_pris", "Home Assistant entity used as the current spot-price source.", physical="Use the market-price sensor that matches the configured price area and unit."),
    p("Data sources", "entity_ev_power", "EV charger power entity", "str", "sensor.zap361270_laddeffekt", "Home Assistant entity used for actual EV charging power.", physical="Zaptec total charge power is preferred when available."),
    p("Data sources", "entity_ev_connected", "EV charger status entity", "str", "sensor.zap361270_laddstatus", "Home Assistant entity used to classify charger connected/charging/finished/disconnected state.", physical="Zaptec charger operation mode/status is preferred."),
    p("Data sources", "entity_ev_soc", "Vehicle SOC entity", "str", "", "Home Assistant entity representing vehicle battery SOC.", physical="Polestar Battery Level is preferred for the vehicle SOC."),
]

PARAM_BY_KEY = {x["key"]: x for x in PARAMETERS}


def _options() -> dict[str, Any]:
    try:
        raw = json.loads(OPTIONS_PATH.read_text(encoding="utf-8")) if OPTIONS_PATH.exists() else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _coerce(meta: dict[str, Any], raw: Any) -> Any:
    kind = meta["kind"]
    if kind == "bool":
        if isinstance(raw, bool): return raw
        if str(raw).lower() in {"true", "1", "yes", "on"}: return True
        if str(raw).lower() in {"false", "0", "no", "off"}: return False
        raise ValueError("must be true/false")
    if kind == "str":
        return str(raw).strip()
    if kind == "int":
        value = int(float(raw))
    else:
        value = float(raw)
    if meta.get("min") is not None and value < meta["min"]: raise ValueError(f"minimum is {meta['min']}")
    if meta.get("max") is not None and value > meta["max"]: raise ValueError(f"maximum is {meta['max']}")
    return value


async def _supervisor_post(path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    token = os.getenv("SUPERVISOR_TOKEN") or ""
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{SUPERVISOR}{path}", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json=payload, timeout=20)
    try: body = r.json()
    except Exception: body = r.text
    return r.status_code, body


V164_EXTENSION = r'''
<style>
.param-edit-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.param-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.param-save-state{font-size:11px;color:var(--muted)}.param-section-edit h2{display:flex;align-items:center;justify-content:space-between}.param-edit-row{display:grid;grid-template-columns:minmax(200px,1.25fr) minmax(180px,.85fr);gap:14px;padding:9px 0;border-bottom:1px solid var(--line);align-items:center}.param-label-wrap{display:flex;align-items:center;gap:7px;min-width:0}.param-info{position:relative;display:inline-grid;place-items:center;width:17px;height:17px;border:1px solid #52687c;border-radius:50%;font-size:11px;font-weight:800;color:#a9bac9;cursor:help;flex:0 0 auto}.param-tip{display:none;position:absolute;z-index:50;left:22px;top:-8px;width:min(360px,70vw);padding:10px 11px;background:#0d151e;border:1px solid #40556a;border-radius:10px;box-shadow:0 8px 25px #0009;font-size:11px;font-weight:400;line-height:1.45;color:var(--text)}.param-info:hover .param-tip,.param-info:focus .param-tip{display:block}.param-tip b{color:#fff}.param-input-wrap{display:flex;align-items:center;gap:7px}.param-input{width:100%;min-width:0;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:8px;padding:7px 9px;font:inherit}.param-input:focus{outline:1px solid var(--blue);border-color:var(--blue)}.param-unit{font-size:10px;color:var(--muted);min-width:48px}.param-changed{border-color:var(--warn)!important}.param-section-count{font-size:10px;color:var(--muted);font-weight:400}.param-restart-note{color:var(--warn);font-size:11px}.param-error{color:var(--bad)}
@media(max-width:700px){.param-edit-row{grid-template-columns:1fr}.param-tip{left:-8px;top:23px}.param-edit-head{align-items:flex-start;flex-direction:column}}
</style>
<script>
let paramMeta=null,paramOriginal={};
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function tipHtml(m){const bits=[`<div>${esc(m.help)}</div>`];if(m.physical)bits.push(`<div style="margin-top:7px"><b>Set value:</b> ${esc(m.physical)}</div>`);if(m.recommended)bits.push(`<div style="margin-top:7px"><b>Recommended:</b> ${esc(m.recommended)}</div>`);if(m.default!==undefined&&m.default!==null)bits.push(`<div style="margin-top:7px"><b>Default:</b> ${esc(m.default)}${m.unit?' '+esc(m.unit):''}</div>`);return bits.join('')}
function fieldHtml(m,v){const common=`class="param-input" data-param="${esc(m.key)}"`;
 if(m.kind==='bool')return `<select ${common}><option value="true" ${v===true?'selected':''}>Enabled</option><option value="false" ${v===false?'selected':''}>Disabled</option></select>`;
 if(m.kind==='str')return `<input ${common} type="text" value="${esc(v??'')}">`;
 return `<input ${common} type="number" value="${esc(v??'')}" ${m.min!=null?`min="${m.min}"`:''} ${m.max!=null?`max="${m.max}"`:''} ${m.step!=null?`step="${m.step}"`:'step="any"'}>`}
function renderParamEditor(d){paramMeta=d;paramOriginal={...d.values};const grid=$('parameterGrid');const sections={};for(const m of d.parameters)(sections[m.section]??=[]).push(m);
 const cards=Object.entries(sections).map(([section,items])=>`<div class="card param-section-edit"><h2>${esc(section)}<span class="param-section-count">${items.length} parameters</span></h2>${items.map(m=>{const v=d.values[m.key];return `<div class="param-edit-row"><div class="param-label-wrap"><span class="param-name">${esc(m.label)}</span><span class="param-info" tabindex="0">i<span class="param-tip">${tipHtml(m)}</span></span></div><div class="param-input-wrap">${fieldHtml(m,v)}<span class="param-unit">${esc(m.unit||'')}</span></div></div>`}).join('')}</div>`).join('');
 const p=$('parameters');p.querySelector('.notice').innerHTML='<strong>Editable optimizer configuration.</strong> Changes are saved to the Home Assistant add-on options through Supervisor. A restart is required before the runtime uses the new values.';
 grid.innerHTML=cards;grid.querySelectorAll('.param-input').forEach(el=>el.addEventListener('input',()=>markParamChanged(el)));}
function valueOf(el,m){if(m.kind==='bool')return el.value==='true';if(m.kind==='int')return Number.parseInt(el.value,10);if(m.kind==='float')return Number(el.value);return el.value}
function markParamChanged(el){const m=paramMeta.parameters.find(x=>x.key===el.dataset.param),v=valueOf(el,m),old=paramOriginal[m.key];el.classList.toggle('param-changed',String(v)!==String(old));updateParamState()}
function changedParams(){const out={};document.querySelectorAll('.param-input').forEach(el=>{const m=paramMeta.parameters.find(x=>x.key===el.dataset.param),v=valueOf(el,m);if(String(v)!==String(paramOriginal[m.key]))out[m.key]=v});return out}
function updateParamState(){const n=Object.keys(changedParams()).length,$s=$('paramSaveState');if($s)$s.textContent=n?`${n} unsaved change${n===1?'':'s'}`:'No unsaved changes'}
async function loadParameterEditor(){try{renderParamEditor(await api('ui/parameters-meta'));updateParamState()}catch(e){$('parameterGrid').innerHTML=`<div class="notice param-error">Could not load editable parameters: ${esc(e.message)}</div>`}}
async function saveParameters(restart=false){const patch=changedParams(),status=$('paramSaveState');if(!Object.keys(patch).length){status.textContent='Nothing to save';return}status.textContent='Saving…';try{const r=await api('ui/parameters-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values:patch,restart})});status.textContent=restart?'Saved · restarting add-on…':'Saved · restart required';if(!restart){await loadParameterEditor()}else{setTimeout(()=>location.reload(),7000)}}catch(e){status.textContent=`Save failed: ${e.message}`;status.classList.add('param-error')}}
function installParameterEditor(){const p=$('parameters');if(!p||$('paramEditActions'))return;const notice=p.querySelector('.notice');notice.insertAdjacentHTML('beforebegin','<div class="param-edit-head"><div><h2 style="margin:0 0 3px">Parameters</h2><div class="param-restart-note">Saved changes take effect after add-on restart.</div></div><div class="param-actions" id="paramEditActions"><span id="paramSaveState" class="param-save-state">Loading…</span><button class="btn" id="paramSave">Save</button><button class="btn" id="paramSaveRestart">Save & restart</button></div></div>');$('paramSave').onclick=()=>saveParameters(false);$('paramSaveRestart').onclick=()=>saveParameters(true);loadParameterEditor()}
installParameterEditor();
</script>
'''


def install_ui_v164(app: FastAPI) -> None:
    @app.middleware("http")
    async def ui_v164(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace("</body>", OVERVIEW_EXTENSION + V158_EXTENSION + V159_EXTENSION + V160_EXTENSION + V161_EXTENSION + V161_FIX_EXTENSION + V163_EXTENSION + V164_EXTENSION + "</body>")
            return HTMLResponse(html)
        return await call_next(request)

    @app.get("/ui/parameters-meta", include_in_schema=False)
    async def parameters_meta():
        opts = _options()
        values = {m["key"]: opts.get(m["key"], m["default"]) for m in PARAMETERS}
        return JSONResponse({"parameters": PARAMETERS, "values": values, "restart_required": True})

    @app.post("/ui/parameters-save", include_in_schema=False)
    async def parameters_save(request: Request):
        body = await request.json()
        patch = body.get("values") or {}
        restart = bool(body.get("restart", False))
        if not isinstance(patch, dict):
            return JSONResponse({"error": "values must be an object"}, status_code=400)
        errors: dict[str, str] = {}
        clean: dict[str, Any] = {}
        for key, raw in patch.items():
            meta = PARAM_BY_KEY.get(key)
            if not meta:
                errors[str(key)] = "parameter is not editable"
                continue
            try: clean[key] = _coerce(meta, raw)
            except Exception as exc: errors[key] = str(exc)
        if errors:
            return JSONResponse({"error": "validation failed", "fields": errors}, status_code=400)
        current = _options()
        merged = {**current, **clean}
        try:
            status, result = await _supervisor_post("/addons/self/options", {"options": merged})
        except Exception as exc:
            return JSONResponse({"error": f"Supervisor save failed: {exc!r}"}, status_code=500)
        if status < 200 or status >= 300:
            return JSONResponse({"error": "Supervisor rejected options", "status": status, "detail": result}, status_code=502)
        if restart:
            async def delayed_restart():
                await asyncio.sleep(0.8)
                try: await _supervisor_post("/addons/self/restart", {})
                except Exception: pass
            asyncio.create_task(delayed_restart())
        return JSONResponse({"saved": sorted(clean), "restart_scheduled": restart, "restart_required": not restart})
