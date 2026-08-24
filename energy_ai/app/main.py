import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query
from .collector import Collector
from .config import load_config
from .db import init_db, latest_rows, insert_llm
from .llm import LLMExplainer
from .models import ExplainRequest, ExplainResponse
cfg=load_config(); collector=Collector(cfg); explainer=LLMExplainer(cfg); task=None

@asynccontextmanager
async def lifespan(app):
    global task
    init_db()
    try: await collector.run_once()
    except Exception as e: collector.last_error=repr(e)
    task=asyncio.create_task(collector.loop()); yield; collector.stop(); task.cancel()

app=FastAPI(title="Energy AI",version="0.1.1",description="Read-only HA energy data + LLM analysis",lifespan=lifespan)
@app.get("/health")
async def health(): return {"ok":collector.last_error is None,"read_only":True,"collector_running":collector.running,"last_error":collector.last_error,"openai_configured":explainer.client is not None,"llm_model":explainer.model}
@app.get("/config")
async def config(): return cfg
@app.get("/state")
async def state():
    if collector.latest is None: raise HTTPException(503,"No HA state collected yet")
    return collector.latest
@app.post("/collect-now")
async def collect_now(): return await collector.run_once()
@app.get("/history")
async def history(resolution:str=Query("15m",pattern="^(raw|15m)$"),limit:int=Query(96,ge=1,le=1000)): return latest_rows("raw_state" if resolution=="raw" else "state_15m",limit)
@app.post("/explain",response_model=ExplainResponse)
async def explain(req:ExplainRequest):
    payload={"proposed_action":req.proposed_action,"reason_data":req.reason_data,"policy":cfg.get("policy",{})}
    if req.include_current_state and collector.latest is not None: payload["current_state"]=collector.latest.model_dump()
    try: text=await asyncio.to_thread(explainer.explain,payload)
    except Exception as e: raise HTTPException(502,f"LLM explanation failed: {e!r}")
    now=datetime.now(timezone.utc).isoformat(); insert_llm(now,explainer.model,payload,text); return ExplainResponse(explanation_sv=text,model=explainer.model)
