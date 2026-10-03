from __future__ import annotations

import asyncio
import ctypes
import multiprocessing as mp
import os
import signal
import threading
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from .diagnostics_store import finish_maintenance_run, start_maintenance_run

LANE_HEAVY = "heavy"
LANE_EVALUATION = "evaluation"
_LANES = (LANE_HEAVY, LANE_EVALUATION)
_DEFAULT_TIMEOUT_SECONDS = {
    LANE_HEAVY: 1800.0,
    LANE_EVALUATION: 600.0,
}
_LANE_LOCKS = {lane: asyncio.Lock() for lane in _LANES}
_SUPERVISORS: dict[str, Any] = {lane: None for lane in _LANES}
_COMMAND_CONNECTIONS: dict[str, Any] = {lane: None for lane in _LANES}
_RESULT_CONNECTIONS: dict[str, Any] = {lane: None for lane in _LANES}
_PROCESS_REQUIRED = False


class MaintenanceJobTimeoutError(RuntimeError):
    """Raised when a maintenance job exceeds its configured runtime deadline."""


def _lane_state() -> dict[str, Any]:
    return {
        "execution_mode": "thread_fallback_before_process_install",
        "supervisor_pid": None,
        "supervisor_started_at": None,
        "supervisor_error": None,
        "running": None,
        "started_at": None,
        "deadline_at": None,
        "timeout_seconds": None,
        "active_job_pid": None,
        "last_job_worker_pid": None,
        "last_completed": None,
        "last_completed_at": None,
        "last_error": None,
        "last_timeout": None,
        "restart_count": 0,
    }


_STATE: dict[str, Any] = {
    "policy": "dual_lane_supervised_job_workers_v3",
    "watchdog_policy": "per_job_deadline_kill_and_fresh_worker",
    "lanes": {lane: _lane_state() for lane in _LANES},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_after(seconds: float) -> str:
    return datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + max(0.0, float(seconds)),
        tz=timezone.utc,
    ).isoformat()


def _set_parent_death_signal() -> None:
    """Ask Linux to terminate a child if its direct parent dies."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG = 1
    except Exception:
        pass


def _apply_low_priority_limits(thread_limit: int = 2) -> int | None:
    nice_value = None
    try:
        os.nice(10)
        nice_value = os.nice(0)
    except Exception:
        pass
    limit = str(max(1, int(thread_limit)))
    for key in ("LOKY_MAX_CPU_COUNT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[key] = limit
    return nice_value


def _job_main(result_conn, fn, args, kwargs) -> None:
    """Execute exactly one maintenance job in a disposable worker process."""
    _set_parent_death_signal()
    _apply_low_priority_limits()
    try:
        value = fn(*args, **kwargs)
        result_conn.send({
            "ok": True,
            "value": value,
        })
    except BaseException as exc:
        try:
            result_conn.send({
                "ok": False,
                "error": repr(exc),
                "traceback": traceback.format_exc(limit=40),
            })
        except Exception:
            pass
    finally:
        try:
            result_conn.close()
        except Exception:
            pass


def _terminate_job_process(process, *, grace_seconds: float = 1.0) -> None:
    if process is None:
        return
    try:
        if process.is_alive():
            process.terminate()
            process.join(timeout=max(0.0, float(grace_seconds)))
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=1.0)
    except Exception:
        pass


def _supervisor_main(command_conn, result_conn, lane: str) -> None:
    """Supervise disposable job workers without ever blocking the runtime process.

    The supervisor is forked during single-threaded startup and remains single
    threaded. Each job is then forked from this safe supervisor. A timed-out job
    can therefore be killed and replaced without forking the already-threaded
    FastAPI/uvicorn parent process.
    """
    _set_parent_death_signal()
    nice_value = _apply_low_priority_limits()
    result_conn.send({
        "kind": "ready",
        "lane": lane,
        "pid": os.getpid(),
        "nice": nice_value,
        "started_at": _now(),
    })

    ctx = mp.get_context("fork")
    restart_count = 0
    while True:
        try:
            command = command_conn.recv()
        except EOFError:
            return
        if command is None:
            return

        job_id, label, fn, args, kwargs, timeout_seconds = command
        timeout_seconds = max(1.0, float(timeout_seconds))
        job_result_recv, job_result_send = ctx.Pipe(duplex=False)
        process = ctx.Process(
            target=_job_main,
            args=(job_result_send, fn, args, kwargs),
            name=f"energy-ai-maintenance-{lane}-job",
            daemon=False,
        )
        process.start()
        try:
            job_result_send.close()
        except Exception:
            pass

        result_conn.send({
            "kind": "started",
            "job_id": job_id,
            "lane": lane,
            "label": label,
            "pid": process.pid,
            "timeout_seconds": timeout_seconds,
        })

        deadline = time.monotonic() + timeout_seconds
        payload = None
        timed_out = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if job_result_recv.poll(min(1.0, remaining)):
                try:
                    payload = job_result_recv.recv()
                except EOFError:
                    payload = None
                break
            if not process.is_alive():
                break

        if timed_out:
            timed_out_pid = process.pid
            _terminate_job_process(process)
            restart_count += 1
            result_conn.send({
                "kind": "result",
                "job_id": job_id,
                "lane": lane,
                "label": label,
                "ok": False,
                "timeout": True,
                "timeout_seconds": timeout_seconds,
                "worker_pid": timed_out_pid,
                "restart_count": restart_count,
                "error": f"maintenance job exceeded {timeout_seconds:.1f}s deadline",
            })
        else:
            process.join(timeout=1.0)
            if process.is_alive():
                _terminate_job_process(process)
            if not isinstance(payload, dict):
                payload = {
                    "ok": False,
                    "error": f"maintenance job worker exited without a result (exitcode={process.exitcode})",
                }
            result_conn.send({
                "kind": "result",
                "job_id": job_id,
                "lane": lane,
                "label": label,
                "worker_pid": process.pid,
                "restart_count": restart_count,
                **payload,
            })

        try:
            job_result_recv.close()
        except Exception:
            pass


def _start_supervisor(lane: str, *, startup_timeout_seconds: float) -> None:
    global _SUPERVISORS, _COMMAND_CONNECTIONS, _RESULT_CONNECTIONS
    ctx = mp.get_context("fork")
    child_command_recv, parent_command_send = ctx.Pipe(duplex=False)
    parent_result_recv, child_result_send = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_supervisor_main,
        args=(child_command_recv, child_result_send, lane),
        name=f"energy-ai-maintenance-{lane}",
        daemon=False,
    )
    process.start()
    if not parent_result_recv.poll(max(1.0, float(startup_timeout_seconds))):
        _terminate_job_process(process)
        raise RuntimeError(f"{lane} maintenance supervisor did not become ready")
    ready = parent_result_recv.recv()
    if not isinstance(ready, dict) or ready.get("kind") != "ready":
        _terminate_job_process(process)
        raise RuntimeError(f"{lane} maintenance supervisor returned invalid startup message: {ready!r}")

    _SUPERVISORS[lane] = process
    _COMMAND_CONNECTIONS[lane] = parent_command_send
    _RESULT_CONNECTIONS[lane] = parent_result_recv
    _STATE["lanes"][lane].update({
        "execution_mode": "supervised_disposable_worker",
        "supervisor_pid": int(ready.get("pid") or process.pid or 0),
        "supervisor_started_at": ready.get("started_at") or _now(),
        "supervisor_error": None,
    })

    for conn in (child_command_recv, child_result_send):
        try:
            conn.close()
        except Exception:
            pass


def install_process_worker(*, app=None, startup_timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Install isolated maintenance supervisors before runtime threads start.

    Two lanes are created: the general heavy lane and a reserved optimizer-day
    evaluation lane. Supervisors are forked only during the intentionally
    single-threaded startup phase. They later fork disposable job workers, so a
    timed-out native/SQLite/model job can be killed and replaced without forking
    the already-threaded FastAPI process.
    """
    global _PROCESS_REQUIRED
    if all(_SUPERVISORS[lane] is not None and _SUPERVISORS[lane].is_alive() for lane in _LANES):
        return status()

    if os.name != "posix":
        for lane in _LANES:
            _STATE["lanes"][lane].update({
                "execution_mode": "thread_fallback_non_posix",
                "supervisor_error": "dedicated maintenance supervision requires POSIX fork",
            })
        return status()

    if threading.active_count() != 1:
        _PROCESS_REQUIRED = True
        error = f"refusing maintenance supervisor fork with {threading.active_count()} active Python threads"
        for lane in _LANES:
            _STATE["lanes"][lane].update({
                "execution_mode": "process_unavailable_fail_closed",
                "supervisor_pid": None,
                "supervisor_error": error,
            })
        return status()

    _PROCESS_REQUIRED = True
    try:
        for lane in _LANES:
            process = _SUPERVISORS[lane]
            if process is None or not process.is_alive():
                _start_supervisor(lane, startup_timeout_seconds=startup_timeout_seconds)
    except Exception as exc:
        shutdown_process_worker(grace_seconds=0.2)
        for lane in _LANES:
            _STATE["lanes"][lane].update({
                "execution_mode": "process_unavailable_fail_closed",
                "supervisor_pid": None,
                "supervisor_error": repr(exc),
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
                    await asyncio.to_thread(shutdown_process_worker)

        app.router.lifespan_context = maintenance_worker_lifespan

    return status()


def shutdown_process_worker(grace_seconds: float = 2.0) -> None:
    global _SUPERVISORS, _COMMAND_CONNECTIONS, _RESULT_CONNECTIONS
    for lane in _LANES:
        process = _SUPERVISORS[lane]
        command_conn = _COMMAND_CONNECTIONS[lane]
        if process is not None:
            try:
                if process.is_alive() and command_conn is not None:
                    try:
                        command_conn.send(None)
                    except Exception:
                        pass
                    process.join(timeout=max(0.0, float(grace_seconds)))
                if process.is_alive():
                    _terminate_job_process(process, grace_seconds=1.0)
            except Exception:
                pass
        for conn in (_COMMAND_CONNECTIONS[lane], _RESULT_CONNECTIONS[lane]):
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        _SUPERVISORS[lane] = None
        _COMMAND_CONNECTIONS[lane] = None
        _RESULT_CONNECTIONS[lane] = None
        _STATE["lanes"][lane].update({
            "execution_mode": "process_stopped",
            "supervisor_pid": None,
            "active_job_pid": None,
            "running": None,
            "deadline_at": None,
        })


def _validate_lane(lane: str) -> str:
    lane = str(lane or LANE_HEAVY)
    if lane not in _LANES:
        raise ValueError(f"unknown maintenance lane: {lane}")
    return lane


async def _run_in_process(
    lane: str,
    job_id: str,
    label: str,
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    timeout_seconds: float,
) -> Any:
    process = _SUPERVISORS[lane]
    command_conn = _COMMAND_CONNECTIONS[lane]
    result_conn = _RESULT_CONNECTIONS[lane]
    lane_state = _STATE["lanes"][lane]
    if process is None or command_conn is None or result_conn is None or not process.is_alive():
        error = lane_state.get("supervisor_error") or "maintenance supervisor is not alive"
        lane_state.update({
            "execution_mode": "process_unavailable_fail_closed",
            "supervisor_error": str(error),
        })
        raise RuntimeError(f"Dedicated {lane} maintenance supervisor unavailable: {error}")

    command = (job_id, str(label), fn, args, kwargs, float(timeout_seconds))
    try:
        await asyncio.wait_for(asyncio.to_thread(command_conn.send, command), timeout=5.0)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Timed out submitting {lane} maintenance job {label!r}") from exc

    while True:
        if not process.is_alive():
            error = f"{lane} maintenance supervisor exited with code {process.exitcode}"
            lane_state.update({
                "execution_mode": "process_unavailable_fail_closed",
                "supervisor_error": error,
            })
            raise RuntimeError(error)

        ready = await asyncio.to_thread(result_conn.poll, 1.0)
        if not ready:
            continue
        try:
            message = result_conn.recv()
        except EOFError as exc:
            raise RuntimeError(f"{lane} maintenance supervisor result pipe closed") from exc
        if not isinstance(message, dict) or message.get("job_id") != job_id:
            continue

        if message.get("kind") == "started":
            lane_state.update({
                "active_job_pid": message.get("pid"),
                "last_job_worker_pid": message.get("pid"),
                "timeout_seconds": float(message.get("timeout_seconds") or timeout_seconds),
            })
            continue
        if message.get("kind") != "result":
            continue

        lane_state["restart_count"] = int(message.get("restart_count") or lane_state.get("restart_count") or 0)
        lane_state["active_job_pid"] = None
        if message.get("timeout"):
            timeout_info = {
                "label": str(label),
                "at": _now(),
                "timeout_seconds": float(message.get("timeout_seconds") or timeout_seconds),
                "terminated_worker_pid": message.get("worker_pid"),
            }
            lane_state["last_timeout"] = timeout_info
            error = str(message.get("error") or "maintenance job timed out")
            raise MaintenanceJobTimeoutError(error)
        if message.get("ok"):
            return message.get("value")
        error = str(message.get("error") or "maintenance job failed")
        detail = str(message.get("traceback") or "")
        raise RuntimeError(f"{error}\n{detail}".rstrip())


async def run_low_priority(
    label: str,
    fn: Callable[..., Any],
    *args: Any,
    lane: str = LANE_HEAVY,
    timeout_seconds: float | None = None,
    **kwargs: Any,
) -> Any:
    """Run maintenance with isolation, serialization and a hard job deadline."""
    lane = _validate_lane(lane)
    timeout = float(
        _DEFAULT_TIMEOUT_SECONDS[lane]
        if timeout_seconds is None
        else max(1.0, float(timeout_seconds))
    )
    lane_state = _STATE["lanes"][lane]
    async with _LANE_LOCKS[lane]:
        job_id = uuid4().hex
        started_monotonic = time.monotonic()
        lane_state.update({
            "running": str(label),
            "started_at": _now(),
            "deadline_at": _iso_after(timeout),
            "timeout_seconds": timeout,
            "last_error": None,
            "last_job_worker_pid": None,
        })
        await asyncio.to_thread(
            start_maintenance_run,
            job_id,
            lane=lane,
            label=str(label),
            timeout_seconds=timeout,
            payload={"execution_mode": lane_state.get("execution_mode")},
        )
        try:
            if _PROCESS_REQUIRED:
                result = await _run_in_process(
                    lane,
                    job_id,
                    str(label),
                    fn,
                    tuple(args),
                    dict(kwargs),
                    timeout,
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(fn, *args, **kwargs),
                    timeout=timeout,
                )
            lane_state.update({
                "last_completed": str(label),
                "last_completed_at": _now(),
            })
            await asyncio.to_thread(
                finish_maintenance_run,
                job_id,
                status="completed",
                worker_pid=lane_state.get("last_job_worker_pid"),
                restart_count=int(lane_state.get("restart_count") or 0),
                duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            )
            return result
        except asyncio.TimeoutError as exc:
            error = MaintenanceJobTimeoutError(
                f"{lane} maintenance job {label!r} exceeded {timeout:.1f}s deadline"
            )
            lane_state["last_timeout"] = {
                "label": str(label),
                "at": _now(),
                "timeout_seconds": timeout,
                "terminated_worker_pid": None,
            }
            lane_state["last_error"] = repr(error)
            await asyncio.to_thread(
                finish_maintenance_run,
                job_id,
                status="timeout",
                worker_pid=lane_state.get("last_job_worker_pid"),
                restart_count=int(lane_state.get("restart_count") or 0),
                duration_seconds=max(0.0, time.monotonic() - started_monotonic),
                error=repr(error),
            )
            raise error from exc
        except MaintenanceJobTimeoutError as exc:
            lane_state["last_error"] = repr(exc)
            await asyncio.to_thread(
                finish_maintenance_run,
                job_id,
                status="timeout",
                worker_pid=lane_state.get("last_job_worker_pid"),
                restart_count=int(lane_state.get("restart_count") or 0),
                duration_seconds=max(0.0, time.monotonic() - started_monotonic),
                error=repr(exc),
            )
            raise
        except Exception as exc:
            lane_state["last_error"] = repr(exc)
            await asyncio.to_thread(
                finish_maintenance_run,
                job_id,
                status="failed",
                worker_pid=lane_state.get("last_job_worker_pid"),
                restart_count=int(lane_state.get("restart_count") or 0),
                duration_seconds=max(0.0, time.monotonic() - started_monotonic),
                error=repr(exc),
                traceback_text=traceback.format_exc(limit=40),
            )
            raise
        finally:
            lane_state.update({
                "running": None,
                "started_at": None,
                "deadline_at": None,
                "timeout_seconds": None,
                "active_job_pid": None,
            })


def status() -> dict[str, Any]:
    lanes: dict[str, Any] = {}
    for lane in _LANES:
        item = dict(_STATE["lanes"][lane])
        process = _SUPERVISORS[lane]
        item["supervisor_alive"] = bool(process is not None and process.is_alive())
        item["supervisor_exitcode"] = None if process is None else process.exitcode
        if item.get("started_at"):
            try:
                started = datetime.fromisoformat(str(item["started_at"]).replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                item["running_seconds"] = max(
                    0.0,
                    (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds(),
                )
            except Exception:
                item["running_seconds"] = None
        else:
            item["running_seconds"] = None
        lanes[lane] = item

    return {
        "policy": _STATE["policy"],
        "watchdog_policy": _STATE["watchdog_policy"],
        "process_required": bool(_PROCESS_REQUIRED),
        "healthy": all(
            (not _PROCESS_REQUIRED) or lane_state.get("supervisor_alive")
            for lane_state in lanes.values()
        ),
        "lanes": lanes,
    }
