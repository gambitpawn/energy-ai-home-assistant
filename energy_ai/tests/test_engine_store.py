from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.engine_contract import EngineDecision, EngineInput
from app.engine_store import competition_rows, insert_engine_run, latest_engine_decisions


class EngineStoreTests(unittest.TestCase):
    def _input(self):
        return EngineInput(
            generated_at="2026-08-27T07:59:20+00:00",
            decision_start="2026-08-27T08:00:00+00:00",
            initial_soc_pct=50.0,
            interval_minutes=15,
            horizon_rows=(
                {
                    "start": "2026-08-27T08:00:00+00:00",
                    "load_kw": 2.0,
                    "pv_kw": 1.0,
                    "load_uncertainty_kw": 0.2,
                    "pv_uncertainty_kw": 0.1,
                    "price_known": True,
                    "price_ore_kwh": 100.0,
                },
            ),
        )

    def _decision(self, engine_input, engine_id="deterministic_v35"):
        return EngineDecision(
            engine_id=engine_id,
            engine_version="3.5" if engine_id == "deterministic_v35" else "1",
            family="deterministic",
            information_vintage_id=engine_input.information_vintage_id,
            generated_at=engine_input.generated_at,
            decision_start=engine_input.decision_start,
            requested_action_kw=1.25,
            expected_soc_pct=48.0,
        )

    def test_store_groups_competitors_by_shared_vintage(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "engine.db"
            with patch("app.engine_store.DB_PATH", db):
                engine_input = self._input()
                decisions = [
                    self._decision(engine_input, "deterministic_v35"),
                    self._decision(engine_input, "deterministic_refined_v1"),
                ]
                self.assertEqual(insert_engine_run(engine_input, decisions), 2)
                latest = latest_engine_decisions(1)
                self.assertIn("deterministic_v35", latest)
                self.assertIn("deterministic_refined_v1", latest)
                rows = competition_rows(
                    "2026-08-27T08:00:00+00:00",
                    "2026-08-27T08:15:00+00:00",
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["information_vintage_id"], engine_input.information_vintage_id)
                self.assertEqual(
                    set(rows[0]["decisions"]),
                    {"deterministic_v35", "deterministic_refined_v1"},
                )

    def test_store_rejects_mismatched_information_vintage(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "engine.db"
            with patch("app.engine_store.DB_PATH", db):
                engine_input = self._input()
                bad = EngineDecision(
                    engine_id="deterministic_refined_v1",
                    engine_version="1",
                    family="deterministic",
                    information_vintage_id="different-vintage",
                    generated_at=engine_input.generated_at,
                    decision_start=engine_input.decision_start,
                    requested_action_kw=0.0,
                    expected_soc_pct=50.0,
                )
                with self.assertRaises(ValueError):
                    insert_engine_run(engine_input, [bad])


if __name__ == "__main__":
    unittest.main()
