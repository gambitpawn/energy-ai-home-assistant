from __future__ import annotations

from typing import Any

from .actuator_watchdog_dynamic_safety import install_dynamic_safety_watchdog_patch


"""Compatibility shim for the retired commissioning power cap.

The temporary downstream ±2 kW commissioning cap was useful while the Solinteg
write path was being verified. It is no longer part of the production control
chain. Physical power is now bounded by the ordinary deterministic actuator
safety envelope: configured battery charge/discharge limits, SOC guard rails,
and grid import/export limits.

This module is intentionally kept so older imports and the consolidated runtime
remain compatible without re-introducing the cap. Its install hook is also the
runtime integration point for the dynamic-safety watchdog: runtime.py calls this
hook after the diagnostics patch has been installed, so the dynamic watchdog is
the final watchdog implementation and normal safety-envelope shrinkage is
corrected in-place rather than pausing production.
"""


def apply_physical_command_cap(safety: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic safety unchanged; the commissioning cap is retired."""
    return dict(safety)


def install_physical_command_cap_patch() -> None:
    """Install the production watchdog correction; keep the old cap retired.

    runtime.py deliberately calls this after install_actuator_diagnostics_patch().
    The dynamic watchdog therefore replaces the legacy watchdog wrapper while
    retaining its runtime-config gate internally.
    """
    install_dynamic_safety_watchdog_patch()
