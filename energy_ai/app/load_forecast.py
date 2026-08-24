from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .load_calibration import MODEL_NAME, load_model, predict_load


class LoadForecaster:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.horizon_hours = int(cfg.get("forecast", {}).get("horizon_hours", 36))

    def refresh(self) -> dict[str, Any]:
        payload = load_model()
        if payload is None:
            raise RuntimeError("No trained load model. Train it under /training/load/train first.")

        now = datetime.now(timezone.utc)
        start = now.replace(second=0, microsecond=0)
        start = start.replace(minute=(start.minute // 15) * 15)
        if start <= now:
            start += timedelta(minutes=15)

        rows = []
        for i in range(self.horizon_hours * 4):
            stamp = start + timedelta(minutes=15 * i)
            pred, uncertainty, meta = predict_load(payload, stamp)
            rows.append({
                "start": stamp.isoformat(),
                "house_load_forecast_kw": round(pred, 4),
                "house_load_uncertainty_kw": round(uncertainty, 4),
                **meta,
            })

        total_energy = sum(float(r["house_load_forecast_kw"]) * 0.25 for r in rows)
        return {
            "generated_at": now.isoformat(),
            "interval_minutes": 15,
            "horizon_hours": self.horizon_hours,
            "model": MODEL_NAME,
            "total_forecast_energy_kwh": round(total_energy, 3),
            "rows": rows,
        }
