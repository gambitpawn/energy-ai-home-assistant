from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DB_PATH
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
                payload = json.loads(payload_json)
                value = (payload.get("mean") or {}).get("house_load_kw")
                if value is None:
                    continue
                stamp = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                out.append({"ts": stamp.astimezone(timezone.utc), "y": float(value)})
            except Exception:
                continue
        return out

    def _temperature_forecast(self) -> dict[str, float]:
        out: dict[str, float] = {}
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute("SELECT MAX(generated_at) FROM pv_forecast_15m").fetchone()
            generated_at = row[0] if row else None
            if not generated_at:
                return out
            rows = c.execute("SELECT start_utc,temperature_c FROM pv_forecast_15m WHERE generated_at=? AND temperature_c IS NOT NULL", (generated_at,)).fetchall()
        for start, temp in rows:
            out[str(start)] = float(temp)
        return out

    def refresh(self) -> dict[str, Any]:
        payload = load_model()
        if payload is None or int(payload.get("model_version", 0)) < 2:
            raise RuntimeError("No trained load v2 model. Train it under /training/load/train first.")

        now = datetime.now(timezone.utc)
        start = now.replace(second=0, microsecond=0)
        start = start.replace(minute=(start.minute // 15) * 15)
        if start <= now:
            start += timedelta(minutes=15)

        history = self._recent_history(28)
        temp_map = self._temperature_forecast()
        rows = []
        for i in range(self.horizon_hours * 4):
            stamp = start + timedelta(minutes=15 * i)
            key = stamp.isoformat()
            temp = temp_map.get(key)
            pred, uncertainty, meta = predict_load(payload, stamp, history=history, temperature_c=temp)
            rows.append({
                "start": key,
                "house_load_forecast_kw": round(pred, 4),
                "house_load_uncertainty_kw": round(uncertainty, 4),
                "temperature_c": temp,
                **meta,
            })

        total_energy = sum(float(r["house_load_forecast_kw"]) * 0.25 for r in rows)
        return {
            "generated_at": now.isoformat(),
            "interval_minutes": 15,
            "horizon_hours": self.horizon_hours,
            "model": MODEL_NAME,
            "live_recent_history_rows": len(history),
            "temperature_forecast_rows": sum(1 for r in rows if r.get("temperature_c") is not None),
            "total_forecast_energy_kwh": round(total_energy, 3),
            "rows": rows,
        }
