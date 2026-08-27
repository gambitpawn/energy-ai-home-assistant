from __future__ import annotations

from typing import Any

import numpy as np

from .engine_contract import EngineDecision, EngineInput
from .engine_registry import descriptor
from .neural_features import FEATURE_SCHEMA, vectorize
from .neural_training import ENGINE_ID, load_model, model_status


class NeuralV1Engine:
    descriptor = descriptor(ENGINE_ID)

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def decide(self, engine_input: EngineInput) -> EngineDecision:
        model, meta = load_model()
        x = np.asarray([vectorize(engine_input)], dtype=float)
        predicted = float(model.predict(x)[0])
        confidence = None
        probabilities = None
        if hasattr(model, "predict_proba"):
            raw = model.predict_proba(x)[0]
            confidence = float(max(raw)) if len(raw) else None
            classes = [float(v) for v in model.classes_]
            probabilities = [
                {"action_kw": action, "probability": round(float(prob), 6)}
                for action, prob in sorted(zip(classes, raw), key=lambda p: p[1], reverse=True)[:5]
            ]

        battery = (self.cfg.get("policy") or {}).get("battery") or {}
        optimizer = self.cfg.get("optimizer") or {}
        cap = float(battery.get("capacity_kwh", 19.6))
        charge_eff = float(optimizer.get("battery_charge_efficiency", 0.95))
        discharge_eff = float(optimizer.get("battery_discharge_efficiency", 0.95))
        dt_h = float(engine_input.interval_minutes) / 60.0
        initial_energy = cap * float(engine_input.initial_soc_pct) / 100.0
        if predicted >= 0:
            expected_energy = initial_energy - predicted * dt_h / max(1e-9, discharge_eff)
        else:
            expected_energy = initial_energy + (-predicted) * charge_eff * dt_h
        expected_soc = max(0.0, min(100.0, expected_energy / cap * 100.0))

        return EngineDecision(
            engine_id=self.descriptor.engine_id,
            engine_version=self.descriptor.engine_version,
            family=self.descriptor.family,
            information_vintage_id=engine_input.information_vintage_id,
            generated_at=engine_input.generated_at,
            decision_start=engine_input.decision_start,
            requested_action_kw=predicted,
            expected_soc_pct=expected_soc,
            status="ok",
            diagnostics={
                "feature_schema": FEATURE_SCHEMA,
                "classification_confidence": None if confidence is None else round(confidence, 6),
                "top_action_probabilities": probabilities,
                "expected_soc_is_pre_safety": True,
            },
            model={
                "kind": meta.get("model_kind"),
                "model_version": meta.get("model_version"),
                "model_revision": meta.get("model_revision"),
                "model_id": meta.get("model_id"),
                "trained_at": meta.get("trained_at"),
                "training_trigger": meta.get("training_trigger"),
                "training_samples": meta.get("samples"),
                "label_source": meta.get("label_source"),
                "validation_accuracy": meta.get("validation_accuracy"),
                "validation_action_mae_kw": meta.get("validation_action_mae_kw"),
                "validation_direction_accuracy": meta.get("validation_direction_accuracy"),
                "shadow_ready": bool(meta.get("shadow_ready")),
                "active_eligible": False,
            },
        )


def neural_runtime_status() -> dict[str, Any]:
    return model_status()
