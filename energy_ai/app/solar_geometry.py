from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def solar_features(timestamp: datetime, latitude_deg: float, longitude_deg: float) -> dict[str, float]:
    """Approximate NOAA-style solar geometry for ML features.

    Inputs are an aware timestamp and geographic latitude/longitude.
    Accuracy is more than sufficient for 15-minute PV forecasting features.
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    ts = timestamp.astimezone(timezone.utc)
    lat = math.radians(float(latitude_deg))
    doy = ts.timetuple().tm_yday
    hour_utc = ts.hour + ts.minute / 60.0 + ts.second / 3600.0

    gamma = 2.0 * math.pi / 365.0 * (doy - 1 + (hour_utc - 12.0) / 24.0)
    eqtime_min = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2.0 * gamma)
        - 0.040849 * math.sin(2.0 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2.0 * gamma)
        + 0.000907 * math.sin(2.0 * gamma)
        - 0.002697 * math.cos(3.0 * gamma)
        + 0.00148 * math.sin(3.0 * gamma)
    )

    true_solar_min = (hour_utc * 60.0 + eqtime_min + 4.0 * float(longitude_deg)) % 1440.0
    hour_angle_deg = true_solar_min / 4.0 - 180.0
    ha = math.radians(hour_angle_deg)

    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation_deg = 90.0 - math.degrees(zenith)

    # Solar azimuth, clockwise from north: 0=N, 90=E, 180=S, 270=W.
    az = math.atan2(
        math.sin(ha),
        math.cos(ha) * math.sin(lat) - math.tan(decl) * math.cos(lat),
    )
    azimuth_deg = (math.degrees(az) + 180.0) % 360.0

    max_elevation_deg = 90.0 - abs(float(latitude_deg) - math.degrees(decl))

    cos_h0_denom = math.cos(lat) * math.cos(decl)
    if abs(cos_h0_denom) < 1e-12:
        daylight_hours = 24.0 if math.sin(lat) * math.sin(decl) > 0 else 0.0
    else:
        cos_h0 = -math.tan(lat) * math.tan(decl)
        if cos_h0 <= -1.0:
            daylight_hours = 24.0
        elif cos_h0 >= 1.0:
            daylight_hours = 0.0
        else:
            daylight_hours = 2.0 * math.degrees(math.acos(cos_h0)) / 15.0

    return {
        "solar_elevation_deg": elevation_deg,
        "solar_azimuth_deg": azimuth_deg,
        "solar_azimuth_sin": math.sin(math.radians(azimuth_deg)),
        "solar_azimuth_cos": math.cos(math.radians(azimuth_deg)),
        "daily_max_solar_elevation_deg": max_elevation_deg,
        "daylight_hours": daylight_hours,
        "solar_declination_deg": math.degrees(decl),
    }
