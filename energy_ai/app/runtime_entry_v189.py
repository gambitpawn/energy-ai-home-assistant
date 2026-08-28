from __future__ import annotations

from . import runtime_entry_v188 as v188
from .optimizer_contract_v189 import contract_status, install_optimizer_interval_contract_patch

app = v188.app
core = v188.core
RUNTIME_BUILD = "1.0.89"

OPTIMIZER_INTERVAL_CONTRACT = install_optimizer_interval_contract_patch()


@app.get(
    "/optimizer/contract/status",
    tags=["optimizer"],
    summary="Optimizer interval-result compatibility status",
)
async def optimizer_contract_status_v189():
    return {
        "runtime_build": RUNTIME_BUILD,
        **contract_status(),
    }


core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
