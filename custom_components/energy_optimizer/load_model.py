"""Small deterministic helpers for household-load and PV forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math

from .const import SHIFTABLE_DEVICE_KEYS


@dataclass(frozen=True)
class WeatherSample:
    """Hourly weather observation or forecast used to shape PV production."""

    temperature_c: float | None = None
    cloud_percent: float | None = None
    rain_fraction: float | None = None
    illuminance_lux: float | None = None


def non_shiftable_load(total_load: float, device_energy: dict[str, float]) -> float:
    """Remove measured flexible jobs from historic household consumption."""
    flexible = sum(
        max(0.0, device_energy.get(key, 0.0)) for key in SHIFTABLE_DEVICE_KEYS
    )
    return max(0.0, total_load - min(total_load, flexible))


def forecast_level_calibration(
    predicted_kwh: float,
    recent_daily_kwh: float | None,
    *,
    horizon_hours: float = 48.0,
    headroom: float = 0.15,
) -> tuple[float, float | None]:
    """Limit an obsolete high forecast level using the recent daily median.

    The robust calendar model still determines the hourly shape. This one-sided
    guard never raises a low prediction, and flexible jobs are added afterwards.
    """
    if predicted_kwh <= 0 or recent_daily_kwh is None or recent_daily_kwh <= 0:
        return 1.0, None
    ceiling = recent_daily_kwh * horizon_hours / 24 * (1.0 + headroom)
    return max(0.0, min(1.0, ceiling / predicted_kwh)), ceiling


def _solar_elevation(moment: datetime, latitude: float, longitude: float) -> float:
    """Return approximate solar elevation in degrees (compact NOAA formula)."""
    if moment.tzinfo is None:
        utc_offset_hours = 0.0
    else:
        offset = moment.utcoffset()
        utc_offset_hours = offset.total_seconds() / 3600 if offset else 0.0
    local_hour = moment.hour + moment.minute / 60
    day = moment.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (day - 1 + (local_hour - 12) / 24)
    equation_minutes = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    solar_minutes = (
        local_hour * 60
        + equation_minutes
        + 4 * longitude
        - 60 * utc_offset_hours
    ) % 1440
    hour_angle = math.radians(solar_minutes / 4 - 180)
    latitude_rad = math.radians(latitude)
    sin_elevation = (
        math.sin(latitude_rad) * math.sin(declination)
        + math.cos(latitude_rad) * math.cos(declination) * math.cos(hour_angle)
    )
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elevation))))


def solar_brightness(
    moment: datetime,
    sample: WeatherSample | None,
    latitude: float,
    longitude: float,
) -> float:
    """Return a sun-height and cloud/lux based factor for PV shaping."""
    elevation = _solar_elevation(moment, latitude, longitude)
    daylight = max(0.0, math.sin(math.radians(elevation)))
    if daylight <= 0:
        return 0.0
    illuminance = sample.illuminance_lux if sample else None
    if illuminance is not None and illuminance >= 0:
        clear_sky_lux = max(1000.0, 110000.0 * daylight)
        return max(0.0, min(1.25, illuminance / clear_sky_lux))
    cloud = sample.cloud_percent if sample else None
    cloud_fraction = max(0.0, min(1.0, cloud / 100)) if cloud is not None else 0.5
    return max(0.0, 1.0 - 0.80 * cloud_fraction)
