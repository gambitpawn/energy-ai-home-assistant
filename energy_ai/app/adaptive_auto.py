from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .adaptive_learning import active_run, has_completed_run, latest_learning_status, run_learning_cycle
from .adaptive_replay import build_daily_evaluator
from .tariff_scenarios import LOCAL_TZ


def default_replay_date(now: datetime | None = None) -> date:
    now_local = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    return now_local.date() - timedelta(days=1)


def automatic_maintenance_once(cfg: dict[str, Any], replay_date: str | None = None, *, force: bool = False) -> dict[str, Any]:
    day = date.fromisoformat(replay_date) if replay_date else default_replay_date()
    day_text = day.isoformat()

    running = active_run(day_text)
    if running is not None:
        return {
            "ok": True,
            "status": "already_running",
            "replay_date": day_text,
            "active_run": running,
            "learning": latest_learning_status(),
        }

    if has_completed_run(day_text) and not force:
        return {
            "ok": True,
            "status": "not_due",
            "reason": "replay_date_already_completed",
            "replay_date": day_text,
            "learning": latest_learning_status(),
        }

    try:
        evaluator = build_daily_evaluator(cfg, day_text)
    except Exception as exc:
        return {
            "ok": True,
            "status": "waiting_for_complete_day",
            "replay_date": day_text,
            "reason": repr(exc),
            "learning": latest_learning_status(),
        }

    result = run_learning_cycle(day_text, evaluator)
    return {
        "ok": True,
        "status": "trained",
        "replay_date": day_text,
        "result": result,
        "learning": latest_learning_status(),
    }


def automatic_status() -> dict[str, Any]:
    target = default_replay_date().isoformat()
    status = latest_learning_status()
    return {
        **status,
        "automatic_feedback_enabled": True,
        "feedback_period": "previous_complete_local_calendar_day",
        "local_timezone": str(LOCAL_TZ),
        "next_replay_date": target,
        "next_replay_completed": has_completed_run(target),
        "active_run": active_run(target),
        "maintenance_poll_interval_hours": 1,
        "learning_mode": "shadow_challenger_only",
    }
