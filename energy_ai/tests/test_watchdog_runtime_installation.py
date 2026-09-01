from __future__ import annotations

from app import actuator_physical_cap_v190 as physical_cap
from app import actuator_watchdog as watchdog
from app import deterministic_actuator as da


def test_consolidated_runtime_install_hook_activates_canonical_watchdog(monkeypatch):
    """runtime.py's historical install hook must install exactly the canonical watchdog."""
    async def legacy_watchdog(self):
        return {"status": "legacy"}

    monkeypatch.setattr(da.DeterministicActuator, "watchdog_tick", legacy_watchdog)
    monkeypatch.setattr(watchdog, "_INSTALLED", False)

    physical_cap.install_physical_command_cap_patch()

    assert da.DeterministicActuator.watchdog_tick is watchdog.watchdog_tick


def test_legacy_dynamic_watchdog_module_is_only_a_compatibility_shim():
    from app import actuator_watchdog_dynamic_safety as legacy

    assert legacy.watchdog_tick_with_dynamic_safety_correction is watchdog.watchdog_tick
    assert legacy.install_dynamic_safety_watchdog_patch.__module__ == "app.actuator_watchdog_dynamic_safety"
