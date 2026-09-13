"""Pure helpers for separating whole-site and EV measurements.

The supported topology has one main meter covering the complete site and an
EV charger meter below that boundary.  The EV reading is subtracted only for
the household load model; dispatch still uses the whole-site measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

DEFAULT_POWER_TOLERANCE_W = 100.0
DEFAULT_ENERGY_TOLERANCE_KWH = 0.05
DEFAULT_MAX_LIVE_AGE_SECONDS = 60.0
DEFAULT_MAX_LIVE_SKEW_SECONDS = 30.0
DEFAULT_MAX_SITE_POWER_W = 60_000.0
DEFAULT_MAX_EV_POWER_W = 25_000.0
DEFAULT_MAX_HOUSE_POWER_W = 30_000.0

# These energy limits apply to one-hour recorder buckets.  The deliberately
# separate site and EV limits allow an 11 kW (or manual 22 kW) EV session to be
# removed before the established small-house plausibility limit is evaluated.
DEFAULT_MAX_SITE_HOURLY_ENERGY_KWH = 31.0
DEFAULT_MAX_EV_HOURLY_ENERGY_KWH = 25.0
DEFAULT_MAX_HOUSE_HOURLY_ENERGY_KWH = 6.0


class SiteAccountingError(ValueError):
    """Base error for an unsafe or inconsistent accounting input."""


class UnsupportedMeterTopologyError(SiteAccountingError):
    """The main meter was not confirmed to include the EV branch."""


class InvalidMeasurementError(SiteAccountingError):
    """A measurement is invalid, stale or physically inconsistent."""


@dataclass(frozen=True, slots=True)
class SitePowerBreakdown:
    """One validated live-power split in watts."""

    site_w: float
    ev_w: float
    house_w: float
    tolerance_adjusted: bool = False


@dataclass(frozen=True, slots=True)
class SiteEnergyBreakdown:
    """One validated interval-energy split in kilowatt-hours."""

    site_kwh: float
    ev_kwh: float
    house_kwh: float
    tolerance_adjusted: bool = False


def require_ev_submeter_topology(*, site_meter_includes_ev: bool) -> None:
    """Require explicit confirmation of the only supported meter topology."""
    if site_meter_includes_ev is not True:
        raise UnsupportedMeterTopologyError(
            "The main site meter must include the EV branch before EV energy "
            "can be subtracted"
        )


def _finite_nonnegative(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidMeasurementError(f"{name} must be a finite number")
    result = float(value)
    if not isfinite(result) or result < 0:
        raise InvalidMeasurementError(f"{name} must be finite and non-negative")
    return result


def _positive_limit(value: float, *, name: str) -> float:
    result = _finite_nonnegative(value, name=name)
    if result <= 0:
        raise InvalidMeasurementError(f"{name} must be greater than zero")
    return result


def _validate_maximum(value: float, *, maximum: float, name: str) -> None:
    if value > maximum:
        raise InvalidMeasurementError(
            f"{name} is {value:g}, above the configured maximum {maximum:g}"
        )


def _split(
    site: float,
    ev: float,
    *,
    tolerance: float,
    maximum_site: float,
    maximum_ev: float,
    maximum_house: float,
    unit: str,
) -> tuple[float, bool]:
    """Validate raw values, subtract EV, then validate the house residual."""
    tolerance = _finite_nonnegative(tolerance, name=f"tolerance_{unit}")
    maximum_site = _positive_limit(maximum_site, name=f"maximum_site_{unit}")
    maximum_ev = _positive_limit(maximum_ev, name=f"maximum_ev_{unit}")
    maximum_house = _positive_limit(maximum_house, name=f"maximum_house_{unit}")

    _validate_maximum(site, maximum=maximum_site, name=f"site_{unit}")
    _validate_maximum(ev, maximum=maximum_ev, name=f"ev_{unit}")

    residual = site - ev
    if residual < -tolerance:
        raise InvalidMeasurementError(
            f"EV measurement exceeds the site measurement by {-residual:g} {unit}"
        )
    tolerance_adjusted = residual < 0
    house = max(0.0, residual)
    _validate_maximum(house, maximum=maximum_house, name=f"house_{unit}")
    return house, tolerance_adjusted


def split_site_power_w(
    site_power_w: float,
    ev_power_w: float,
    *,
    site_meter_includes_ev: bool,
    site_age_seconds: float,
    ev_age_seconds: float,
    maximum_age_seconds: float = DEFAULT_MAX_LIVE_AGE_SECONDS,
    maximum_skew_seconds: float = DEFAULT_MAX_LIVE_SKEW_SECONDS,
    tolerance_w: float = DEFAULT_POWER_TOLERANCE_W,
    maximum_site_power_w: float = DEFAULT_MAX_SITE_POWER_W,
    maximum_ev_power_w: float = DEFAULT_MAX_EV_POWER_W,
    maximum_house_power_w: float = DEFAULT_MAX_HOUSE_POWER_W,
) -> SitePowerBreakdown:
    """Split a fresh whole-site power reading into house and EV power.

    Ages are supplied by the caller so this helper remains independent of Home
    Assistant and wall-clock access.  Both readings must be recent and close
    enough in time to make subtraction meaningful.
    """
    require_ev_submeter_topology(
        site_meter_includes_ev=site_meter_includes_ev
    )
    site = _finite_nonnegative(site_power_w, name="site_power_w")
    ev = _finite_nonnegative(ev_power_w, name="ev_power_w")
    site_age = _finite_nonnegative(site_age_seconds, name="site_age_seconds")
    ev_age = _finite_nonnegative(ev_age_seconds, name="ev_age_seconds")
    maximum_age = _positive_limit(
        maximum_age_seconds, name="maximum_age_seconds"
    )
    maximum_skew = _finite_nonnegative(
        maximum_skew_seconds, name="maximum_skew_seconds"
    )
    if site_age > maximum_age or ev_age > maximum_age:
        raise InvalidMeasurementError("Site and EV power readings must be fresh")
    if abs(site_age - ev_age) > maximum_skew:
        raise InvalidMeasurementError(
            "Site and EV power readings are too far apart in time"
        )

    house, adjusted = _split(
        site,
        ev,
        tolerance=tolerance_w,
        maximum_site=maximum_site_power_w,
        maximum_ev=maximum_ev_power_w,
        maximum_house=maximum_house_power_w,
        unit="w",
    )
    return SitePowerBreakdown(site, ev, house, adjusted)


def split_site_energy_kwh(
    site_energy_kwh: float,
    ev_energy_kwh: float,
    *,
    site_meter_includes_ev: bool,
    tolerance_kwh: float = DEFAULT_ENERGY_TOLERANCE_KWH,
    maximum_site_energy_kwh: float = DEFAULT_MAX_SITE_HOURLY_ENERGY_KWH,
    maximum_ev_energy_kwh: float = DEFAULT_MAX_EV_HOURLY_ENERGY_KWH,
    maximum_house_energy_kwh: float = DEFAULT_MAX_HOUSE_HOURLY_ENERGY_KWH,
) -> SiteEnergyBreakdown:
    """Split one aligned interval of site energy into house and EV energy.

    Raw site energy is intentionally checked against a site-scale limit.  The
    smaller household limit is applied only after the EV submeter value has
    been removed, so valid 11 kW charging hours are retained for modelling.
    """
    require_ev_submeter_topology(
        site_meter_includes_ev=site_meter_includes_ev
    )
    site = _finite_nonnegative(site_energy_kwh, name="site_energy_kwh")
    ev = _finite_nonnegative(ev_energy_kwh, name="ev_energy_kwh")
    house, adjusted = _split(
        site,
        ev,
        tolerance=tolerance_kwh,
        maximum_site=maximum_site_energy_kwh,
        maximum_ev=maximum_ev_energy_kwh,
        maximum_house=maximum_house_energy_kwh,
        unit="kwh",
    )
    return SiteEnergyBreakdown(site, ev, house, adjusted)
