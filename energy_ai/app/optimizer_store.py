from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DB_PATH


def _init_tables() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS optimizer_plan(
            generated_at TEXT NOT NULL,
            start_utc TEXT NOT NULL,
            planner TEXT NOT NULL,
            battery_action_kw REAL NOT NULL,
            expected_soc_pct REAL NOT NULL,
            grid_import_kw REAL NOT NULL,
            grid_export_kw REAL NOT NULL,
            curtailed_kw REAL NOT NULL,
            interval_cost_ore REAL NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(generated_at,start_utc)
        );
        CREATE TABLE IF NOT EXISTS optimizer_plan_summary(
            generated_at TEXT PRIMARY KEY,
            planner TEXT NOT NULL,
            horizon_hours INTEGER NOT NULL,
            expected_cost_ore REAL NOT NULL,
            baseline_cost_ore REAL NOT NULL,
            expected_saving_ore REAL NOT NULL,
            payload_json TEXT NOT NULL
        );
        ''')


def insert_plan(plan: dict[str, Any]) -> int:
    _init_tables()
    generated = str(plan["generated_at"])
    planner = str(plan.get("planner") or "unknown")
    rows = plan.get("rows") or []
    with sqlite3.connect(DB_PATH) as c:
        c.executemany(
            '''INSERT OR REPLACE INTO optimizer_plan(
               generated_at,start_utc,planner,battery_action_kw,expected_soc_pct,
               grid_import_kw,grid_export_kw,curtailed_kw,interval_cost_ore,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            [(
                generated,
                r["start"],
                planner,
                float(r["battery_action_kw"]),
                float(r["expected_soc_pct"]),
                float(r["grid_import_kw"]),
                float(r["grid_export_kw"]),
                float(r.get("curtailed_kw") or 0.0),
                float(r["interval_cost_ore"]),
                json.dumps(r, ensure_ascii=False),
            ) for r in rows],
        )
        s = plan.get("summary") or {}
        c.execute(
            '''INSERT OR REPLACE INTO optimizer_plan_summary(
               generated_at,planner,horizon_hours,expected_cost_ore,baseline_cost_ore,
               expected_saving_ore,payload_json) VALUES (?,?,?,?,?,?,?)''',
            (
                generated,
                planner,
                int(plan.get("horizon_hours") or 0),
                float(s.get("expected_cost_ore") or 0.0),
                float(s.get("baseline_cost_ore") or 0.0),
                float(s.get("expected_saving_ore") or 0.0),
                json.dumps(plan, ensure_ascii=False),
            ),
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
        c.execute("DELETE FROM optimizer_plan WHERE generated_at < ?", (cutoff,))
        c.execute("DELETE FROM optimizer_plan_summary WHERE generated_at < ?", (cutoff,))
    return len(rows)


def latest_plan(limit: int = 144) -> dict[str, Any]:
    _init_tables()
    limit = max(1, min(int(limit), 500))
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT generated_at,payload_json FROM optimizer_plan_summary ORDER BY generated_at DESC LIMIT 1").fetchone()
        if not row:
            return {"generated_at": None, "rows": []}
        generated, payload = row
    try:
        plan = json.loads(payload)
    except Exception:
        return {"generated_at": generated, "rows": []}
    plan["rows"] = (plan.get("rows") or [])[:limit]
    return plan


def plan_history(limit: int = 20) -> list[dict[str, Any]]:
    _init_tables()
    limit = max(1, min(int(limit), 100))
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT generated_at,planner,horizon_hours,expected_cost_ore,baseline_cost_ore,expected_saving_ore FROM optimizer_plan_summary ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "generated_at": r[0],
            "planner": r[1],
            "horizon_hours": r[2],
            "expected_cost_ore": r[3],
            "baseline_cost_ore": r[4],
            "expected_saving_ore": r[5],
        }
        for r in rows
    ]
