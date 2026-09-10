from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .db import DB_PATH
from .engine_operator_selection import control_status, set_operator_preference
from .optimizer_store import latest_plan


UI_EXTENSION = r'''
<script>
// The Plan/Overview forecast surface must show the same routed engine horizon that
// is eligible for physical control, not the frozen deterministic baseline store.
loadPlan=async function(){
  try{state.plan=await api('ui/control-plan?limit=144');renderPlan()}
  catch(e){$('planMeta').textContent=e.message}
};
// dashboard.init() starts before extensions are appended, so replace any baseline
// plan fetched during initialisation as soon as this authority extension loads.
loadPlan();
</script>
'''


def _utc(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _same_start(a: Any, b: Any) -> bool:
    try:
        return _utc(str(a)) == _utc(str(b))
    except Exception:
        return str(a) == str(b)


def _latest_routing_for_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    rows = list(plan.get("rows") or [])
    first_start = rows[0].get("start") if rows else None
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            row = None
            if first_start is not None:
                candidates = c.execute(
                    '''SELECT information_vintage_id,decision_start,created_at,routed_engine_id,
                              decision_id,requested_action_kw,fallback_used,reason,payload_json
                       FROM engine_control_selection
                       ORDER BY created_at DESC LIMIT 24'''
                ).fetchall()
                row = next((r for r in candidates if _same_start(r[1], first_start)), None)
            if row is None:
                row = c.execute(
                    '''SELECT information_vintage_id,decision_start,created_at,routed_engine_id,
                              decision_id,requested_action_kw,fallback_used,reason,payload_json
                       FROM engine_control_selection
                       ORDER BY created_at DESC LIMIT 1'''
                ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[8] or "{}")
    except Exception:
        payload = {}
    return {
        "information_vintage_id": str(row[0]),
        "decision_start": str(row[1]),
        "created_at": str(row[2]),
        "routed_engine_id": None if row[3] is None else str(row[3]),
        "decision_id": None if row[4] is None else str(row[4]),
        "requested_action_kw": None if row[5] is None else float(row[5]),
        "fallback_used": bool(row[6]),
        "reason": str(row[7]),
        "payload": payload if isinstance(payload, dict) else {},
    }


def _decision_payload(decision_id: str | None) -> dict[str, Any] | None:
    if not decision_id:
        return None
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            row = c.execute(
                "SELECT payload_json FROM engine_decision WHERE decision_id=? LIMIT 1",
                (str(decision_id),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0] or "{}")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _reason(action: float, load: float, pv: float) -> str:
    if action < -0.05:
        surplus = max(0.0, pv - load)
        return "pv_charge" if -action <= surplus + 1e-6 else "mixed_charge"
    if action > 0.05:
        return "discharge"
    return "idle"


def merge_selected_engine_plan(
    base_plan: dict[str, Any],
    routing: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    *,
    limit: int = 144,
) -> dict[str, Any]:
    """Merge common forecast inputs with the routed engine's complete horizon.

    optimizer_plan remains the immutable baseline store. This projection is for
    operator/control truth: forecast inputs come from that common vintage while
    battery action and expected SOC come from the routed engine decision.
    """
    result = dict(base_plan)
    base_rows = [dict(r) for r in (base_plan.get("rows") or [])]
    plan_rows = [dict(r) for r in ((decision or {}).get("plan_rows") or [])]
    by_start = {
        _utc(str(r.get("start") or r.get("start_utc"))).isoformat(): r
        for r in plan_rows
        if r.get("start") is not None or r.get("start_utc") is not None
    }
    routed_engine = None if not routing else routing.get("routed_engine_id")
    if not by_start or not routed_engine:
        result["control_plan_source"] = "baseline_fallback"
        result["routing"] = routing
        result["rows"] = base_rows[: max(1, min(int(limit), 500))]
        return result

    merged: list[dict[str, Any]] = []
    charge_kwh = discharge_kwh = grid_import_kwh = grid_export_kwh = 0.0
    for base in base_rows:
        raw_start = base.get("start") or base.get("start_utc")
        if raw_start is None:
            continue
        key = _utc(str(raw_start)).isoformat()
        chosen = by_start.get(key)
        row = dict(base)
        if chosen is not None:
            action_raw = chosen.get("requested_action_kw", chosen.get("battery_action_kw", chosen.get("action_kw")))
            if action_raw is not None:
                action = float(action_raw)
                row["battery_action_kw"] = action
                row["action_kw"] = action
                if chosen.get("expected_soc_pct") is not None:
                    row["expected_soc_pct"] = float(chosen["expected_soc_pct"])
                elif chosen.get("soc_end_pct") is not None:
                    row["expected_soc_pct"] = float(chosen["soc_end_pct"])
                load = float(row.get("load_kw") or 0.0)
                pv = float(row.get("pv_kw") or 0.0)
                grid = load - pv - action
                row["grid_import_kw"] = max(0.0, grid)
                row["grid_export_kw"] = max(0.0, -grid)
                row["reason"] = _reason(action, load, pv)
        dt = float(row.get("duration_hours") or 0.25)
        action = float(row.get("battery_action_kw", row.get("action_kw", 0.0)) or 0.0)
        charge_kwh += max(0.0, -action) * dt
        discharge_kwh += max(0.0, action) * dt
        grid_import_kwh += float(row.get("grid_import_kw") or 0.0) * dt
        grid_export_kwh += float(row.get("grid_export_kw") or 0.0) * dt
        merged.append(row)

    result["planner"] = str(routed_engine)
    result["mode"] = "selected_engine_control"
    result["control_plan_source"] = "routed_engine_decision"
    result["routing"] = routing
    result["engine_decision_id"] = None if routing is None else routing.get("decision_id")
    result["rows"] = merged[: max(1, min(int(limit), 500))]
    summary = dict(result.get("summary") or {})
    summary.update({
        "initial_soc_pct": result.get("initial_soc_pct"),
        "charge_kwh": round(charge_kwh, 4),
        "discharge_kwh": round(discharge_kwh, 4),
        "grid_import_kwh": round(grid_import_kwh, 4),
        "grid_export_kwh": round(grid_export_kwh, 4),
    })
    result["summary"] = summary
    return result


def selected_control_plan(limit: int = 144) -> dict[str, Any]:
    base_plan = latest_plan(500)
    if base_plan.get("generated_at") is None:
        return base_plan
    routing = _latest_routing_for_plan(base_plan)
    decision = _decision_payload(None if routing is None else routing.get("decision_id"))
    return merge_selected_engine_plan(base_plan, routing, decision, limit=limit)


def _remove_route(app, path: str, method: str | None = None) -> None:
    wanted = None if method is None else method.upper()
    kept = []
    for route in app.router.routes:
        if getattr(route, "path", None) != path:
            kept.append(route)
            continue
        methods = {str(m).upper() for m in (getattr(route, "methods", None) or set())}
        if wanted is not None and wanted not in methods:
            kept.append(route)
    app.router.routes[:] = kept


def install_selected_engine_authority(*, app, base, runtime_ui_module) -> dict[str, Any]:
    """Make routed engine choice authoritative for plan display and replanning."""
    if getattr(base, "_SELECTED_ENGINE_AUTHORITY_INSTALLED", False):
        return {"installed": True, "already_installed": True}

    legacy_live_replan = base.run_live_replan
    base._LEGACY_DETERMINISTIC_V36_LIVE_REPLAN = legacy_live_replan

    async def selected_engine_replan(reason: str) -> dict[str, Any]:
        # Replanning must use the same engine-routing pipeline as quarter control.
        # This is heavier than the old v36-only path, so the existing SOC threshold
        # and cooldown remain the gate; no extra periodic optimizer work is added.
        async with base._OPTIMIZER_REFRESH_LOCK:
            pipeline = await base._refresh_optimizer_pipeline_unlocked()
            plan = await asyncio.to_thread(latest_plan, 500)
        routed = dict(pipeline.get("model_selector") or {})
        generated_at = plan.get("generated_at") or datetime.now(timezone.utc).isoformat()
        base._REPLAN_STATE.update({
            "status": "replanned",
            "last_triggered_at": generated_at,
            "last_trigger_reason": reason,
            "last_error": None,
            "trigger_count": int(base._REPLAN_STATE.get("trigger_count") or 0) + 1,
            "last_selected_engine_replan": {
                "generated_at": generated_at,
                "routed_engine_id": routed.get("routed_engine_id"),
                "selection_mode": routed.get("selection_mode"),
                "fallback_used": bool(routed.get("fallback_used")),
                "decision_start": routed.get("decision_start"),
            },
        })
        return {
            "ok": True,
            "status": "replanned",
            "reason": reason,
            "generated_at": generated_at,
            "planner": routed.get("routed_engine_id"),
            "selection_mode": routed.get("selection_mode"),
            "fallback_used": bool(routed.get("fallback_used")),
            "comparison_eligible": False,
            "selected_engine_authority": True,
            "actuator": pipeline.get("actuator"),
        }

    base.run_live_replan = selected_engine_replan

    _remove_route(app, "/ui/model-control", "POST")

    @app.post("/ui/model-control", include_in_schema=False)
    async def ui_set_model_control_authoritative(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        selection = str((payload or {}).get("selection") or "").strip()
        previous = control_status(base.core.cfg)
        try:
            set_operator_preference(selection)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        refresh: dict[str, Any]
        if selection == previous.get("selection"):
            refresh = {"status": "unchanged", "refreshed": False}
        else:
            try:
                result = await base.refresh_optimizer_plan()
                refresh = {
                    "status": "ok",
                    "refreshed": True,
                    "routed_engine_id": (result.get("model_selector") or {}).get("routed_engine_id"),
                    "actuator_status": (result.get("actuator") or {}).get("status"),
                }
            except Exception as exc:
                # Preference is persisted; report that immediate refresh failed
                # instead of pretending the old engine is still the requested one.
                refresh = {"status": "failed", "refreshed": False, "error": repr(exc)}
        response = control_status(base.core.cfg)
        response["routing_refresh"] = refresh
        return JSONResponse(response)

    @app.get("/ui/control-plan", include_in_schema=False)
    async def ui_control_plan(limit: int = 144):
        try:
            return JSONResponse(selected_control_plan(limit))
        except Exception as exc:
            return JSONResponse({"error": repr(exc), "rows": []}, status_code=500)

    if UI_EXTENSION not in runtime_ui_module.CURRENT_UI_EXTENSION:
        runtime_ui_module.CURRENT_UI_EXTENSION += UI_EXTENSION

    setattr(base, "_SELECTED_ENGINE_AUTHORITY_INSTALLED", True)
    return {
        "installed": True,
        "already_installed": False,
        "normal_replanning": "selected_engine_pipeline",
        "legacy_v36_direct_actuation": False,
        "manual_selection_immediate_refresh": True,
        "plan_ui_source": "routed_engine_decision",
    }
