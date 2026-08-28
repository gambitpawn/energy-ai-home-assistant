from __future__ import annotations

from typing import Any


"""Compatibility shim for the retired commissioning power cap.

The temporary downstream ±2 kW commissioning cap was useful while the Solinteg
write path was being verified. It is no longer part of the production control
chain. Physical power is now bounded by the ordinary deterministic actuator
safety envelope: configured battery charge/discharge limits, SOC guard rails,
and grid import/export limits.

This module is intentionally kept so older imports and the consolidated 1.0.94
runtime remain compatible without re-introducing the cap.
"""


def apply_physical_command_cap(safety: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic safety unchanged; the commissioning cap is retired."""
    return dict(safety)


def install_physical_command_cap_patch() -> None:
    """Compatibility no-op.

    Kept because the consolidated runtime still imports and calls this function.
    No monkeypatch is installed and no physical command is capped here.
    """
    return None
