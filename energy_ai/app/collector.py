import asyncio
from datetime import datetime, timedelta, timezone

from .db import insert_raw, rebuild_15m_bucket
from .ha import HomeAssistantClient


def quarter_bucket(ts):
    d=datetime.fromisoformat(ts.replace("Z","+00:00")).astimezone(timezone.utc)
    m=(d.minute//15)*15
    return d.replace(minute=m,second=0,microsecond=0)


class Collector:
    """Poll and persist Home Assistant state only.

    The collector is the safety-critical source for ``raw_state`` freshness used
    by the actuator watchdog. Forecast generation/evaluation must therefore not
    run inline in this coroutine: those jobs can take minutes on constrained
    hardware and would otherwise stop state polling for their entire runtime.

    Forecast maintenance is already owned by the separate maintenance loop in
    ``main._forecast_maintenance_loop`` / ``runtime_maintenance``.
    """

    def __init__(self,cfg):
        self.cfg=cfg
        self.ha=HomeAssistantClient(cfg)
        self.poll_seconds=int(cfg.get("collector",{}).get("poll_seconds",60))
        self.latest=None
        self.last_error=None
        self.running=False

    async def run_once(self):
        state=await self.ha.snapshot()
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

        self.latest=state
        self.last_error=None
        return state

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
