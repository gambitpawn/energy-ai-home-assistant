from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

_LOW_PRIORITY_LOCK = asyncio.Lock()
_STATE: dict[str, Any] = {
    "policy": "single_low_priority_maintenance_job_v1",
    "running": None,
    "started_at": None,
    "last_completed": None,
    "last_completed_at": None,
    "last_error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_low_priority(
    label: str,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Serialize CPU/SQLite-heavy learning work away from its peer jobs.

    Control planning and the watchdog do not acquire this lock and therefore
    cannot queue behind model training.
    """
    async with _LOW_PRIORITY_LOCK:
        _STATE.update({"running": str(label), "started_at": _now(), "last_error": None})
        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
            _STATE.update({"last_completed": str(label), "last_completed_at": _now()})
            return result
        except Exception as exc:
            _STATE["last_error"] = repr(exc)
            raise
        finally:
            _STATE["running"] = None


def status() -> dict[str, Any]:
    return dict(_STATE)
