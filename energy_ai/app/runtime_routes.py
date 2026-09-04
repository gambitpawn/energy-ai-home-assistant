from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Query, Request

from . import model_selector as selector
from .actuator_timing_v194 import candidate_start_status
from .adaptive_auto import automatic_maintenance_once as adaptive_maintenance_once, automatic_status as adaptive_auto_status
from .adaptive_learning import active_run, current_parameters, latest_learning_status
from .adaptive_replay import build_daily_evaluator
from .app_comparison_v2 import compare_app_vs_planner
from .engine_contract import ENGINE_DECISION_SCHEMA, ENGINE_INPUT_SCHEMA
from .engine_registry import BASELINE_ENGINE_ID, baseline_decision_from_plan, registry_status
from .engine_store import latest_engine_decisions
from .historical_closed_loop import replay_regression
from .historical_closed_loop_v2 import compare_closed_loop
from .optimizer_contract_v189 import contract_status
from .optimizer_evaluation import evaluate_matured_optimizer_days
from .optimizer_store import latest_plan
from .price_economics import CURRENT_ECONOMICS, HISTORICAL_ECONOMICS, economics_for_timestamp, economics_payload, economics_signature, economics_versions, effective_prices
from .production_state import cancel_override, create_override, scheduled_overrides, set_mode, status as production_status
from .pv_auto import automatic_pv_retraining_once, pv_auto_status
from .regret_decomposition import regret_decomposition
from .settings_store import load_setting_overrides, settings_status
from .actuator_config import effective_actuator_config_report
from .actuator_release_state import release_status

OPTIONS_PATH = Path("/data/options.json")


def _sauna_default_duration() -> int:
    raw = load_setting_overrides().get("sauna_default_duration_minutes")
    if raw is None:
        try:
            opts = json.loads(OPTIONS_PATH.read_text(encoding="utf-8")) if OPTIONS_PATH.exists() else {}
            if isinstance(opts, dict):
                raw = opts.get("sauna_default_duration_minutes")
        except Exception:
            raw = None
    try:
        return max(15, min(360, int(raw if raw is not None else 120)))
    except Exception:
        return 120


def install_runtime_routes(
    *,
    app,
    core,
    actuator,
    adapter,
    live_state_cache,
    replan_state: dict[str, Any],
    soc_replanning_check: Callable[..., Any],
    candidate_from_selection: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    candidate_from_live_plan: Callable[[dict[str, Any]], dict[str, Any] | None],
    runtime_build: str,
    economics_version: Any,
    economics_patch_status: Any,
    economics_compat_status: Any,
) -> None:
    # ---- Optimizer evaluation / diagnostics ---------------------------------
    @app.get("/optimizer/evaluation/evaluate-now", tags=["optimizer-evaluation"], summary="Evaluate matured optimizer days")
    async def optimizer_evaluation_now_get(lookback_days: int = Query(7, ge=1, le=90)):
        try:
            return await asyncio.to_thread(evaluate_matured_optimizer_days, core.cfg, lookback_days)
        except Exception as exc:
            raise HTTPException(500, f"Optimizer hindsight evaluation failed: {exc!r}")

    @app.get("/optimizer/evaluation/app-comparison", tags=["optimizer-evaluation"])
    async def optimizer_app_comparison(
        start: str | None = None,
        end: str | None = None,
        hours: int | None = Query(None, ge=1, le=744),
        days: int | None = Query(None, ge=1, le=31),
        min_plan_coverage: float = Query(0.90, ge=0.50, le=1.0),
        min_actual_coverage: float = Query(0.90, ge=0.50, le=1.0),
        include_rows: bool = True,
    ):
        try:
            return await asyncio.to_thread(compare_app_vs_planner, core.cfg, start=start, end=end, hours=hours, days=days, min_plan_coverage=min_plan_coverage, min_actual_coverage=min_actual_coverage, include_rows=include_rows)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(500, f"App-vs-planner comparison failed: {exc!r}")

    @app.get("/optimizer/evaluation/closed-loop/regression", tags=["optimizer-evaluation"])
    async def optimizer_closed_loop_regression(samples: int = Query(12, ge=1, le=100)):
        try:
            return await asyncio.to_thread(replay_regression, core.cfg, samples)
        except Exception as exc:
            raise HTTPException(500, f"Closed-loop replay regression failed: {exc!r}")

    @app.get("/optimizer/evaluation/closed-loop", tags=["optimizer-evaluation"])
    async def optimizer_closed_loop(
        start: str | None = None,
        end: str | None = None,
        hours: int | None = Query(None, ge=1, le=744),
        days: int | None = Query(None, ge=1, le=31),
        min_information_coverage: float = Query(0.90, ge=0.50, le=1.0),
        min_actual_coverage: float = Query(0.90, ge=0.50, le=1.0),
        include_rows: bool = True,
    ):
        try:
            return await asyncio.to_thread(compare_closed_loop, core.cfg, start=start, end=end, hours=hours, days=days, min_information_coverage=min_information_coverage, min_actual_coverage=min_actual_coverage, include_rows=include_rows)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(500, f"Historical closed-loop comparison failed: {exc!r}")

    @app.get("/optimizer/evaluation/regret-decomposition", tags=["optimizer-evaluation"])
    async def optimizer_regret_decomposition(
        start: str | None = None,
        end: str | None = None,
        hours: int | None = Query(None, ge=1, le=744),
        days: int | None = Query(None, ge=1, le=31),
        include_rows: bool = False,
    ):
        try:
            return await asyncio.to_thread(regret_decomposition, core.cfg, start=start, end=end, hours=hours, days=days, include_rows=include_rows)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(500, f"Optimizer regret decomposition failed: {exc!r}")

    @app.get("/optimizer/contract/status", tags=["optimizer"])
    async def optimizer_contract_status():
        return {"runtime_build": runtime_build, **contract_status()}

    @app.get("/optimizer/replanning/status", tags=["optimizer"])
    async def optimizer_replanning_status():
        plan = await asyncio.to_thread(latest_plan, 500)
        from .soc_replanning import replanning_snapshot
        snapshot = await asyncio.to_thread(replanning_snapshot, plan, core.cfg) if plan.get("generated_at") and plan.get("rows") else {"status": "no_plan", "should_replan": False}
        return {"runtime_build": runtime_build, "normal_shared_vintage_planner": "deterministic_battery_dp_v3_5", "intra_quarter_live_planner": "deterministic_battery_dp_v3_6_live", "monitor": dict(replan_state), "current": snapshot}

    @app.post("/optimizer/replanning/run", tags=["optimizer"])
    async def optimizer_replanning_run(force: bool = Query(False)):
        return await soc_replanning_check(force=force)

    # ---- Engines -------------------------------------------------------------
    @app.get("/engines", tags=["engines"])
    async def engines_registry():
        data = registry_status()
        selection = await asyncio.to_thread(selector.selector_status, core.cfg)
        selected_engine = (selection.get("state") or {}).get("selected_engine_id")
        for item in data.get("engines") or []:
            item["logical_control_selected"] = item.get("engine_id") == selected_engine
        data["selection"] = {"logical_control_selection_enabled": True, "selected_engine_id": selected_engine, "selected_model_key": selection.get("selected_model_key"), "fallback_engine_id": BASELINE_ENGINE_ID, "selection_mode": "robust_10_day_promotion_with_live_disqualification", "requires_downstream_deterministic_safety": True}
        return data

    @app.get("/engines/contract", tags=["engines"])
    async def engines_contract():
        return {"contract_version": "v1", "input_schema": ENGINE_INPUT_SCHEMA, "decision_schema": ENGINE_DECISION_SCHEMA, "baseline_engine_id": BASELINE_ENGINE_ID, "shared_information_vintage": {"required": True}, "safety_boundary": {"inside_engine": False, "required_downstream_layer": "deterministic actuator safety"}}

    @app.get("/engines/baseline/latest", tags=["engines"])
    async def engines_baseline_latest(include_horizon: bool = False, include_plan_rows: bool = False):
        plan = await asyncio.to_thread(latest_plan, 500)
        if not plan.get("generated_at") or not plan.get("rows"):
            raise HTTPException(404, "No deterministic optimizer plan is available")
        engine_input, decision = await asyncio.to_thread(baseline_decision_from_plan, core.cfg, plan)
        stored = float(plan["rows"][0]["battery_action_kw"]); replayed = float(decision.requested_action_kw)
        return {"contract_version": "v1", "baseline_engine_id": BASELINE_ENGINE_ID, "engine_input": engine_input.as_dict(include_horizon=include_horizon), "engine_decision": decision.as_dict(include_plan_rows=include_plan_rows), "compatibility": {"stored_first_action_kw": stored, "contract_first_action_kw": replayed, "difference_kw": replayed-stored, "pass": abs(replayed-stored) <= 0.00011}}

    @app.get("/engines/history", tags=["engines"])
    async def engines_history(limit_per_engine: int = Query(1, ge=1, le=100)):
        return {"baseline_engine_id": BASELINE_ENGINE_ID, "decisions": await asyncio.to_thread(latest_engine_decisions, limit_per_engine)}

    @app.get("/engines/adaptive/status", tags=["engines-adaptive"])
    async def adaptive_status(): return await asyncio.to_thread(adaptive_auto_status)

    @app.get("/engines/adaptive/latest", tags=["engines-adaptive"])
    async def adaptive_latest(limit: int = Query(5, ge=1, le=100)):
        decisions = await asyncio.to_thread(latest_engine_decisions, limit)
        return {"engine_id": "adaptive_deterministic_v1", "decisions": decisions.get("adaptive_deterministic_v1") or [], "candidate_parameters": (await asyncio.to_thread(current_parameters, "candidate")).as_dict()}

    @app.post("/engines/adaptive/auto/run", tags=["engines-adaptive"])
    async def adaptive_auto_run(replay_date: str | None = None, force: bool = False): return await asyncio.to_thread(adaptive_maintenance_once, core.cfg, replay_date, force=force)

    @app.get("/engines/adaptive/replay/check", tags=["engines-adaptive"])
    async def adaptive_replay_check(replay_date: str = Query(...)):
        try:
            evaluator = await asyncio.to_thread(build_daily_evaluator, core.cfg, replay_date)
            return {"ok": True, "replay_date": replay_date, "initial_soc_pct": evaluator.initial_soc_pct, "intervals": len(evaluator.rows), "actual_coverage_fraction": evaluator.data.get("actual_coverage_fraction"), "information_vintages": len(evaluator.vintage_map), "reference_price_ore_kwh": evaluator.reference_price_ore_kwh}
        except Exception as exc:
            return {"ok": False, "replay_date": replay_date, "reason": repr(exc)}

    @app.get("/engines/selector/status", tags=["engines-selector"])
    async def selector_status_route(): return await asyncio.to_thread(selector.selector_status, core.cfg)

    @app.get("/engines/selector/scores", tags=["engines-selector"])
    async def selector_scores_route(days: int = Query(30, ge=1, le=180)): return await asyncio.to_thread(selector.selector_scores, core.cfg, days)

    @app.get("/engines/selector/control/latest", tags=["engines-selector"])
    async def selector_control_latest(): return {"selection": await asyncio.to_thread(selector.latest_control_selection)}

    @app.post("/engines/selector/evaluate", tags=["engines-selector"])
    async def selector_evaluate(local_date: str = Query(...), force: bool = False): return await asyncio.to_thread(selector.evaluate_selector_day, core.cfg, local_date, force=force)

    @app.post("/engines/selector/run", tags=["engines-selector"])
    async def selector_run(force: bool = False):
        running = await asyncio.to_thread(active_run)
        if running is not None: return {"ok": True, "status": "deferred", "reason": "adaptive_learning_active", "adaptive_run": running}
        return await asyncio.to_thread(selector.automatic_selector_maintenance_once, core.cfg, force=force)

    # ---- Economics / persistent settings ------------------------------------
    @app.get("/settings/status", tags=["settings"])
    async def persistent_settings_status(): return await asyncio.to_thread(settings_status)

    @app.get("/economics/status", tags=["economics"])
    async def economics_status_route():
        return {"ok": True, "runtime_build": runtime_build, "default_replay_mode": CURRENT_ECONOMICS, "historical_replay_mode": HISTORICAL_ECONOMICS, "current": economics_payload(core.cfg), "signature": economics_signature(core.cfg), "registered_version": economics_version, "runtime_patches": economics_patch_status, "compatibility_patches": economics_compat_status}

    @app.get("/economics/versions", tags=["economics"])
    async def economics_version_history(limit: int = Query(50, ge=1, le=500)): return {"versions": economics_versions(limit), "default_training_mode": CURRENT_ECONOMICS}

    @app.get("/economics/price", tags=["economics"])
    async def economics_price(spot_ore_kwh: float, mode: str = CURRENT_ECONOMICS, at: str | None = None):
        if mode not in {CURRENT_ECONOMICS, HISTORICAL_ECONOMICS}: raise HTTPException(400, "invalid economics mode")
        economics, source = economics_for_timestamp(core.cfg, at, mode)
        return {"ok": True, "mode": mode, "at": at, "economics_source": source, "economics": economics, **effective_prices(spot_ore_kwh, economics)}

    # ---- User controls -------------------------------------------------------
    async def _refresh_after_override():
        result = {}
        try: result["load_forecast"] = await core._refresh_load_forecast()
        except Exception as exc: result["load_forecast"] = {"ok": False, "error": repr(exc)}
        try: result["optimizer_plan"] = await core._refresh_optimizer_plan()
        except Exception as exc: result["optimizer_plan"] = {"ok": False, "error": repr(exc)}
        return result

    @app.get("/control/status", tags=["control"])
    async def control_status(): return {"production": await asyncio.to_thread(production_status), "overrides": await asyncio.to_thread(scheduled_overrides)}

    @app.post("/control/override", tags=["control"])
    async def control_override(request: Request):
        body = await request.json(); kind = str(body.get("kind") or "").strip()
        if kind not in {"sauna", "ev_charge_now"}: raise HTTPException(400, "kind must be sauna or ev_charge_now")
        starts_at = body.get("starts_at")
        if starts_at:
            try: datetime.fromisoformat(str(starts_at).replace("Z", "+00:00"))
            except Exception: raise HTTPException(400, "starts_at must be an ISO datetime")
        duration = _sauna_default_duration() if kind == "sauna" else 120
        item = await asyncio.to_thread(create_override, kind, starts_at=starts_at, duration_minutes=duration, payload={"source": "overview_quick_control"})
        prod = await asyncio.to_thread(production_status)
        return {"ok": True, "override": item, "operating_mode": prod["operating_mode"], "physical_writes_enabled": prod["physical_writes_enabled"], "refresh": await _refresh_after_override()}

    @app.post("/control/override/{override_id}/cancel", tags=["control"])
    async def control_override_cancel(override_id: int):
        try: item = await asyncio.to_thread(cancel_override, override_id)
        except KeyError: raise HTTPException(404, "override not found")
        return {"ok": True, "override": item, "refresh": await _refresh_after_override()}

    @app.post("/control/mode/{mode}", tags=["control"])
    async def control_mode(mode: str):
        normalized = str(mode).strip().lower()
        if normalized not in {"shadow", "active", "paused"}: raise HTTPException(400, f"unsupported mode {normalized!r}")
        current = production_status()
        if normalized == "active":
            preflight = await actuator.preflight()
            if not preflight.get("ok"): raise HTTPException(409, {"error": "actuator_preflight_required_before_active", "preflight": preflight})
            if release_status().get("release_pending"): raise HTTPException(409, "ACTIVE blocked until pending Solinteg safe release succeeds")
            if not current.get("actuator_ready"): raise HTTPException(409, "ACTIVE requires a successful zero-power arm handshake")
            plan = await asyncio.to_thread(latest_plan, 500)
            candidate = candidate_from_live_plan(plan) if str(plan.get("planner") or "") == "deterministic_battery_dp_v3_6_live" else candidate_from_selection(await asyncio.to_thread(selector.latest_control_selection))
            if candidate is None: raise HTTPException(409, "ACTIVE requires a current selector/live control candidate")

            timing = candidate_start_status(candidate)
            if timing.get("state") == "future":
                pending = await actuator.process_candidate(candidate)
                raise HTTPException(409, {
                    "error": "active_candidate_not_started",
                    "message": "ACTIVE remains disabled until the candidate decision_start is reached",
                    "timing": timing,
                    "pending": pending,
                    "production": production_status(),
                })
            if timing.get("state") != "started":
                raise HTTPException(409, {"error": timing.get("reason") or "candidate_timing_invalid", "timing": timing})

            try:
                scheduler = getattr(actuator, "_decision_start_scheduler_v194", None)
                if scheduler is not None:
                    async def _activate():
                        return await asyncio.to_thread(set_mode, "active", reason="api_actuator_transition")
                    actuation = await scheduler.activate_with(candidate, _activate)
                else:
                    await asyncio.to_thread(set_mode, "active", reason="api_actuator_transition")
                    actuation = await actuator.process_candidate(candidate)
            except Exception as exc:
                try: await actuator.fail_safe("active_transition_failed", {"error": repr(exc)})
                except Exception: pass
                raise HTTPException(500, f"ACTIVE transition failed: {exc!r}")
            if actuation.get("status") not in {"acknowledged", "held_existing"}:
                try: await actuator.fail_safe("active_transition_unacknowledged", {"actuation": actuation})
                except Exception: pass
                raise HTTPException(409, f"ACTIVE transition did not produce an acknowledged command: {actuation}")
            return {**production_status(), "actuator_transition": actuation, "requested_mode": "active"}
        release = None
        if current.get("operating_mode") == "active" or current.get("physical_writes_enabled"):
            try:
                release = await adapter.safe_release()
                if not release.get("released"): raise RuntimeError(f"safe release incomplete: {release}")
            except Exception as exc:
                from .production_state import mark_actuator_ready
                mark_actuator_ready(False, detail="mode_exit_safe_release_failed")
                await asyncio.to_thread(set_mode, "paused", reason="safe_release_failed")
                raise HTTPException(503, f"Could not safely release Solinteg before leaving ACTIVE: {exc!r}")
        prod = await asyncio.to_thread(set_mode, normalized, reason="api_actuator_transition")
        return {**prod, "actuator_transition": {"safe_release": release}, "requested_mode": normalized}

    # ---- Actuator ------------------------------------------------------------
    @app.get("/actuator/status", tags=["actuator"])
    async def actuator_status():
        data = await actuator.status(); data["safe_release"] = await asyncio.to_thread(release_status); return data

    @app.get("/actuator/timing/status", tags=["actuator"])
    async def actuator_timing_status():
        scheduler = getattr(actuator, "_decision_start_scheduler_v194", None)
        if scheduler is None:
            return {"runtime_build": runtime_build, "policy": "unavailable", "no_early_dispatch": False}
        return {"runtime_build": runtime_build, **scheduler.status()}

    @app.get("/actuator/discover", tags=["actuator"])
    async def actuator_discover():
        try: return await adapter.discovery_report()
        except Exception as exc: raise HTTPException(503, f"Solinteg discovery failed: {exc!r}")

    @app.post("/actuator/preflight", tags=["actuator"])
    async def actuator_preflight(): return await actuator.preflight()

    @app.post("/actuator/arm", tags=["actuator"])
    async def actuator_arm(confirm: bool = Query(False)):
        if not confirm: raise HTTPException(400, "confirm=true is required; arming performs a physical zero-target/mode handshake")
        return await actuator.zero_handshake_and_arm()

    @app.post("/actuator/disarm", tags=["actuator"])
    async def actuator_disarm(): return await actuator.disarm("api")

    @app.post("/actuator/run", tags=["actuator"])
    async def actuator_run():
        plan = await asyncio.to_thread(latest_plan, 500)
        candidate = candidate_from_live_plan(plan) if str(plan.get("planner") or "") == "deterministic_battery_dp_v3_6_live" else candidate_from_selection(await asyncio.to_thread(selector.latest_control_selection))
        if candidate is None: raise HTTPException(409, "No current control candidate is available")
        return await actuator.process_candidate(candidate)

    @app.get("/actuator/physical-cap/status", tags=["actuator"])
    async def actuator_physical_cap_status():
        report = effective_actuator_config_report(core.cfg); runtime = report.get("runtime") or {}
        return {"runtime_build": runtime_build, "max_physical_command_kw": float(runtime.get("max_physical_command_kw", 2.0)), "applies_to": ["charge", "discharge"], "position_in_control_chain": "after_deterministic_safety_before_solinteg_dispatch", "affects_optimizer_or_selector": False, "restart_required": bool(report.get("restart_required")), "runtime_matches_persisted": bool(report.get("runtime_matches_persisted"))}

    # ---- PV automatic calibration -------------------------------------------
    @app.get("/forecast/pv/auto/status", tags=["forecast-pv"])
    async def pv_retraining_status(): return await asyncio.to_thread(pv_auto_status)

    @app.post("/forecast/pv/auto/run", tags=["forecast-pv"])
    async def pv_retraining_run(force: bool = False):
        running = await asyncio.to_thread(active_run)
        if running is not None: return {"ok": True, "status": "deferred", "reason": "adaptive_learning_active", "adaptive_run": running}
        return await asyncio.to_thread(automatic_pv_retraining_once, force=force)

    app.openapi_schema = None
