from __future__ import annotations

import importlib
import sys


def test_consolidated_runtime_imports_without_versioned_entry_chain():
    runtime = importlib.import_module("app.runtime")

    assert runtime.RUNTIME_BUILD == "1.0.92"
    assert runtime.app.version == "1.0.92"

    loaded_legacy = sorted(
        name for name in sys.modules
        if name.startswith("app.runtime_entry_v")
    )
    assert loaded_legacy == []

    paths = {getattr(route, "path", None) for route in runtime.app.router.routes}
    for required in {
        "/ui",
        "/control/status",
        "/control/mode/{mode}",
        "/actuator/status",
        "/actuator/preflight",
        "/actuator/arm",
        "/actuator/physical-cap/status",
        "/engines",
        "/engines/selector/status",
        "/engines/neural/status",
        "/engines/adaptive/status",
        "/optimizer/replanning/status",
        "/optimizer/contract/status",
        "/economics/status",
        "/settings/status",
    }:
        assert required in paths
