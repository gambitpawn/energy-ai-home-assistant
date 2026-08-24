from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

import httpx

from .pv_calibration import load_model, predict_calibrated
from .solar_geometry import solar_features


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class PVForecaster:
    def __init__(self, cfg: dict[str, Any], ha_client):
        self.cfg = cfg
        self.ha = ha_client
        pv_cfg = cfg.get("forecast", {}).get("pv", {})
        self.capacity_kw = float(pv_cfg.get("capacity_kw", 10.0))
        self.horizon_hours = int(cfg.get("forecast", {}).get("horizon_hours", 36))
        self.tilt_deg = pv_cfg.get("tilt_deg")
        self.azimuth_deg = pv_cfg.get("azimuth_deg")

    async def refresh(self) -> dict[str, Any]:
        ha_cfg = await self.ha.system_config()
        lat = ha_cfg.get("latitude")
        lon = ha_cfg.get("longitude")
        if lat is None or lon is None:
            raise RuntimeError("Home Assistant config does not expose latitude/longitude")
        lat = float(lat)
        lon = float(lon)

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

        calibrated = None
        if use_gti:
            try:
                candidate = load_model()
                if candidate and candidate.get("latitude_deg") is not None and candidate.get("longitude_deg") is not None:
                    calibrated = candidate
            except Exception:
                calibrated = None

        generated_at = datetime.now(timezone.utc).isoformat()
        rows = []
        model_name = "pv_hist_gradient_boosting_v2_solar_geometry" if calibrated else "physical_baseline_v1"
        for i in range(n):
            irr = max(0.0, float(radiation[i] or 0.0))
            cloud = None if clouds[i] is None else float(clouds[i])
            temp = None if temps[i] is None else float(temps[i])
            start = str(times[i])
            if "+" not in start and not start.endswith("Z"):
                start += "+00:00"
            stamp = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            solar = solar_features(stamp, lat, lon)

            if calibrated:
                try:
                    forecast_kw, uncertainty_kw = predict_calibrated(calibrated, stamp, irr, temp, cloud)
                except Exception:
                    forecast_kw = min(self.capacity_kw, self.capacity_kw * irr / 1000.0)
                    cloud_fraction = 0.0 if cloud is None else max(0.0, min(1.0, cloud / 100.0))
                    uncertainty_kw = min(self.capacity_kw, max(0.15, forecast_kw * 0.15 + self.capacity_kw * 0.08 * cloud_fraction))
            else:
                forecast_kw = min(self.capacity_kw, self.capacity_kw * irr / 1000.0)
                cloud_fraction = 0.0 if cloud is None else max(0.0, min(1.0, cloud / 100.0))
                uncertainty_kw = min(
                    self.capacity_kw,
                    max(0.15, forecast_kw * 0.15 + self.capacity_kw * 0.08 * cloud_fraction),
                )

            if solar["solar_elevation_deg"] <= -1.0:
                forecast_kw = 0.0

            rows.append({
                "start": start,
                "pv_power_forecast_kw": round(forecast_kw, 4),
                "pv_power_uncertainty_kw": round(uncertainty_kw, 4),
                "irradiance_w_m2": irr,
                "cloud_cover_pct": cloud,
                "temperature_c": temp,
                "solar_elevation_deg": round(solar["solar_elevation_deg"], 3),
                "solar_azimuth_deg": round(solar["solar_azimuth_deg"], 3),
                "daily_max_solar_elevation_deg": round(solar["daily_max_solar_elevation_deg"], 3),
                "daylight_hours": round(solar["daylight_hours"], 3),
            })

        return {
            "generated_at": generated_at,
            "interval_minutes": 15,
            "horizon_hours": self.horizon_hours,
            "capacity_kw": self.capacity_kw,
            "radiation_feature": radiation_var,
            "orientation_configured": use_gti,
            "model": model_name,
            "calibrated_model_active": calibrated is not None,
            "solar_geometry_features": True,
            "rows": rows,
        }
