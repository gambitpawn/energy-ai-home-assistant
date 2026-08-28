from __future__ import annotations

from . import runtime as base
from .operator_mode_control import install_operator_mode_control

app = base.app

install_operator_mode_control(
    app=app,
    core=base.core,
    actuator=base.ACTUATOR,
    adapter=base.ADAPTER,
    timing_scheduler=base.ACTUATOR_TIMING,
    selector_module=base.selector,
    candidate_from_selection=base._candidate_from_selection,
)

# Keep the current 1.0.94 release identity. This layer changes operator UX only.
app.version = base.RUNTIME_BUILD
app.openapi_schema = None
