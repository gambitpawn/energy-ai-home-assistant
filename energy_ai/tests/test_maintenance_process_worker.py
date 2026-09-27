from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
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


def _sleep_probe(seconds: float) -> dict[str, object]:
    time.sleep(float(seconds))
    return _probe_worker()


def _reset_worker_state():
    mc.shutdown_process_worker(grace_seconds=0.1)
    mc._PROCESS_REQUIRED = False
    mc._STATE["lanes"] = {lane: mc._lane_state() for lane in (mc.LANE_HEAVY, mc.LANE_EVALUATION)}


def test_heavy_job_executes_in_distinct_low_priority_process():
    if os.name != "posix":
        return
    _reset_worker_state()
    try:
        state = mc.install_process_worker(startup_timeout_seconds=5)
        heavy = state["lanes"][mc.LANE_HEAVY]
        assert heavy["execution_mode"] == "supervised_disposable_worker"
        assert heavy["supervisor_alive"] is True
        result = asyncio.run(mc.run_low_priority("probe", _probe_worker))
        assert result["pid"] != os.getpid()
        assert result["pid"] != heavy["supervisor_pid"]
        assert int(result["nice"]) >= 10
        for value in result["limits"].values():
            assert value == "2"
    finally:
        _reset_worker_state()


def test_timeout_kills_job_worker_and_next_job_uses_fresh_process():
    if os.name != "posix":
        return
    _reset_worker_state()
    try:
        mc.install_process_worker(startup_timeout_seconds=5)
        with pytest.raises(mc.MaintenanceJobTimeoutError):
            asyncio.run(
                mc.run_low_priority(
                    "intentional_timeout",
                    _sleep_probe,
                    2.0,
                    timeout_seconds=1.0,
                )
            )
        timed_out = mc.status()["lanes"][mc.LANE_HEAVY]
        assert timed_out["restart_count"] == 1
        assert timed_out["last_timeout"]["label"] == "intentional_timeout"
        old_pid = timed_out["last_timeout"]["terminated_worker_pid"]

        result = asyncio.run(mc.run_low_priority("probe_after_timeout", _probe_worker, timeout_seconds=5))
        assert result["pid"] != old_pid
        assert mc.status()["lanes"][mc.LANE_HEAVY]["supervisor_alive"] is True
    finally:
        _reset_worker_state()


def test_worker_lanes_are_independent_and_serialized_per_lane():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    assert 'LANE_HEAVY = "heavy"' in source
    assert 'LANE_EVALUATION = "evaluation"' in source
    assert "_LANE_LOCKS" in source
    assert "async with _LANE_LOCKS[lane]" in source
    assert "dual_lane_supervised_job_workers_v3" in source
    assert "energy-ai-maintenance-{lane}-job" in source
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


def test_broken_supervisor_fails_closed_instead_of_running_heavy_work_in_control_process():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    required_block = source[source.index("if _PROCESS_REQUIRED:"):source.index("def status()")]
    assert "await _run_in_process" in required_block
    assert "await asyncio.to_thread(fn" not in required_block.split("else:", 1)[0]
    assert "process_unavailable_fail_closed" in source


def test_parent_death_signal_and_two_core_limits_are_reinforced_in_job_worker():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    assert "libc.prctl(1, signal.SIGTERM)" in source
    assert "os.nice(10)" in source
    for key in ("LOKY_MAX_CPU_COUNT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        assert key in source


def test_lifespan_install_does_not_start_a_maintenance_job():
    source = (ROOT / "app" / "maintenance_coordination.py").read_text(encoding="utf-8")
    install = source[source.index("def install_process_worker"):source.index("def shutdown_process_worker")]
    assert "run_low_priority(" not in install


def test_runtime_exposes_maintenance_status_route():
    source = (ROOT / "app" / "runtime_routes.py").read_text(encoding="utf-8")
    assert 'from .maintenance_coordination import status as maintenance_status' in source
    assert '@app.get("/maintenance/status"' in source
    assert "**maintenance_status()" in source
