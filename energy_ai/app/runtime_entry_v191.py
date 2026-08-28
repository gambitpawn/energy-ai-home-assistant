from __future__ import annotations

from . import runtime_entry_v190 as v190

app = v190.app
core = v190.core
RUNTIME_BUILD = "1.0.91"

# v1.0.91 is intentionally a routing-only hotfix. The ACTIVE preflight wrapper
# in runtime_entry_v188 now references the actual v1.0.87 actuator instance.
# Optimizer, selector, physical cap and Solinteg dispatch semantics are unchanged.
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
