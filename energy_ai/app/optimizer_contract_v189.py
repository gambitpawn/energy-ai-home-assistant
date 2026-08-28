from __future__ import annotations

from typing import Any, Callable

from . import optimizer as optimizer

_INSTALLED = False
_ORIGINAL_INTERVAL_RESULT: Callable[..., dict[str, Any]] | None = None

REQUIRED_HURDLE_KEYS = (
    "hurdle_cost_ore",
    "discretionary_shift_hurdle_cost_ore",
    "arbitrage_hurdle_cost_ore",
)


def normalize_interval_result(result: dict[str, Any]) -> dict[str, Any]:
    """Restore the frozen v3.5 interval-result contract around runtime patches.

    v1.0.79 repriced the optimizer through a runtime replacement of
    optimizer._interval_result. The repriced implementation returned only
    ``hurdle_cost_ore`` while frozen v3.5 later reads
    ``discretionary_shift_hurdle_cost_ore``. Keep economics unchanged and add
    the historical aliases expected by the frozen planner.
    """
    out = dict(result)
    hurdle = out.get("discretionary_shift_hurdle_cost_ore")
    if hurdle is None:
        hurdle = out.get("arbitrage_hurdle_cost_ore")
    if hurdle is None:
        hurdle = out.get("hurdle_cost_ore")
    if hurdle is None:
        hurdle = 0.0
    hurdle = float(hurdle)
    out.setdefault("hurdle_cost_ore", hurdle)
    out.setdefault("discretionary_shift_hurdle_cost_ore", hurdle)
    out.setdefault("arbitrage_hurdle_cost_ore", hurdle)
    return out


def install_optimizer_interval_contract_patch() -> dict[str, Any]:
    global _INSTALLED, _ORIGINAL_INTERVAL_RESULT
    if _INSTALLED:
        return contract_status()

    previous = optimizer._interval_result
    _ORIGINAL_INTERVAL_RESULT = previous

    def compatible_interval_result(row, action, cfg):
        return normalize_interval_result(previous(row, action, cfg))

    compatible_interval_result.__name__ = "_interval_result_v189_compatible"
    compatible_interval_result.__doc__ = (
        "v1.0.89 compatibility wrapper preserving current economics while "
        "restoring frozen deterministic-v3.5 hurdle-cost result aliases."
    )
    optimizer._interval_result = compatible_interval_result
    _INSTALLED = True
    return contract_status()


def contract_status() -> dict[str, Any]:
    current = optimizer._interval_result
    return {
        "installed": bool(_INSTALLED),
        "planner": optimizer.PLANNER_NAME,
        "current_interval_result_function": getattr(current, "__name__", repr(current)),
        "required_hurdle_keys": list(REQUIRED_HURDLE_KEYS),
        "economics_semantics_changed": False,
        "frozen_v35_algorithm_changed": False,
        "purpose": "restore interval-result key compatibility after economics repricing patch",
    }
