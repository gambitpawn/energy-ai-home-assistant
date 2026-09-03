from __future__ import annotations

import asyncio
import ctypes
import multiprocessing as mp
import os
import queue
import signal
import threading
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

_LOW_PRIORITY_LOCK = asyncio.Lock()
_PROCESS = None
_JOB_QUEUE = None
_RESULT_QUEUE = None
_PROCESS_REQUIRED = False
_STATE: dict[str, Any] = {
    "policy": "single_low_priority_maintenance_process_v2",
    "execution_mode": "thread_fallback_before_process_install",
    "worker_pid": None,
    "worker_nice": None,
    "worker_started_at": None,
    "worker_error": None,
    "running": None,
    "started_at": None,
    "last_completed": None,
    "last_completed_at": None,
    "last_error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_parent_death_signal() -> None:
    """Ask Linux to terminate the maintenance worker if the uvicorn parent dies."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG = 1
    except Exception:
        pass


def _worker_main(job_queue, result_queue) -> None:
    """Single long-lived low-priority process for CPU/SQLite-heavy jobs."""
    parent_pid = os.getppid()
    _set_parent_death_signal()
    if os.getppid() != parent_pid or os.getppid() == 1:
        return

    nice_value = None
    try:
        os.nice(10)
        nice_value = os.nice(0)
    except Exception:
        pass

    # run.sh sets these before NumPy/sklearn are imported, which is what actually
    # constrains already-initialized native pools. Reassert the same ceiling in
    # the child so subprocesses spawned by loky/joblib inherit the limit too.
    for key in ("LOKY_MAX_CPU_COUNT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[key] = "2"

    result_queue.put({
        "kind": "ready",
        "pid": os.getpid(),
        "nice": nice_value,
        "started_at": _now(),
    })

    while True:
        job = job_queue.get()
        if job is None:
            return
        job_id, label, fn, args, kwargs = job
        try:
            value = fn(*args, **kwargs)
            result_queue.put({
                "kind": "result",
                "job_id": job_id,
                "label": label,
                "ok": True,
                "value": value,
            })
        except BaseException as exc:
            result_queue.put({
                "kind": "result",
                "job_id": job_id,
                "label": label,
                "ok": False,
                "error": repr(exc),
                "traceback": traceback.format_exc(limit=40),
            })


def _queue_get(q, timeout: float):
    return q.get(True, timeout)


def _queue_put(q, item, timeout: float) -> None:
    q.put(item, True, timeout)


def install_process_worker(*, app=None, startup_timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Fork one maintenance worker before the runtime event loop starts.

    Forking here is deliberate: runtime_operator calls this only after all model
    and selector monkey-patches are installed, but before FastAPI lifespan tasks
    and worker threads start. The child therefore inherits the exact maintenance
    implementation used by the parent without forking an already-threaded
    process. If startup fails, maintenance fails closed rather than falling back
    to heavy in-process computation; planner/actuator/watchdog startup continues.
    """
    global _PROCESS, _JOB_QUEUE, _RESULT_QUEUE, _PROCESS_REQUIRED
    if _PROCESS is not None and _PROCESS.is_alive():
        return status()

    if os.name != "posix":
        _STATE.update({
            "execution_mode": "thread_fallback_non_posix",
            "worker_error": "dedicated maintenance process requires POSIX fork",
        })
        return status()

    # Fork only during the intentionally single-threaded import/startup phase.
    # If some future dependency starts a Python thread during import, disabling
    # heavy maintenance is safer than forking a live threaded control process.
    if threading.active_count() != 1:
        _PROCESS_REQUIRED = True
        error = f"refusing maintenance fork with {threading.active_count()} active Python threads"
        _STATE.update({
            "execution_mode": "process_unavailable_fail_closed",
            "worker_pid": None,
            "worker_error": error,
        })
        return status()

    _PROCESS_REQUIRED = True
    try:
        ctx = mp.get_context("fork")
        job_queue = ctx.Queue(maxsize=2)
        result_queue = ctx.Queue(maxsize=2)
        process = ctx.Process(
            target=_worker_main,
            args=(job_queue, result_queue),
            name="energy-ai-maintenance",
            daemon=False,
        )
        process.start()
        ready = result_queue.get(True, max(1.0, float(startup_timeout_seconds)))
        if not isinstance(ready, dict) or ready.get("kind") != "ready":
            raise RuntimeError(f"maintenance worker returned invalid startup message: {ready!r}")
        _PROCESS, _JOB_QUEUE, _RESULT_QUEUE = process, job_queue, result_queue
        _STATE.update({
            "execution_mode": "dedicated_process",
            "worker_pid": int(ready.get("pid") or process.pid or 0),
            "worker_nice": ready.get("nice"),
            "worker_started_at": ready.get("started_at") or _now(),
            "worker_error": None,
        })
    except Exception as exc:
        try:
            if 'process' in locals() and process.is_alive():
                process.terminate()
                process.join(timeout=2)
        except Exception:
            pass
        _PROCESS = _JOB_QUEUE = _RESULT_QUEUE = None
        _STATE.update({
            "execution_mode": "process_unavailable_fail_closed",
            "worker_pid": None,
            "worker_error": repr(exc),
        })

    if app is not None and not getattr(app.state, "maintenance_worker_lifespan_installed", False):
        app.state.maintenance_worker_lifespan_installed = True
        base_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def maintenance_worker_lifespan(application):
            async with base_lifespan(application) as lifespan_state:
                try:
                    yield lifespan_state
                finally:
                    # Stop/terminate CPU work before outer lifecycle wrappers mark
                    # the application cleanly shut down. This avoids a long model
                    # job delaying an add-on update or holding SQLite during the
                    # final operating-mode persistence writes.
                    await asyncio.to_thread(shutdown_process_worker)

        app.router.lifespan_context = maintenance_worker_lifespan

    return status()


def shutdown_process_worker(grace_seconds: float = 2.0) -> None:
    global _PROCESS, _JOB_QUEUE, _RESULT_QUEUE
    process = _PROCESS
    job_queue = _JOB_QUEUE
    if process is None:
        return
    try:
        if process.is_alive() and job_queue is not None:
            try:
                job_queue.put(None, False)
            except Exception:
                pass
            process.join(timeout=max(0.0, float(grace_seconds)))
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
    finally:
        for q in (_JOB_QUEUE, _RESULT_QUEUE):
            try:
                q.close()
                q.join_thread()
            except Exception:
                pass
        _PROCESS = _JOB_QUEUE = _RESULT_QUEUE = None
        _STATE.update({
            "execution_mode": "process_stopped",
            "worker_pid": None,
        })


async def _run_in_process(label: str, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    process, job_queue, result_queue = _PROCESS, _JOB_QUEUE, _RESULT_QUEUE
    if process is None or job_queue is None or result_queue is None or not process.is_alive():
        error = _STATE.get("worker_error") or "maintenance worker is not alive"
        _STATE.update({"execution_mode": "process_unavailable_fail_closed", "worker_error": str(error)})
        raise RuntimeError(f"Dedicated maintenance process unavailable: {error}")

    job_id = uuid4().hex
    await asyncio.to_thread(_queue_put, job_queue, (job_id, str(label), fn, args, kwargs), 5.0)
    while True:
        if not process.is_alive():
            error = f"maintenance worker exited with code {process.exitcode}"
            _STATE.update({"execution_mode": "process_unavailable_fail_closed", "worker_error": error})
            raise RuntimeError(error)
        try:
            message = await asyncio.to_thread(_queue_get, result_queue, 1.0)
        except queue.Empty:
            continue
        if not isinstance(message, dict) or message.get("kind") != "result":
            continue
        if message.get("job_id") != job_id:
            continue
        if message.get("ok"):
            return message.get("value")
        error = str(message.get("error") or "maintenance job failed")
        detail = str(message.get("traceback") or "")
        raise RuntimeError(f"{error}\n{detail}".rstrip())


async def run_low_priority(
    label: str,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Serialize heavy maintenance and isolate it from the control process."""
    async with _LOW_PRIORITY_LOCK:
        _STATE.update({"running": str(label), "started_at": _now(), "last_error": None})
        try:
            if _PROCESS_REQUIRED:
                result = await _run_in_process(str(label), fn, tuple(args), dict(kwargs))
            else:
                result = await asyncio.to_thread(fn, *args, **kwargs)
            _STATE.update({"last_completed": str(label), "last_completed_at": _now()})
            return result
        except Exception as exc:
            _STATE["last_error"] = repr(exc)
            raise
        finally:
            _STATE["running"] = None


def status() -> dict[str, Any]:
    result = dict(_STATE)
    process = _PROCESS
    result["worker_alive"] = bool(process is not None and process.is_alive())
    result["worker_exitcode"] = None if process is None else process.exitcode
    result["process_required"] = bool(_PROCESS_REQUIRED)
    return result
