from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_caps_parallel_compute_to_two_threads():
    run_sh = (ROOT / "run.sh").read_text(encoding="utf-8")
    for name in (
        "LOKY_MAX_CPU_COUNT",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
    ):
        assert f"export {name}=2" in run_sh


def test_compute_cap_is_applied_before_uvicorn_starts():
    run_sh = (ROOT / "run.sh").read_text(encoding="utf-8")
    uvicorn_pos = run_sh.index("exec /opt/energy-ai/venv/bin/uvicorn")
    for name in (
        "LOKY_MAX_CPU_COUNT",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
    ):
        assert run_sh.index(f"export {name}=2") < uvicorn_pos


def test_compute_cap_does_not_change_per_lane_maintenance_serialization_policy():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    assert "_LANE_LOCKS = {lane: asyncio.Lock() for lane in _LANES}" in source
    assert "async with _LANE_LOCKS[lane]" in source
    assert "supervised_disposable_worker" in source
    assert "_apply_low_priority_limits()" in source
