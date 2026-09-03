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


def test_compute_cap_does_not_change_maintenance_serialization_policy():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    assert "_LOW_PRIORITY_LOCK = asyncio.Lock()" in source
    assert "async with _LOW_PRIORITY_LOCK" in source
    assert "await asyncio.to_thread(fn, *args, **kwargs)" in source
