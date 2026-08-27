from __future__ import annotations

import json
from datetime import datetime

from fastapi import Query

from . import neural_auto
from . import neural_features as neural_features_module
from . import neural_training as neural_training_module
from . import neural_training_v2
from . import runtime_entry_v171
from . import runtime_entry_v172
from .engine_input_v2 import input_from_optimizer_plan_v2
from .neural_features import FEATURE_SCHEMA, feature_metadata
from .neural_teacher_v2 import LABEL_SOURCE_V2, perfect_information_teacher_v2
from .runtime_entry_v174 import app, core
from .tariff_scenarios import LOCAL_TZ, _calendar_active

RUNTIME_BUILD = "1.0.75"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD

# Feature schemas are immutable datasets. v1 samples remain in SQLite for audit,
# but only FEATURE_SCHEMA rows count toward v2 readiness/training.
neural_training_v2.install_into_v1_module()
neural_training_module._perfect_information_teacher = perfect_information_teacher_v2
neural_training_module.LABEL_SOURCE = LABEL_SOURCE_V2

# An incompatible active model must not be used after a feature-schema migration.
# Version archives remain untouched under /data/models/neural_v1_versions.
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

# Existing routes resolve these module globals at call time. Both baseline and
# neural challenger therefore receive the same generalized v2 information vintage.
runtime_entry_v171.input_from_optimizer_plan = input_from_optimizer_plan_v2
runtime_entry_v171.build_training_samples = neural_training_v2.build_training_samples
runtime_entry_v172.training_maturity_status = neural_training_v2.training_maturity_status

# neural_auto imported callable references in v1.0.74, so redirect them explicitly.
neural_auto.build_training_samples = neural_training_v2.build_training_samples
neural_auto.sample_count = neural_training_v2.sample_count
neural_auto.train_model = neural_training_module.train_model
neural_auto.model_status = neural_training_module.model_status

# Tariff calendars use local civil time. Horizon timestamps are UTC in the common
# contract, so convert before evaluating active tariff windows.
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


@app.get("/engines/neural/features", tags=["engines-neural"], summary="Current generalized neural feature schema")
async def neural_feature_schema():
    return {
        **feature_metadata(),
        "label_source": LABEL_SOURCE_V2,
        "installation_profile_included": True,
        "demand_tariff_state_included": True,
        "tariff_teacher_when_active": "tariff_shadow_milp_v1",
        "schema_migration": "older feature-schema samples are retained but excluded from current-model training",
        "physical_writes_enabled": False,
    }


@app.get("/engines/neural/input/latest", tags=["engines-neural"], summary="Latest generalized engine input used by neural v2 features")
async def neural_input_latest(include_horizon: bool = Query(False)):
    from .optimizer_store import latest_plan
    plan = latest_plan(500)
    engine_input = input_from_optimizer_plan_v2(plan, core.cfg)
    return engine_input.as_dict(include_horizon=include_horizon)


app.openapi_schema = None
