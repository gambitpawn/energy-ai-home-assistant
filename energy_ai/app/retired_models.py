"""Central fail-closed retirement policy for unused learned battery engines.

This does not affect the adaptive deterministic model or PV/load forecasters.
No retirement code may ever remove history or files outside named retired models.
"""
from __future__ import annotations

import shutil
from pathlib import Path

RETIRED_ENGINE_IDS = frozenset({"neural_v1", "gradient_v1", "hybrid_v1"})
RETIRED_MODEL_FILES = (
    "neural_v1.joblib",
    "neural_v1.json",
    "neural_v1_qualification.json",
    "neural_auto_status.json",
    "gradient_v1.joblib",
    "gradient_v1.json",
    "gradient_v1_qualification.json",
    "gradient_v1_auto_status.json",
)
RETIRED_MODEL_DIRS = ("neural_v1_versions", "gradient_v1_versions")


def is_retired(engine_id: str | None) -> bool:
    return str(engine_id or "") in RETIRED_ENGINE_IDS


def remove_retired_model_artifacts(model_dir: Path = Path("/data/models")) -> dict:
    """Delete only recognized inactive model artifacts during cold startup.

    Called before uvicorn loads any model or runtime selector. Does not touch
    neural_training_sample: historical rows are intentionally retained.
    """
    deleted: list[str] = []
    for name in RETIRED_MODEL_FILES:
        path = model_dir / name
        if path.is_symlink():
            path.unlink()
            deleted.append(name)
        elif path.is_file():
            path.unlink()
            deleted.append(name)
    for name in RETIRED_MODEL_DIRS:
        path = model_dir / name
        if path.is_symlink():
            path.unlink()
            deleted.append(name)
        elif path.is_dir():
            shutil.rmtree(path)
            deleted.append(name + "/")
    return {"retired_engines": sorted(RETIRED_ENGINE_IDS), "deleted_artifacts": deleted}


def install_retired_selector_guard() -> None:
    """Exclude retired engines from auto-ranking and reset stale incumbents."""
    from . import model_selector as ms
    from . import model_selector_robust as robust

    original_candidates = robust._candidate_engines
    original_ensure_state = robust._ensure_robust_state

    def active_candidates(context: str, selected_engine: str) -> list[str]:
        return [x for x in original_candidates(context, selected_engine) if not is_retired(x)]

    def safe_state(cfg: dict) -> dict:
        state = original_ensure_state(cfg)
        if is_retired(state.get("selected_engine_id")):
            return robust._set_selected_model(
                cfg,
                ms.BASELINE_ENGINE_ID,
                robust.BASELINE_MODEL_KEY,
                "Retired learned engine replaced by deterministic baseline",
                "retired_engine_fallback",
                {"retired_engine_id": state.get("selected_engine_id")},
            )
        return state

    robust._candidate_engines = active_candidates
    robust._ensure_robust_state = safe_state
