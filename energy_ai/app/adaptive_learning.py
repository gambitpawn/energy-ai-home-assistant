from __future__ import annotations

import json
import sqlite3
from dataclasses import fields, replace
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

CANDIDATE_LEARNING_RATE = 0.20
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
                score_ore REAL
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
            CREATE INDEX IF NOT EXISTS idx_adaptive_learning_run_date
                ON adaptive_learning_run(replay_date, status);

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

            CREATE TABLE IF NOT EXISTS adaptive_learning_progress(
                run_id INTEGER PRIMARY KEY,
                updated_at TEXT NOT NULL,
                stage TEXT NOT NULL,
                parameter_name TEXT,
                stage_trial_index INTEGER NOT NULL,
                stage_trial_total INTEGER NOT NULL,
                total_trials_written INTEGER NOT NULL,
                message TEXT,
                FOREIGN KEY(run_id) REFERENCES adaptive_learning_run(run_id)
            );
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
            "SELECT parameters_json FROM adaptive_parameter_state WHERE role=? ORDER BY state_id DESC LIMIT 1",
            (role,),
        ).fetchone()
    return _from_json(row[0] if row else None)


def persist_parameters(params: AdaptiveParameters, role: str, *, score_ore: float | None = None, source_run_id: int | None = None) -> None:
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


def _blend(old: AdaptiveParameters, target: AdaptiveParameters, rate: float = CANDIDATE_LEARNING_RATE) -> AdaptiveParameters:
    rate = min(1.0, max(0.0, float(rate)))
    values = {}
    for f in fields(AdaptiveParameters):
        a = float(getattr(old, f.name))
        b = float(getattr(target, f.name))
        values[f.name] = a + rate * (b - a)
    return AdaptiveParameters(**values).bounded()


def _local_grid(name: str, value: float) -> tuple[float, ...]:
    """Three-point local refinement around the nearest configured grid value."""
    grid = PARAMETER_GRIDS[name]
    nearest = min(range(len(grid)), key=lambda i: abs(grid[i] - float(value)))
    indices = sorted(set((max(0, nearest - 1), nearest, min(len(grid) - 1, nearest + 1))))
    return tuple(sorted(set(float(grid[i]) for i in indices) | {float(value)}))


def _write_progress(
    run_id: int,
    *,
    stage: str,
    parameter_name: str | None,
    stage_trial_index: int,
    stage_trial_total: int,
    message: str | None = None,
) -> None:
    with sqlite3.connect(DB_PATH) as c:
        total = int(c.execute("SELECT COUNT(*) FROM adaptive_learning_trial WHERE run_id=?", (run_id,)).fetchone()[0])
        c.execute(
            '''
            INSERT INTO adaptive_learning_progress(run_id,updated_at,stage,parameter_name,stage_trial_index,stage_trial_total,total_trials_written,message)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET
                updated_at=excluded.updated_at,
                stage=excluded.stage,
                parameter_name=excluded.parameter_name,
                stage_trial_index=excluded.stage_trial_index,
                stage_trial_total=excluded.stage_trial_total,
                total_trials_written=excluded.total_trials_written,
                message=excluded.message
            ''',
            (run_id, _now(), stage, parameter_name, stage_trial_index, stage_trial_total, total, message),
        )


def _record_trial(run_id: int, item: dict[str, Any], *, index: int, total: int) -> None:
    with sqlite3.connect(DB_PATH) as c:
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
    _write_progress(
        run_id,
        stage=item["stage"],
        parameter_name=item.get("parameter_name"),
        stage_trial_index=index,
        stage_trial_total=total,
    )


def single_parameter_sweeps(
    params: AdaptiveParameters,
    evaluate: EvaluationFn,
    *,
    run_id: int | None = None,
) -> tuple[AdaptiveParameters, list[dict[str, Any]]]:
    baseline_score = float(evaluate(params))
    observations: list[dict[str, Any]] = []
    best_values: dict[str, float] = {}
    candidates_by_name = {
        name: tuple(sorted(set(PARAMETER_GRIDS[name] + (float(getattr(params, name)),))))
        for name in PARAMETER_ORDER
    }
    total = sum(len(v) for v in candidates_by_name.values())
    index = 0
    for name in PARAMETER_ORDER:
        best_value = float(getattr(params, name))
        best_score = baseline_score
        for value in candidates_by_name[name]:
            trial = _replace_parameter(params, name, value)
            score = float(evaluate(trial))
            item = {
                "stage": "isolated_sweep",
                "parameter_name": name,
                "parameter_value": float(value),
                "parameters": trial.as_dict(),
                "score_ore": score,
                "improvement_ore": baseline_score - score,
            }
            observations.append(item)
            index += 1
            if run_id is not None:
                _record_trial(run_id, item, index=index, total=total)
            if score < best_score:
                best_value, best_score = float(value), score
        best_values[name] = best_value

    diagnostic = params
    for name in PARAMETER_ORDER:
        diagnostic = _replace_parameter(diagnostic, name, best_values[name])
    return diagnostic, observations


def coordinate_descent(
    params: AdaptiveParameters,
    evaluate: EvaluationFn,
    *,
    passes: int = 1,
    run_id: int | None = None,
) -> tuple[AdaptiveParameters, list[dict[str, Any]]]:
    current = params.bounded()
    observations: list[dict[str, Any]] = []
    grids = {name: _local_grid(name, float(getattr(current, name))) for name in PARAMETER_ORDER}
    total_per_pass = sum(len(v) for v in grids.values())
    total = total_per_pass * max(1, int(passes))
    index = 0
    for pass_index in range(max(1, int(passes))):
        changed = False
        for name in PARAMETER_ORDER:
            before_score = float(evaluate(current))
            best, best_score = current, before_score
            candidates = _local_grid(name, float(getattr(current, name)))
            for value in candidates:
                trial = _replace_parameter(current, name, value)
                score = float(evaluate(trial))
                item = {
                    "stage": f"coordinate_pass_{pass_index + 1}",
                    "parameter_name": name,
                    "parameter_value": float(value),
                    "parameters": trial.as_dict(),
                    "score_ore": score,
                    "improvement_ore": before_score - score,
                }
                observations.append(item)
                index += 1
                if run_id is not None:
                    _record_trial(run_id, item, index=index, total=total)
                if score < best_score:
                    best, best_score = trial, score
            if best != current:
                current, changed = best, True
        if not changed:
            break
    return current, observations


def has_completed_run(replay_date: str) -> bool:
    init_adaptive_learning_store()
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT 1 FROM adaptive_learning_run WHERE replay_date=? AND status='complete' LIMIT 1",
            (replay_date,),
        ).fetchone()
    return row is not None


def active_run(replay_date: str | None = None) -> dict[str, Any] | None:
    init_adaptive_learning_store()
    query = "SELECT run_id,started_at,replay_date FROM adaptive_learning_run WHERE status='running'"
    args: tuple[Any, ...] = ()
    if replay_date is not None:
        query += " AND replay_date=?"
        args = (replay_date,)
    query += " ORDER BY run_id DESC LIMIT 1"
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(query, args).fetchone()
        if not row:
            return None
        progress = c.execute(
            "SELECT updated_at,stage,parameter_name,stage_trial_index,stage_trial_total,total_trials_written,message FROM adaptive_learning_progress WHERE run_id=?",
            (row[0],),
        ).fetchone()
    return {
        "run_id": row[0],
        "started_at": row[1],
        "replay_date": row[2],
        "progress": None if not progress else {
            "updated_at": progress[0],
            "stage": progress[1],
            "parameter_name": progress[2],
            "stage_trial_index": progress[3],
            "stage_trial_total": progress[4],
            "total_trials_written": progress[5],
            "message": progress[6],
        },
    }


def mark_orphaned_running_runs() -> int:
    """On a fresh process, any persisted running row is from a terminated prior process."""
    init_adaptive_learning_store()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT run_id,diagnostics_json FROM adaptive_learning_run WHERE status='running'").fetchall()
        for run_id, diagnostics_raw in rows:
            try:
                diagnostics = json.loads(diagnostics_raw or "{}")
            except Exception:
                diagnostics = {}
            diagnostics["orphaned_by_restart"] = True
            diagnostics["orphaned_at"] = _now()
            c.execute(
                "UPDATE adaptive_learning_run SET completed_at=?,status='interrupted',diagnostics_json=? WHERE run_id=?",
                (_now(), json.dumps(diagnostics), run_id),
            )
    return len(rows)


def latest_learning_status() -> dict[str, Any]:
    init_adaptive_learning_store()
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT run_id,started_at,completed_at,replay_date,baseline_parameters_json,result_parameters_json,baseline_score_ore,result_score_ore,status,diagnostics_json FROM adaptive_learning_run ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        trials = int(c.execute("SELECT COUNT(*) FROM adaptive_learning_trial").fetchone()[0])
        runs = int(c.execute("SELECT COUNT(*) FROM adaptive_learning_run WHERE status='complete'").fetchone()[0])
        progress = None
        if row:
            p = c.execute(
                "SELECT updated_at,stage,parameter_name,stage_trial_index,stage_trial_total,total_trials_written,message FROM adaptive_learning_progress WHERE run_id=?",
                (row[0],),
            ).fetchone()
            if p:
                progress = {
                    "updated_at": p[0],
                    "stage": p[1],
                    "parameter_name": p[2],
                    "stage_trial_index": p[3],
                    "stage_trial_total": p[4],
                    "total_trials_written": p[5],
                    "message": p[6],
                }
    latest = None
    if row:
        latest = {
            "run_id": row[0], "started_at": row[1], "completed_at": row[2], "replay_date": row[3],
            "baseline_parameters": json.loads(row[4]),
            "daily_optimum_parameters": json.loads(row[5]) if row[5] else None,
            "baseline_score_ore": row[6], "daily_optimum_score_ore": row[7], "status": row[8],
            "diagnostics": json.loads(row[9] or "{}"),
            "progress": progress,
        }
    return {
        "engine_id": "adaptive_deterministic_v1",
        "learning_enabled": True,
        "physical_writes_enabled": False,
        "completed_runs": runs,
        "total_trials": trials,
        "candidate_learning_rate": CANDIDATE_LEARNING_RATE,
        "candidate_parameters": current_parameters("candidate").as_dict(),
        "latest_run": latest,
    }


def run_learning_cycle(replay_date: str, evaluate: EvaluationFn, *, start: AdaptiveParameters | None = None) -> dict[str, Any]:
    """One 24h feedback cycle: isolated sweeps, local joint refinement, then slow candidate update."""
    init_adaptive_learning_store()
    initial = (start or current_parameters("candidate")).bounded()
    baseline_score = float(evaluate(initial))
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO adaptive_learning_run(started_at,replay_date,baseline_parameters_json,baseline_score_ore,status,diagnostics_json) VALUES (?,?,?,?,?,?)",
            (_now(), replay_date, json.dumps(initial.as_dict(), sort_keys=True), baseline_score, "running", "{}"),
        )
        run_id = int(cur.lastrowid)
    _write_progress(run_id, stage="starting", parameter_name=None, stage_trial_index=0, stage_trial_total=0, message="baseline complete")

    try:
        isolated_best, isolated = single_parameter_sweeps(initial, evaluate, run_id=run_id)
        _write_progress(run_id, stage="coordinate_setup", parameter_name=None, stage_trial_index=0, stage_trial_total=0,
                        message="isolated sweeps complete; starting local joint refinement")
        daily_optimum, coordinate = coordinate_descent(isolated_best, evaluate, passes=1, run_id=run_id)
        daily_score = float(evaluate(daily_optimum))
        learned_candidate = _blend(initial, daily_optimum)
        learned_score = float(evaluate(learned_candidate))
        observations = isolated + coordinate
        evaluator_details = getattr(evaluate, "diagnostics", None)
        baseline_details = evaluator_details(initial) if callable(evaluator_details) else None
        optimum_details = evaluator_details(daily_optimum) if callable(evaluator_details) else None
        candidate_details = evaluator_details(learned_candidate) if callable(evaluator_details) else None

        diagnostics = {
            "trial_count": len(observations),
            "parameter_order": list(PARAMETER_ORDER),
            "objective_semantics": "external_fixed_realized_cost",
            "candidate_learning_rate": CANDIDATE_LEARNING_RATE,
            "search_strategy": "isolated_full_grid_then_one_pass_local_coordinate_refinement",
            "baseline_replay": baseline_details,
            "daily_optimum_replay": optimum_details,
            "learned_candidate_replay": candidate_details,
        }
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "UPDATE adaptive_learning_run SET completed_at=?,result_parameters_json=?,result_score_ore=?,status='complete',diagnostics_json=? WHERE run_id=?",
                (_now(), json.dumps(daily_optimum.as_dict(), sort_keys=True), daily_score, json.dumps(diagnostics), run_id),
            )
        _write_progress(run_id, stage="complete", parameter_name=None, stage_trial_index=len(observations),
                        stage_trial_total=len(observations), message="learning cycle complete")

        persist_parameters(daily_optimum, "daily_optimum", score_ore=daily_score, source_run_id=run_id)
        persist_parameters(learned_candidate, "candidate", score_ore=learned_score, source_run_id=run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "replay_date": replay_date,
            "baseline_parameters": initial.as_dict(),
            "isolated_best_parameters": isolated_best.as_dict(),
            "daily_optimum_parameters": daily_optimum.as_dict(),
            "candidate_parameters": learned_candidate.as_dict(),
            "baseline_score_ore": baseline_score,
            "daily_optimum_score_ore": daily_score,
            "candidate_score_ore": learned_score,
            "daily_optimum_improvement_ore": baseline_score - daily_score,
            "candidate_improvement_ore": baseline_score - learned_score,
            "trial_count": len(observations),
        }
    except Exception as exc:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "UPDATE adaptive_learning_run SET completed_at=?,status='failed',diagnostics_json=? WHERE run_id=?",
                (_now(), json.dumps({"error": repr(exc)}), run_id),
            )
        _write_progress(run_id, stage="failed", parameter_name=None, stage_trial_index=0, stage_trial_total=0, message=repr(exc))
        raise
