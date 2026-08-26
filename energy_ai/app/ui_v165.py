from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

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
from .ui_v164 import V164_EXTENSION, PARAM_BY_KEY, _coerce, _options, _supervisor_post

OPTIONS_PATH = Path("/data/options.json")


V165_EXTENSION = r'''
<script>
// 1.0.65: handle parameter persistence when Supervisor token is unavailable.
saveParameters=async function(restart=false){
  const patch=changedParams(),status=$('paramSaveState');
  status.classList.remove('param-error');
  if(!Object.keys(patch).length){status.textContent='Nothing to save';return}
  status.textContent='Saving…';
  try{
    const r=await api('ui/parameters-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values:patch,restart})});
    if(r.restart_scheduled){
      status.textContent='Saved · restarting add-on…';
      setTimeout(()=>location.reload(),7000);
      return;
    }
    if(restart && r.manual_restart_required){
      status.textContent='Saved · restart add-on manually';
      await loadParameterEditor();
      return;
    }
    status.textContent='Saved · restart required';
    await loadParameterEditor();
  }catch(e){
    status.textContent=`Save failed: ${e.message}`;
    status.classList.add('param-error');
  }
};
if($('paramSave'))$('paramSave').onclick=()=>saveParameters(false);
if($('paramSaveRestart'))$('paramSaveRestart').onclick=()=>saveParameters(true);
</script>
'''


def _atomic_write_options(options: dict[str, Any]) -> None:
    OPTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OPTIONS_PATH.with_name(OPTIONS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(options, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, OPTIONS_PATH)


def install_ui_v165(app: FastAPI) -> None:
    @app.middleware("http")
    async def ui_v165(request: Request, call_next):
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
                + "</body>",
            )
            return HTMLResponse(html)

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
            for key, raw in patch.items():
                meta = PARAM_BY_KEY.get(key)
                if not meta:
                    errors[str(key)] = "parameter is not editable"
                    continue
                try:
                    value = _coerce(meta, raw)
                    # Home Assistant add-on schema declares these two as strings.
                    if key in {"pv_tilt_deg", "pv_azimuth_deg"}:
                        value = str(value)
                    clean[key] = value
                except Exception as exc:
                    errors[str(key)] = str(exc)

            if errors:
                return JSONResponse({"error": "validation failed", "fields": errors}, status_code=400)

            merged = {**_options(), **clean}
            save_method = "options_file"
            supervisor_saved = False
            supervisor_error: str | None = None

            # Prefer Supervisor when a token is actually available. If not, use the
            # add-on's persistent /data/options.json, which is the same options store
            # read at startup by load_config().
            if os.getenv("SUPERVISOR_TOKEN"):
                try:
                    status, result = await _supervisor_post("/addons/self/options", {"options": merged})
                    if 200 <= status < 300:
                        supervisor_saved = True
                        save_method = "supervisor"
                    else:
                        supervisor_error = f"Supervisor returned HTTP {status}: {result!r}"
                except Exception as exc:
                    supervisor_error = repr(exc)

            if not supervisor_saved:
                try:
                    _atomic_write_options(merged)
                except Exception as exc:
                    return JSONResponse(
                        {"error": f"Could not persist add-on options: {exc!r}", "supervisor_error": supervisor_error},
                        status_code=500,
                    )

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

            return JSONResponse({
                "saved": sorted(clean),
                "save_method": save_method,
                "restart_required": not restart_scheduled,
                "restart_scheduled": restart_scheduled,
                "manual_restart_required": manual_restart_required,
                "supervisor_error": supervisor_error,
            })

        return await call_next(request)
