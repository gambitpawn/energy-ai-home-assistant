from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from .adaptive_deterministic import AdaptiveParameters, DEFAULT_PARAMETERS
from .db import DB_PATH

PARAMETER_ORDER = (
    "pv_forecast_risk",
    "load_forecast_risk",
    "terminal_energy_value_ore_kwh",
    "discharge_hurdle_ore_kwh",
    "reserve_energy_value_ore_kwh",
    "charge_hurdle_ore_kwh",
    "cycling_penalty_ore_kwh",
)

PARAMETER_GRIDS: dict[str, tuple[float, ...]] = {
    "pv_forecast_risk": (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5),
    "load_forecast_risk": (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5),
    "terminal_energy_value_ore_kwh": (50.0, 100.0, 125.0, 150.0, 175.0, 225.0, 300.0),
    "discharge_hurdle_ore_kwh": (0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 40.0),
    "reserve_energy_value_ore_kwh": (0.0, 5.0, 10.0, 20.0, 40.0, 75.0, 125.0),
    "charge_hurdle_ore_kwh": (0.0, 2.5, 5.0, 7.5, 10.0, 15.0, 20.0),
    "cycling_penalty_ore_kwh": (0.0, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0),
}

EvaluationFn = Callable[[AdaptiveParameters], float]


def init_adaptive_learning_store() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS adaptive_parameter_state(
                state_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL,
                source_run_id INTEGER,
                parameters_json TEXT NOT NULL,
                score_ore REAL,
                UNIQUE(role, created_at)
            );
            CREATE INDEX IF NOT EXISTS idx_adaptive_parameter_state_role
                ON adaptive_parameter_state(role, created_at DESC);

            CREATE TABLE IF NOT EXISTS adaptive_learning_run(
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                replay_date TEXT NOT NULL,
                baseline_parameters_json TEXT NOT NULL,
                result_parameters_json TEXT,
                baseline_score_ore REAL,
                result_score_ore REAL,
                status TEXT NOT NULL,
                diagnostics_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS adaptive_learning_trial(
                trial_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                parameter_name TEXT,
                parameter_value REAL,
                parameters_json TEXT NOT NULL,
                score_ore REAL NOT NULL,
                improvement_ore REAL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES adaptive_learning_run(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_adaptive_learning_trial_run
                ON adaptive_learning_trial(run_id, stage, parameter_name);
            '''
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _from_json(raw: str | None) -> AdaptiveParameters:
    if not raw:
        return DEFAULT_PARAMETERS
    try:
        values = json.loads(raw)
        allowed = {k: values[k] for k in PARAMETER_ORDER if k in values}
        return AdaptiveParameters(**allowed).bounded()
    except Exception:
        return DEFAULT_PARAMETERS


def current_parameters(role: str = "candidate") -> AdaptiveParameters:
    init_adaptive_learning_store()
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT parameters_json FROM adaptive_parameter_state WHERE role=? ORDER BY created_at DESC LIMIT 1",
            (role,),
        ).fetchone()
    return _from_json(row[0] if row else None)


def persist_parameters(
    params: AdaptiveParameters,
    role: str,
    *,
    score_ore: float | None = None,
    source_run_id: int | None = None,
) -> None:
    init_adaptive_learning_store()
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT INTO adaptive_parameter_state(created_at,role,source_run_id,parameters_json,score_ore) VALUES (?,?,?,?,?)",
            (_now(), role, source_run_id, json.dumps(params.bounded().as_dict(), sort_keys=True), score_ore),
        )


def _replace_parameter(params: AdaptiveParameters, name: str, value: float) -> AdaptiveParameters:
    if name not in PARAMETER_ORDER:
        raise KeyError(name)
    return replace(params, **{name: float(value)}).bounded()


def single_parameter_sweeps(
    params: AdaptiveParameters,
    evaluate: EvaluationFn,
) -> tuple[AdaptiveParameters, list[dict[str, Any]]]:
    """Isolate every learnable dimension while holding all other dimensions fixed."""
    baseline_score = float(evaluate(params))
    observations: list[dict[str, Any]] = []
    best_by_parameter: dict[str, tuple[float, float]] = {}
    for name in PARAMETER_ORDER:
        best_value = float(getattr(params, name))
        best_score = baseline_score
        for value in PARAMETER_GRIDS[name]:
            trial = _replace_parameter(params, name, value)
            score = float(evaluate(trial))
            observations.append({
                "stage": "isolated_sweep",
                "parameter_name": name,
                "parameter_value": float(value),
                "parameters": trial.as_dict(),
                "score_ore": score,
                "improvement_ore": baseline_score - score,
            })
            if score < best_score:
                best_value, best_score = float(value), score
        best_by_parameter[name] = (best_value, best_score)

    diagnostic = params
    for name in PARAMETER_ORDER:
        diagnostic = _replace_parameter(diagnostic, name, best_by_parameter[name][0])
    return diagnostic, observations


def coordinate_descent(
    params: AdaptiveParameters,
    evaluate: EvaluationFn,
    *,
    passes: int = 2,
) -> tuple[AdaptiveParameters, list[dict[str, Any]]]:
    current = params.bounded()
    observations: list[dict[str, Any]] = []
    for pass_index in range(max(1, int(passes))):
        changed = False
        for name in PARAMETER_ORDER:
            before_score = float(evaluate(current))
            best, best_score = current, before_score
            for value in PARAMETER_GRIDS[name]:
                trial = _replace_parameter(current, name, value)
                score = float(evaluate(trial))
                observations.append({
                    "stage": f"coordinate_pass_{pass_index + 1}",
                    "parameter_name": name,
                    "parameter_value": float(value),
                    "parameters": trial.as_dict(),
                    "score_ore": score,
                    "improvement_ore": before_score - score,
                })
                if score < best_score:
                    best, best_score = trial, score
            if best != current:
                changed = True
                current = best
        if not changed:
            break
    return current, observations


def run_learning_cycle(
    replay_date: str,
    evaluate: EvaluationFn,
    *,
    start: AdaptiveParameters | None = None,
) -> dict[str, Any]:
    """Run isolated sensitivity and coordinate descent against one fixed external evaluator."""
    init_adaptive_learning_store()
    initial = (start or current_parameters("candidate")).bounded()
    baseline_score = float(evaluate(initial))
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO adaptive_learning_run(started_at,replay_date,baseline_parameters_json,baseline_score_ore,status,diagnostics_json) VALUES (?,?,?,?,?,?)",
            (_now(), replay_date, json.dumps(initial.as_dict(), sort_keys=True), baseline_score, "running", "{}"),
        )
        run_id = int(cur.lastrowid)

    _, isolated = single_parameter_sweeps(initial, evaluate)
    candidate, coordinate = coordinate_descent(initial, evaluate)
    result_score = float(evaluate(candidate))
    observations = isolated + coordinate

    with sqlite3.connect(DB_PATH) as c:
        for item in observations:
            c.execute(
                "INSERT INTO adaptive_learning_trial(run_id,stage,parameter_name,parameter_value,parameters_json,score_ore,improvement_ore,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    item["stage"],
                    item.get("parameter_name"),
                    item.get("parameter_value"),
                    json.dumps(item["parameters"], sort_keys=True),
                    item["score_ore"],
                    item["improvement_ore"],
                    _now(),
                ),
            )
        diagnostics = {
            "trial_count": len(observations),
            "parameter_order": list(PARAMETER_ORDER),
            "objective_semantics": "external_fixed_realized_cost",
        }
        c.execute(
            "UPDATE adaptive_learning_run SET completed_at=?,result_parameters_json=?,result_score_ore=?,status=?,diagnostics_json=? WHERE run_id=?",
            (_now(), json.dumps(candidate.as_dict(), sort_keys=True), result_score, "complete", json.dumps(diagnostics), run_id),
        )

    persist_parameters(candidate, "daily_optimum", score_ore=result_score, source_run_id=run_id)
    persist_parameters(candidate, "candidate", score_ore=result_score, source_run_id=run_id)
    return {
        "run_id": run_id,
        "replay_date": replay_date,
        "baseline_parameters": initial.as_dict(),
        "candidate_parameters": candidate.as_dict(),
        "baseline_score_ore": baseline_score,
        "candidate_score_ore": result_score,
        "improvement_ore": baseline_score - result_score,
        "trial_count": len(observations),
    }
