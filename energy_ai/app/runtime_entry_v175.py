from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from fastapi import Query

from . import neural_features as neural_features_module
from . import neural_training as neural_training_module
from .db import DB_PATH
from .engine_input_v2 import input_from_optimizer_plan_v2
from .neural_features import FEATURE_SCHEMA, feature_metadata
from .neural_teacher_v2 import LABEL_SOURCE_V2, perfect_information_teacher_v2
from .runtime_entry_v174 import app, core
from .tariff_scenarios import LOCAL_TZ, _calendar_active

RUNTIME_BUILD = "1.0.75"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD


# --- Feature-schema v2 migration -------------------------------------------------
# Training samples are immutable observations. Different feature dimensions must
# never be mixed in one model fit, so only the active schema remains in the active
# sample table. Model revision archives remain preserved under /data/models.
try:
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "DELETE FROM neural_training_sample WHERE feature_schema<>?",
            (FEATURE_SCHEMA,),
        )
except sqlite3.OperationalError:
    pass

try:
    meta_path = neural_training_module.MODEL_META_PATH
    model_path = neural_training_module.MODEL_PATH
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("feature_schema") != FEATURE_SCHEMA:
            if model_path.exists():
                model_path.unlink()
            meta_path.unlink()
except Exception:
    pass


# Historical pre-v2 contract vintages did not freeze the generalized installation
# profile. For training, ignore those few contract rows and rebuild older v3.5 plan
# snapshots through the causal v2 builder instead.
_original_payload_builder = neural_training_module._engine_input_from_payload


def _engine_input_from_payload_v2_only(payload):
    item = _original_payload_builder(payload)
    if (item.source or {}).get("input_profile") != "generalized_installation_tariff_v2":
        raise ValueError("pre-v2 contract vintage excluded from neural v2 training")
    return item


neural_training_module._engine_input_from_payload = _engine_input_from_payload_v2_only
neural_training_module.input_from_optimizer_plan = input_from_optimizer_plan_v2
neural_training_module._perfect_information_teacher = perfect_information_teacher_v2
neural_training_module.LABEL_SOURCE = LABEL_SOURCE_V2


# neural_features v2 stores UTC horizon timestamps, while tariff calendars are local.
# Patch the helper at module-global level so all existing vectorize imports use the
# same Europe/Stockholm calendar semantics without duplicating vectorization code.
def _tariff_active_fraction_local(chunk, tariff, enabled):
    if not enabled or not chunk:
        return 0.0
    active = 0
    for row in chunk:
        try:
            local = datetime.fromisoformat(str(row["start"]).replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            if _calendar_active(local, tariff, False):
                active += 1
        except Exception:
            continue
    return active / float(len(chunk))


neural_features_module._tariff_active_fraction = _tariff_active_fraction_local


@app.get(
    "/engines/neural/features",
    tags=["engines-neural"],
    summary="Current generalized neural feature schema",
)
async def neural_feature_schema():
    return {
        **feature_metadata(),
        "label_source": LABEL_SOURCE_V2,
        "installation_profile_included": True,
        "demand_tariff_state_included": True,
        "tariff_teacher_when_active": "tariff_shadow_milp_v1",
        "physical_writes_enabled": False,
    }


@app.get(
    "/engines/neural/input/latest",
    tags=["engines-neural"],
    summary="Latest generalized engine input used by neural v2 features",
)
async def neural_input_latest(include_horizon: bool = Query(False)):
    from .optimizer_store import latest_plan
    plan = latest_plan(500)
    engine_input = input_from_optimizer_plan_v2(plan, core.cfg)
    return engine_input.as_dict(include_horizon=include_horizon)


app.openapi_schema = None
