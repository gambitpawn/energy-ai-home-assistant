import asyncio
import math
from datetime import datetime, timedelta, timezone

from .db import insert_raw, rebuild_15m_bucket
from .ha import HomeAssistantClient


def quarter_bucket(ts):
    d=datetime.fromisoformat(ts.replace("Z","+00:00")).astimezone(timezone.utc)
    m=(d.minute//15)*15
    return d.replace(minute=m,second=0,microsecond=0)


class Collector:
    """Poll and persist Home Assistant state only.

    The collector's process-local latest snapshot is the safety source used by
    the actuator. SQLite ``raw_state`` is a downstream historical copy. Forecast
    generation/evaluation and persistence must therefore never delay publishing
    the process-local snapshot or the next Home Assistant poll.

    Forecast maintenance is already owned by the separate maintenance loop in
    ``main._forecast_maintenance_loop`` / ``runtime_maintenance``.
    """

    def __init__(self,cfg):
        self.cfg=cfg
        self.ha=HomeAssistantClient(cfg)
        self.poll_seconds=int(cfg.get("collector",{}).get("poll_seconds",60))
        self.latest=None
        self.last_error=None
        self.last_persistence_error=None
        self.running=False
        self.persistence_queue_max=16
        self._persistence_queue=asyncio.Queue(maxsize=self.persistence_queue_max)
        self._persistence_task=None
        self.persistence_dropped=0
        self.persistence_written=0
        self.persistence_retried=0

    def _ensure_runtime_state(self):
        # Tests and a few legacy construction paths instantiate Collector via
        # __new__. Keep the safety state self-healing without weakening runtime.
        if not hasattr(self,"last_persistence_error"): self.last_persistence_error=None
        if not hasattr(self,"persistence_queue_max"): self.persistence_queue_max=16
        if not hasattr(self,"_persistence_queue"): self._persistence_queue=asyncio.Queue(maxsize=self.persistence_queue_max)
        if not hasattr(self,"_persistence_task"): self._persistence_task=None
        if not hasattr(self,"persistence_dropped"): self.persistence_dropped=0
        if not hasattr(self,"persistence_written"): self.persistence_written=0
        if not hasattr(self,"persistence_retried"): self.persistence_retried=0

    def _persist_state(self,state):
        payload=state.model_dump()
        insert_raw(state.collected_at,payload)
        bucket_start=quarter_bucket(state.collected_at)
        bucket_end=bucket_start+timedelta(minutes=15)
        expected_samples=max(1,round(900/self.poll_seconds))
        rebuild_15m_bucket(
            bucket_start.isoformat(),
            bucket_end.isoformat(),
            expected_samples=expected_samples,
        )

    async def _persistence_loop(self):
        try:
            while True:
                state,attempt=await self._persistence_queue.get()
                try:
                    await asyncio.to_thread(self._persist_state,state)
                    self.persistence_written+=1
                    self.last_persistence_error=None
                except Exception as exc:
                    # Persistence is deliberately not allowed to make the live
                    # safety snapshot stale. A later poll still proceeds.
                    self.last_persistence_error=repr(exc)
                    if attempt < 3:
                        if self._persistence_queue.full():
                            try:
                                self._persistence_queue.get_nowait()
                                self._persistence_queue.task_done()
                                self.persistence_dropped+=1
                            except asyncio.QueueEmpty:
                                pass
                        self._persistence_queue.put_nowait((state,attempt+1))
                        self.persistence_retried+=1
                    else:
                        self.persistence_dropped+=1
                finally:
                    self._persistence_queue.task_done()
        except asyncio.CancelledError:
            raise

    def _ensure_persistence_worker(self):
        self._ensure_runtime_state()
        if self._persistence_task is None or self._persistence_task.done():
            self._persistence_task=asyncio.create_task(
                self._persistence_loop(),name="energy-ai-state-persistence"
            )

    def _queue_state(self,state):
        self._ensure_persistence_worker()
        if self._persistence_queue.full():
            try:
                self._persistence_queue.get_nowait()
                self._persistence_queue.task_done()
                self.persistence_dropped+=1
            except asyncio.QueueEmpty:
                pass
        self._persistence_queue.put_nowait((state,0))

    async def run_once(self):
        state=await self.ha.snapshot()
        # Publish current state before touching SQLite. The actuator watchdog can
        # therefore keep supervising safely even while persistence is busy.
        self.latest=state
        self.last_error=None
        self._queue_state(state)
        return state

    def actuator_actual(self):
        state=self.latest
        if state is None: return None

        def value(field):
            item=getattr(state,field,None)
            if item is None or not bool(getattr(item,"available",False)): return None
            try:
                number=float(getattr(item,"state",None))
                return number if math.isfinite(number) else None
            except (TypeError,ValueError): return None

        try:
            observed=datetime.fromisoformat(str(state.collected_at).replace("Z","+00:00"))
            if observed.tzinfo is None: observed=observed.replace(tzinfo=timezone.utc)
            observed=observed.astimezone(timezone.utc)
        except Exception:
            return None
        return {
            "observed_at":observed.isoformat(),
            "age_seconds":max(0.0,(datetime.now(timezone.utc)-observed).total_seconds()),
            "soc_pct":value("battery_soc_pct"),
            "load_kw":value("house_load_kw"),
            "pv_kw":value("pv_power_kw"),
            "grid_kw":value("grid_power_kw"),
            "battery_kw":value("battery_power_kw"),
            "source":"collector_process_memory",
        }

    def persistence_status(self):
        self._ensure_runtime_state()
        return {
            "policy":"bounded_single_writer_queue_v1",
            "pending":self._persistence_queue.qsize(),
            "capacity":self.persistence_queue_max,
            "written":self.persistence_written,
            "retried":self.persistence_retried,
            "dropped":self.persistence_dropped,
            "last_error":self.last_persistence_error,
            "worker_running":self._persistence_task is not None and not self._persistence_task.done(),
        }

    async def loop(self):
        self.running=True
        while self.running:
            try:
                await self.run_once()
            except Exception as e:
                self.last_error=repr(e)
            await asyncio.sleep(self.poll_seconds)

    def stop(self):
        self.running=False

    async def close(self):
        self.running=False
        task=self._persistence_task
        if task is None: return
        try:
            await asyncio.wait_for(self._persistence_queue.join(),timeout=5.0)
        except asyncio.TimeoutError:
            pass
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._persistence_task=None
