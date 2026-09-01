from __future__ import annotations

"""Compatibility shim for the pre-consolidation dynamic watchdog.

The watchdog policy now lives in :mod:`app.actuator_watchdog`.  Keep the old
module/function names so older imports and tests fail closed onto the canonical
implementation instead of installing a second, diverging watchdog policy.
"""

from .actuator_watchdog import install_actuator_watchdog_patch, watchdog_tick

# Historical function name retained for compatibility.
watchdog_tick_with_dynamic_safety_correction = watchdog_tick


def install_dynamic_safety_watchdog_patch() -> None:
    install_actuator_watchdog_patch()
