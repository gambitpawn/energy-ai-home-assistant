from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable


class ActuatorAuditBacklog:
    """Bounded retry queue for non-control SQLite audit records."""

    def __init__(self, max_items: int = 512) -> None:
        self._lock = RLock()
        self._items: deque[dict[str, Any]] = deque()
        self._max_items = max(16, int(max_items))
        self._dropped = 0
        self._written = 0
        self._last_error: str | None = None
        self._last_flush_at: str | None = None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def enqueue(self, kind: str, payload: dict[str, Any]) -> None:
        item = {"kind": str(kind), "payload": deepcopy(payload), "queued_at": self._now()}
        with self._lock:
            if len(self._items) >= self._max_items:
                self._items.popleft()
                self._dropped += 1
            self._items.append(item)

    def flush(
        self,
        write_command: Callable[[dict[str, Any]], Any],
        write_event: Callable[[dict[str, Any]], Any],
        *,
        limit: int = 64,
    ) -> dict[str, Any]:
        written = 0
        for _ in range(max(1, int(limit))):
            with self._lock:
                if not self._items:
                    break
                # Own the item while it is being written. Leaving it at index
                # zero would let a concurrent full-queue enqueue evict it and
                # make the successful flush remove the following record.
                item = self._items.popleft()
            try:
                if item["kind"] == "command":
                    write_command(item["payload"])
                elif item["kind"] == "event":
                    write_event(item["payload"])
                else:
                    raise ValueError(f"unsupported actuator audit item {item['kind']!r}")
            except Exception as exc:
                with self._lock:
                    if len(self._items) >= self._max_items:
                        self._items.pop()
                        self._dropped += 1
                    self._items.appendleft(item)
                    self._last_error = repr(exc)
                    self._last_flush_at = self._now()
                break
            with self._lock:
                self._written += 1
                self._last_error = None
                self._last_flush_at = self._now()
            written += 1
        return {**self.status(), "written_this_flush": written}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "policy": "bounded_best_effort_retry_v1",
                "pending": len(self._items),
                "capacity": self._max_items,
                "written": self._written,
                "dropped": self._dropped,
                "last_error": self._last_error,
                "last_flush_at": self._last_flush_at,
            }
