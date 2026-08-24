from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DB_PATH
from .flexible_loads import flexible_load_forecast
from .load_calibration import MODEL_NAME, load_model, predict_load


class LoadForecaster:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.horizon_hours = int(cfg.get("forecast", {}).get("horizon_hours", 36))

    def _recent_history(self, days: int = 28) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        out = []
        with sqlite3.connect(DB_PATH) as c:
            rows = c.execute("SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? ORDER BY bucket_start ASC", (cutoff,)).fetchall()
        for ts, payload_json in rows:
            try:
                payload = json.loads(payload_json); means = payload.get("mean") or {}; value = means.get("house_load_kw")
                if value is None: continue
                ev_kw = float(means.get("ev_power_kw") or 0.0); sauna_kw = float(means.get("sauna_power_kw") or 0.0)
                # From schema v3 onward measured flexible loads are removed from the base-history signal.
                base_value = max(0.0, float(value) - ev_kw - sauna_kw)
                stamp = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
                out.append({"ts": stamp.astimezone(timezone.utc), "y": base_value, "house_load_kw": float(value), "ev_power_kw": ev_kw, "sauna_power_kw": sauna_kw})
            except Exception:
                continue
        return out

    def _temperature_forecast(self) -> dict[str, float]:
        out: dict[str, float] = {}
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute("SELECT MAX(generated_at) FROM pv_forecast_15m").fetchone(); generated_at = row[0] if row else None
            if not generated_at: return out
            rows = c.execute("SELECT start_utc,temperature_c FROM pv_forecast_15m WHERE generated_at=? AND temperature_c IS NOT NULL", (generated_at,)).fetchall()
        for start, temp in rows: out[str(start)] = float(temp)
        return out

    def refresh(self) -> dict[str, Any]:
        payload = load_model()
        if payload is None or int(payload.get("model_version", 0)) < 3:
            raise RuntimeError("No trained load v3 model. Train it under /training/load/train first.")
        now = datetime.now(timezone.utc); start = now.replace(second=0, microsecond=0); start = start.replace(minute=(start.minute // 15) * 15)
        if start <= now: start += timedelta(minutes=15)
        history = self._recent_history(28); temp_map = self._temperature_forecast(); starts = [start + timedelta(minutes=15*i) for i in range(self.horizon_hours*4)]
        flex = flexible_load_forecast(self.cfg, starts); flex_rows = {r["start"]: r for r in flex["rows"]}; rows=[]
        for stamp in starts:
            key=stamp.isoformat(); temp=temp_map.get(key); base_pred, uncertainty, meta = predict_load(payload, stamp, history=history, temperature_c=temp); f=flex_rows.get(key) or {}; ev_kw=float(f.get("ev_forecast_kw") or 0.0); sauna_kw=float(f.get("sauna_forecast_kw") or 0.0); total=max(0.0,base_pred+ev_kw+sauna_kw)
            rows.append({"start":key,"base_household_forecast_kw":round(base_pred,4),"ev_forecast_kw":round(ev_kw,4),"sauna_forecast_kw":round(sauna_kw,4),"house_load_forecast_kw":round(total,4),"house_load_uncertainty_kw":round(uncertainty,4),"temperature_c":temp,**meta})
        base_energy=sum(float(r["base_household_forecast_kw"])*.25 for r in rows); ev_energy=sum(float(r["ev_forecast_kw"])*.25 for r in rows); sauna_energy=sum(float(r["sauna_forecast_kw"])*.25 for r in rows); total_energy=sum(float(r["house_load_forecast_kw"])*.25 for r in rows)
        separated_rows=sum(1 for r in history if float(r.get("ev_power_kw") or 0.0)>0 or float(r.get("sauna_power_kw") or 0.0)>0)
        return {"generated_at":now.isoformat(),"interval_minutes":15,"horizon_hours":self.horizon_hours,"model":MODEL_NAME,"composition":"base_household + ev + sauna","live_recent_history_rows":len(history),"history_rows_with_measured_flexible_load":separated_rows,"temperature_forecast_rows":sum(1 for r in rows if r.get("temperature_c") is not None),"flexible_loads":{"ev":flex["ev"],"sauna":flex["sauna"],"provisional":flex.get("provisional",True)},"energy_kwh":{"base_household":round(base_energy,3),"ev":round(ev_energy,3),"sauna":round(sauna_energy,3),"total":round(total_energy,3)},"total_forecast_energy_kwh":round(total_energy,3),"rows":rows}
