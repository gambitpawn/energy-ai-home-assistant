from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Query, Request

from . import model_selector as selector
from .actuator_config import effective_actuator_config_report
from .adaptive_auto import automatic_maintenance_once as adaptive_maintenance_once, automatic_status as adaptive_status_data
from .adaptive_learning import active_run, current_parameters, latest_learning_status
from .adaptive_replay import build_daily_evaluator
from .app_comparison_v2 import compare_app_vs_planner
from .engine_contract import ENGINE_DECISION_SCHEMA, ENGINE_INPUT_SCHEMA
from .engine_input_v2 import input_from_optimizer_plan_v2
from .engine_registry import BASELINE_ENGINE_ID, baseline_decision_from_plan, registry_status
from .engine_store import latest_engine_decisions
from .historical_closed_loop import replay_regression
from .historical_closed_loop_v2 import compare_closed_loop
from .model_selector import release_status if False else selector_status  # type: ignore
from .neural_auto import automatic_maintenance_once as neural_maintenance_once, automatic_status as neural_auto_status_data
from .neural_engine import neural_runtime_status
from .neural_features import feature_metadata
from .neural_teacher_v2 import LABEL_SOURCE_V2
from .neural_training import model_history, train_model
from .neural_training_v2 import build_training_samples, training_maturity_status
from .optimizer_contract_v189 import contract_status
from .optimizer_evaluation import evaluate_matured_optimizer_days
from .optimizer_store import latest_plan
from .price_economics import CURRENT_ECONOMICS, HISTORICAL_ECONOMICS, economics_for_timestamp, economics_payload, economics_signature, economics_versions, effective_prices
from .production_state import cancel_override, create_override, scheduled_overrides, set_mode, status as production_status
from .pv_auto import automatic_pv_retraining_once, pv_auto_status
from .regret_decomposition import regret_decomposition
from .settings_store import load_setting_overrides, settings_status
from .actuator_release_state import release_status

OPTIONS_PATH = Path("/data/options.json")


def _sauna_default_duration() -> int:
    raw = load_setting_overrides().get("sauna_default_duration_minutes")
    if raw is None:
        try:
            opts = json.loads(OPTIONS_PATH.read_text(encoding="utf-8")) if OPTIONS_PATH.exists() else {}
            raw = opts.get("sauna_default_duration_minutes") if isinstance(opts, dict) else None
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
    economics_version: str,
    economics_patch_status: Any,
    economics_compat_status: Any,
    economics_neural_status: Any,
) -> None:
    # Remove only routes whose final implementation is owned here. Tariff/UI/base
    # routes remain registered by their semantic modules.
    owned = {
        "/optimizer/evaluation/evaluate-now", "/optimizer/evaluation/app-comparison",
        "/optimizer/evaluation/closed-loop", "/optimizer/evaluation/closed-loop/regression",
        "/optimizer/evaluation/regret-decomposition", "/engines", "/engines/contract",
        "/engines/baseline/latest", "/engines/history", "/engines/neural/status",
        "/engines/neural/build-samples", "/engines/neural/train", "/engines/neural/bootstrap",
        "/engines/neural/latest", "/engines/neural/maturity", "/engines/neural/auto/status",
        "/engines/neural/auto/run", "/engines/neural/models", "/engines/neural/features",
        "/engines/neural/input/latest", "/engines/adaptive/status", "/engines/adaptive/latest",
        "/engines/adaptive/auto/run", "/engines/adaptive/replay/check", "/engines/selector/status",
        "/engines/selector/scores", "/engines/selector/control/latest", "/engines/selector/evaluate",
        "/engines/selector/run", "/forecast/pv/auto/status", "/forecast/pv/auto/run",
        "/economics/status", "/economics/versions", "/economics/price", "/settings/status",
        "/control/status", "/control/mode/{mode}", "/control/override", "/control/override/{override_id}/cancel",
        "/optimizer/replanning/status", "/optimizer/replanning/run", "/actuator/status",
        "/actuator/discover", "/actuator/preflight", "/actuator/arm", "/actuator/disarm",
        "/actuator/run", "/optimizer/contract/status", "/actuator/physical-cap/status",
    }
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) not in owned]

    @app.get("/optimizer/evaluation/evaluate-now", tags=["optimizer-evaluation"])
    async def optimizer_evaluation_now_get(lookback_days: int = Query(7, ge=1, le=90)):
        return await asyncio.to_thread(evaluate_matured_optimizer_days, core.cfg, lookback_days)

    @app.get("/optimizer/evaluation/app-comparison", tags=["optimizer-evaluation"])
    async def optimizer_app_comparison(start: str | None = None, end: str | None = None, hours: int | None = Query(None, ge=1, le=744), days: int | None = Query(None, ge=1, le=31), min_plan_coverage: float = Query(.90, ge=.5, le=1), min_actual_coverage: float = Query(.90, ge=.5, le=1), include_rows: bool = True):
        try:
            return await asyncio.to_thread(compare_app_vs_planner, core.cfg, start=start, end=end, hours=hours, days=days, min_plan_coverage=min_plan_coverage, min_actual_coverage=min_actual_coverage, include_rows=include_rows)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/optimizer/evaluation/closed-loop/regression", tags=["optimizer-evaluation"])
    async def optimizer_closed_loop_regression(samples: int = Query(12, ge=1, le=100)):
        return await asyncio.to_thread(replay_regression, core.cfg, samples)

    @app.get("/optimizer/evaluation/closed-loop", tags=["optimizer-evaluation"])
    async def optimizer_closed_loop_v2(start: str | None = None, end: str | None = None, hours: int | None = Query(None, ge=1, le=744), days: int | None = Query(None, ge=1, le=31), min_information_coverage: float = Query(.90, ge=.5, le=1), min_actual_coverage: float = Query(.90, ge=.5, le=1), include_rows: bool = True):
        try:
            return await asyncio.to_thread(compare_closed_loop, core.cfg, start=start, end=end, hours=hours, days=days, min_information_coverage=min_information_coverage, min_actual_coverage=min_actual_coverage, include_rows=include_rows)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/optimizer/evaluation/regret-decomposition", tags=["optimizer-evaluation"])
    async def optimizer_regret_decomposition(start: str | None = None, end: str | None = None, hours: int | None = Query(None, ge=1, le=744), days: int | None = Query(None, ge=1, le=31), include_rows: bool = False):
        try:
            return await asyncio.to_thread(regret_decomposition, core.cfg, start=start, end=end, hours=hours, days=days, include_rows=include_rows)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/engines", tags=["engines"])
    async def engines_registry():
        data = registry_status()
        selection = await asyncio.to_thread(selector.selector_status, core.cfg)
        neural = await asyncio.to_thread(neural_runtime_status)
        selected = (selection.get("state") or {}).get("selected_engine_id")
        for item in data.get("engines") or []:
            if item.get("engine_id") == "neural_v1":
                item["available"] = bool(neural.get("shadow_ready"))
                item["learning_enabled"] = bool(neural.get("model_exists"))
                item["runtime_status"] = {k: neural.get(k) for k in ("model_exists", "samples", "shadow_ready", "trained_at", "model_id")}
            item["logical_control_selected"] = item.get("engine_id") == selected
        data["selection"] = {
            "logical_control_selection_enabled": True,
            "selected_engine_id": selected,
            "selected_model_key": selection.get("selected_model_key"),
            "fallback_engine_id": BASELINE_ENGINE_ID,
            "selection_mode": "robust_10_day_promotion_with_live_disqualification",
            "physical_writes_enabled": bool(production_status().get("physical_writes_enabled")),
            "requires_downstream_deterministic_safety": True,
        }
        return data

    @app.get("/engines/contract", tags=["engines"])
    async def engines_contract():
        return {"contract_version": "v1", "input_schema": ENGINE_INPUT_SCHEMA, "decision_schema": ENGINE_DECISION_SCHEMA, "baseline_engine_id": BASELINE_ENGINE_ID, "shared_information_vintage_required": True, "requested_action_sign": "positive discharge, negative charge", "requires_downstream_deterministic_safety": True}

    @app.get("/engines/baseline/latest", tags=["engines"])
    async def engines_baseline_latest(include_horizon: bool = False, include_plan_rows: bool = False):
        plan = latest_plan(500)
        if plan.get("generated_at") is None or not plan.get("rows"):
            raise HTTPException(404, "No deterministic optimizer plan is available")
        engine_input, decision = await asyncio.to_thread(baseline_decision_from_plan, core.cfg, plan)
        diff = float(decision.requested_action_kw) - float(plan["rows"][0]["battery_action_kw"])
        return {"contract_version": "v1", "baseline_engine_id": BASELINE_ENGINE_ID, "engine_input": engine_input.as_dict(include_horizon=include_horizon), "engine_decision": decision.as_dict(include_plan_rows=include_plan_rows), "compatibility": {"difference_kw": round(diff, 6), "tolerance_kw": .00011, "pass": abs(diff) <= .00011}}

    @app.get("/engines/history", tags=["engines"])
    async def engines_history(limit_per_engine: int = Query(1, ge=1, le=100)):
        return {"baseline_engine_id": BASELINE_ENGINE_ID, "decisions": await asyncio.to_thread(latest_engine_decisions, limit_per_engine)}

    @app.get("/engines/neural/status", tags=["engines-neural"])
    async def neural_status(): return await asyncio.to_thread(neural_runtime_status)

    @app.post("/engines/neural/build-samples", tags=["engines-neural"])
    async def neural_build_samples(max_new: int = Query(32, ge=1, le=256), candidate_limit: int = Query(1500, ge=10, le=10000)):
        return await asyncio.to_thread(build_training_samples, core.cfg, max_new, candidate_limit)

    @app.post("/engines/neural/train", tags=["engines-neural"])
    async def neural_train(): return await asyncio.to_thread(train_model)

    @app.post("/engines/neural/bootstrap", tags=["engines-neural"])
    async def neural_bootstrap(max_new: int = Query(64, ge=1, le=256), candidate_limit: int = Query(2000, ge=10, le=10000)):
        samples = await asyncio.to_thread(build_training_samples, core.cfg, max_new, candidate_limit)
        training = await asyncio.to_thread(train_model)
        return {"samples": samples, "training": training, "status": await asyncio.to_thread(neural_runtime_status)}

    @app.get("/engines/neural/latest", tags=["engines-neural"])
    async def neural_latest(limit: int = Query(5, ge=1, le=100)):
        d = await asyncio.to_thread(latest_engine_decisions, limit)
        return {"baseline_engine_id": BASELINE_ENGINE_ID, "neural_v1": d.get("neural_v1") or []}

    @app.get("/engines/neural/maturity", tags=["engines-neural"])
    async def neural_maturity(candidate_limit: int = Query(2000, ge=10, le=10000)):
        return await asyncio.to_thread(training_maturity_status, core.cfg, candidate_limit)

    @app.get("/engines/neural/auto/status", tags=["engines-neural"])
    async def neural_auto_status(): return await asyncio.to_thread(neural_auto_status_data)

    @app.post("/engines/neural/auto/run", tags=["engines-neural"])
    async def neural_auto_run(): return await asyncio.to_thread(neural_maintenance_once, core.cfg)

    @app.get("/engines/neural/models", tags=["engines-neural"])
    async def neural_models(limit: int = Query(20, ge=1, le=100)):
        return {"engine_id": "neural_v1", "models": await asyncio.to_thread(model_history, limit)}

    @app.get("/engines/neural/features", tags=["engines-neural"])
    async def neural_features(): return {**feature_metadata(), "label_source": LABEL_SOURCE_V2, "installation_profile_included": True, "demand_tariff_state_included": True}

    @app.get("/engines/neural/input/latest", tags=["engines-neural"])
    async def neural_input_latest(include_horizon: bool = False):
        return input_from_optimizer_plan_v2(latest_plan(500), core.cfg).as_dict(include_horizon=include_horizon)

    @app.get("/engines/adaptive/status", tags=["engines-adaptive"])
    async def adaptive_status(): return await asyncio.to_thread(adaptive_status_data)

    @app.get("/engines/adaptive/latest", tags=["engines-adaptive"])
    async def adaptive_latest(limit: int = Query(5, ge=1, le=100)):
        d = await asyncio.to_thread(latest_engine_decisions, limit)
        return {"engine_id": "adaptive_deterministic_v1", "decisions": d.get("adaptive_deterministic_v1") or [], "candidate_parameters": (await asyncio.to_thread(current_parameters, "candidate")).as_dict()}

    @app.post("/engines/adaptive/auto/run", tags=["engines-adaptive"])
    async def adaptive_auto_run(replay_date: str | None = None, force: bool = False):
        return await asyncio.to_thread(adaptive_maintenance_once, core.cfg, replay_date, force=force)

    @app.get("/engines/adaptive/replay/check", tags=["engines-adaptive"])
    async def adaptive_replay_check(replay_date: str):
        try:
            evaluator = await asyncio.to_thread(build_daily_evaluator, core.cfg, replay_date)
            learning = await asyncio.to_thread(latest_learning_status)
            return {"ok": True, "replay_date": replay_date, "initial_soc_pct": evaluator.initial_soc_pct, "intervals": len(evaluator.rows), "actual_coverage_fraction": evaluator.data.get("actual_coverage_fraction"), "information_vintages": len(evaluator.vintage_map), "candidate_parameters": learning.get("candidate_parameters")}
        except Exception as exc:
            return {"ok": False, "replay_date": replay_date, "reason": repr(exc)}

    @app.get("/engines/selector/status", tags=["engines-selector"])
    async def selector_status_route(): return await asyncio.to_thread(selector.selector_status, core.cfg)

    @app.get("/engines/selector/scores", tags=["engines-selector"])
    async def selector_scores_route(days: int = Query(30, ge=1, le=180)): return await asyncio.to_thread(selector.selector_scores, core.cfg, days)

    @app.get("/engines/selector/control/latest", tags=["engines-selector"])
    async def selector_control_latest(): return {"selection": await asyncio.to_thread(selector.latest_control_selection), "physical_writes_enabled": bool(production_status().get("physical_writes_enabled"))}

    @app.post("/engines/selector/evaluate", tags=["engines-selector"])
    async def selector_evaluate(local_date: str, force: bool = False): return await asyncio.to_thread(selector.evaluate_selector_day, core.cfg, local_date, force=force)

    @app.post("/engines/selector/run", tags=["engines-selector"])
    async def selector_run(force: bool = False):
        running = await asyncio.to_thread(active_run)
        if running is not None: return {"ok": True, "status": "deferred", "reason": "adaptive_learning_active", "adaptive_run": running}
        return await asyncio.to_thread(selector.automatic_selector_maintenance_once, core.cfg, force=force)

    @app.get("/forecast/pv/auto/status", tags=["forecast-pv"])
    async def pv_auto_status_route(): return await asyncio.to_thread(pv_auto_status)

    @app.post("/forecast/pv/auto/run", tags=["forecast-pv"])
    async def pv_auto_run(force: bool = False):
        running = await asyncio.to_thread(active_run)
        if running is not None: return {"ok": True, "status": "deferred", "reason": "adaptive_learning_active", "adaptive_run": running}
        return await asyncio.to_thread(automatic_pv_retraining_once, force=force)

    @app.get("/economics/status", tags=["economics"])
    async def economics_status():
        return {"ok": True, "runtime_build": runtime_build, "default_replay_mode": CURRENT_ECONOMICS, "historical_replay_mode": HISTORICAL_ECONOMICS, "current": economics_payload(core.cfg), "signature": economics_signature(core.cfg), "registered_version": economics_version, "runtime_patches": economics_patch_status, "compatibility_patches": economics_compat_status, "neural_teacher_patches": economics_neural_status}

    @app.get("/economics/versions", tags=["economics"])
    async def economics_version_history(limit: int = Query(50, ge=1, le=500)): return {"versions": economics_versions(limit), "default_training_mode": CURRENT_ECONOMICS}

    @app.get("/economics/price", tags=["economics"])
    async def economics_price(spot_ore_kwh: float, mode: str = CURRENT_ECONOMICS, at: str | None = None):
        if mode not in {CURRENT_ECONOMICS, HISTORICAL_ECONOMICS}: raise HTTPException(400, "invalid mode")
        economics, source = economics_for_timestamp(core.cfg, at, mode)
        return {"mode": mode, "at": at, "economics_source": source, "economics": economics, **effective_prices(spot_ore_kwh, economics)}

    @app.get("/settings/status", tags=["settings"])
    async def settings_status_route(): return await asyncio.to_thread(settings_status)

    @app.get("/control/status", tags=["control"])
    async def control_status(): return {"production": await asyncio.to_thread(production_status), "overrides": await asyncio.to_thread(scheduled_overrides)}

    async def _refresh_after_override():
        out = {}
        try: out["load_forecast"] = await core._refresh_load_forecast()
        except Exception as exc: out["load_forecast"] = {"ok": False, "error": repr(exc)}
        try: out["optimizer_plan"] = await core._refresh_optimizer_plan()
        except Exception as exc: out["optimizer_plan"] = {"ok": False, "error": repr(exc)}
        return out

    @app.post("/control/override", tags=["control"])
    async def control_override(request: Request):
        body = await request.json(); kind = str(body.get("kind") or "").strip()
        if kind not in {"sauna", "ev_charge_now"}: raise HTTPException(400, "kind must be sauna or ev_charge_now")
        starts_at = None
        if body.get("starts_at"):
            raw = str(body["starts_at"])
            try:
                d = datetime.fromisoformat(raw.replace("Z", "+00:00")); starts_at = raw if d.tzinfo is None else d.astimezone(timezone.utc).isoformat()
            except Exception: raise HTTPException(400, "starts_at must be an ISO datetime")
        duration = _sauna_default_duration() if kind == "sauna" else 120
        override = await asyncio.to_thread(create_override, kind, starts_at=starts_at, duration_minutes=duration, payload={"source": "overview_quick_control"})
        return {"ok": True, "override": override, "operating_mode": production_status()["operating_mode"], "refresh": await _refresh_after_override()}

    @app.post("/control/override/{override_id}/cancel", tags=["control"])
    async def control_override_cancel(override_id: int):
        try: item = await asyncio.to_thread(cancel_override, override_id)
        except KeyError: raise HTTPException(404, "override not found")
        return {"ok": True, "override": item, "refresh": await _refresh_after_override()}

    async def _current_candidate():
        plan = await asyncio.to_thread(latest_plan, 500)
        if str(plan.get("planner") or "") == "deterministic_battery_dp_v3_6_live": return candidate_from_live_plan(plan)
        return candidate_from_selection(await asyncio.to_thread(selector.latest_control_selection))

    @app.post("/control/mode/{mode}", tags=["control"])
    async def control_mode(mode: str):
        mode = str(mode).strip().lower()
        if mode not in {"shadow", "active", "paused"}: raise HTTPException(400, f"unsupported mode {mode!r}")
        current = production_status()
        if mode == "active":
            # This is the v1.0.91 routing fix expressed directly: preflight always
            # targets the actual consolidated actuator instance.
            preflight = await actuator.preflight()
            if not preflight.get("ok"): raise HTTPException(409, {"error": "actuator_preflight_required_before_active", "preflight": preflight})
            if release_status().get("release_pending"): raise HTTPException(409, "ACTIVE blocked by pending safe release")
            if not current.get("actuator_ready"): raise HTTPException(409, "ACTIVE requires successful /actuator/arm?confirm=true")
            candidate = await _current_candidate()
            if candidate is None: raise HTTPException(409, "ACTIVE requires a current control candidate")
            await asyncio.to_thread(set_mode, "active", reason="api_actuator_transition")
            result = await actuator.process_candidate(candidate)
            if result.get("status") not in {"acknowledged", "held_existing"}:
                await actuator.fail_safe("active_transition_unacknowledged", {"actuation": result})
                raise HTTPException(409, f"ACTIVE transition unacknowledged: {result}")
            return {**production_status(), "actuator_transition": result, "requested_mode": "active"}
        release = None
        if current.get("operating_mode") == "active" or current.get("physical_writes_enabled"):
            release = await adapter.safe_release()
            if not release.get("released"): raise HTTPException(503, f"Could not safely release Solinteg: {release}")
        return {**(await asyncio.to_thread(set_mode, mode, reason="api_actuator_transition")), "actuator_transition": {"safe_release": release}, "requested_mode": mode}

    @app.get("/optimizer/replanning/status", tags=["optimizer"])
    async def replanning_status_route():
        plan = await asyncio.to_thread(latest_plan, 500)
        snap = await asyncio.to_thread(__import__('energy_ai.app.soc_replanning', fromlist=['replanning_snapshot']).replanning_snapshot, plan, core.cfg) if plan.get("rows") else {"status": "no_plan", "should_replan": False}
        return {"runtime_build": runtime_build, "normal_shared_vintage_planner": "deterministic_battery_dp_v3_5", "intra_quarter_live_planner": "deterministic_battery_dp_v3_6_live", "monitor": dict(replan_state), "current": snap}

    @app.post("/optimizer/replanning/run", tags=["optimizer"])
    async def replanning_run(force: bool = False): return await soc_replanning_check(force=force)

    @app.get("/actuator/status", tags=["actuator"])
    async def actuator_status():
        data = await actuator.status(); data["safe_release"] = await asyncio.to_thread(release_status); return data

    @app.get("/actuator/discover", tags=["actuator"])
    async def actuator_discover(): return await adapter.discovery_report()

    @app.post("/actuator/preflight", tags=["actuator"])
    async def actuator_preflight(): return await actuator.preflight()

    @app.post("/actuator/arm", tags=["actuator"])
    async def actuator_arm(confirm: bool = False):
        if not confirm: raise HTTPException(400, "confirm=true is required")
        return await actuator.zero_handshake_and_arm()

    @app.post("/actuator/disarm", tags=["actuator"])
    async def actuator_disarm(): return await actuator.disarm("api")

    @app.post("/actuator/run", tags=["actuator"])
    async def actuator_run():
        candidate = await _current_candidate()
        if candidate is None: raise HTTPException(409, "No current control candidate is available")
        return await actuator.process_candidate(candidate)

    @app.get("/optimizer/contract/status", tags=["optimizer"])
    async def optimizer_contract_status(): return {"runtime_build": runtime_build, **contract_status()}

    @app.get("/actuator/physical-cap/status", tags=["actuator"])
    async def physical_cap_status():
        report = effective_actuator_config_report(core.cfg); rt = report.get("runtime") or {}
        return {"runtime_build": runtime_build, "max_physical_command_kw": float(rt.get("max_physical_command_kw", 2.0)), "applies_to": ["charge", "discharge"], "position_in_control_chain": "after_deterministic_safety_before_solinteg_dispatch", "affects_optimizer_or_selector": False, "restart_required": bool(report.get("restart_required")), "runtime_matches_persisted": bool(report.get("runtime_matches_persisted"))}

    app.openapi_schema = None
