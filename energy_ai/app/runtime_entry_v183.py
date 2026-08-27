from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request

from . import load_forecast as load_forecast_module
from . import flexible_loads as flexible_loads_module
from .production_state import cancel_override, create_override, scheduled_overrides, set_mode, status
from .runtime_entry_v182 import app, core
from .settings_store import load_setting_overrides
from .ui_v183 import install_ui_v183
from .user_override_forecast import build_override_aware_forecast

RUNTIME_BUILD = "1.0.83"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

# Patch the load-forecast module's imported function reference. The frozen
# deterministic v3.5 solver is untouched; only flexible-load input composition
# receives explicit user overrides before a new forecast/plan is generated.
_base_flexible_load_forecast = flexible_loads_module.flexible_load_forecast
load_forecast_module.flexible_load_forecast = build_override_aware_forecast(_base_flexible_load_forecast)

install_ui_v183(app)


def _sauna_default_duration() -> int:
    overrides = load_setting_overrides()
    raw = overrides.get("sauna_default_duration_minutes", 120)
    try:
        return max(15, min(360, int(raw)))
    except Exception:
        return 120


async def _refresh_after_override() -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        result["load_forecast"] = await core._refresh_load_forecast()
    except Exception as exc:
        result["load_forecast"] = {"ok": False, "error": repr(exc)}
    try:
        result["optimizer_plan"] = await core._refresh_optimizer_plan()
    except Exception as exc:
        result["optimizer_plan"] = {"ok": False, "error": repr(exc)}
    return result


@app.get("/control/status", tags=["control"], summary="Persistent production operating mode and user overrides")
async def production_control_status():
    return {"production": await asyncio.to_thread(status), "overrides": await asyncio.to_thread(scheduled_overrides)}


@app.post("/control/mode/{mode}", tags=["control"], summary="Set persistent operating mode")
async def production_control_mode(mode: str):
    try:
        return await asyncio.to_thread(set_mode, mode, reason="api")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.post("/control/override", tags=["control"], summary="Create sauna or EV user override and immediately replan")
async def production_control_override(request: Request):
    body = await request.json()
    kind = str(body.get("kind") or "").strip()
    if kind not in {"sauna", "ev_charge_now"}:
        raise HTTPException(400, "kind must be sauna or ev_charge_now")

    starts_at = None
    if body.get("starts_at"):
        raw = str(body["starts_at"])
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if d.tzinfo is None:
                # Browser datetime-local is interpreted in the installation's
                # local timezone by the UI layer; preserve that wall-clock value
                # as Stockholm time through an explicit offset-free timestamp.
                starts_at = raw
            else:
                starts_at = d.astimezone(timezone.utc).isoformat()
        except Exception:
            raise HTTPException(400, "starts_at must be an ISO datetime")

    if kind == "sauna":
        duration = _sauna_default_duration()
    else:
        # Keep the existing provisional two-hour EV persistence semantics for
        # shadow planning. A later EV target-SOC/departure policy will replace it.
        duration = 120

    override = await asyncio.to_thread(
        create_override,
        kind,
        starts_at=starts_at,
        duration_minutes=duration,
        payload={"source": "overview_quick_control"},
    )
    refreshed = await _refresh_after_override()
    prod = await asyncio.to_thread(status)
    return {
        "ok": True,
        "override": override,
        "operating_mode": prod["operating_mode"],
        "physical_write_performed": False,
        "physical_writes_enabled": prod["physical_writes_enabled"],
        "refresh": refreshed,
    }


@app.post("/control/override/{override_id}/cancel", tags=["control"], summary="Cancel a user override and immediately replan")
async def production_control_override_cancel(override_id: int):
    try:
        item = await asyncio.to_thread(cancel_override, override_id)
    except KeyError:
        raise HTTPException(404, "override not found")
    refreshed = await _refresh_after_override()
    return {"ok": True, "override": item, "refresh": refreshed}


app.openapi_schema = None
