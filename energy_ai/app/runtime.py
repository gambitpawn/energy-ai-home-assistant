from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import flexible_loads as flexible_loads_module
from . import load_forecast as load_forecast_module
from . import main as core
from . import model_selector as selector
from . import neural_auto
from . import neural_features as neural_features_module
from . import neural_training as neural_training_module
from . import neural_training_v2
from .actuator_config import install_actuator_config
from .actuator_diagnostics_v188 import install_actuator_diagnostics_patch
from .actuator_physical_cap_v190 import install_physical_command_cap_patch
from .actuator_release_state import TrackedSolintegCommandAdapter, mark_release_pending, release_status
from .actuator_timing_v194 import install_decision_start_scheduler
from .adaptive_deterministic import AdaptiveDeterministicV1
from .adaptive_learning import current_parameters, mark_orphaned_running_runs
from .db import DB_PATH
from .deterministic_actuator import DeterministicActuator
from .engine_input_v2 import input_from_optimizer_plan_v2
from .engine_registry import baseline_decision_from_plan
from .engine_store import insert_engine_run
from .live_state import LiveStateCache
from .model_selector_policy import install_selector_policy_patch
from .model_selector_robust import install_robust_selector_patch
from .model_selector_robust_hardening import install_robust_selector_hardening
from .model_selector_state import install_selector_state_patch
from .neural_engine import NeuralV1Engine, neural_runtime_status
from .neural_features import FEATURE_SCHEMA
from .neural_teacher_v2 import LABEL_SOURCE_V2, perfect_information_teacher_v2
from .optimizer_contract_v189 import install_optimizer_interval_contract_patch
from .optimizer_store import insert_plan, latest_plan
from .optimizer_v36_live import build_live_plan
from .price_economics import install_current_economics, register_current_economics
from .price_economics_compat import install_compatibility_patches
from .price_economics_neural_compat import install_neural_teacher_economics
from .price_economics_runtime import install_economics_patches
from .production_state import mark_actuator_ready, set_mode, status as production_status
from .replanning_config import install_replanning_config
from .runtime_maintenance import combined_maintenance_loop
from .runtime_ui import install_runtime_ui
from .soc_replanning import replanning_snapshot
from .tariff_entry import app
from .tariff_scenarios import LOCAL_TZ as TARIFF_LOCAL_TZ, _calendar_active
from .user_override_forecast import build_override_aware_forecast

RUNTIME_BUILD = "1.0.94"
OPTIONS_PATH = Path("/data/options.json")

CURRENT_ECONOMICS_CONFIG = install_current_economics(core.cfg)
ECONOMICS_VERSION = register_current_economics(core.cfg)
ECONOMICS_PATCH_STATUS = install_economics_patches(core.cfg)
ECONOMICS_COMPAT_STATUS = install_compatibility_patches(core.cfg)
ECONOMICS_NEURAL_STATUS = install_neural_teacher_economics(core.cfg)
REPLANNING_CONFIG = install_replanning_config(core.cfg)
ACTUATOR_CONFIG = install_actuator_config(core.cfg)
ORPHANED_ADAPTIVE_RUNS_ON_STARTUP = mark_orphaned_running_runs()

install_selector_state_patch()
install_selector_policy_patch()
install_robust_selector_patch()
install_robust_selector_hardening()
OPTIMIZER_INTERVAL_CONTRACT = install_optimizer_interval_contract_patch()

neural_training_v2.install_into_v1_module()
neural_training_module._perfect_information_teacher = perfect_information_teacher_v2
neural_training_module.LABEL_SOURCE = LABEL_SOURCE_V2
neural_auto.build_training_samples = neural_training_v2.build_training_samples
neural_auto.sample_count = neural_training_v2.sample_count
neural_auto.train_model = neural_training_module.train_model
neural_auto.model_status = neural_training_module.model_status

try:
    if neural_training_module.MODEL_META_PATH.exists():
        meta = json.loads(neural_training_module.MODEL_META_PATH.read_text(encoding="utf-8"))
        if meta.get("feature_schema") != FEATURE_SCHEMA:
            if neural_training_module.MODEL_PATH.exists():
                neural_training_module.MODEL_PATH.unlink()
            neural_training_module.MODEL_META_PATH.unlink()
except Exception:
    pass


def _latest_completed_actual_start():
    now = datetime.now(timezone.utc)
    current_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    latest_allowed = current_start - timedelta(minutes=15)
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute(
                "SELECT MAX(bucket_start) FROM state_15m WHERE bucket_start<=?",
                (latest_allowed.isoformat(),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        return neural_training_module._utc(str(row[0]))
    except Exception:
        return None


neural_training_module._latest_actual_start = _latest_completed_actual_start


def _tariff_active_fraction_local(chunk, tariff, enabled):
    if not enabled or not chunk:
        return 0.0
    active = 0
    for row in chunk:
        try:
            local = datetime.fromisoformat(str(row["start"]).replace("Z", "+00:00")).astimezone(TARIFF_LOCAL_TZ)
            if _calendar_active(local, tariff, False):
                active += 1
        except Exception:
            continue
    return active / float(len(chunk))


neural_features_module._tariff_active_fraction = _tariff_active_fraction_local
load_forecast_module.flexible_load_forecast = build_override_aware_forecast(
    flexible_loads_module.flexible_load_forecast
)

_PREVIOUS_PRODUCTION = production_status()
_NEEDS_STARTUP_RELEASE = bool(
    _PREVIOUS_PRODUCTION.get("operating_mode") == "active"
    or _PREVIOUS_PRODUCTION.get("physical_writes_enabled")
)
try:
    set_mode("shadow", reason="startup_disarm_before_validation")
finally:
    mark_actuator_ready(False, detail="startup_requires_new_zero_handshake")
if _NEEDS_STARTUP_RELEASE:
    mark_release_pending("startup_detected_previous_active_control")

ADAPTER = TrackedSolintegCommandAdapter(core.cfg, core.collector.ha)
ACTUATOR = DeterministicActuator(core.cfg, ADAPTER)
install_actuator_diagnostics_patch()
install_physical_command_cap_patch()
ACTUATOR_TIMING = install_decision_start_scheduler(ACTUATOR)

_BASE_OPTIMIZER_REFRESH = core._refresh_optimizer_plan
_BASE_MAINTENANCE_LOOP = core._forecast_maintenance_loop
_OPTIMIZER_REFRESH_LOCK = asyncio.Lock()


def _candidate_from_selection(selection: dict[str, Any] | None) -> dict[str, Any] | None:
    if not selection or selection.get("requested_action_kw") is None or not selection.get("decision_start"):
        return None
    start = selector._dt(str(selection["decision_start"]))
    return {
        "source": "selector_quarter_control",
        "source_id": selection.get("information_vintage_id"),
        "engine_id": selection.get("routed_engine_id"),
        "decision_start": start.isoformat(),
        "valid_until": (start + timedelta(minutes=15)).isoformat(),
        "requested_action_kw": float(selection["requested_action_kw"]),
        "selector_fallback_used": bool(selection.get("fallback_used")),
        "selector_reason": selection.get("reason"),
    }


def _candidate_from_live_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    rows = plan.get("rows") or []
    if not rows:
        return None
    row = rows[0]
    start = str(row.get("start") or "")
    if not start or row.get("battery_action_kw") is None:
        return None
    valid_until = row.get("end") or (
        selector._dt(start) + timedelta(minutes=float(row.get("duration_minutes") or 15.0))
    ).isoformat()
    return {
        "source": "live_soc_replan_safety_override",
        "source_id": plan.get("generated_at"),
        "engine_id": "deterministic_v36_live",
        "decision_start": start,
        "valid_until": str(valid_until),
        "requested_action_kw": float(row["battery_action_kw"]),
        "replan_reason": plan.get("replan_reason"),
    }


async def _refresh_optimizer_pipeline_unlocked() -> dict[str, Any]:
    result = await _BASE_OPTIMIZER_REFRESH()
    plan = await asyncio.to_thread(latest_plan, 500)
    if plan.get("generated_at") is None or not plan.get("rows"):
        return {**result, "model_selector": {"status": "no_information_vintage"}}

    engine_input = None
    try:
        engine_input, baseline = await asyncio.to_thread(baseline_decision_from_plan, core.cfg, plan)
        await asyncio.to_thread(insert_engine_run, engine_input, [baseline])
        result["engine_contract"] = {
            "baseline_engine_id": "deterministic_v35",
            "information_vintage_id": engine_input.information_vintage_id,
            "decision_id": baseline.decision_id,
            "mirrored": True,
        }
    except Exception as exc:
        result["engine_contract"] = {"mirrored": False, "error": repr(exc)}

    if engine_input is None:
        try:
            engine_input = input_from_optimizer_plan_v2(plan, core.cfg)
        except Exception as exc:
            return {**result, "model_selector": {"status": "failed", "error": repr(exc)}}

    neural = await asyncio.to_thread(neural_runtime_status)
    if neural.get("shadow_ready"):
        try:
            decision = await asyncio.to_thread(NeuralV1Engine(core.cfg).decide, engine_input)
            await asyncio.to_thread(insert_engine_run, engine_input, [decision])
            result["neural_v1"] = {
                "shadow_decision": True,
                "information_vintage_id": engine_input.information_vintage_id,
                "decision_id": decision.decision_id,
                "requested_action_kw": decision.requested_action_kw,
                "confidence": decision.diagnostics.get("classification_confidence"),
            }
        except Exception as exc:
            result["neural_v1"] = {"shadow_decision": False, "status": "failed", "error": repr(exc)}
    else:
        result["neural_v1"] = {
            "shadow_decision": False,
            "status": "model_not_ready",
            "samples": neural.get("samples"),
        }

    try:
        params = await asyncio.to_thread(current_parameters, "candidate")
        decision = await asyncio.to_thread(AdaptiveDeterministicV1(core.cfg, params).decide, engine_input)
        await asyncio.to_thread(insert_engine_run, engine_input, [decision])
        result["adaptive_deterministic_v1"] = {
            "shadow_decision": True,
            "information_vintage_id": engine_input.information_vintage_id,
            "decision_id": decision.decision_id,
            "requested_action_kw": decision.requested_action_kw,
            "candidate_parameters": params.as_dict(),
            "physical_writes_enabled": False,
        }
    except Exception as exc:
        result["adaptive_deterministic_v1"] = {
            "shadow_decision": False,
            "status": "failed",
            "error": repr(exc),
        }

    try:
        routed = await asyncio.to_thread(
            selector.route_selected_decision,
            core.cfg,
            engine_input.information_vintage_id,
            engine_input.decision_start,
        )
        result["model_selector"] = routed
    except Exception as exc:
        routed = None
        result["model_selector"] = {
            "status": "failed",
            "error": repr(exc),
            "configured_fallback_engine_id": "deterministic_v35",
        }

    candidate = _candidate_from_selection(routed)
    try:
        actuation = (
            {"status": "no_control_candidate", "physical_write_performed": False}
            if candidate is None
            else await ACTUATOR.process_candidate(candidate)
        )
    except Exception as exc:
        if production_status().get("physical_writes_enabled"):
            actuation = await ACTUATOR.fail_safe("quarter_actuation_exception", {"error": repr(exc)})
        else:
            actuation = {"status": "failed", "error": repr(exc), "physical_write_performed": False}
    return {**result, "actuator": actuation}


async def refresh_optimizer_plan() -> dict[str, Any]:
    async with _OPTIMIZER_REFRESH_LOCK:
        return await _refresh_optimizer_pipeline_unlocked()


core._refresh_optimizer_plan = refresh_optimizer_plan

_REPLAN_STATE: dict[str, Any] = {
    "status": "starting",
    "last_checked_at": None,
    "last_triggered_at": None,
    "last_trigger_reason": None,
    "last_error": None,
    "trigger_count": 0,
}


async def run_live_replan(reason: str) -> dict[str, Any]:
    async with _OPTIMIZER_REFRESH_LOCK:
        plan = await asyncio.to_thread(build_live_plan, core.cfg, replan_reason=reason)
        inserted = await asyncio.to_thread(insert_plan, plan)
        candidate = _candidate_from_live_plan(plan)
        try:
            actuation = (
                {"status": "no_live_candidate", "physical_write_performed": False}
                if candidate is None
                else await ACTUATOR.process_candidate(candidate)
            )
        except Exception as exc:
            if production_status().get("physical_writes_enabled"):
                actuation = await ACTUATOR.fail_safe("live_replan_actuation_exception", {"error": repr(exc)})
            else:
                actuation = {"status": "failed", "error": repr(exc), "physical_write_performed": False}
    _REPLAN_STATE.update({
        "status": "replanned",
        "last_triggered_at": plan["generated_at"],
        "last_trigger_reason": reason,
        "last_error": None,
        "trigger_count": int(_REPLAN_STATE.get("trigger_count") or 0) + 1,
        "last_live_plan": {
            "generated_at": plan["generated_at"],
            "planner": plan["planner"],
            "initial_soc_pct": plan["initial_soc_pct"],
            "initial_soc_observed_at": plan.get("initial_soc_observed_at"),
            "initial_soc_age_seconds": plan.get("initial_soc_age_seconds"),
            "first_interval_minutes": (plan.get("horizon_diagnostics") or {}).get("first_interval_minutes"),
            "intervals": inserted,
        },
    })
    return {
        "ok": True,
        "status": "replanned",
        "reason": reason,
        "generated_at": plan["generated_at"],
        "planner": plan["planner"],
        "initial_soc_pct": plan["initial_soc_pct"],
        "first_interval_minutes": (plan.get("horizon_diagnostics") or {}).get("first_interval_minutes"),
        "comparison_eligible": False,
        "actuator": actuation,
    }


def _cooldown_elapsed(plan: dict[str, Any], now: datetime) -> tuple[bool, float]:
    minimum = max(0.0, float((core.cfg.get("optimizer") or {}).get("soc_replan_min_interval_seconds", 60.0)))
    generated = plan.get("generated_at")
    if not generated:
        return True, minimum
    try:
        d = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        age = max(0.0, (now - d.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return True, minimum
    return age >= minimum, max(0.0, minimum - age)


async def soc_replanning_check(*, force: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    plan = await asyncio.to_thread(latest_plan, 500)
    _REPLAN_STATE["last_checked_at"] = now.isoformat()
    if plan.get("generated_at") is None or not plan.get("rows"):
        _REPLAN_STATE["status"] = "no_plan"
        return {"status": "no_plan", "should_replan": False}
    snapshot = await asyncio.to_thread(replanning_snapshot, plan, core.cfg, now=now)
    cooldown_ok, cooldown_remaining = _cooldown_elapsed(plan, now)
    snapshot["cooldown_remaining_seconds"] = round(cooldown_remaining, 2)
    snapshot["cooldown_elapsed"] = cooldown_ok
    _REPLAN_STATE["last_snapshot"] = snapshot
    if force:
        return await run_live_replan("manual_force")
    if not snapshot.get("should_replan"):
        _REPLAN_STATE["status"] = str(snapshot.get("status") or "ok")
        return snapshot
    emergency = float((core.cfg.get("optimizer") or {}).get("soc_replan_emergency_threshold_pct", 5.0))
    deviation = float(snapshot.get("absolute_deviation_pct_points") or 0.0)
    if not cooldown_ok and deviation < emergency:
        _REPLAN_STATE["status"] = "cooldown"
        return {**snapshot, "status": "cooldown", "should_replan": True}
    reason = (
        f"soc_deviation:{snapshot.get('actual_soc_pct'):.2f}%_vs_"
        f"{snapshot.get('expected_soc_pct'):.2f}%_delta_"
        f"{snapshot.get('deviation_pct_points'):+.2f}pp"
    )
    try:
        return {**snapshot, **(await run_live_replan(reason))}
    except Exception as exc:
        _REPLAN_STATE.update({"status": "failed", "last_error": repr(exc)})
        return {**snapshot, "status": "failed", "error": repr(exc)}


async def _soc_replanning_loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            await soc_replanning_check()
        except Exception as exc:
            _REPLAN_STATE.update({"status": "failed", "last_error": repr(exc)})
        await asyncio.sleep(max(15.0, float((core.cfg.get("collector") or {}).get("poll_seconds", 60))))


async def _actuator_watchdog_loop() -> None:
    global _NEEDS_STARTUP_RELEASE
    await asyncio.sleep(10)
    while True:
        try:
            if release_status().get("release_pending"):
                release = await ADAPTER.safe_release()
                if release.get("released"):
                    _NEEDS_STARTUP_RELEASE = False
            else:
                await ACTUATOR.watchdog_tick()
        except Exception:
            pass
        await asyncio.sleep(max(10.0, float((core.cfg.get("actuator") or {}).get("watchdog_poll_seconds", 30.0))))


async def _combined_maintenance() -> None:
    await combined_maintenance_loop(
        _BASE_MAINTENANCE_LOOP,
        core.cfg,
        _soc_replanning_loop,
        _actuator_watchdog_loop,
    )


core._forecast_maintenance_loop = _combined_maintenance

live_state_cache = LiveStateCache(core.cfg, core.collector.ha)
_BASE_LIFESPAN = app.router.lifespan_context


@asynccontextmanager
async def runtime_lifespan(application):
    global _NEEDS_STARTUP_RELEASE
    if release_status().get("release_pending"):
        try:
            release = await ADAPTER.safe_release()
            if release.get("released"):
                _NEEDS_STARTUP_RELEASE = False
        except Exception:
            pass
    async with _BASE_LIFESPAN(application) as lifespan_state:
        live_state_cache.seed(core.collector.latest)
        live_task = asyncio.create_task(live_state_cache.run(), name="energy-ai-live-state")
        try:
            yield lifespan_state
        finally:
            live_state_cache.stop()
            live_task.cancel()
            try:
                await live_task
            except asyncio.CancelledError:
                pass
            await ACTUATOR_TIMING.close()
            if production_status().get("physical_writes_enabled"):
                try:
                    await ADAPTER.safe_release()
                except Exception:
                    pass
            try:
                set_mode("shadow", reason="clean_shutdown")
            finally:
                mark_actuator_ready(False, detail="clean_shutdown")


app.router.lifespan_context = runtime_lifespan
install_runtime_ui(app, core, live_state_cache)

from .runtime_routes import install_runtime_routes  # noqa: E402

install_runtime_routes(
    app=app,
    core=core,
    actuator=ACTUATOR,
    adapter=ADAPTER,
    live_state_cache=live_state_cache,
    replan_state=_REPLAN_STATE,
    soc_replanning_check=soc_replanning_check,
    candidate_from_selection=_candidate_from_selection,
    candidate_from_live_plan=_candidate_from_live_plan,
    runtime_build=RUNTIME_BUILD,
    economics_version=ECONOMICS_VERSION,
    economics_patch_status=ECONOMICS_PATCH_STATUS,
    economics_compat_status=ECONOMICS_COMPAT_STATUS,
    economics_neural_status=ECONOMICS_NEURAL_STATUS,
)

app.servers = [{"url": ".", "description": "Current Home Assistant Ingress path"}]
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
