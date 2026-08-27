from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from app.engine_contract import EngineInput
from app.neural_engine import NeuralV1Engine
from app.neural_features import FEATURE_NAMES, vectorize
from app.neural_training import _round_action


class _FakeModel:
    classes_ = np.asarray([-4.0, 0.0, 4.0])

    def predict(self, x):
        return np.asarray([4.0])

    def predict_proba(self, x):
        return np.asarray([[0.1, 0.2, 0.7]])


class NeuralV1Tests(unittest.TestCase):
    def _input(self):
        rows = []
        for i in range(16):
            hour = 8 + (i // 4)
            minute = (i % 4) * 15
            rows.append({
                "start": f"2026-08-27T{hour:02d}:{minute:02d}:00+00:00",
                "load_kw": 2.0 + i * 0.01,
                "pv_kw": 1.0,
                "load_uncertainty_kw": 0.2,
                "pv_uncertainty_kw": 0.1,
                "price_known": i < 8,
                "price_ore_kwh": 100.0 + i if i < 8 else None,
            })
        return EngineInput(
            generated_at="2026-08-27T07:59:20+00:00",
            decision_start="2026-08-27T08:00:00+00:00",
            initial_soc_pct=50.0,
            interval_minutes=15,
            horizon_rows=tuple(rows),
        )

    def test_feature_vector_has_stable_schema(self):
        values = vectorize(self._input())
        self.assertEqual(len(values), len(FEATURE_NAMES))
        self.assertTrue(all(isinstance(v, float) for v in values))

    def test_action_label_rounding(self):
        self.assertEqual(_round_action(7.44), 7.0)
        self.assertEqual(_round_action(-7.7), -8.0)
        self.assertEqual(_round_action(0.2), 0.0)

    @patch("app.neural_engine.load_model")
    def test_neural_decision_uses_common_contract(self, load_model):
        load_model.return_value = (
            _FakeModel(),
            {
                "model_kind": "sklearn_mlp_classifier",
                "model_version": "1",
                "trained_at": "2026-08-27T08:30:00+00:00",
                "samples": 100,
                "label_source": "perfect_information_v35_teacher_v1",
                "validation_accuracy": 0.6,
                "validation_action_mae_kw": 1.2,
                "validation_direction_accuracy": 0.8,
                "shadow_ready": True,
            },
        )
        decision = NeuralV1Engine({
            "policy": {"battery": {"capacity_kwh": 19.6}},
            "optimizer": {"battery_charge_efficiency": 0.95, "battery_discharge_efficiency": 0.95},
        }).decide(self._input())
        self.assertEqual(decision.engine_id, "neural_v1")
        self.assertEqual(decision.requested_action_kw, 4.0)
        self.assertEqual(decision.status, "ok")
        self.assertEqual(decision.diagnostics["classification_confidence"], 0.7)
        self.assertFalse(decision.model["active_eligible"])
        self.assertFalse(decision.as_dict()["safety_semantics"]["physical_authority"])


if __name__ == "__main__":
    unittest.main()
