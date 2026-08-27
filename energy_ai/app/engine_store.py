from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .db import DB_PATH
from .engine_contract import EngineDecision, EngineInput

RETENTION_DAYS = 180


def init_engine_store() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS engine_information_vintage(
                information_vintage_id TEXT PRIMARY KEY,
                generated_at TEXT NOT NULL,
                decision_start TEXT NOT NULL,
                initial_soc_pct REAL NOT NULL,
                interval_minutes INTEGER NOT NULL,
                horizon_intervals INTEGER NOT NULL,
                price_known_intervals INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_engine_vintage_decision_start
                ON engine_information_vintage(decision_start);

            CREATE TABLE IF NOT EXISTS engine_decision(
                decision_id TEXT PRIMARY KEY,
                information_vintage_id TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                decision_start TEXT NOT NULL,
                engine_id TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                family TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_action_kw REAL NOT NULL,
                expected_soc_pct REAL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(information_vintage_id)
                    REFERENCES engine_information_vintage(information_vintage_id)
            );
            CREATE INDEX IF NOT EXISTS idx_engine_decision_engine_start
                ON engine_decision(engine_id,decision_start);
            CREATE INDEX IF NOT EXISTS idx_engine_decision_vintage
                ON engine_decision(information_vintage_id);
            '''
        )


def insert_engine_run(engine_input: EngineInput, decisions: Iterable[EngineDecision]) -> int:
    init_engine_store()
    decision_list = list(decisions)
    for decision in decision_list:
        if decision.information_vintage_id != engine_input.information_vintage_id:
            raise ValueError(
                f"decision {decision.engine_id} does not share input vintage "
                f"{engine_input.information_vintage_id}"
            )
        if decision.decision_start != engine_input.decision_start:
            raise ValueError(f"decision_start mismatch for {decision.engine_id}")

    input_payload = engine_input.as_dict(include_horizon=True)
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            '''INSERT OR REPLACE INTO engine_information_vintage(
               information_vintage_id,generated_at,decision_start,initial_soc_pct,
               interval_minutes,horizon_intervals,price_known_intervals,payload_json)
               VALUES (?,?,?,?,?,?,?,?)''',
            (
                engine_input.information_vintage_id,
                engine_input.generated_at,
                engine_input.decision_start,
                float(engine_input.initial_soc_pct),
                int(engine_input.interval_minutes),
                len(engine_input.horizon_rows),
                engine_input.price_known_intervals,
                json.dumps(input_payload, ensure_ascii=False),
            ),
        )
        for decision in decision_list:
            payload = decision.as_dict(include_plan_rows=True)
            c.execute(
                '''INSERT OR REPLACE INTO engine_decision(
                   decision_id,information_vintage_id,generated_at,decision_start,
                   engine_id,engine_version,family,status,requested_action_kw,
                   expected_soc_pct,payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    decision.decision_id,
                    decision.information_vintage_id,
                    decision.generated_at,
                    decision.decision_start,
                    decision.engine_id,
                    decision.engine_version,
                    decision.family,
                    decision.status,
                    float(decision.requested_action_kw),
                    None if decision.expected_soc_pct is None else float(decision.expected_soc_pct),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
        c.execute("DELETE FROM engine_decision WHERE generated_at < ?", (cutoff,))
        c.execute(
            "DELETE FROM engine_information_vintage WHERE generated_at < ? "
            "AND information_vintage_id NOT IN (SELECT DISTINCT information_vintage_id FROM engine_decision)",
            (cutoff,),
        )
    return len(decision_list)


def latest_engine_decisions(limit_per_engine: int = 1) -> dict[str, Any]:
    init_engine_store()
    limit_per_engine = max(1, min(int(limit_per_engine), 100))
    with sqlite3.connect(DB_PATH) as c:
        engine_ids = [r[0] for r in c.execute("SELECT DISTINCT engine_id FROM engine_decision ORDER BY engine_id").fetchall()]
        result: dict[str, Any] = {}
        for engine_id in engine_ids:
            rows = c.execute(
                "SELECT payload_json FROM engine_decision WHERE engine_id=? ORDER BY decision_start DESC, generated_at DESC LIMIT ?",
                (engine_id, limit_per_engine),
            ).fetchall()
            result[engine_id] = [json.loads(r[0]) for r in rows]
    return result


def competition_rows(start: str, end: str) -> list[dict[str, Any]]:
    """Return decisions grouped by decision interval and shared information vintage."""
    init_engine_store()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            '''SELECT decision_start,information_vintage_id,engine_id,payload_json
               FROM engine_decision
               WHERE decision_start>=? AND decision_start<?
               ORDER BY decision_start,information_vintage_id,engine_id''',
            (start, end),
        ).fetchall()
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for decision_start, vintage_id, engine_id, raw in rows:
        key = (decision_start, vintage_id)
        item = grouped.setdefault(
            key,
            {
                "decision_start": decision_start,
                "information_vintage_id": vintage_id,
                "decisions": {},
            },
        )
        item["decisions"][engine_id] = json.loads(raw)
    return list(grouped.values())
