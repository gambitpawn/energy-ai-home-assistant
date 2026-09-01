from __future__ import annotations

from typing import Any

from .actuator_runtime_resilience import install_actuator_runtime_resilience_patch
from .actuator_watchdog_dynamic_safety import install_dynamic_safety_watchdog_patch


"""Compatibility shim for the retired commissioning power cap.

The temporary downstream ±2 kW commissioning cap was useful while the Solinteg
write path was being verified. It is no longer part of the production control
chain. Physical power is now bounded by the ordinary deterministic actuator
safety envelope: configured battery charge/discharge limits, SOC guard rails,
and grid import/export limits.

This module is intentionally kept so older imports and the consolidated runtime
remain compatible without re-introducing the cap. Its install hook is also the
runtime integration point for actuator hardening because runtime.py calls it
after the diagnostics patch and before the production loops start.
"""


def apply_physical_command_cap(safety: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic safety unchanged; the commissioning cap is retired."""
    return dict(safety)


def install_physical_command_cap_patch() -> None:
    """Install actuator hardening while keeping the old commissioning cap retired.

    Order matters: first replace the shared safety filter / Solinteg command
    behavior, then install the watchdog implementation that consumes that shared
    filter. Both process_candidate() and watchdog_tick() consequently use the same
    remaining-horizon safety semantics.
    """
    install_actuator_runtime_resilience_patch()
    install_dynamic_safety_watchdog_patch()
