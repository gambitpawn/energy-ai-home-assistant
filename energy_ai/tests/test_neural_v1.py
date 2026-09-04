from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from app.engine_contract import EngineInput
from app.neural_engine import NeuralV1Engine
from app.neural_features import FEATURE_NAMES, vectorize
from app.neural_training import _round_action
from app.neural_training_v2 import OrderedActionRegressor


class _FakeRegressor:
    classes_ = np.asarray([-8.0, -7.0, -6.0, -5.0, -4.0, -3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])

    def predict(self, x):
        return np.asarray([3.65])

    def predict_proba(self, x):
        raw = np.exp(-0.5 * ((self.classes_ - 3.65) / 1.0) ** 2)
        return np.asarray([raw / raw.sum()])


class _ConstantEstimator:
    def predict(self, x):
        return np.asarray([2.4 for _ in range(len(x))])


class NeuralV1Tests(unittest.TestCase):
    def _input(self, max_discharge_kw: float = 8.0):
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
            constraints={"battery_max_charge_kw": 8.0, "battery_max_discharge_kw": max_discharge_kw},
        )

    def test_feature_vector_has_stable_schema(self):
        values = vectorize(self._input())
        self.assertEqual(len(values), len(FEATURE_NAMES))
        self.assertTrue(all(isinstance(v, float) for v in values))

    def test_action_label_rounding_remains_audit_only(self):
        self.assertEqual(_round_action(7.44), 7.0)
        self.assertEqual(_round_action(-7.7), -8.0)
        self.assertEqual(_round_action(0.2), 0.0)

    def test_ordered_regressor_projects_soft_probability_around_prediction(self):
        model = OrderedActionRegressor(_ConstantEstimator(), sigma_kw=1.0)
        probabilities = model.predict_proba(np.zeros((1, 2)))[0]
        top_action = float(model.classes_[int(np.argmax(probabilities))])
        self.assertEqual(top_action, 2.0)
        self.assertGreater(probabilities[list(model.classes_).index(2.0)], probabilities[list(model.classes_).index(-2.0)])
        self.assertAlmostEqual(float(np.sum(probabilities)), 1.0, places=8)

    @patch("app.neural_engine.load_model")
    def test_neural_decision_uses_continuous_regression_output(self, load_model):
        load_model.return_value = (
            _FakeRegressor(),
            {
                "model_kind": "sklearn_mlp_regressor_ordered_action",
                "model_version": "2",
                "trained_at": "2026-08-27T08:30:00+00:00",
                "samples": 100,
                "label_source": "perfect_information_v35_teacher_v1",
                "target_kind": "continuous_teacher_action_kw",
                "validation_action_mae_kw": 1.2,
                "validation_within_1kw": 0.6,
                "validation_within_2kw": 0.8,
                "validation_direction_accuracy": 0.8,
                "shadow_ready": True,
            },
        )
        decision = NeuralV1Engine({
            "policy": {"battery": {"capacity_kwh": 19.6}},
            "optimizer": {"battery_charge_efficiency": 0.95, "battery_discharge_efficiency": 0.95},
        }).decide(self._input())
        self.assertEqual(decision.engine_id, "neural_v1")
        self.assertEqual(decision.requested_action_kw, 3.65)
        self.assertEqual(decision.status, "ok")
        self.assertFalse(decision.diagnostics["regression_action_clipped"])
        self.assertIsNotNone(decision.diagnostics["soft_action_confidence"])
        self.assertFalse(decision.model["active_eligible"])
        self.assertFalse(decision.as_dict()["safety_semantics"]["physical_authority"])

    @patch("app.neural_engine.load_model")
    def test_neural_decision_clips_regression_to_physical_power_limit(self, load_model):
        model = _FakeRegressor()
        model.predict = lambda x: np.asarray([7.2])
        load_model.return_value = (model, {"model_kind": "sklearn_mlp_regressor_ordered_action", "model_version": "2", "shadow_ready": True})
        decision = NeuralV1Engine({
            "policy": {"battery": {"capacity_kwh": 19.6}},
            "optimizer": {"battery_charge_efficiency": 0.95, "battery_discharge_efficiency": 0.95},
        }).decide(self._input(max_discharge_kw=5.0))
        self.assertEqual(decision.requested_action_kw, 5.0)
        self.assertTrue(decision.diagnostics["regression_action_clipped"])
        self.assertEqual(decision.diagnostics["raw_regression_action_kw"], 7.2)


if __name__ == "__main__":
    unittest.main()
