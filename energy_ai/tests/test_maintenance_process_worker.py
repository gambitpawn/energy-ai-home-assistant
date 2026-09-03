from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI

import app.maintenance_coordination as mc

ROOT = Path(__file__).resolve().parents[1]


def _probe_worker() -> dict[str, object]:
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "nice": os.nice(0),
        "limits": {
            key: os.environ.get(key)
            for key in ("LOKY_MAX_CPU_COUNT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
        },
    }


def _reset_worker_state():
    mc.shutdown_process_worker(grace_seconds=0.1)
    mc._PROCESS_REQUIRED = False
    mc._STATE.update({
        "execution_mode": "thread_fallback_before_process_install",
        "worker_pid": None,
        "worker_nice": None,
        "worker_started_at": None,
        "worker_error": None,
        "running": None,
        "last_error": None,
    })


def test_heavy_job_executes_in_distinct_low_priority_process():
    if os.name != "posix":
        return
    _reset_worker_state()
    try:
        state = mc.install_process_worker(startup_timeout_seconds=5)
        assert state["execution_mode"] == "dedicated_process"
        assert state["worker_alive"] is True
        result = asyncio.run(mc.run_low_priority("probe", _probe_worker))
        assert result["pid"] != os.getpid()
        assert result["pid"] == state["worker_pid"]
        assert int(result["nice"]) >= 10
        # CI may not inherit run.sh, but the child itself must never widen a
        # configured limit. Production gets all four values from run.sh.
        for value in result["limits"].values():
            assert value in {None, "2"}
    finally:
        _reset_worker_state()


def test_worker_remains_single_and_jobs_are_serialized_by_existing_lock():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    assert "ctx.Process(" in source
    assert 'name="energy-ai-maintenance"' in source
    assert "async with _LOW_PRIORITY_LOCK" in source
    assert "single_low_priority_maintenance_process_v2" in source
    assert "ProcessPoolExecutor" not in source


def test_worker_is_forked_after_model_patches_but_before_persistent_lifecycle_wrapper():
    source = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    worker = source.index("MAINTENANCE_PROCESS = install_process_worker(app=app)")
    last_model_patch = source.index("install_gradient_runtime_patch(base.core.cfg)")
    persistent = source.index("PERSISTENT_OPERATING_MODE = install_persistent_operating_mode(")
    assert last_model_patch < worker < persistent


def test_worker_shutdown_is_inside_persistent_clean_shutdown_order():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    operator = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(shutdown_process_worker)" in source
    assert operator.index("install_process_worker(app=app)") < operator.index("install_persistent_operating_mode(")


def test_broken_worker_fails_closed_instead_of_running_heavy_work_in_control_process():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    required_block = source[source.index("if _PROCESS_REQUIRED:"):source.index("def status()")]
    assert "await _run_in_process" in required_block
    assert "await asyncio.to_thread(fn" not in required_block.split("else:", 1)[0]
    assert "process_unavailable_fail_closed" in source


def test_parent_death_signal_and_two_core_limits_are_reinforced_in_child():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    assert "libc.prctl(1, signal.SIGTERM)" in source
    assert "os.nice(10)" in source
    for key in ("LOKY_MAX_CPU_COUNT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        assert key in source


def test_lifespan_install_does_not_start_or_run_a_maintenance_job():
    # Installation may fork the idle worker synchronously before the event loop,
    # but it must not invoke any training/evaluation function during app startup.
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    install = source[source.index("def install_process_worker"):source.index("def shutdown_process_worker")]
    assert "fn(" not in install
    assert "run_low_priority(" not in install
