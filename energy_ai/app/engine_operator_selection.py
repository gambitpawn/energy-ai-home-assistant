from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from . import model_selector as selector
from . import model_selector_robust as robust
from .db import DB_PATH
from .engine_registry import registry_status

AUTO_SELECTION = "auto"
DISPLAY_NAMES = {
    "deterministic_v35": "Deterministic v3.5",
    "adaptive_deterministic_v1": "Adaptive deterministic",
    "deterministic_refined_v1": "Refined deterministic",
    "stochastic_deterministic_v1": "Stochastic deterministic",
}

_INSTALLED = False
_ORIGINAL_ROUTE = None
_INSTALL_LOCK = threading.Lock()


def display_name(engine_id: str | None) -> str:
    if not engine_id:
        return "—"
    return DISPLAY_NAMES.get(str(engine_id), str(engine_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_table() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS engine_operator_selection(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                selection TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        c.execute(
            """INSERT OR IGNORE INTO engine_operator_selection(singleton,selection,updated_at)
               VALUES (1,?,?)""",
            (AUTO_SELECTION, _now()),
        )


def registered_engine_ids() -> list[str]:
    rows = registry_status().get("engines") or []
    return [
        str(row.get("engine_id"))
        for row in rows
        if row.get("engine_id")
    ]


def operator_preference() -> dict[str, Any]:
    _init_table()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        row = c.execute(
            "SELECT selection,updated_at FROM engine_operator_selection WHERE singleton=1"
        ).fetchone()
    selection = str(row[0]) if row else AUTO_SELECTION
    updated_at = str(row[1]) if row else _now()
    valid = selection == AUTO_SELECTION or selection in registered_engine_ids()
    if not valid:
        selection = AUTO_SELECTION
    return {
        "selection": selection,
        "mode": "auto" if selection == AUTO_SELECTION else "manual",
        "manual_engine_id": None if selection == AUTO_SELECTION else selection,
        "updated_at": updated_at,
    }


def set_operator_preference(selection_value: str) -> dict[str, Any]:
    selection_value = str(selection_value or "").strip()
    allowed = set(registered_engine_ids())
    if selection_value != AUTO_SELECTION and selection_value not in allowed:
        raise ValueError(
            "selection must be 'auto' or a registered engine id: "
            + ", ".join(sorted(allowed))
        )
    _init_table()
    updated_at = _now()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """INSERT INTO engine_operator_selection(singleton,selection,updated_at)
               VALUES (1,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                 selection=excluded.selection,
                 updated_at=excluded.updated_at""",
            (selection_value, updated_at),
        )
    return operator_preference()


def _latest_routing() -> dict[str, Any] | None:
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            row = c.execute(
                """SELECT decision_start,configured_selected_engine_id,routed_engine_id,
                          fallback_used,reason,payload_json
                   FROM engine_control_selection
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[5] or "{}")
    except Exception:
        payload = {}
    return {
        "decision_start": str(row[0]),
        "configured_selected_engine_id": str(row[1]),
        "routed_engine_id": row[2],
        "fallback_used": bool(row[3]),
        "reason": str(row[4]),
        "selection_mode": payload.get("selection_mode", "auto"),
    }


def control_status(cfg: dict[str, Any]) -> dict[str, Any]:
    pref = operator_preference()
    auto_state = robust._ensure_robust_state(cfg)
    choices = [
        {
            "value": AUTO_SELECTION,
            "engine_id": None,
            "label": "Auto",
            "available": True,
        }
    ]
    registry = registry_status()
    for item in registry.get("engines") or []:
        engine_id = str(item.get("engine_id") or "")
        if not engine_id:
            continue
        choices.append(
            {
                "value": engine_id,
                "engine_id": engine_id,
                "label": display_name(engine_id),
                "available": bool(item.get("available")),
                "family": item.get("family"),
            }
        )
    requested_engine = (
        str(auto_state["selected_engine_id"])
        if pref["mode"] == "auto"
        else str(pref["manual_engine_id"])
    )
    return {
        **pref,
        "default_selection": AUTO_SELECTION,
        "auto_selected_engine_id": str(auto_state["selected_engine_id"]),
        "auto_selected_engine_label": display_name(auto_state["selected_engine_id"]),
        "auto_selected_model_key": str(auto_state["selected_model_key"]),
        "requested_engine_id": requested_engine,
        "requested_engine_label": display_name(requested_engine),
        "choices": choices,
        "latest_routing": _latest_routing(),
        "auto_race_continues_during_manual_selection": True,
    }


def _mean_comparison(
    pairs: list[tuple[str, dict[str, Any], dict[str, Any]]]
) -> dict[str, Any]:
    if not pairs:
        return {
            "paired_days": 0,
            "win_days": 0,
            "challenger_mean_regret_ore": None,
            "incumbent_mean_regret_ore": None,
            "absolute_improvement_ore_per_decision": None,
            "relative_improvement_fraction": None,
        }
    cvals = [float(c["mean_regret_ore"]) for _, c, _ in pairs]
    ivals = [float(i["mean_regret_ore"]) for _, _, i in pairs]
    cmean, imean = mean(cvals), mean(ivals)
    absolute = imean - cmean
    return {
        "paired_days": len(pairs),
        "win_days": sum(1 for c, i in zip(cvals, ivals) if c < i),
        "challenger_mean_regret_ore": cmean,
        "incumbent_mean_regret_ore": imean,
        "absolute_improvement_ore_per_decision": absolute,
        "relative_improvement_fraction": absolute / max(0.1, imean),
    }


def race_ranking(cfg: dict[str, Any]) -> dict[str, Any]:
    auto_state = robust._ensure_robust_state(cfg)
    context = str(auto_state["context_signature"])
    incumbent = str(auto_state["selected_engine_id"])
    incumbent_key = str(auto_state["selected_model_key"])
    engine_ids = registered_engine_ids()
    ranking: list[dict[str, Any]] = []

    for engine_id in engine_ids:
        if engine_id == incumbent:
            ranking.append(
                {
                    "engine_id": engine_id,
                    "label": display_name(engine_id),
                    "model_key": incumbent_key,
                    "is_auto_incumbent": True,
                    "paired_days": robust.QUALIFICATION_DAYS,
                    "required_days": robust.QUALIFICATION_DAYS,
                    "win_days": None,
                    "required_win_days": robust.QUALIFICATION_WIN_DAYS,
                    "relative_improvement_fraction": 0.0,
                    "absolute_improvement_ore_per_decision": 0.0,
                    "eligible": True,
                    "qualification_state": "incumbent",
                    "reason": "Current Auto selector incumbent.",
                }
            )
            continue

        model_key = robust._current_model_key(engine_id)
        if not model_key:
            ranking.append(
                {
                    "engine_id": engine_id,
                    "label": display_name(engine_id),
                    "model_key": None,
                    "is_auto_incumbent": False,
                    "paired_days": 0,
                    "required_days": robust.QUALIFICATION_DAYS,
                    "win_days": 0,
                    "required_win_days": robust.QUALIFICATION_WIN_DAYS,
                    "relative_improvement_fraction": None,
                    "absolute_improvement_ore_per_decision": None,
                    "eligible": False,
                    "qualification_state": "waiting_model",
                    "reason": "No current comparable model revision yet.",
                }
            )
            continue

        dq = robust._disqualification_status(context, engine_id, model_key)
        not_before = dq.get("qualification_not_before")
        pairs = robust._paired_model_scores(
            context,
            engine_id,
            model_key,
            incumbent,
            incumbent_key,
            limit=robust.QUALIFICATION_DAYS,
            not_before=not_before,
        )
        comparison = _mean_comparison(pairs)
        gate: dict[str, Any] = {}
        if engine_id != selector.BASELINE_ENGINE_ID:
            gate = robust._robust_promotion_gate(
                context,
                engine_id,
                model_key,
                incumbent,
                incumbent_key,
            )

        paired_days = int(comparison["paired_days"])
        if dq.get("quarantine_active"):
            state = "quarantined"
            reason = "Model revision is quarantined after a live health failure."
        elif engine_id == selector.BASELINE_ENGINE_ID:
            state = "fallback_reference"
            reason = "Permanent deterministic fallback/reference against the current incumbent."
        elif bool(gate.get("eligible")):
            state = "qualified"
            reason = "Passed all robust10 promotion gates."
        elif paired_days < robust.QUALIFICATION_DAYS:
            state = "evaluating" if model_key else "waiting_model"
            reason = f"Collecting complete scored days ({paired_days}/{robust.QUALIFICATION_DAYS})."
        else:
            state = "not_qualified"
            reason = str(gate.get("reason") or "Completed robust10 but did not pass all promotion gates.")

        ranking.append(
            {
                "engine_id": engine_id,
                "label": display_name(engine_id),
                "model_key": model_key,
                "is_auto_incumbent": False,
                **comparison,
                "required_days": robust.QUALIFICATION_DAYS,
                "required_win_days": robust.QUALIFICATION_WIN_DAYS,
                "eligible": bool(gate.get("eligible")),
                "qualification_state": state,
                "reason": reason,
                "gates": gate.get("gates"),
                "quarantine_active": bool(dq.get("quarantine_active")),
            }
        )

    def sort_key(item: dict[str, Any]) -> tuple[int, float, int]:
        relative = item.get("relative_improvement_fraction")
        has_score = relative is not None
        return (
            1 if has_score else 0,
            float(relative) if has_score else float("-inf"),
            1 if item.get("is_auto_incumbent") else 0,
        )

    ranking.sort(key=sort_key, reverse=True)
    for index, item in enumerate(ranking, start=1):
        item["rank"] = index

    return {
        "policy": robust.POLICY_VERSION,
        "ranking_semantics": "Current mean oracle-regret improvement versus the Auto incumbent; higher improvement ranks better. Qualification gates still determine promotion.",
        "auto_incumbent_engine_id": incumbent,
        "auto_incumbent_label": display_name(incumbent),
        "auto_incumbent_model_key": incumbent_key,
        "required_days": robust.QUALIFICATION_DAYS,
        "required_win_days": robust.QUALIFICATION_WIN_DAYS,
        "rows": ranking,
    }


def _load_decisions(information_vintage_id: str) -> dict[str, dict[str, Any]]:
    with sqlite3.connect(selector.DB_PATH, timeout=20) as c:
        rows = c.execute(
            """SELECT engine_id,decision_id,status,requested_action_kw,expected_soc_pct,payload_json
               FROM engine_decision WHERE information_vintage_id=?""",
            (str(information_vintage_id),),
        ).fetchall()
    decisions: dict[str, dict[str, Any]] = {}
    for engine_id, decision_id, status, action, expected_soc, raw in rows:
        if str(status) != "ok":
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        decisions[str(engine_id)] = {
            "engine_id": str(engine_id),
            "decision_id": str(decision_id),
            "requested_action_kw": float(action),
            "expected_soc_pct": None if expected_soc is None else float(expected_soc),
            "payload": payload,
        }
    return decisions


def _manual_route(
    cfg: dict[str, Any],
    information_vintage_id: str,
    decision_start: str,
    manual_engine: str,
) -> dict[str, Any]:
    auto_state = robust._ensure_robust_state(cfg)
    context = str(auto_state["context_signature"])
    decisions = _load_decisions(information_vintage_id)
    baseline = decisions.get(selector.BASELINE_ENGINE_ID)
    chosen = None
    fallback_used = False
    fault_type = None
    health = None
    disqualification = None
    model_key: str | None = None

    if manual_engine == selector.BASELINE_ENGINE_ID:
        chosen = baseline
        model_key = robust.BASELINE_MODEL_KEY
        reason = "manual_deterministic_v35_selected"
    else:
        selected = decisions.get(manual_engine)
        if selected is None:
            fallback_used = True
            model_key = robust._current_model_key(manual_engine)
            reason = "manual_engine_missing_fallback_to_deterministic_v35"
            fault_type = "missing_decision"
            if model_key:
                robust._health_event(
                    context,
                    decision_start,
                    manual_engine,
                    model_key,
                    "fault",
                    fault_type,
                    True,
                    {"selection_mode": "manual"},
                )
                breaker = robust._circuit_breaker_reason(
                    context, manual_engine, model_key, fault_type
                )
                if breaker:
                    disqualification = robust._disqualify_model(
                        cfg,
                        manual_engine,
                        model_key,
                        breaker["reason"],
                        {**breaker, "selection_mode": "manual"},
                    )
            chosen = baseline
        else:
            model_key = robust._engine_model_key(
                manual_engine, selected["payload"], decision_start
            )
            dq = robust._disqualification_status(context, manual_engine, model_key)
            if dq.get("quarantine_active"):
                fallback_used = True
                fault_type = "quarantined_model_revision"
                reason = "manual_model_quarantined_fallback_to_deterministic_v35"
                disqualification = dq
                chosen = baseline
            else:
                health = robust._decision_health(
                    cfg,
                    information_vintage_id,
                    selected["requested_action_kw"],
                    selected["expected_soc_pct"],
                )
                if not health["ok"]:
                    fallback_used = True
                    fault_type = str(health.get("fault_type") or "model_health_fault")
                    reason = "manual_model_failed_live_health_check_fallback_to_deterministic_v35"
                    robust._health_event(
                        context,
                        decision_start,
                        manual_engine,
                        model_key,
                        "fault",
                        fault_type,
                        True,
                        {**health, "selection_mode": "manual"},
                    )
                    breaker = robust._circuit_breaker_reason(
                        context, manual_engine, model_key, fault_type
                    )
                    if breaker:
                        disqualification = robust._disqualify_model(
                            cfg,
                            manual_engine,
                            model_key,
                            breaker["reason"],
                            {
                                **breaker,
                                "current_health": health,
                                "selection_mode": "manual",
                            },
                        )
                    chosen = baseline
                else:
                    robust._health_event(
                        context,
                        decision_start,
                        manual_engine,
                        model_key,
                        "healthy",
                        None,
                        False,
                        {**health, "selection_mode": "manual"},
                    )
                    chosen = selected
                    reason = "manual_engine_available"

    if chosen is None:
        reason = "no_baseline_decision_available"

    result = {
        "information_vintage_id": str(information_vintage_id),
        "decision_start": str(decision_start),
        "selection_mode": "manual",
        "manual_selected_engine_id": manual_engine,
        "auto_selected_engine_id": str(auto_state["selected_engine_id"]),
        "auto_selected_model_key": str(auto_state["selected_model_key"]),
        "configured_selected_engine_id": manual_engine,
        "configured_selected_model_key": model_key,
        "routed_engine_id": None if chosen is None else chosen["engine_id"],
        "routed_model_key": (
            None
            if chosen is None
            else robust._engine_model_key(
                chosen["engine_id"], chosen["payload"], decision_start
            )
        ),
        "decision_id": None if chosen is None else chosen["decision_id"],
        "requested_action_kw": None if chosen is None else chosen["requested_action_kw"],
        "fallback_used": bool(fallback_used),
        "fault_type": fault_type,
        "health": health,
        "disqualification": disqualification,
        "reason": reason,
        "requires_downstream_deterministic_safety": True,
        "physical_writes_enabled": False,
        "auto_race_state_unchanged": True,
    }
    robust._persist_control_selection(result)
    return result


def install_operator_engine_routing() -> None:
    """Install a routing-only operator override in front of the Auto selector.

    Auto remains the default and delegates unchanged to robust10. Manual mode
    chooses an engine id for routing only; the selector's incumbent, race scores,
    qualification state, promotion and rollback policy continue in the background.
    """
    global _INSTALLED, _ORIGINAL_ROUTE
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        _ORIGINAL_ROUTE = selector.route_selected_decision

        def route_with_operator(
            cfg: dict[str, Any], information_vintage_id: str, decision_start: str
        ) -> dict[str, Any]:
            pref = operator_preference()
            if pref["mode"] == "auto":
                if _ORIGINAL_ROUTE is None:
                    raise RuntimeError("operator engine routing is not initialized")
                return _ORIGINAL_ROUTE(cfg, information_vintage_id, decision_start)
            return _manual_route(
                cfg,
                information_vintage_id,
                decision_start,
                str(pref["manual_engine_id"]),
            )

        selector.route_selected_decision = route_with_operator
        _INSTALLED = True
