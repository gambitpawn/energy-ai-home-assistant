from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import deterministic_actuator as da

_INSTALLED = False
_ORIGINAL_ZERO_HANDSHAKE_AND_ARM = None


async def zero_handshake_and_arm_control_mode_held(self) -> dict[str, Any]:
    """Arm at zero power and leave Solinteg in the configured EMS control mode.

    Safe release is deliberately an exit/fault operation. Returning to the safe
    working mode during a successful arm would make the actuator look ready while
    the inverter is no longer in EMS BattCtrl, which the watchdog must reject.

    The acknowledged zero handshake is also stored as the newest effective
    command. This is important because command history survives pause/re-arm. If
    the previous effective command were allowed to remain current, the
    min-action-change optimisation could incorrectly hold that stale target even
    though the handshake has physically reset the inverter target to zero. The
    watchdog would then see a target drift and pause the controller immediately.
    """
    preflight = await self.preflight()
    if not preflight.get("ok"):
        da.mark_actuator_ready(False, detail=f"preflight_failed:{preflight.get('error')}")
        return {"ok": False, "stage": "preflight", "preflight": preflight}

    control_mode = str((self.cfg.get("actuator") or {}).get("control_working_mode") or "EMS BattCtrl")
    tolerance = max(0.0, float((self.cfg.get("actuator") or {}).get("ack_tolerance_kw", 0.10)))
    try:
        entered = await self.adapter.enter_control_mode_zero()
        if not entered.get("acknowledged"):
            raise RuntimeError(f"control-mode zero handshake was not acknowledged: {entered}")
        if str(entered.get("working_mode")) != control_mode:
            raise RuntimeError(
                f"control-mode zero handshake ended in {entered.get('working_mode')!r}, expected {control_mode!r}"
            )
        target = entered.get("battery_power_target_kw")
        if target is None or abs(float(target)) > tolerance:
            raise RuntimeError(
                f"control-mode zero handshake target {target!r} outside zero tolerance {tolerance:.3f} kW"
            )

        now = datetime.now(timezone.utc)
        handshake_candidate = {
            "source": "actuator_arm_zero_handshake",
            "source_id": now.isoformat(),
            "engine_id": "actuator_safety",
            "decision_start": now.isoformat(),
            "valid_until": (now + timedelta(minutes=15)).isoformat(),
            "requested_action_kw": 0.0,
        }
        handshake_payload = {
            "status": "acknowledged",
            "reason": "zero_handshake_acknowledged_control_mode_held",
            "requested_action_kw": 0.0,
            "safe_action_kw": 0.0,
            "readback": entered,
            "physical_write_performed": True,
            "control_mode_held": True,
        }
        # Establish process-local control truth before audit persistence. If the
        # database is busy, the acknowledged zero target is still the target the
        # watchdog must supervise.
        lease = getattr(self, "control_lease", None)
        if lease is None:
            raise RuntimeError("actuator_control_lease_unavailable")
        lease.acknowledge(
            handshake_candidate,
            target_kw=0.0,
            reason="zero_handshake_acknowledged_control_mode_held",
            readback=entered,
        )
        handshake_command_id = da._insert_command(
            handshake_candidate,
            safe_action_kw=0.0,
            physical_write=True,
            status="acknowledged",
            reason="zero_handshake_acknowledged_control_mode_held",
            payload=handshake_payload,
        )

        da.mark_actuator_ready(True, detail="solinteg_zero_handshake_acknowledged_control_mode_held")
        da._event(
            "actuator_armed",
            "zero_handshake_acknowledged_control_mode_held",
            {
                "entered": entered,
                "control_mode_held": True,
                "handshake_command_id": handshake_command_id,
            },
        )
        return {
            "ok": True,
            "stage": "armed",
            "physical_write_performed": True,
            "zero_power_only": True,
            "control_mode_held": True,
            "control_mode_test": entered,
            "handshake_command_id": handshake_command_id,
            "audit_queued": handshake_command_id is None,
            "production": da.production_status(),
        }
    except Exception as exc:
        try:
            release = await self.adapter.safe_release()
        except Exception as release_exc:
            release = {"released": False, "error": repr(release_exc)}
        da.mark_actuator_ready(False, detail=f"zero_handshake_failed:{exc!r}")
        da._event("actuator_arm_failed", repr(exc), {"safe_release": release})
        return {
            "ok": False,
            "stage": "zero_handshake",
            "error": repr(exc),
            "safe_release": release,
        }


def install_arm_control_mode_patch() -> None:
    global _INSTALLED, _ORIGINAL_ZERO_HANDSHAKE_AND_ARM
    if _INSTALLED:
        return
    _ORIGINAL_ZERO_HANDSHAKE_AND_ARM = da.DeterministicActuator.zero_handshake_and_arm
    da.DeterministicActuator.zero_handshake_and_arm = zero_handshake_and_arm_control_mode_held
    _INSTALLED = True
