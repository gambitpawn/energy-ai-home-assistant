from __future__ import annotations

from pathlib import Path

from app import engine_operator_selection as eos


ROOT = Path(__file__).resolve().parents[1]


def test_operator_selection_defaults_to_auto_and_persists_manual_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(eos, "DB_PATH", tmp_path / "operator.db")
    monkeypatch.setattr(
        eos,
        "registered_engine_ids",
        lambda: ["deterministic_v35", "adaptive_deterministic_v1", "neural_v1", "hybrid_v1"],
    )

    assert eos.operator_preference()["selection"] == "auto"
    saved = eos.set_operator_preference("hybrid_v1")
    assert saved["mode"] == "manual"
    assert saved["manual_engine_id"] == "hybrid_v1"
    assert eos.operator_preference()["selection"] == "hybrid_v1"

    auto = eos.set_operator_preference("auto")
    assert auto["mode"] == "auto"
    assert auto["manual_engine_id"] is None


def test_manual_routing_is_a_wrapper_and_auto_delegates_unchanged(monkeypatch):
    original = eos.selector.route_selected_decision
    monkeypatch.setattr(eos, "_INSTALLED", False)
    monkeypatch.setattr(eos, "_ORIGINAL_ROUTE", None)

    try:
        monkeypatch.setattr(
            eos.selector,
            "route_selected_decision",
            lambda cfg, vintage, start: {"path": "auto", "vintage": vintage},
        )
        monkeypatch.setattr(eos, "operator_preference", lambda: {"mode": "auto"})
        eos.install_operator_engine_routing()
        assert eos.selector.route_selected_decision({}, "v1", "t1")["path"] == "auto"

        monkeypatch.setattr(
            eos,
            "operator_preference",
            lambda: {"mode": "manual", "manual_engine_id": "neural_v1"},
        )
        monkeypatch.setattr(
            eos,
            "_manual_route",
            lambda cfg, vintage, start, engine: {
                "path": "manual",
                "engine": engine,
                "vintage": vintage,
            },
        )
        routed = eos.selector.route_selected_decision({}, "v2", "t2")
        assert routed == {"path": "manual", "engine": "neural_v1", "vintage": "v2"}
    finally:
        eos.selector.route_selected_decision = original
        eos._INSTALLED = False
        eos._ORIGINAL_ROUTE = None


def test_ranking_orders_current_performance_but_keeps_qualification_status(monkeypatch):
    monkeypatch.setattr(
        eos,
        "registered_engine_ids",
        lambda: ["deterministic_v35", "adaptive_deterministic_v1", "neural_v1"],
    )
    monkeypatch.setattr(
        eos.robust,
        "_ensure_robust_state",
        lambda cfg: {
            "context_signature": "ctx",
            "selected_engine_id": "deterministic_v35",
            "selected_model_key": "deterministic_v35:3.5",
        },
    )
    keys = {
        "adaptive_deterministic_v1": "adaptive:g1",
        "neural_v1": "neural:r1",
    }
    monkeypatch.setattr(eos.robust, "_current_model_key", lambda engine: keys.get(engine))
    monkeypatch.setattr(
        eos.robust,
        "_disqualification_status",
        lambda context, engine, key: {"quarantine_active": False, "qualification_not_before": None},
    )

    def pairs(context, challenger, challenger_key, incumbent, incumbent_key, *, limit, not_before=None):
        challenger_mean = 8.0 if challenger == "adaptive_deterministic_v1" else 12.0
        return [
            (
                f"2026-08-{day:02d}",
                {"mean_regret_ore": challenger_mean},
                {"mean_regret_ore": 10.0},
            )
            for day in range(1, 6)
        ]

    monkeypatch.setattr(eos.robust, "_paired_model_scores", pairs)
    monkeypatch.setattr(
        eos.robust,
        "_robust_promotion_gate",
        lambda *args, **kwargs: {"eligible": False, "reason": "insufficient complete qualification days"},
    )

    ranking = eos.race_ranking({})["rows"]
    assert [row["engine_id"] for row in ranking] == [
        "adaptive_deterministic_v1",
        "deterministic_v35",
        "neural_v1",
    ]
    assert ranking[0]["relative_improvement_fraction"] > 0
    assert ranking[0]["qualification_state"] == "evaluating"
    assert ranking[2]["relative_improvement_fraction"] < 0


def test_models_extension_uses_named_series_and_leaves_overview_outside_extension():
    source = (ROOT / "app" / "ui_model_control.py").read_text(encoding="utf-8")
    assert "label:modelDisplayName(id)" in source
    assert "name:id" not in source
    assert "Control engine" in source
    assert "Current model ranking" in source
    assert "Manual engine selection does not change this Auto ranking" in source
    assert "overviewPlan" not in source


def test_runtime_installs_operator_routing_before_hybrid_wrapper():
    source = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    operator_pos = source.index("install_operator_engine_routing()")
    hybrid_pos = source.index("install_hybrid_runtime_patch(base.core.cfg)")
    assert operator_pos < hybrid_pos
