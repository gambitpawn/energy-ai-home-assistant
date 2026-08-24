from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .component_registry import component_specs, registry_status
from .db import DB_PATH
from .flexible_loads import flexible_load_forecast
from .load_calibration import MODEL_NAME, load_model, predict_load


class LoadForecaster:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.horizon_hours = int(cfg.get("forecast", {}).get("horizon_hours", 36))

    def _recent_history(self, days: int = 28) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        out=[]
        with sqlite3.connect(DB_PATH) as c:
            rows=c.execute("SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? ORDER BY bucket_start ASC",(cutoff,)).fetchall()
        for ts,payload_json in rows:
            try:
                payload=json.loads(payload_json); means=payload.get("mean") or {}; total=means.get("house_load_kw")
                if total is None: continue
                component_means={k:float(v) for k,v in (payload.get("component_mean_kw") or {}).items() if v is not None}
                measured_components=sum(max(0.0,v) for v in component_means.values())
                base=max(0.0,float(total)-measured_components)
                stamp=datetime.fromisoformat(str(ts).replace("Z","+00:00")); stamp=stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
                out.append({"ts":stamp.astimezone(timezone.utc),"y":base,"house_load_kw":float(total),"component_mean_kw":component_means})
            except Exception:
                continue
        return out

    def _temperature_forecast(self) -> dict[str,float]:
        out={}
        with sqlite3.connect(DB_PATH) as c:
            row=c.execute("SELECT MAX(generated_at) FROM pv_forecast_15m").fetchone(); generated=row[0] if row else None
            if not generated: return out
            rows=c.execute("SELECT start_utc,temperature_c FROM pv_forecast_15m WHERE generated_at=? AND temperature_c IS NOT NULL",(generated,)).fetchall()
        for start,temp in rows: out[str(start)]=float(temp)
        return out

    def _latest_component_power(self) -> dict[str,float]:
        with sqlite3.connect(DB_PATH) as c:
            row=c.execute("SELECT payload_json FROM raw_state ORDER BY id DESC LIMIT 1").fetchone()
        if not row: return {}
        try: payload=json.loads(row[0])
        except Exception: return {}
        out={}
        for cid,item in (payload.get("load_components") or {}).items():
            try:
                if item.get("available") and item.get("state") is not None: out[cid]=max(0.0,float(item["state"]))
            except Exception: pass
        return out

    def refresh(self) -> dict[str, Any]:
        payload=load_model()
        if payload is None or int(payload.get("model_version",0))<3: raise RuntimeError("No trained load v3 model. Train it under /training/load/train first.")
        now=datetime.now(timezone.utc); start=now.replace(second=0,microsecond=0); start=start.replace(minute=(start.minute//15)*15)
        if start<=now: start+=timedelta(minutes=15)
        history=self._recent_history(28); temp_map=self._temperature_forecast(); starts=[start+timedelta(minutes=15*i) for i in range(self.horizon_hours*4)]
        flex=flexible_load_forecast(self.cfg,starts); flex_rows={r["start"]:r for r in flex["rows"]}
        specs=component_specs(self.cfg); current_power=self._latest_component_power(); rows=[]
        for stamp in starts:
            key=stamp.isoformat(); temp=temp_map.get(key); base_pred,uncertainty,meta=predict_load(payload,stamp,history=history,temperature_c=temp)
            legacy=flex_rows.get(key) or {}; lead_h=max(0.0,(stamp-now).total_seconds()/3600.0); comp={}
            for spec in specs:
                if not spec.enabled: continue
                if spec.id=="ev": kw=float(legacy.get("ev_forecast_kw") or 0.0)
                elif spec.id=="sauna": kw=float(legacy.get("sauna_forecast_kw") or 0.0)
                else:
                    # Generic hook v1: measured-load persistence for 2h. A component-specific
                    # forecaster can replace this without changing the total-load model.
                    kw=float(current_power.get(spec.id,0.0)) if lead_h<=2.0 else 0.0
                comp[spec.id]=round(max(0.0,kw),4)
            total=max(0.0,base_pred+sum(comp.values()))
            rows.append({"start":key,"base_household_forecast_kw":round(base_pred,4),"component_forecast_kw":comp,"ev_forecast_kw":comp.get("ev",0.0),"sauna_forecast_kw":comp.get("sauna",0.0),"house_load_forecast_kw":round(total,4),"house_load_uncertainty_kw":round(uncertainty,4),"temperature_c":temp,**meta})
        base_energy=sum(r["base_household_forecast_kw"]*.25 for r in rows); component_energy={}
        for spec in specs:
            component_energy[spec.id]=round(sum(float(r["component_forecast_kw"].get(spec.id,0.0))*.25 for r in rows),3)
        total_energy=sum(r["house_load_forecast_kw"]*.25 for r in rows)
        separated_rows=sum(1 for r in history if any(float(v)>0 for v in (r.get("component_mean_kw") or {}).values()))
        return {"generated_at":now.isoformat(),"interval_minutes":15,"horizon_hours":self.horizon_hours,"model":MODEL_NAME,"composition":"base_load + sum(load_components)","component_registry":registry_status(self.cfg),"live_recent_history_rows":len(history),"history_rows_with_measured_components":separated_rows,"temperature_forecast_rows":sum(1 for r in rows if r.get("temperature_c") is not None),"flexible_loads":{"ev":flex["ev"],"sauna":flex["sauna"],"provisional":flex.get("provisional",True)},"energy_kwh":{"base_load":round(base_energy,3),"components":component_energy,"total":round(total_energy,3)},"total_forecast_energy_kwh":round(total_energy,3),"rows":rows}
