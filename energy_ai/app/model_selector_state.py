from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import model_selector as ms


def ensure_selector_state(cfg: dict[str, Any]) -> dict[str, Any]:
    """SQLite-safe selector state loader/reset.

    Event persistence is intentionally performed only after the state transaction
    has closed. This avoids opening a second writer while a context-reset update
    still owns SQLite's write lock.
    """
    ms._init_tables()
    context = ms._context_signature(cfg)
    today = datetime.now(ms.LOCAL_TZ).date().isoformat()
    pending_event: dict[str, Any] | None = None

    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        row = c.execute(
            '''SELECT selected_engine_id,fallback_engine_id,context_signature,
                      evaluation_start_date,selected_since,cooldown_until,updated_at,
                      last_evaluated_date,last_selection_reason
               FROM engine_selector_state WHERE singleton=1'''
        ).fetchone()
        if row is None:
            now = ms._now()
            c.execute(
                '''INSERT INTO engine_selector_state(
                   singleton,selected_engine_id,fallback_engine_id,context_signature,
                   evaluation_start_date,selected_since,cooldown_until,updated_at,
                   last_evaluated_date,last_selection_reason)
                   VALUES (1,?,?,?,?,?,?,?,?,?)''',
                (
                    ms.BASELINE_ENGINE_ID,
                    ms.BASELINE_ENGINE_ID,
                    context,
                    today,
                    now,
                    None,
                    now,
                    None,
                    "initial_baseline",
                ),
            )
            row = (
                ms.BASELINE_ENGINE_ID,
                ms.BASELINE_ENGINE_ID,
                context,
                today,
                now,
                None,
                now,
                None,
                "initial_baseline",
            )
        elif str(row[2]) != context:
            previous = str(row[0])
            now = ms._now()
            c.execute(
                '''UPDATE engine_selector_state SET
                   selected_engine_id=?,fallback_engine_id=?,context_signature=?,
                   evaluation_start_date=?,selected_since=?,cooldown_until=NULL,
                   updated_at=?,last_evaluated_date=NULL,last_selection_reason=?
                   WHERE singleton=1''',
                (
                    ms.BASELINE_ENGINE_ID,
                    ms.BASELINE_ENGINE_ID,
                    context,
                    today,
                    now,
                    now,
                    "control_context_changed_reset_to_baseline",
                ),
            )
            row = (
                ms.BASELINE_ENGINE_ID,
                ms.BASELINE_ENGINE_ID,
                context,
                today,
                now,
                None,
                now,
                None,
                "control_context_changed_reset_to_baseline",
            )
            pending_event = {
                "from_engine_id": previous,
                "to_engine_id": ms.BASELINE_ENGINE_ID,
                "evaluation_start_date": today,
            }

    if pending_event is not None:
        ms._event(
            "context_reset",
            context,
            "Control/economics context changed; restart validation from frozen baseline.",
            from_engine_id=pending_event["from_engine_id"],
            to_engine_id=pending_event["to_engine_id"],
            payload={"evaluation_start_date": pending_event["evaluation_start_date"]},
        )

    return {
        "selected_engine_id": str(row[0]),
        "fallback_engine_id": str(row[1]),
        "context_signature": str(row[2]),
        "evaluation_start_date": str(row[3]),
        "selected_since": str(row[4]),
        "cooldown_until": row[5],
        "updated_at": str(row[6]),
        "last_evaluated_date": row[7],
        "last_selection_reason": str(row[8]),
    }


def install_selector_state_patch() -> None:
    # model_selector functions resolve this global at call time, so one explicit
    # installation patches every evaluation/promotion/routing path consistently.
    ms.ensure_selector_state = ensure_selector_state
