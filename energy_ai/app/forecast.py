from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .pv_calibration import dynamic_daily_energy_uncertainty, load_model, predict_calibrated
from .solar_geometry import solar_features

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
STOCKHOLM = ZoneInfo("Europe/Stockholm")


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
        lat = ha_cfg.get("latitude"); lon = ha_cfg.get("longitude")
        if lat is None or lon is None:
            raise RuntimeError("Home Assistant config does not expose latitude/longitude")
        lat = float(lat); lon = float(lon)

        use_gti = self.tilt_deg is not None and self.azimuth_deg is not None
        radiation_var = "global_tilted_irradiance" if use_gti else "shortwave_radiation"
        params: dict[str, Any] = {
            "latitude": lat, "longitude": lon,
            "minutely_15": f"{radiation_var},cloud_cover,temperature_2m",
            "forecast_minutely_15": self.horizon_hours * 4,
            "timezone": "UTC",
        }
        if use_gti:
            params["tilt"] = float(self.tilt_deg); params["azimuth"] = float(self.azimuth_deg)

        async with httpx.AsyncClient() as client:
            response = await client.get(OPEN_METEO_URL, params=params, timeout=20.0)
            response.raise_for_status(); data = response.json()

        min15 = data.get("minutely_15") or {}
        times = min15.get("time") or []; radiation = min15.get(radiation_var) or []
        clouds = min15.get("cloud_cover") or [None] * len(times); temps = min15.get("temperature_2m") or [None] * len(times)
        n = min(len(times), len(radiation), len(clouds), len(temps))

        calibrated = None
        if use_gti:
            try:
                candidate = load_model()
                if candidate and candidate.get("latitude_deg") is not None and candidate.get("longitude_deg") is not None and int(candidate.get("model_version", 0)) >= 31:
                    calibrated = candidate
            except Exception:
                calibrated = None

        generated_at = datetime.now(timezone.utc).isoformat(); rows = []
        model_name = "pv_hist_gradient_boosting_v3_1_guarded" if calibrated else "physical_baseline_v1"
        for i in range(n):
            irr = max(0.0, float(radiation[i] or 0.0)); cloud = None if clouds[i] is None else float(clouds[i]); temp = None if temps[i] is None else float(temps[i])
            start = str(times[i]); start += "+00:00" if "+" not in start and not start.endswith("Z") else ""
            stamp = datetime.fromisoformat(start.replace("Z", "+00:00")); stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
            solar = solar_features(stamp, lat, lon); meta = {"ml_weight": 0.0, "extrapolation_guard_active": False}

            if calibrated:
                try:
                    forecast_kw, uncertainty_kw, meta = predict_calibrated(calibrated, stamp, irr, temp, cloud)
                except Exception:
                    forecast_kw = min(self.capacity_kw, self.capacity_kw * irr / 1000.0)
                    cf = 0.0 if cloud is None else max(0.0, min(1.0, cloud / 100.0))
                    uncertainty_kw = min(self.capacity_kw, max(0.15, forecast_kw * 0.15 + self.capacity_kw * 0.08 * cf))
            else:
                forecast_kw = min(self.capacity_kw, self.capacity_kw * irr / 1000.0)
                cf = 0.0 if cloud is None else max(0.0, min(1.0, cloud / 100.0))
                uncertainty_kw = min(self.capacity_kw, max(0.15, forecast_kw * 0.15 + self.capacity_kw * 0.08 * cf))

            if solar["solar_elevation_deg"] <= 0.0:
                forecast_kw = 0.0; uncertainty_kw = 0.0

            rows.append({
                "start": start, "pv_power_forecast_kw": round(forecast_kw, 4), "pv_power_uncertainty_kw": round(uncertainty_kw, 4),
                "irradiance_w_m2": irr, "cloud_cover_pct": cloud, "temperature_c": temp,
                "solar_elevation_deg": round(solar["solar_elevation_deg"], 3), "solar_azimuth_deg": round(solar["solar_azimuth_deg"], 3),
                "daily_max_solar_elevation_deg": round(solar["daily_max_solar_elevation_deg"], 3), "daylight_hours": round(solar["daylight_hours"], 3),
                "ml_weight": meta.get("ml_weight", 0.0), "extrapolation_guard_active": bool(meta.get("extrapolation_guard_active", False)),
                "ml_prediction_kw": meta.get("ml_prediction_kw"), "physical_baseline_kw": meta.get("physical_baseline_kw"),
            })

        now_utc = datetime.now(timezone.utc); today = now_utc.astimezone(STOCKHOLM).date(); remaining_today_rows = []
        for row in rows:
            stamp = datetime.fromisoformat(str(row["start"]).replace("Z", "+00:00")); stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
            if stamp.astimezone(STOCKHOLM).date() == today and stamp >= now_utc:
                remaining_today_rows.append(row)
        remaining_energy_kwh = sum(float(r["pv_power_forecast_kw"]) * 0.25 for r in remaining_today_rows)

        daylight_remaining = [r for r in remaining_today_rows if float(r.get("solar_elevation_deg") or 0.0) > 0.0]
        mean_cloud = sum(float(r.get("cloud_cover_pct") or 0.0) for r in daylight_remaining) / len(daylight_remaining) if daylight_remaining else 0.0
        min_ml_weight = min((float(r.get("ml_weight") or 0.0) for r in daylight_remaining), default=0.0)

        if calibrated:
            p80 = dynamic_daily_energy_uncertainty(calibrated, remaining_energy_kwh, mean_cloud, min_ml_weight, "p80")
            p95 = dynamic_daily_energy_uncertainty(calibrated, remaining_energy_kwh, mean_cloud, min_ml_weight, "p95")
            remaining_uncertainty_p80 = float(p80["kwh"]); remaining_uncertainty_p95 = float(p95["kwh"])
            uncertainty_basis = "dynamic_empirical_daily_energy_residual"
            uncertainty_meta = {"mean_cloud_cover_pct": round(mean_cloud, 2), "min_ml_weight": round(min_ml_weight, 3),
                                "p80_relative_component": p80.get("relative_component"), "p95_relative_component": p95.get("relative_component")}
        else:
            remaining_uncertainty_p80 = max(0.5, remaining_energy_kwh * 0.20); remaining_uncertainty_p95 = max(1.0, remaining_energy_kwh * 0.35)
            uncertainty_basis = "physical_fallback"; uncertainty_meta = {}

        guard_rows = sum(1 for r in rows if r.get("extrapolation_guard_active"))
        return {
            "generated_at": generated_at, "interval_minutes": 15, "horizon_hours": self.horizon_hours, "capacity_kw": self.capacity_kw,
            "radiation_feature": radiation_var, "orientation_configured": use_gti, "model": model_name,
            "calibrated_model_active": calibrated is not None, "solar_geometry_features": True,
            "extrapolation_guard": {"active_intervals": guard_rows, "total_intervals": len(rows)},
            "pv_remaining_energy_today_kwh": round(remaining_energy_kwh, 3),
            "pv_remaining_energy_uncertainty": {"p80_kwh": round(remaining_uncertainty_p80, 3), "p95_kwh": round(remaining_uncertainty_p95, 3),
                                                 "basis": uncertainty_basis, **uncertainty_meta},
            "rows": rows,
        }
