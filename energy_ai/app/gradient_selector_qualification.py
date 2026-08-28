from __future__ import annotations

from typing import Any

from . import model_selector as ms
from . import model_selector_robust as robust
from .gradient_qualification import GRADIENT_ENGINE_ID, rotate_qualification_candidate

_INSTALLED = False
_ORIGINAL_DISQUALIFY = None
_ORIGINAL_RUN_SELECTION_POLICY = None


def _gradient_assessment(assessments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in assessments:
        if str(item.get("challenger_engine_id")) == GRADIENT_ENGINE_ID:
            return item
    return None


def _maybe_rotate_failed_gradient_candidate(
    cfg: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    state = robust._ensure_robust_state(cfg)
    if str(state.get("selected_engine_id")) == GRADIENT_ENGINE_ID:
        return {
            "rotated": False,
            "status": "selected_gradient_incumbent_is_frozen",
            "selected_engine_id": GRADIENT_ENGINE_ID,
        }
    assessment = _gradient_assessment(list(result.get("challengers") or []))
    if not assessment:
        return None
    if bool(assessment.get("eligible")):
        return None
    if int(assessment.get("paired_days") or 0) < int(robust.QUALIFICATION_DAYS):
        return None

    rotation = rotate_qualification_candidate(
        "completed_robust10_without_promotion",
        {
            "context_signature": state.get("context_signature"),
            "assessment": {
                "model_key": assessment.get("challenger_model_key"),
                "paired_days": assessment.get("paired_days"),
                "eligible": assessment.get("eligible"),
                "gates": assessment.get("gates"),
                "reason": assessment.get("reason"),
            },
        },
    )
    if rotation.get("rotated"):
        ms._event(
            "gradient_qualification_candidate_rotated",
            str(state.get("context_signature")),
            "Gradient frozen candidate completed robust10 without promotion; snapshot latest trained gradient model.",
            from_engine_id=GRADIENT_ENGINE_ID,
            to_engine_id=GRADIENT_ENGINE_ID,
            payload=rotation,
        )
    return rotation


def install_gradient_selector_qualification() -> None:
    global _INSTALLED, _ORIGINAL_DISQUALIFY, _ORIGINAL_RUN_SELECTION_POLICY
    if _INSTALLED:
        return

    _ORIGINAL_DISQUALIFY = robust._disqualify_model
    _ORIGINAL_RUN_SELECTION_POLICY = robust.run_selection_policy

    def disqualify_with_gradient_rotation(
        cfg: dict[str, Any],
        engine_id: str,
        model_key: str,
        reason: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        if _ORIGINAL_DISQUALIFY is None:
            raise RuntimeError("gradient selector qualification not initialized")
        result = _ORIGINAL_DISQUALIFY(cfg, engine_id, model_key, reason, details)
        if str(engine_id) == GRADIENT_ENGINE_ID:
            result["gradient_qualification_candidate_rotation"] = rotate_qualification_candidate(
                "selected_gradient_candidate_disqualified",
                {
                    "engine_id": str(engine_id),
                    "model_key": str(model_key),
                    "disqualification_reason": str(reason),
                },
            )
        return result

    def run_selection_policy_with_gradient(cfg: dict[str, Any]) -> dict[str, Any]:
        if _ORIGINAL_RUN_SELECTION_POLICY is None:
            raise RuntimeError("gradient selector qualification not initialized")
        result = _ORIGINAL_RUN_SELECTION_POLICY(cfg)
        if not isinstance(result, dict):
            return result
        # If another challenger was promoted, no failed-candidate rotation is
        # needed in this cycle. Otherwise independently rotate gradient_v1 after
        # its own completed failed robust10 window without coupling it to the
        # neural/hybrid qualification generation.
        if str(result.get("action")) != "promote":
            rotation = _maybe_rotate_failed_gradient_candidate(cfg, result)
            if rotation is not None:
                result = {**result, "gradient_qualification_candidate_rotation": rotation}
        return result

    robust._disqualify_model = disqualify_with_gradient_rotation
    robust.run_selection_policy = run_selection_policy_with_gradient
    ms.run_selection_policy = run_selection_policy_with_gradient
    _INSTALLED = True
