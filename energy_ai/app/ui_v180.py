from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import ui_v164
from .dashboard import DASHBOARD_HTML
from .overview_extension import OVERVIEW_EXTENSION
from .settings_store import (
    apply_setting_overrides,
    delete_setting_overrides,
    load_setting_overrides,
    set_setting_overrides,
)
from .ui_v158 import V158_EXTENSION
from .ui_v159 import V159_EXTENSION
from .ui_v160 import V160_EXTENSION
from .ui_v161 import V161_EXTENSION
from .ui_v161_fix import V161_FIX_EXTENSION
from .ui_v163 import V163_EXTENSION
from .ui_v164 import V164_EXTENSION, _coerce, _supervisor_post
from .ui_v165 import V165_EXTENSION


def _install_parameter_registry() -> None:
    replacement = [
        ui_v164.p(
            "Economics",
            "import_fixed_including_energy_tax_ore_kwh",
            "Fixed import cost incl. energy tax",
            "float",
            36.0,
            "Fixed per-kWh import component. The default is the 2026 Swedish energy tax, excluding VAT. Include any additional fixed per-kWh network component here if applicable.",
            unit="öre/kWh",
            recommended="2026 energy-tax default: 36.00 öre/kWh excluding VAT. Adjust to the full fixed per-kWh import amount used by your contract.",
            minimum=-500,
            maximum=1000,
            step=0.01,
        ),
        ui_v164.p(
            "Economics",
            "import_spot_percentage",
            "Import spot-linked grid fee",
            "float",
            6.86,
            "Percentage of the quarter-hour spot price added to import economics.",
            unit="%",
            physical="Use the network contract percentage applied to spot price.",
            minimum=-100,
            maximum=500,
            step=0.01,
        ),
        ui_v164.p(
            "Economics",
            "export_fixed_compensation_ore_kwh",
            "Fixed export compensation",
            "float",
            2.84,
            "Fixed per-kWh network compensation added to the spot value of exported production.",
            unit="öre/kWh",
            physical="C4 Energi 0.4 kV value from 2026-01-01: 2.84 öre/kWh.",
            minimum=-500,
            maximum=1000,
            step=0.01,
        ),
        ui_v164.p(
            "Economics",
            "export_spot_percentage",
            "Export spot-linked compensation",
            "float",
            6.05,
            "Percentage of the quarter-hour spot price added to export compensation.",
            unit="%",
            physical="C4 Energi 0.4 kV value from 2026-01-01: 6.05%.",
            minimum=-100,
            maximum=500,
            step=0.01,
        ),
        ui_v164.p(
            "Economics",
            "minimum_arbitrage_margin_ore_kwh",
            "Minimum arbitrage margin",
            "float",
            20.0,
            "Minimum extra value required before discretionary battery arbitrage is considered worthwhile.",
            unit="öre/kWh",
            recommended="15–40 öre/kWh is a practical starting range; higher values reduce cycling.",
            minimum=0,
            maximum=500,
            step=1,
        ),
        ui_v164.p(
            "Economics",
            "optimizer_battery_degradation_ore_kwh",
            "Battery degradation cost",
            "float",
            5.0,
            "External economic wear cost assigned to battery throughput.",
            unit="öre/kWh",
            recommended="Use a conservative lifecycle-cost estimate; 5–20 öre/kWh is a useful sensitivity range.",
            minimum=0,
            maximum=100,
            step=1,
        ),
    ]

    rebuilt: list[dict[str, Any]] = []
    inserted = False
    for item in ui_v164.PARAMETERS:
        if item.get("section") == "Economics":
            if not inserted:
                rebuilt.extend(replacement)
                inserted = True
            continue
        rebuilt.append(item)
    if not inserted:
        rebuilt.extend(replacement)
    ui_v164.PARAMETERS[:] = rebuilt
    ui_v164.PARAM_BY_KEY.clear()
    ui_v164.PARAM_BY_KEY.update({item["key"]: item for item in ui_v164.PARAMETERS})


_install_parameter_registry()
PARAMETERS = ui_v164.PARAMETERS
PARAM_BY_KEY = ui_v164.PARAM_BY_KEY


def _raw_supervisor_options() -> dict[str, Any]:
    try:
        raw = json.loads(ui_v164.OPTIONS_PATH.read_text(encoding="utf-8")) if ui_v164.OPTIONS_PATH.exists() else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


V180_EXTENSION = r'''
<style>
.param-reset{border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:7px;padding:6px 8px;font-size:10px;cursor:pointer;white-space:nowrap}.param-reset:hover{border-color:var(--blue);color:var(--text)}.param-source{font-size:9px;color:var(--muted);margin-left:5px;font-weight:400}.param-source-db{color:var(--warn)}
</style>
<script>
function renderParamEditor(d){
 paramMeta=d;paramOriginal={...d.values};const grid=$('parameterGrid');const sections={};for(const m of d.parameters)(sections[m.section]??=[]).push(m);
 const cards=Object.entries(sections).map(([section,items])=>`<div class="card param-section-edit"><h2>${esc(section)}<span class="param-section-count">${items.length} parameters</span></h2>${items.map(m=>{const v=d.values[m.key],src=(d.sources||{})[m.key]||'default',isDb=src==='db_override';return `<div class="param-edit-row"><div class="param-label-wrap"><span class="param-name">${esc(m.label)}</span>${isDb?'<span class="param-source param-source-db">DB override</span>':''}<span class="param-info" tabindex="0">i<span class="param-tip">${tipHtml(m)}</span></span></div><div class="param-input-wrap">${fieldHtml(m,v)}<span class="param-unit">${esc(m.unit||'')}</span>${isDb?`<button class="param-reset" data-reset-param="${esc(m.key)}" title="Remove Energy AI override and use Home Assistant/default value">Use HA/default</button>`:''}</div></div>`}).join('')}</div>`).join('');
 const p=$('parameters');p.querySelector('.notice').innerHTML='<strong>Persistent Energy AI settings.</strong> Values saved here are stored in the Energy AI database and override Home Assistant add-on options after restart. “Use HA/default” removes the database override for that parameter.';
 grid.innerHTML=cards;grid.querySelectorAll('.param-input').forEach(el=>el.addEventListener('input',()=>markParamChanged(el)));grid.querySelectorAll('[data-reset-param]').forEach(el=>el.addEventListener('click',()=>resetParameter(el.dataset.resetParam)));
}
async function resetParameter(key){const status=$('paramSaveState');status.textContent='Resetting…';try{await api('ui/parameters-reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys:[key],restart:false})});status.textContent='Override removed · restart required';await loadParameterEditor()}catch(e){status.textContent=`Reset failed: ${e.message}`;status.classList.add('param-error')}}
if($('parameterGrid'))loadParameterEditor();
</script>
'''


def install_ui_v180(app: FastAPI) -> None:
    @app.middleware("http")
    async def ui_v180(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace(
                "</body>",
                OVERVIEW_EXTENSION
                + V158_EXTENSION
                + V159_EXTENSION
                + V160_EXTENSION
                + V161_EXTENSION
                + V161_FIX_EXTENSION
                + V163_EXTENSION
                + V164_EXTENSION
                + V165_EXTENSION
                + V180_EXTENSION
                + "</body>",
            )
            return HTMLResponse(html)

        if request.url.path == "/ui/parameters-meta" and request.method.upper() == "GET":
            raw = _raw_supervisor_options()
            overrides = load_setting_overrides()
            effective = {**raw, **overrides}
            values = {m["key"]: effective.get(m["key"], m["default"]) for m in PARAMETERS}
            sources = {
                m["key"]: (
                    "db_override"
                    if m["key"] in overrides
                    else "home_assistant_options"
                    if m["key"] in raw
                    else "default"
                )
                for m in PARAMETERS
            }
            return JSONResponse(
                {
                    "parameters": PARAMETERS,
                    "values": values,
                    "sources": sources,
                    "restart_required": True,
                    "storage_precedence": ["default", "home_assistant_options", "db_override"],
                }
            )

        if request.url.path == "/ui/parameters-save" and request.method.upper() == "POST":
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "invalid JSON body"}, status_code=400)
            patch = body.get("values") or {}
            restart = bool(body.get("restart", False))
            if not isinstance(patch, dict):
                return JSONResponse({"error": "values must be an object"}, status_code=400)

            errors: dict[str, str] = {}
            clean: dict[str, Any] = {}
            for key, raw_value in patch.items():
                meta = PARAM_BY_KEY.get(key)
                if not meta:
                    errors[str(key)] = "parameter is not editable"
                    continue
                try:
                    clean[key] = _coerce(meta, raw_value)
                except Exception as exc:
                    errors[str(key)] = str(exc)
            if errors:
                return JSONResponse({"error": "validation failed", "fields": errors}, status_code=400)

            try:
                db_result = set_setting_overrides(clean, source="ui")
            except Exception as exc:
                return JSONResponse({"error": f"Could not persist settings in SQLite: {exc!r}"}, status_code=500)

            # Supervisor sync is best-effort only. SQLite is authoritative for
            # Energy AI UI settings and must succeed even without a token.
            supervisor_saved = False
            supervisor_error: str | None = None
            if os.getenv("SUPERVISOR_TOKEN"):
                try:
                    merged = apply_setting_overrides(_raw_supervisor_options())
                    status, result = await _supervisor_post("/addons/self/options", {"options": merged})
                    if 200 <= status < 300:
                        supervisor_saved = True
                    else:
                        supervisor_error = f"Supervisor returned HTTP {status}: {result!r}"
                except Exception as exc:
                    supervisor_error = repr(exc)

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

            return JSONResponse(
                {
                    "saved": sorted(clean),
                    "persistent_store": "sqlite",
                    "db_result": db_result,
                    "supervisor_synced": supervisor_saved,
                    "supervisor_error": supervisor_error,
                    "restart_required": not restart_scheduled,
                    "restart_scheduled": restart_scheduled,
                    "manual_restart_required": manual_restart_required,
                }
            )

        if request.url.path == "/ui/parameters-reset" and request.method.upper() == "POST":
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
            return JSONResponse(
                {
                    "removed_db_overrides": removed,
                    "fallback": "home_assistant_options_then_code_default",
                    "restart_required": not restart_scheduled,
                    "restart_scheduled": restart_scheduled,
                    "manual_restart_required": manual_restart_required,
                }
            )

        return await call_next(request)
