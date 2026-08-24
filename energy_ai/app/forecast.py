from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

import httpx


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class PVForecaster:
    def __init__(self, cfg: dict[str, Any], ha_client):
        self.cfg = cfg
        self.ha = ha_client
        pv_cfg = cfg.get("forecast", {}).get("pv", {})
        self.capacity_kw = float(pv_cfg.get("capacity_kw", 12.0))
        self.horizon_hours = int(cfg.get("forecast", {}).get("horizon_hours", 36))
        self.tilt_deg = pv_cfg.get("tilt_deg")
        self.azimuth_deg = pv_cfg.get("azimuth_deg")

    async def refresh(self) -> dict[str, Any]:
        ha_cfg = await self.ha.system_config()
        lat = ha_cfg.get("latitude")
        lon = ha_cfg.get("longitude")
        if lat is None or lon is None:
            raise RuntimeError("Home Assistant config does not expose latitude/longitude")

        use_gti = self.tilt_deg is not None and self.azimuth_deg is not None
        radiation_var = "global_tilted_irradiance" if use_gti else "shortwave_radiation"
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "minutely_15": f"{radiation_var},cloud_cover,temperature_2m",
            "forecast_minutely_15": self.horizon_hours * 4,
            "timezone": "UTC",
        }
        if use_gti:
            params["tilt"] = float(self.tilt_deg)
            params["azimuth"] = float(self.azimuth_deg)

        async with httpx.AsyncClient() as client:
            response = await client.get(OPEN_METEO_URL, params=params, timeout=20.0)
            response.raise_for_status()
            data = response.json()

        min15 = data.get("minutely_15") or {}
        times = min15.get("time") or []
        radiation = min15.get(radiation_var) or []
        clouds = min15.get("cloud_cover") or [None] * len(times)
        temps = min15.get("temperature_2m") or [None] * len(times)
        n = min(len(times), len(radiation), len(clouds), len(temps))

        generated_at = datetime.now(timezone.utc).isoformat()
        rows = []
        for i in range(n):
            irr = max(0.0, float(radiation[i] or 0.0))
            cloud = None if clouds[i] is None else float(clouds[i])
            temp = None if temps[i] is None else float(temps[i])
            # Deliberately simple physical baseline. A learned local calibration
            # will replace this once enough site history has accumulated.
            forecast_kw = min(self.capacity_kw, self.capacity_kw * irr / 1000.0)
            cloud_fraction = 0.0 if cloud is None else max(0.0, min(1.0, cloud / 100.0))
            uncertainty_kw = min(
                self.capacity_kw,
                max(0.15, forecast_kw * 0.15 + self.capacity_kw * 0.08 * cloud_fraction),
            )
            start = str(times[i])
            if "+" not in start and not start.endswith("Z"):
                start += "+00:00"
            rows.append({
                "start": start,
                "pv_power_forecast_kw": round(forecast_kw, 4),
                "pv_power_uncertainty_kw": round(uncertainty_kw, 4),
                "irradiance_w_m2": irr,
                "cloud_cover_pct": cloud,
                "temperature_c": temp,
            })

        return {
            "generated_at": generated_at,
            "interval_minutes": 15,
            "horizon_hours": self.horizon_hours,
            "capacity_kw": self.capacity_kw,
            "radiation_feature": radiation_var,
            "orientation_configured": use_gti,
            "model": "physical_baseline_v1",
            "rows": rows,
        }
