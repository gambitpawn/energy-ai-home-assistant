from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import model_selector as ms
from . import model_selector_robust as robust
from .neural_qualification import LEARNED_ENGINE_IDS, rotate_qualification_candidate

_ORIGINAL_DISQUALIFY = robust._disqualify_model


def _disqualify_model(
    cfg: dict[str, Any],
    engine_id: str,
    model_key: str,
    reason: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(details or {})
    if str(engine_id) == robust.ADAPTIVE_ENGINE_ID:
        enriched["adaptive_state_id_at_disqualification"] = robust._latest_adaptive_state_id()
    result = _ORIGINAL_DISQUALIFY(cfg, engine_id, model_key, reason, enriched)
    if str(engine_id) in LEARNED_ENGINE_IDS:
        rotation = rotate_qualification_candidate(
            "selected_learned_candidate_disqualified",
            {
                "engine_id": str(engine_id),
                "model_key": str(model_key),
                "disqualification_reason": str(reason),
            },
        )
        result["qualification_candidate_rotation"] = rotation
    return result


def _maybe_advance_adaptive_generation(context: str | None) -> dict[str, Any]:
    current = robust._ensure_adaptive_generation()
    current_key = f"{robust.ADAPTIVE_ENGINE_ID}:generation-{current['generation']}"
    if not context:
        return current
    disqualification = robust._latest_disqualification(context, robust.ADAPTIVE_ENGINE_ID, current_key)
    if disqualification is None:
        return current

    latest_state_id = robust._latest_adaptive_state_id()
    details = disqualification.get("details") or {}
    disqualified_state_id = details.get("adaptive_state_id_at_disqualification")
    # A new generation is only justified by a candidate persisted AFTER the
    # health failure. Historical parameter updates from earlier in the same
    # generation must never make a disqualified policy look new.
    if latest_state_id is None:
        return current
    if disqualified_state_id is None or latest_state_id <= int(disqualified_state_id):
        return current

    now = ms._now()
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        active = c.execute(
            '''SELECT generation FROM engine_model_generation
               WHERE engine_id=? AND ended_at IS NULL ORDER BY generation DESC LIMIT 1''',
            (robust.ADAPTIVE_ENGINE_ID,),
        ).fetchone()
        if not active or int(active[0]) != int(current["generation"]):
            return robust._ensure_adaptive_generation()
        c.execute(
            '''UPDATE engine_model_generation SET ended_at=?
               WHERE engine_id=? AND generation=?''',
            (now, robust.ADAPTIVE_ENGINE_ID, int(current["generation"])),
        )
        new_generation = int(current["generation"]) + 1
        c.execute(
            '''INSERT INTO engine_model_generation(
               engine_id,generation,started_at,ended_at,source_state_id,reason)
               VALUES (?,?,?,?,?,?)''',
            (
                robust.ADAPTIVE_ENGINE_ID,
                new_generation,
                now,
                None,
                latest_state_id,
                "new_candidate_persisted_after_disqualification",
            ),
        )
    ms._event(
        "model_generation_advanced",
        context,
        "Adaptive candidate changed after disqualification; start a new 10-day qualification generation.",
        from_engine_id=robust.ADAPTIVE_ENGINE_ID,
        to_engine_id=robust.ADAPTIVE_ENGINE_ID,
        payload={
            "previous_model_key": current_key,
            "new_model_key": f"{robust.ADAPTIVE_ENGINE_ID}:generation-{new_generation}",
            "adaptive_state_id_at_disqualification": disqualified_state_id,
            "new_source_state_id": latest_state_id,
        },
    )
    return robust._ensure_adaptive_generation()


def _cooldown_active(base_state: dict[str, Any]) -> bool:
    raw = base_state.get("cooldown_until")
    if not raw:
        return False
    try:
        return ms._dt(str(raw)) > datetime.now(timezone.utc)
    except Exception:
        return False


def _learned_candidate_rotation_ready(assessments: list[dict[str, Any]]) -> bool:
    learned = [
        item for item in assessments
        if str(item.get("challenger_engine_id")) in LEARNED_ENGINE_IDS
    ]
    if not learned or any(bool(item.get("eligible")) for item in learned):
        return False
    return all(
        int(item.get("paired_days") or 0) >= int(robust.QUALIFICATION_DAYS)
        for item in learned
    )


def _maybe_rotate_failed_learned_candidate(
    context: str,
    selected_engine: str,
    assessments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if str(selected_engine) in LEARNED_ENGINE_IDS:
        return {
            "rotated": False,
            "status": "selected_learned_incumbent_is_frozen",
            "selected_engine_id": str(selected_engine),
        }
    if not _learned_candidate_rotation_ready(assessments):
        return None

    compact = [
        {
            "engine_id": item.get("challenger_engine_id"),
            "model_key": item.get("challenger_model_key"),
            "paired_days": item.get("paired_days"),
            "eligible": item.get("eligible"),
            "gates": item.get("gates"),
            "reason": item.get("reason"),
        }
        for item in assessments
        if str(item.get("challenger_engine_id")) in LEARNED_ENGINE_IDS
    ]
    rotation = rotate_qualification_candidate(
        "completed_robust10_without_promotion",
        {"context_signature": context, "assessments": compact},
    )
    if rotation.get("rotated"):
        ms._event(
            "learned_qualification_candidate_rotated",
            context,
            "Neural/hybrid frozen candidate completed robust10 without promotion; snapshot latest trained neural model.",
            from_engine_id="neural_v1",
            to_engine_id="neural_v1",
            payload=rotation,
        )
    return rotation


def run_selection_policy(cfg: dict[str, Any]) -> dict[str, Any]:
    robust_state = robust._ensure_robust_state(cfg)
    base_state = ms.ensure_selector_state(cfg)
    context = robust_state["context_signature"]
    selected_engine = robust_state["selected_engine_id"]
    selected_key = robust_state["selected_model_key"]

    # Rollback and quarantine checks always remain active, even during cooldown.
    if selected_engine != ms.BASELINE_ENGINE_ID:
        dq = robust._disqualification_status(context, selected_engine, selected_key)
        if dq.get("quarantine_active"):
            state = robust._set_selected_model(
                cfg,
                ms.BASELINE_ENGINE_ID,
                robust.BASELINE_MODEL_KEY,
                "selected model revision is quarantined",
                "rollback_quarantine",
                {"selected_model_key": selected_key, "disqualification": dq},
            )
            return {"action": "rollback", "reason": "quarantine", "state": state, "disqualification": dq}

        rollback = robust._robust_rollback_gate(context, selected_engine, selected_key)
        if rollback.get("eligible"):
            state = robust._set_selected_model(
                cfg,
                ms.BASELINE_ENGINE_ID,
                robust.BASELINE_MODEL_KEY,
                "selected model materially underperformed deterministic_v35",
                "rollback_performance",
                rollback,
            )
            return {"action": "rollback", "reason": "performance", "state": state, "rollback_gate": rollback}
    else:
        rollback = {"eligible": False, "reason": "baseline is selected"}

    # A promotion or rollback sets the legacy cooldown timestamp. We use it only
    # to suppress new promotions; safety and rollback are never suppressed.
    if _cooldown_active(base_state):
        return {
            "action": "hold",
            "reason": "promotion_cooldown",
            "state": {**base_state, **robust_state},
            "rollback_gate": rollback,
            "cooldown_until": base_state.get("cooldown_until"),
        }

    tariffs_enabled = bool((cfg.get("tariffs") or {}).get("enabled", False))
    if tariffs_enabled:
        return {
            "action": "hold",
            "reason": "auto promotion blocked while demand-tariff objective is enabled",
            "state": {**base_state, **robust_state},
            "rollback_gate": rollback,
        }

    assessments: list[dict[str, Any]] = []
    for engine_id in robust._candidate_engines(context, selected_engine):
        model_key = robust._current_model_key(engine_id)
        if not model_key:
            continue
        assessments.append(
            robust._robust_promotion_gate(
                context,
                engine_id,
                model_key,
                selected_engine,
                selected_key,
            )
        )

    candidate_rotation = _maybe_rotate_failed_learned_candidate(context, selected_engine, assessments)
    eligible = [item for item in assessments if item.get("eligible")]
    if not eligible:
        return {
            "action": "hold",
            "reason": "no challenger passed robust 10-day promotion gates",
            "state": {**base_state, **robust_state},
            "rollback_gate": rollback,
            "challengers": assessments,
            "qualification_candidate_rotation": candidate_rotation,
        }

    winner = max(
        eligible,
        key=lambda item: (
            float(item.get("relative_improvement_fraction") or 0.0),
            float(item.get("absolute_improvement_ore_per_decision") or 0.0),
        ),
    )
    state = robust._set_selected_model(
        cfg,
        str(winner["challenger_engine_id"]),
        str(winner["challenger_model_key"]),
        "model revision passed robust 10-day qualification",
        "promotion",
        winner,
    )
    return {
        "action": "promote",
        "winner": winner,
        "state": state,
        "challengers": assessments,
        "qualification_candidate_rotation": candidate_rotation,
        "physical_writes_enabled": False,
    }


def install_robust_selector_hardening() -> None:
    robust._disqualify_model = _disqualify_model
    robust._maybe_advance_adaptive_generation = _maybe_advance_adaptive_generation
    robust.run_selection_policy = run_selection_policy
    ms.run_selection_policy = run_selection_policy
