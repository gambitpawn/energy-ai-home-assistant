from __future__ import annotations

from app import actuator_physical_cap_v190 as physical_cap
from app import actuator_watchdog_dynamic_safety as dynamic_watchdog
from app import deterministic_actuator as da


def test_consolidated_runtime_install_hook_activates_dynamic_watchdog(monkeypatch):
    """The hook called by runtime.py must leave the dynamic watchdog installed.

    This guards against the regression where the correction implementation and
    its unit tests existed, but the consolidated runtime continued using the
    legacy fail-safe watchdog.
    """
    async def legacy_watchdog(self):
        return {"status": "legacy"}

    monkeypatch.setattr(da.DeterministicActuator, "watchdog_tick", legacy_watchdog)
    monkeypatch.setattr(dynamic_watchdog, "_INSTALLED", False)

    physical_cap.install_physical_command_cap_patch()

    assert da.DeterministicActuator.watchdog_tick is dynamic_watchdog.watchdog_tick_with_dynamic_safety_correction
