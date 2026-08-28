from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from . import model_selector as selector
from .db import DB_PATH
from .engine_contract import EngineInput
from .engine_store import insert_engine_run
from .stochastic_engine import ENGINE_ID, StochasticDeterministicV1Engine

_INSTALLED = False
_ORIGINAL_ROUTE = None
_LOCK = threading.Lock()
_LAST_STATUS: dict[str, Any] = {
    "engine_id": ENGINE_ID,
    "status": "not_run",
    "shadow_decision": False,
}


def _engine_input_for_vintage(information_vintage_id: str) -> EngineInput | None:
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            row = c.execute(
                "SELECT payload_json FROM engine_information_vintage WHERE information_vintage_id=?",
                (str(information_vintage_id),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0])
        return EngineInput(
            generated_at=str(payload["generated_at"]),
            decision_start=str(payload["decision_start"]),
            initial_soc_pct=float(payload["initial_soc_pct"]),
            interval_minutes=int(payload.get("interval_minutes") or 15),
            horizon_rows=tuple(payload.get("horizon_rows") or ()),
            constraints=dict(payload.get("constraints") or {}),
            objective=dict(payload.get("objective") or {}),
            source=dict(payload.get("source") or {}),
            information_vintage_id=str(payload.get("information_vintage_id") or information_vintage_id),
        )
    except Exception:
        return None


def _prepare_stochastic_decision(cfg: dict[str, Any], information_vintage_id: str) -> dict[str, Any]:
    engine_input = _engine_input_for_vintage(information_vintage_id)
    if engine_input is None:
        return {
            "engine_id": ENGINE_ID,
            "status": "missing_information_vintage",
            "shadow_decision": False,
            "information_vintage_id": str(information_vintage_id),
        }

    try:
        decision = StochasticDeterministicV1Engine(cfg).decide(engine_input)
        insert_engine_run(engine_input, [decision])
        return {
            "engine_id": ENGINE_ID,
            "status": "ok",
            "shadow_decision": True,
            "information_vintage_id": engine_input.information_vintage_id,
            "decision_id": decision.decision_id,
            "requested_action_kw": decision.requested_action_kw,
            "expected_soc_pct": decision.expected_soc_pct,
            "model_id": decision.model.get("model_id"),
            "model_revision": decision.model.get("model_revision"),
            "collapsed_to_deterministic": decision.diagnostics.get("collapsed_to_deterministic"),
            "baseline_action_kw": decision.diagnostics.get("baseline_action_kw"),
            "expected_scenario_cost_ore": decision.diagnostics.get("expected_scenario_cost_ore"),
            "cvar_cost_ore": decision.diagnostics.get("cvar_cost_ore"),
            "risk_adjusted_score_ore": decision.diagnostics.get("risk_adjusted_score_ore"),
            "risk_premium_ore": decision.diagnostics.get("risk_premium_ore"),
            "scenario_count": decision.diagnostics.get("scenario_count"),
            "uncertainty": decision.diagnostics.get("uncertainty"),
            "compute_ms": decision.diagnostics.get("compute_ms"),
            "physical_writes_enabled": False,
        }
    except Exception as exc:
        return {
            "engine_id": ENGINE_ID,
            "status": "failed",
            "shadow_decision": False,
            "information_vintage_id": engine_input.information_vintage_id,
            "error": repr(exc),
        }


def stochastic_runtime_status() -> dict[str, Any]:
    with _LOCK:
        return dict(_LAST_STATUS)


def install_stochastic_runtime_patch(cfg: dict[str, Any]) -> None:
    """Prepare stochastic_v1 for a shared vintage before selector routing."""
    global _INSTALLED, _ORIGINAL_ROUTE, _LAST_STATUS
    if _INSTALLED:
        return
    _ORIGINAL_ROUTE = selector.route_selected_decision

    def route_with_stochastic(
        runtime_cfg: dict[str, Any], information_vintage_id: str, decision_start: str
    ) -> dict[str, Any]:
        global _LAST_STATUS
        stochastic = _prepare_stochastic_decision(runtime_cfg, information_vintage_id)
        with _LOCK:
            _LAST_STATUS = dict(stochastic)
        if _ORIGINAL_ROUTE is None:
            raise RuntimeError("stochastic runtime patch is not initialized")
        routed = _ORIGINAL_ROUTE(runtime_cfg, information_vintage_id, decision_start)
        if isinstance(routed, dict):
            routed = {**routed, "stochastic_deterministic_v1": stochastic}
        return routed

    selector.route_selected_decision = route_with_stochastic
    _INSTALLED = True
