from __future__ import annotations

from typing import Any

from .actuator_runtime_resilience import install_actuator_runtime_resilience_patch
from .actuator_watchdog import install_actuator_watchdog_patch


"""Compatibility shim for the retired commissioning power cap.

The temporary downstream ±2 kW commissioning cap was useful while the Solinteg
write path was being verified. It is no longer part of the production control
chain. Physical power is now bounded by the ordinary deterministic actuator
safety envelope: configured battery charge/discharge limits, SOC guard rails,
and grid import/export limits.

This module is intentionally kept because runtime.py still calls its historical
install hook. The hook now composes the production actuator exactly once:
remaining-horizon safety/command-path resilience first, then the single canonical
watchdog policy. The decision-start scheduler is installed afterwards and captures
that completed actuator implementation.
"""


def apply_physical_command_cap(safety: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic safety unchanged; the commissioning cap is retired."""
    return dict(safety)


def install_physical_command_cap_patch() -> None:
    install_actuator_runtime_resilience_patch()
    install_actuator_watchdog_patch()
