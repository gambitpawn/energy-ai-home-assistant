from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from . import model_selector as selector
from .db import DB_PATH

WINDOW_SCORE_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}


def install_model_comparison_patch(ui_module, cfg: dict[str, Any]) -> None:
    base_fn: Callable[[str], dict[str, Any]] = ui_module._model_comparison

    def comparison(window: str) -> dict[str, Any]:
        result = base_fn(window)
        score_days = WINDOW_SCORE_DAYS.get(window, 7)
        # Resolve through the module at call time so robust selector/state patches
        # installed by the consolidated runtime are always honored.
        state = selector.ensure_selector_state(cfg)
        context = state["context_signature"]

        with sqlite3.connect(DB_PATH, timeout=20) as c:
            dates = [
                str(r[0])
                for r in c.execute(
                    '''SELECT DISTINCT local_date FROM engine_daily_score
                       WHERE context_signature=? ORDER BY local_date DESC LIMIT ?''',
                    (context, score_days),
                ).fetchall()
            ]
            dates.reverse()
            rows = []
            if dates:
                placeholders = ",".join("?" for _ in dates)
                rows = c.execute(
                    f'''SELECT local_date,engine_id,intervals,mean_regret_ore,p90_regret_ore,clamp_rate,payload_json
                        FROM engine_daily_score
                        WHERE context_signature=? AND local_date IN ({placeholders})
                        ORDER BY local_date,engine_id''',
                    (context, *dates),
                ).fetchall()

        economics: dict[str, list[dict[str, Any]]] = {}
        cumulative: dict[str, float] = {}
        for local_date, engine_id, intervals, mean_regret, p90_regret, clamp_rate, raw in rows:
            try:
                payload = json.loads(raw or "{}")
            except Exception:
                payload = {}
            total_regret_ore = payload.get("total_regret_ore")
            if total_regret_ore is None:
                total_regret_ore = float(mean_regret) * int(intervals)
            daily_regret_sek = float(total_regret_ore) / 100.0
            eid = str(engine_id)
            cumulative[eid] = cumulative.get(eid, 0.0) + daily_regret_sek
            economics.setdefault(eid, []).append({
                "date": str(local_date),
                "intervals": int(intervals),
                "mean_regret_ore": float(mean_regret),
                "p90_regret_ore": float(p90_regret),
                "clamp_rate": float(clamp_rate),
                "daily_oracle_regret_sek": round(daily_regret_sek, 4),
                "cumulative_oracle_regret_sek": round(cumulative[eid], 4),
            })

        result["economics"] = economics
        result["economic_score_dates"] = dates
        result["economic_window_semantics"] = (
            f"latest {score_days} mature scored day(s); oracle scoring has an inherent realization lag"
        )
        result["metric_note"] = (
            "Economic comparison uses the latest mature selector-score days, not incomplete recent calendar time. "
            "Lower realized oracle regret is better. Behaviour remains a trailing wall-clock window."
        )
        return result

    ui_module._model_comparison = comparison
