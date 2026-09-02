from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import Any


class ActuatorControlLease:
    """Process-local truth for the physical command currently under supervision.

    The lease is deliberately never restored from SQLite. A new process starts
    without a lease and must complete the normal zero handshake before ACTIVE is
    possible. SQLite remains an audit store; it is not consulted to decide what
    target the watchdog is responsible for supervising.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._generation = 0
        self._state = "unarmed"
        self._command: dict[str, Any] | None = None
        self._last_command: dict[str, Any] | None = None
        self._updated_at = self._now()
        self._updated_monotonic = monotonic()
        self._reason = "process_started_without_control_lease"
        self._readback: dict[str, Any] | None = None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def acknowledge(
        self,
        candidate: dict[str, Any],
        *,
        target_kw: float,
        reason: str,
        readback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        command = {
            "command_id": None,
            "created_at": now,
            "source": candidate.get("source"),
            "source_id": candidate.get("source_id"),
            "engine_id": candidate.get("engine_id"),
            "decision_start": candidate.get("decision_start"),
            "valid_until": candidate.get("valid_until"),
            "requested_action_kw": candidate.get("requested_action_kw"),
            "safe_action_kw": float(target_kw),
            "status": "acknowledged",
            "reason": str(reason),
            "payload": {"readback": deepcopy(readback) if readback else None},
            "control_truth_source": "process_memory_verified_readback",
        }
        with self._lock:
            self._generation += 1
            command["lease_generation"] = self._generation
            self._state = "active"
            self._command = command
            self._last_command = deepcopy(command)
            self._updated_at = now
            self._updated_monotonic = monotonic()
            self._reason = str(reason)
            self._readback = deepcopy(readback) if readback else None
            return deepcopy(command)

    def renew(
        self,
        candidate: dict[str, Any],
        *,
        target_kw: float,
        reason: str,
        readback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Renew validity after the physical target has been re-verified."""
        return self.acknowledge(
            candidate,
            target_kw=target_kw,
            reason=reason,
            readback=readback,
        )

    def release(
        self,
        reason: str,
        *,
        readback: dict[str, Any] | None = None,
        released: bool = True,
    ) -> None:
        with self._lock:
            if self._command is not None:
                self._last_command = deepcopy(self._command)
            self._generation += 1
            self._state = "released" if released else "release_unverified"
            self._command = None
            self._updated_at = self._now()
            self._updated_monotonic = monotonic()
            self._reason = str(reason)
            self._readback = deepcopy(readback) if readback else None

    def current_command(self) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._command)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "policy": "process_local_verified_control_lease_v1",
                "restored_from_database": False,
                "generation": self._generation,
                "state": self._state,
                "reason": self._reason,
                "updated_at": self._updated_at,
                "age_seconds": round(max(0.0, monotonic() - self._updated_monotonic), 3),
                "command": deepcopy(self._command),
                "last_command": deepcopy(self._last_command),
                "readback": deepcopy(self._readback),
            }
