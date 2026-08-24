import asyncio
from datetime import datetime, timezone
from .db import insert_raw, upsert_15m
from .ha import HomeAssistantClient

def quarter_bucket(ts):
    d=datetime.fromisoformat(ts.replace("Z","+00:00")).astimezone(timezone.utc); m=(d.minute//15)*15
    return d.replace(minute=m,second=0,microsecond=0).isoformat()

class Collector:
    def __init__(self,cfg):
        self.ha=HomeAssistantClient(cfg); self.poll_seconds=int(cfg.get("collector",{}).get("poll_seconds",60)); self.latest=None; self.last_error=None; self.running=False
    async def run_once(self):
        state=await self.ha.snapshot(); p=state.model_dump(); insert_raw(state.collected_at,p); upsert_15m(quarter_bucket(state.collected_at),state.collected_at,p); self.latest=state; self.last_error=None; return state
    async def loop(self):
        self.running=True
        while self.running:
            try: await self.run_once()
            except Exception as e: self.last_error=repr(e)
            await asyncio.sleep(self.poll_seconds)
    def stop(self): self.running=False
