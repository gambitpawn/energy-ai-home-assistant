from __future__ import annotations

import importlib
import sys


def test_consolidated_runtime_imports_without_versioned_entry_chain(tmp_path):
    # The add-on normally persists the current economics learning epoch under
    # /data. CI deliberately has no writable Home Assistant add-on mount, so
    # redirect only that persistence path; production code remains unchanged.
    from app import price_economics_runtime

    price_economics_runtime.ECONOMICS_EPOCH_PATH = tmp_path / "economics_model_epoch.json"

    runtime = importlib.import_module("app.runtime")

    assert runtime.RUNTIME_BUILD == "1.0.94"
    assert runtime.app.version == "1.0.94"

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
        "/actuator/timing/status",
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
