from __future__ import annotations

import json
from pathlib import Path

from . import runtime_entry_v183 as runtime
from .settings_store import load_setting_overrides
from .ui_v183_fix import install_ui_v183_fix

app = runtime.app
core = runtime.core
RUNTIME_BUILD = "1.0.83"
OPTIONS_PATH = Path("/data/options.json")


def _sauna_default_duration() -> int:
    raw = None
    overrides = load_setting_overrides()
    if "sauna_default_duration_minutes" in overrides:
        raw = overrides["sauna_default_duration_minutes"]
    if raw is None:
        try:
            opts = json.loads(OPTIONS_PATH.read_text(encoding="utf-8")) if OPTIONS_PATH.exists() else {}
            if isinstance(opts, dict):
                raw = opts.get("sauna_default_duration_minutes")
        except Exception:
            raw = None
    try:
        return max(15, min(360, int(raw if raw is not None else 120)))
    except Exception:
        return 120


# The endpoint in runtime_entry_v183 resolves this global function at request
# time, so replacing it here gives the final precedence without duplicating the
# control routes: DB override -> HA add-on option -> 120 min default.
runtime._sauna_default_duration = _sauna_default_duration
install_ui_v183_fix(app)

core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
