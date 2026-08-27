from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import model_selector as ms

# Require at least 95.8% of a normal 96-interval day. This sharply limits
# survivorship bias where a challenger could otherwise be compared only on the
# intervals for which it happened to emit a decision.
MIN_DAILY_INTERVALS = 92


def _paired_daily_scores(
    context: str, challenger: str, incumbent: str, limit: int
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Pair only score days belonging to the current automatic-validation epoch.

    Manual force-evaluation remains useful for diagnostics/backtests, but it must
    never be able to accelerate automatic promotion using pre-epoch history.
    """
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        state = c.execute(
            '''SELECT evaluation_start_date FROM engine_selector_state
               WHERE singleton=1 AND context_signature=?''',
            (context,),
        ).fetchone()
        evaluation_start = str(state[0]) if state else "0001-01-01"
        rows = c.execute(
            '''SELECT a.local_date,a.payload_json,b.payload_json
               FROM engine_daily_score a
               JOIN engine_daily_score b
                 ON b.local_date=a.local_date AND b.context_signature=a.context_signature
               WHERE a.context_signature=? AND a.engine_id=? AND b.engine_id=?
                 AND a.local_date>=?
                 AND a.intervals>=? AND b.intervals>=?
               ORDER BY a.local_date DESC LIMIT ?''',
            (
                context,
                challenger,
                incumbent,
                evaluation_start,
                MIN_DAILY_INTERVALS,
                MIN_DAILY_INTERVALS,
                max(1, int(limit)),
            ),
        ).fetchall()
    parsed = [(str(d), json.loads(a), json.loads(b)) for d, a, b in rows]
    parsed.reverse()
    return parsed


def install_selector_policy_patch() -> None:
    ms.MIN_DAILY_INTERVALS = MIN_DAILY_INTERVALS
    ms._paired_daily_scores = _paired_daily_scores
