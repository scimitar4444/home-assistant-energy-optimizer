"""Data coordinator for the Home Assistant Energy Optimizer."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial
from math import isfinite
from statistics import mean, median
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .appliance_planning import (
    PendingApplianceJob,
    RunningApplianceJob,
    add_running_jobs,
    schedule_pending_jobs,
)
from .config import OptimizerConfig
from .const import (
    LIVE_GRID_WARNING_W,
    LIVE_POWER_MAX_AGE_SECONDS,
    LIVE_POWER_MAX_W,
    LOAD_MODEL_RECENT_WEIGHT,
    MAX_COMBINED_APPLIANCE_AVERAGE_POWER_KW,
    UPDATE_INTERVAL,
)
from .control import bounded_pv_store_grid_setpoint_w, build_control_command
from .ev_observation import (
    EVObservationPlanner,
    disabled_ev_plan_payload,
)
from .load_model import (
    WeatherSample,
    forecast_level_calibration,
    non_shiftable_load,
    solar_brightness,
)
from .optimizer import ForecastSlot, optimize_battery
from .price_adapter import extract_price_timeline
from .site_accounting import (
    DEFAULT_MAX_EV_HOURLY_ENERGY_KWH,
    DEFAULT_MAX_EV_POWER_W,
    DEFAULT_MAX_SITE_HOURLY_ENERGY_KWH,
    DEFAULT_MAX_SITE_POWER_W,
    InvalidMeasurementError,
    SitePowerBreakdown,
    split_site_energy_kwh,
    split_site_power_w,
)

_LOGGER = logging.getLogger(__name__)

_EV_LIVE_MAX_AGE_SECONDS = 60.0
_EV_LIVE_MAX_SKEW_SECONDS = 30.0
_EV_SITE_LOCAL_SUPPLY_HEADROOM_W = 25_000.0


def _state_unit(state: Any) -> str:
    """Return a normalized Home Assistant unit string."""
    attributes = getattr(state, "attributes", {})
    return str(attributes.get("unit_of_measurement", "")).strip()


def _month_distance(first: int, second: int) -> int:
    difference = abs(first - second)
    return min(difference, 12 - difference)


def _quarter(value: datetime) -> datetime:
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)


def _first_slot_grid_setpoint_w(
    *,
    now: datetime,
    action: str,
    plan: list[dict[str, float | str | bool]],
    live: dict[str, float] | None,
    maximum_grid_setpoint_w: int,
) -> tuple[int, int, int]:
    """Translate the first plan slot into one safe positive grid target.

    The first slot contains only the unelapsed fraction of its quarter hour.
    ``PV_STORE`` is additionally bounded by fresh live house/PV power: the
    controllable PV diversion plus the unavoidable live deficit can never be
    greater than the current house load.
    """
    remaining_hours = max(
        0.005,
        min(
            0.25,
            ((_quarter(now) + timedelta(minutes=15)) - now).total_seconds()
            / 3600,
        ),
    )
    first = plan[0]
    grid_import_kwh = max(0.0, float(first["grid_import_kwh"]))
    grid_charge_kwh = max(0.0, float(first["grid_to_battery_kwh"]))
    grid_to_house_kwh = max(0.0, grid_import_kwh - grid_charge_kwh)
    planned_grid_to_house_w = round(grid_to_house_kwh * 1000 / remaining_hours)
    planned_grid_charge_w = round(grid_charge_kwh * 1000 / remaining_hours)

    requested_w = 0.0
    if action == "PV_STORE" and live is not None:
        requested_w = bounded_pv_store_grid_setpoint_w(
            planned_grid_to_house_w=planned_grid_to_house_w,
            live_house_w=live["load_w"],
            live_pv_w=live["pv_w"],
            maximum_grid_setpoint_w=maximum_grid_setpoint_w,
        )
    elif action == "GRID_CHARGE":
        requested_w = planned_grid_to_house_w + planned_grid_charge_w

    requested_w = round(
        max(0.0, min(float(maximum_grid_setpoint_w), requested_w))
    )
    return requested_w, planned_grid_to_house_w, planned_grid_charge_w


class EnergyOptimizerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Build forecasts from recorder statistics and optimize battery dispatch."""

    def __init__(self, hass: HomeAssistant, config: OptimizerConfig) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Home Assistant Energy Optimizer",
            update_interval=UPDATE_INTERVAL,
        )
        self.config = config
        self._ev_observation = EVObservationPlanner(hass, config.ev)
        self._load_samples: dict[tuple[int, bool, int], list[float]] = defaultdict(list)
        self._other_load_samples: dict[tuple[int, bool, int], list[float]] = defaultdict(list)
        self._tv_weekday_samples: dict[tuple[int, int, int], list[float]] = defaultdict(list)
        self._load_by_local_hour: dict[tuple[date, int], float] = {}
        self._weather_forecast_by_moment: dict[datetime, WeatherSample] = {}
        self._weather_forecast_refreshed_at: datetime | None = None
        self._weather_forecast_source_count = 0
        self._pv_samples: dict[tuple[int, bool, int], list[float]] = defaultdict(list)
        self._price_samples: dict[tuple[int, bool, int], list[float]] = defaultdict(list)
        self._recent_price_samples: dict[tuple[bool, int], list[float]] = (
            defaultdict(list)
        )
        self._recent_price_average: float | None = None
        self._recent_price_average_by_day_type: dict[bool, float] = {}
        self._pv_by_day: dict[date, float] = defaultdict(float)
        self._history_hours = 0
        self._history_refresh_date: date | None = None
        self._recent_base_daily_kwh: float | None = None
        self._recent_base_days = 0
        self._ev_history_accounting_valid = not config.ev.enabled

    async def _async_setup(self) -> None:
        await self._async_refresh_history()
        await self._async_refresh_weather_forecast()

    async def _async_refresh_history(self) -> None:
        end = dt_util.utcnow()
        start = end - timedelta(days=365)
        statistic_ids = {
            entity_id
            for entity_id in (
                *self.config.energy_history_entities,
                self.config.price_history_entity,
                self.config.tv_light_energy_entity,
                *self.config.device_energy_entities.values(),
                (
                    self.config.ev.energy_entity
                    if self.config.ev.enabled
                    else ""
                ),
            )
            if entity_id
        }
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            statistic_ids,
            "hour",
            None,
            {"mean", "change"},
        )
        ev_energy_state = (
            self.hass.states.get(self.config.ev.energy_entity)
            if self.config.ev.enabled
            else None
        )
        ev_energy_unit_valid = (
            not self.config.ev.enabled or _state_unit(ev_energy_state) == "kWh"
        )
        if self.config.ev.enabled and not ev_energy_unit_valid:
            _LOGGER.warning(
                "EV history cleaning disabled: charger energy must use kWh"
            )
        changes = {
            entity_id: {
                row["start"]: row.get("change") for row in result.get(entity_id, [])
            }
            for entity_id in self.config.energy_history_entities
        }
        if not all(changes.values()):
            raise UpdateFailed("Long-term energy statistics are incomplete")

        common_starts = set.intersection(*(set(rows) for rows in changes.values()))
        tv_changes = {
            row["start"]: row.get("change")
            for row in result.get(self.config.tv_light_energy_entity, [])
        }
        device_changes = {
            key: {
                row["start"]: row.get("change")
                for row in result.get(entity_id, [])
            }
            for key, entity_id in self.config.device_energy_entities.items()
        }
        ev_changes = (
            {
                row["start"]: row.get("change")
                for row in result.get(self.config.ev.energy_entity, [])
            }
            if self.config.ev.enabled and ev_energy_unit_valid
            else {}
        )
        ev_first_timestamp = min(ev_changes) if ev_changes else None
        ev_split_expected_hours = (
            sum(
                1
                for timestamp in common_starts
                if ev_first_timestamp is not None and timestamp >= ev_first_timestamp
            )
            if self.config.ev.enabled
            else 0
        )
        ev_split_valid_hours = 0
        load_samples: dict[tuple[int, bool, int], list[float]] = defaultdict(list)
        other_load_samples: dict[tuple[int, bool, int], list[float]] = defaultdict(list)
        tv_weekday_samples: dict[tuple[int, int, int], list[float]] = defaultdict(list)
        load_by_local_hour: dict[tuple[date, int], float] = {}
        pv_samples: dict[tuple[int, bool, int], list[float]] = defaultdict(list)
        pv_by_day: dict[date, float] = defaultdict(float)
        valid_tv_hours = 0
        valid_load_hours = 0
        for timestamp in common_starts:
            values = [
                changes[entity_id][timestamp]
                for entity_id in self.config.energy_history_entities
            ]
            if any(
                value is None
                or value < 0
                or value
                > (
                    DEFAULT_MAX_SITE_HOURLY_ENERGY_KWH
                    if self.config.ev.enabled and index < 2
                    else 6
                )
                for index, value in enumerate(values)
            ):
                continue
            grid_import, exported, pv, charged, discharged = values
            site_load = grid_import + pv + discharged - charged - exported
            local = dt_util.as_local(dt_util.utc_from_timestamp(timestamp))
            key = (local.month, local.weekday() >= 5, local.hour)
            # PV is independent of the optional wallbox submeter. Retain its
            # valid history even if this EV interval cannot safely be split.
            if self.config.ev.enabled and 0 <= pv <= 6:
                pv_samples[key].append(pv)
                pv_by_day[local.date()] += pv

            if self.config.ev.enabled:
                if not ev_energy_unit_valid:
                    continue
                ev_energy = ev_changes.get(timestamp)
                if ev_energy is None:
                    # Preserve the established household model before the EV
                    # submeter's first statistic. Once that counter exists,
                    # gaps are skipped rather than silently learning EV load.
                    if (
                        ev_first_timestamp is None
                        or timestamp >= ev_first_timestamp
                        or not 0 <= site_load <= 6
                    ):
                        continue
                    load = site_load
                else:
                    try:
                        load = split_site_energy_kwh(
                            site_load,
                            ev_energy,
                            site_meter_includes_ev=(
                                self.config.ev.site_meter_includes_ev
                            ),
                        ).house_kwh
                        ev_split_valid_hours += 1
                    except InvalidMeasurementError:
                        continue
            else:
                load = site_load
                if not 0 <= load <= 6:
                    continue

            device_hour: dict[str, float] = {}
            for device_key, history in device_changes.items():
                device_energy = history.get(timestamp)
                if device_energy is not None and 0 <= device_energy <= 4:
                    device_hour[device_key] = float(device_energy)
            base_load = non_shiftable_load(load, device_hour)
            tv_load = tv_changes.get(timestamp)
            if tv_load is None or not 0 <= tv_load <= 2:
                tv_load = 0.0
            else:
                valid_tv_hours += 1
            tv_load = min(base_load, tv_load)
            load_samples[key].append(base_load)
            other_load_samples[key].append(max(0.0, base_load - tv_load))
            tv_weekday_samples[(local.month, local.weekday(), local.hour)].append(
                tv_load
            )
            load_by_local_hour[(local.date(), local.hour)] = base_load
            if not self.config.ev.enabled:
                pv_samples[key].append(pv)
                pv_by_day[local.date()] += pv
            valid_load_hours += 1

        price_samples: dict[tuple[int, bool, int], list[float]] = defaultdict(list)
        recent_price_samples: dict[tuple[bool, int], list[float]] = defaultdict(list)
        recent_prices: list[float] = []
        recent_prices_by_day_type: dict[bool, list[float]] = defaultdict(list)
        recent_price_cutoff = end - timedelta(days=7)
        for row in result.get(self.config.price_history_entity, []):
            price = row.get("mean")
            if price is None or not -0.5 < price < 2:
                continue
            moment = dt_util.utc_from_timestamp(row["start"])
            local = dt_util.as_local(moment)
            price_samples[(local.month, local.weekday() >= 5, local.hour)].append(price)
            if moment >= recent_price_cutoff:
                weekend = local.weekday() >= 5
                recent_price_samples[(weekend, local.hour)].append(float(price))
                recent_prices_by_day_type[weekend].append(float(price))
                recent_prices.append(float(price))

        self._load_samples = load_samples
        self._other_load_samples = other_load_samples
        self._tv_weekday_samples = tv_weekday_samples
        self._load_by_local_hour = load_by_local_hour
        self._pv_samples = pv_samples
        self._price_samples = price_samples
        self._recent_price_samples = recent_price_samples
        self._recent_price_average = mean(recent_prices) if recent_prices else None
        self._recent_price_average_by_day_type = {
            weekend: mean(values)
            for weekend, values in recent_prices_by_day_type.items()
            if values
        }
        self._pv_by_day = pv_by_day
        self._history_hours = (
            valid_load_hours if self.config.ev.enabled else len(common_starts)
        )
        if self.config.ev.enabled:
            self._ev_history_accounting_valid = bool(
                ev_energy_unit_valid
                and ev_split_valid_hours >= 24
                and ev_split_valid_hours / max(1, ev_split_expected_hours) >= 0.8
            )
        self._load_model_ready = (
            self._history_hours >= 24 * 120
            and (
                not self.config.tv_light_energy_entity
                or valid_tv_hours / max(1, self._history_hours) >= 0.8
            )
        )

        # Calibrate the overall level from each counter's independent daily
        # change. The common hourly intersection used for the calendar profile
        # contains only 18-21 hours on some days (PV is often absent
        # at night); extrapolating that intersection to 24 hours biased the
        # level upward.  Independent daily totals correctly treat those absent
        # zero-change PV intervals without discarding real household load.
        today = dt_util.now().date()
        daily_energy: dict[str, dict[date, float]] = {}
        daily_energy_starts: dict[str, dict[date, set[float]]] = {}
        for entity_index, entity_id in enumerate(self.config.energy_history_entities):
            per_day: dict[date, float] = defaultdict(float)
            per_day_starts: dict[date, set[float]] = defaultdict(set)
            for row in result.get(entity_id, []):
                value = row.get("change")
                component_limit = (
                    DEFAULT_MAX_SITE_HOURLY_ENERGY_KWH
                    if self.config.ev.enabled and entity_index < 2
                    else 6
                )
                if value is None or value < 0 or value > component_limit:
                    continue
                day = dt_util.as_local(
                    dt_util.utc_from_timestamp(row["start"])
                ).date()
                if day < today:
                    per_day[day] += float(value)
                    per_day_starts[day].add(row["start"])
            daily_energy[entity_id] = per_day
            daily_energy_starts[entity_id] = per_day_starts

        daily_devices: dict[str, dict[date, float]] = {}
        for device_key, entity_id in self.config.device_energy_entities.items():
            per_day = defaultdict(float)
            for row in result.get(entity_id, []):
                value = row.get("change")
                if value is None or value < 0 or value > 4:
                    continue
                day = dt_util.as_local(
                    dt_util.utc_from_timestamp(row["start"])
                ).date()
                if day < today:
                    per_day[day] += float(value)
            daily_devices[device_key] = per_day

        daily_ev: dict[date, float] = defaultdict(float)
        daily_ev_starts: dict[date, set[float]] = defaultdict(set)
        ev_first_day: date | None = None
        if self.config.ev.enabled and ev_energy_unit_valid:
            for row in result.get(self.config.ev.energy_entity, []):
                value = row.get("change")
                if (
                    value is None
                    or value < 0
                    or value > DEFAULT_MAX_EV_HOURLY_ENERGY_KWH
                ):
                    continue
                day = dt_util.as_local(
                    dt_util.utc_from_timestamp(row["start"])
                ).date()
                ev_first_day = (
                    day if ev_first_day is None else min(ev_first_day, day)
                )
                if day < today:
                    daily_ev[day] += float(value)
                    daily_ev_starts[day].add(row["start"])

        site_starts_by_day: dict[date, set[float]] = defaultdict(set)
        for per_entity in daily_energy_starts.values():
            for day, starts in per_entity.items():
                site_starts_by_day[day].update(starts)

        complete_days = set.intersection(
            *(set(per_day) for per_day in daily_energy.values())
        )
        recent_days = sorted(complete_days)[-14:]
        normalized_daily: list[float] = []
        for day in recent_days:
            grid_import, exported, pv, charged, discharged = [
                daily_energy[entity_id][day]
                for entity_id in self.config.energy_history_entities
            ]
            total_load = grid_import + pv + discharged - charged - exported
            if self.config.ev.enabled:
                if not ev_energy_unit_valid:
                    continue
                before_ev_history = (
                    ev_first_day is not None and day < ev_first_day
                )
                if not before_ev_history:
                    if (
                        day not in daily_ev
                        or not site_starts_by_day[day]
                        or not site_starts_by_day[day].issubset(daily_ev_starts[day])
                    ):
                        continue
                    try:
                        total_load = split_site_energy_kwh(
                            total_load,
                            daily_ev[day],
                            site_meter_includes_ev=(
                                self.config.ev.site_meter_includes_ev
                            ),
                            maximum_site_energy_kwh=200,
                            maximum_ev_energy_kwh=180,
                            maximum_house_energy_kwh=30,
                        ).house_kwh
                    except InvalidMeasurementError:
                        continue
            device_energy = {
                key: per_day.get(day, 0.0)
                for key, per_day in daily_devices.items()
            }
            if 0 <= total_load <= 30:
                normalized_daily.append(
                    non_shiftable_load(total_load, device_energy)
                )
        self._recent_base_daily_kwh = (
            median(normalized_daily) if normalized_daily else None
        )
        self._recent_base_days = len(normalized_daily)
        self._history_refresh_date = today

    def _median_profile(
        self,
        samples: dict[tuple[int, bool, int], list[float]],
        month: int,
        weekend: bool,
        hour: int,
        default: float,
    ) -> float:
        values: list[float] = []
        for (sample_month, sample_weekend, sample_hour), candidates in samples.items():
            if (
                _month_distance(sample_month, month) <= 1
                and sample_weekend == weekend
                and sample_hour == hour
            ):
                values.extend(candidates)
        if len(values) < 4:
            for (_, sample_weekend, sample_hour), candidates in samples.items():
                if sample_weekend == weekend and sample_hour == hour:
                    values.extend(candidates)
        return median(values) if values else default

    def _weekday_profile(
        self,
        samples: dict[tuple[int, int, int], list[float]],
        month: int,
        weekday: int,
        hour: int,
        default: float,
    ) -> float:
        """Return an exact-weekday median with a seasonal fallback."""
        values: list[float] = []
        for (sample_month, sample_weekday, sample_hour), candidates in samples.items():
            if (
                _month_distance(sample_month, month) <= 1
                and sample_weekday == weekday
                and sample_hour == hour
            ):
                values.extend(candidates)
        if len(values) < 4:
            for (_, sample_weekday, sample_hour), candidates in samples.items():
                if sample_weekday == weekday and sample_hour == hour:
                    values.extend(candidates)
        return median(values) if values else default

    def _recent_price(self, hour: int, weekend: bool, default: float) -> float:
        """Return the seven-day mean for the matching day type and hour."""
        values = self._recent_price_samples.get((weekend, hour), [])
        if values:
            return mean(values)
        day_type_average = self._recent_price_average_by_day_type.get(weekend)
        if day_type_average is not None:
            return day_type_average
        if self._recent_price_average is not None:
            return self._recent_price_average
        return default

    def _recent_weekly_load(self, slot_time: datetime, default: float) -> float:
        """Return the median of the last comparable local weekday hours."""
        values = []
        candidate = slot_time - timedelta(days=7)
        while len(values) < 10:
            value = self._load_by_local_hour.get((candidate.date(), candidate.hour))
            if value is not None:
                values.append(value)
            candidate -= timedelta(days=7)
            if candidate < slot_time - timedelta(days=77):
                break
        return median(values) if len(values) >= 3 else default

    def _historical_daily_pv(self, target_day: date) -> float:
        """Return a seasonal daily PV estimate when the forecast is unavailable."""
        comparable = [
            value
            for sample_day, value in self._pv_by_day.items()
            if _month_distance(sample_day.month, target_day.month) <= 1
            and (sample_day.weekday() >= 5) == (target_day.weekday() >= 5)
        ]
        return max(0.0, median(comparable)) if comparable else 0.0

    def _historical_remaining_pv(
        self, now: datetime, first_slot_fraction: float
    ) -> float:
        """Scale a historical daily PV estimate to the unelapsed part of today."""
        daily_pv = self._historical_daily_pv(now.date())
        if daily_pv <= 0:
            return 0.0

        weekend = now.weekday() >= 5
        hourly_weights = {
            hour: max(
                0.0,
                self._median_profile(
                    self._pv_samples, now.month, weekend, hour, 0.0
                ),
            )
            for hour in range(24)
        }
        full_day_weight = 4 * sum(hourly_weights.values())
        if full_day_weight <= 0:
            return 0.0

        current_slot = _quarter(now)
        remaining_weight = 0.0
        slot_time = current_slot
        first = True
        while slot_time.date() == now.date():
            weight = hourly_weights[slot_time.hour]
            remaining_weight += weight * (first_slot_fraction if first else 1.0)
            first = False
            slot_time += timedelta(minutes=15)
        return daily_pv * min(1.0, remaining_weight / full_day_weight)

    def _optional_numeric_state(self, entity_id: str) -> float | None:
        """Return a numeric state, or None for a transiently missing sensor."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable"}:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        return value if isfinite(value) else None

    @staticmethod
    def _merge_weather(
        existing: WeatherSample | None,
        *,
        temperature_c: float | None = None,
        cloud_percent: float | None = None,
        rain_fraction: float | None = None,
        illuminance_lux: float | None = None,
    ) -> WeatherSample:
        """Overlay available values without discarding another source."""
        existing = existing or WeatherSample()
        return WeatherSample(
            temperature_c=(
                temperature_c
                if temperature_c is not None
                else existing.temperature_c
            ),
            cloud_percent=(
                cloud_percent if cloud_percent is not None else existing.cloud_percent
            ),
            rain_fraction=(
                rain_fraction if rain_fraction is not None else existing.rain_fraction
            ),
            illuminance_lux=(
                illuminance_lux
                if illuminance_lux is not None
                else existing.illuminance_lux
            ),
        )

    def _forecast_series(self, entity_id: str) -> dict[datetime, float]:
        """Read the hourly ``data`` array emitted by the DWD integration."""
        state = self.hass.states.get(entity_id)
        values: dict[datetime, float] = {}
        if state is None:
            return values
        for item in state.attributes.get("data", []) or []:
            try:
                timestamp = str(item["datetime"]).replace("Z", "+00:00")
                moment = dt_util.as_local(datetime.fromisoformat(timestamp)).replace(
                    minute=0, second=0, microsecond=0
                )
                values[moment] = float(item["value"])
            except (KeyError, TypeError, ValueError):
                continue
        return values

    async def _async_refresh_weather_forecast(self) -> None:
        """Combine optional hourly sensors and a weather forecast entity."""
        forecast: dict[datetime, WeatherSample] = {}
        for moment, value in self._forecast_series(self.config.weather_temperature_entity).items():
            if -40 <= value <= 60:
                forecast[moment] = self._merge_weather(
                    forecast.get(moment), temperature_c=value
                )
        for moment, value in self._forecast_series(self.config.weather_cloud_entity).items():
            if 0 <= value <= 100:
                forecast[moment] = self._merge_weather(
                    forecast.get(moment), cloud_percent=value
                )

        try:
            response = None
            if self.config.weather_forecast_entity:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {
                        "entity_id": self.config.weather_forecast_entity,
                        "type": "hourly",
                    },
                    blocking=True,
                    return_response=True,
                )
            hourly = (response or {}).get(
                self.config.weather_forecast_entity, {}
            ).get("forecast", [])
            rainy_conditions = {
                "rainy",
                "pouring",
                "lightning-rainy",
                "snowy-rainy",
            }
            for item in hourly:
                try:
                    timestamp = str(item["datetime"]).replace("Z", "+00:00")
                    moment = dt_util.as_local(datetime.fromisoformat(timestamp)).replace(
                        minute=0, second=0, microsecond=0
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                precipitation = item.get("precipitation")
                try:
                    rain = min(1.0, max(0.0, float(precipitation)))
                except (TypeError, ValueError):
                    rain = (
                        1.0
                        if str(item.get("condition", "")) in rainy_conditions
                        else 0.0
                    )
                temperature = item.get("temperature")
                cloud = item.get("cloud_coverage")
                forecast[moment] = self._merge_weather(
                    forecast.get(moment),
                    temperature_c=(
                        float(temperature) if temperature is not None else None
                    ),
                    cloud_percent=(float(cloud) if cloud is not None else None),
                    rain_fraction=rain,
                )
        except Exception:  # noqa: BLE001 - weather is an optional model input
            _LOGGER.warning("Hourly weather forecast could not be read")

        current_hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
        current_temperature = self._optional_numeric_state(self.config.weather_temperature_entity)
        current_cloud = self._optional_numeric_state(self.config.weather_cloud_entity)
        current_lux = self._optional_numeric_state(self.config.weather_illuminance_entity)
        rain_state = self.hass.states.get(self.config.weather_rain_binary_entity)
        current_rain = (
            1.0
            if rain_state is not None and rain_state.state == "on"
            else 0.0
        )
        forecast[current_hour] = self._merge_weather(
            forecast.get(current_hour),
            temperature_c=current_temperature,
            cloud_percent=current_cloud,
            rain_fraction=current_rain,
            illuminance_lux=current_lux,
        )
        self._weather_forecast_by_moment = forecast
        self._weather_forecast_source_count = len(forecast)
        self._weather_forecast_refreshed_at = dt_util.now()

    def _weather_for(self, moment: datetime) -> WeatherSample | None:
        """Return the observation/forecast for an hourly forecast slot."""
        return self._weather_forecast_by_moment.get(
            moment.replace(minute=0, second=0, microsecond=0)
        )

    def _live_power_reading(
        self,
        entity_id: str,
        *,
        allow_stale_zero: bool = False,
        maximum_age_seconds: float = LIVE_POWER_MAX_AGE_SECONDS,
        maximum_power_w: float = LIVE_POWER_MAX_W,
        required_unit: str | None = None,
    ) -> tuple[float, float] | None:
        """Return a validated power value together with its age in seconds."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable"}:
            return None
        if required_unit is not None and _state_unit(state) != required_unit:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if not isfinite(value) or not 0 <= value <= maximum_power_w:
            return None
        age = (dt_util.utcnow() - state.last_updated).total_seconds()
        if age < 0 or (
            age > maximum_age_seconds
            and not (allow_stale_zero and value == 0)
        ):
            return None
        return value, age

    def _live_power_state(
        self,
        entity_id: str,
        *,
        allow_stale_zero: bool = False,
        required_unit: str | None = None,
    ) -> float | None:
        """Return a recent live power value, or None when it is not trustworthy."""
        reading = self._live_power_reading(
            entity_id,
            allow_stale_zero=allow_stale_zero,
            required_unit=required_unit,
        )
        return reading[0] if reading is not None else None

    def _live_measurements(self) -> dict[str, Any] | None:
        """Read the current PV/load measurements for first-slot correction."""
        sun = self.hass.states.get("sun.sun")
        pv = self._live_power_state(
            self.config.live_pv_power_entity,
            allow_stale_zero=sun is not None and sun.state == "below_horizon",
            required_unit="W",
        )
        grid = self._live_power_state(
            self.config.live_grid_power_entity,
            required_unit="W",
        )
        fresh_load_readings = [
            self._live_power_reading(entity, required_unit="W")
            for entity in self.config.live_load_power_entities
        ]
        if pv is None or grid is None or not any(
            reading is not None for reading in fresh_load_readings
        ):
            return None
        # Victron phase sensors are edge-triggered: a phase that stays at 0 W
        # can legitimately retain an old timestamp.  Accept only that stale
        # zero when another phase is fresh; stale non-zero or a completely
        # stale phase group still invalidates the live correction.
        loads = [
            reading[0]
            if reading is not None
            else self._live_power_state(
                entity,
                allow_stale_zero=True,
                required_unit="W",
            )
            for entity, reading in zip(
                self.config.live_load_power_entities,
                fresh_load_readings,
            )
        ]
        if any(value is None for value in loads):
            return None
        site_load_w = sum(value for value in loads if value is not None)
        live: dict[str, Any] = {
            "pv_w": pv,
            # The authoritative first-slot/control value remains the complete
            # site load. Thus a manually started EV is never hidden merely
            # because the optional planner is in observation mode.
            "load_w": site_load_w,
            "site_load_w": site_load_w,
            "house_load_w": site_load_w,
            "ev_load_w": 0.0 if not self.config.ev.enabled else None,
            "ev_power_age_seconds": None,
            "ev_accounting_valid": not self.config.ev.enabled,
            "grid_w": grid,
        }
        if not self.config.ev.enabled:
            return live

        ev_reading = self._live_power_reading(
            self.config.ev.live_power_entity,
            maximum_age_seconds=_EV_LIVE_MAX_AGE_SECONDS,
            maximum_power_w=DEFAULT_MAX_EV_POWER_W,
            required_unit="W",
        )
        fresh_site_ages = [
            reading[1]
            for reading in fresh_load_readings
            if reading is not None and reading[0] > 0
        ]
        if not fresh_site_ages:
            fresh_site_ages = [
                reading[1]
                for reading in fresh_load_readings
                if reading is not None
            ]
        if ev_reading is None or not fresh_site_ages:
            return live
        live["ev_load_w"] = ev_reading[0]
        live["ev_power_age_seconds"] = round(ev_reading[1], 1)
        try:
            split: SitePowerBreakdown = split_site_power_w(
                site_load_w,
                ev_reading[0],
                site_meter_includes_ev=self.config.ev.site_meter_includes_ev,
                # The aggregate is only as fresh as its oldest contributing
                # non-zero/fresh phase reading.
                site_age_seconds=max(fresh_site_ages),
                ev_age_seconds=ev_reading[1],
                maximum_age_seconds=_EV_LIVE_MAX_AGE_SECONDS,
                maximum_skew_seconds=_EV_LIVE_MAX_SKEW_SECONDS,
                # The import setting is not a whole-site load limit: local PV
                # and the stationary battery may supply additional power.
                # Keep explicit local-supply headroom, still capped by the
                # pure accounting helper's conservative absolute ceiling.
                maximum_site_power_w=min(
                    DEFAULT_MAX_SITE_POWER_W,
                    self.config.ev.site_max_import_power_kw * 1000
                    + _EV_SITE_LOCAL_SUPPLY_HEADROOM_W,
                ),
                maximum_ev_power_w=DEFAULT_MAX_EV_POWER_W,
            )
        except InvalidMeasurementError:
            return live
        live.update(
            {
                "house_load_w": split.house_w,
                "ev_load_w": split.ev_w,
                "ev_accounting_valid": True,
            }
        )
        return live

    def _build_slots(
        self, now: datetime
    ) -> tuple[
        list[ForecastSlot],
        int,
        float,
        float,
        bool,
        dict[str, float | None],
        dict[str, float] | None,
    ]:
        timeline_state = self.hass.states.get(self.config.price_timeline_entity)
        known_prices = extract_price_timeline(
            timeline_state.attributes if timeline_state is not None else {},
            dt_util.as_local,
        )

        current_slot = _quarter(now)
        slot_times = [current_slot + timedelta(minutes=15 * index) for index in range(192)]
        first_slot_fraction = max(
            0.02,
            min(
                1.0,
                ((current_slot + timedelta(minutes=15)) - now).total_seconds() / 900,
            ),
        )
        today_sensor = self._optional_numeric_state(self.config.pv_today_remaining_entity)
        tomorrow_sensor = self._optional_numeric_state(self.config.pv_tomorrow_entity)
        if today_sensor is not None and not 0 <= today_sensor <= 100:
            today_sensor = None
        if tomorrow_sensor is not None and not 0 <= tomorrow_sensor <= 100:
            tomorrow_sensor = None
        pv_forecast_fallback = today_sensor is None or tomorrow_sensor is None
        today_remaining = (
            self._historical_remaining_pv(now, first_slot_fraction)
            if today_sensor is None
            else max(0.0, today_sensor)
        )
        tomorrow = (
            self._historical_daily_pv((now + timedelta(days=1)).date())
            if tomorrow_sensor is None
            else max(0.0, tomorrow_sensor)
        )
        pv_forecast: dict[date, float] = {
            now.date(): today_remaining,
            (now + timedelta(days=1)).date(): tomorrow,
        }
        for day_offset in range(2, 4):
            day = (now + timedelta(days=day_offset)).date()
            pv_forecast[day] = self._historical_daily_pv(day)

        pv_raw_weights: list[float] = []
        pv_weight_totals: dict[date, float] = defaultdict(float)
        for index, slot_time in enumerate(slot_times):
            weight = max(
                0.0,
                self._median_profile(
                    self._pv_samples,
                    slot_time.month,
                    slot_time.weekday() >= 5,
                    slot_time.hour,
                    0.0,
                ),
            )
            weather = self._weather_for(slot_time)
            if weather is not None:
                brightness = solar_brightness(
                    slot_time,
                    weather,
                    self.hass.config.latitude,
                    self.hass.config.longitude,
                )
                # The trusted external PV forecast still sets each day's total.
                # Weather/sun only move that energy toward the clearer hours.
                weight *= 0.40 + 0.60 * brightness
            if index == 0:
                weight *= first_slot_fraction
            pv_raw_weights.append(weight)
            pv_weight_totals[slot_time.date()] += weight

        # ``today_remaining`` is already a remainder and is distributed over
        # the visible part of today.  Every later value is a full-day total.
        # The rolling 48-hour horizon ends part-way through day two; using the
        # included weights as denominator there used to compress a complete
        # historical day into that partial morning/afternoon.
        pv_weight_denominators = dict(pv_weight_totals)
        for day in {slot_time.date() for slot_time in slot_times}:
            if day == now.date():
                continue
            full_day_weight = 0.0
            full_day_start = datetime.combine(day, datetime.min.time(), now.tzinfo)
            for quarter_index in range(96):
                moment = full_day_start + timedelta(minutes=15 * quarter_index)
                weight = max(
                    0.0,
                    self._median_profile(
                        self._pv_samples,
                        moment.month,
                        moment.weekday() >= 5,
                        moment.hour,
                        0.0,
                    ),
                )
                weather = self._weather_for(moment)
                if weather is not None:
                    weight *= 0.40 + 0.60 * solar_brightness(
                        moment,
                        weather,
                        self.hass.config.latitude,
                        self.hass.config.longitude,
                    )
                full_day_weight += weight
            if full_day_weight > 0:
                pv_weight_denominators[day] = full_day_weight

        slots: list[ForecastSlot] = []
        robust_slot_loads: list[float] = []
        known_count = 0
        baseline_load_total = 0.0
        for index, slot_time in enumerate(slot_times):
            weekend = slot_time.weekday() >= 5
            baseline_hourly_load = self._median_profile(
                self._load_samples, slot_time.month, weekend, slot_time.hour, 0.25
            )
            load = baseline_hourly_load
            if self._load_model_ready:
                other_load = self._median_profile(
                    self._other_load_samples,
                    slot_time.month,
                    weekend,
                    slot_time.hour,
                    baseline_hourly_load,
                )
                tv_light_load = self._weekday_profile(
                    self._tv_weekday_samples,
                    slot_time.month,
                    slot_time.weekday(),
                    slot_time.hour,
                    0.0,
                )
                recent_load = self._recent_weekly_load(
                    slot_time, baseline_hourly_load
                )
                load = (
                    (1 - LOAD_MODEL_RECENT_WEIGHT) * (other_load + tv_light_load)
                    + LOAD_MODEL_RECENT_WEIGHT * recent_load
                )
            robust_load = load / 4
            load = robust_load
            baseline_slot_load = baseline_hourly_load / 4
            if index == 0:
                load *= first_slot_fraction
                robust_load *= first_slot_fraction
                baseline_slot_load *= first_slot_fraction
            baseline_load_total += baseline_slot_load
            robust_slot_loads.append(robust_load)
            weight_total = pv_weight_denominators.get(slot_time.date(), 0.0)
            pv = (
                pv_forecast.get(slot_time.date(), 0.0)
                * pv_raw_weights[index]
                / weight_total
                if weight_total
                else 0.0
            )
            known_price = known_prices.get(slot_time)
            if known_price is None:
                historical_fallback = self._median_profile(
                    self._price_samples, slot_time.month, weekend, slot_time.hour, 0.30
                )
                price = self._recent_price(
                    slot_time.hour, weekend, historical_fallback
                )
                estimated = True
            else:
                price = known_price
                estimated = False
                known_count += 1
            dynamic_grid_charge = self.config.allow_grid_charging and not estimated
            quiet_hours = (
                slot_time.hour >= self.config.quiet_hours_start
                or slot_time.hour < self.config.quiet_hours_end
            )
            max_grid_charge_kw = (
                self.config.quiet_grid_charge_kw if quiet_hours else self.config.day_grid_charge_kw
            ) if dynamic_grid_charge else 0.0
            slots.append(
                ForecastSlot(
                    slot_time.isoformat(),
                    price,
                    load,
                    pv,
                    estimated,
                    max_grid_charge_kw,
                )
            )

        # Keep the robust hourly shape, but remove an obsolete upward level
        # bias.  Flexible appliance energy is deliberately added only later.
        robust_factor, base_ceiling = forecast_level_calibration(
            sum(robust_slot_loads), self._recent_base_daily_kwh
        )
        if robust_factor < 1.0:
            slots = [
                replace(slot, load_kwh=slot.load_kwh * robust_factor)
                for slot in slots
            ]
            robust_slot_loads = [value * robust_factor for value in robust_slot_loads]

        live = self._live_measurements()
        if live is not None:
            # Forecast slots are quarter-hour energy values.  Apply the same
            # remaining-slot fraction used above so the current measured power
            # replaces only the unelapsed part of this interval.
            slots[0] = replace(
                slots[0],
                load_kwh=live["load_w"] / 1000 * 0.25 * first_slot_fraction,
                pv_kwh=live["pv_w"] / 1000 * 0.25 * first_slot_fraction,
            )
            robust_slot_loads[0] = live["load_w"] / 1000 * 0.25 * first_slot_fraction
        robust_load_total = sum(robust_slot_loads)
        return (
            slots,
            known_count,
            baseline_load_total,
            robust_load_total,
            pv_forecast_fallback,
            {
                "recent_daily_kwh": self._recent_base_daily_kwh,
                "days": float(self._recent_base_days),
                "ceiling_48h_kwh": base_ceiling,
                "robust_factor": robust_factor,
            },
            live,
        )

    @staticmethod
    def _override_entities(settings: dict[str, Any]) -> tuple[str, ...]:
        """Return every immediate-start mode configured for an appliance."""
        return (
            str(settings["override_entity"]),
            *(
                str(entity_id)
                for entity_id in settings.get("additional_override_entities", ())
            ),
        )

    def _override_active(self, settings: dict[str, Any]) -> bool:
        """Return whether an appliance's immediate-start override is active."""
        return any(
            self.hass.states.is_state(entity_id, "on")
            for entity_id in self._override_entities(settings)
        )

    def _running_appliance_jobs(self, now: datetime) -> list[RunningApplianceJob]:
        """Build bounded remaining-load estimates for active appliance cycles."""
        jobs: list[RunningApplianceJob] = []
        for name, settings in self.config.appliances.items():
            run_state = self.hass.states.get(str(settings["status_entity"]))
            if run_state is None or run_state.state != "job_ongoing":
                continue
            paused = self.hass.states.get(str(settings["pause_entity"]))
            override_active = self._override_active(settings)
            if paused is not None and paused.state == "on" and not override_active:
                continue

            # A planned cycle first changes to ``job_ongoing``, is paused, and
            # later resumes without another run-state transition. In that case
            # the pause-off timestamp is the useful estimate of the real start.
            transitions = [run_state.last_changed]
            if paused is not None and paused.state == "off":
                transitions.append(paused.last_changed)
            for entity_id in self._override_entities(settings):
                override = self.hass.states.get(entity_id)
                if override is not None and override.state == "on":
                    transitions.append(override.last_changed)
            started_at = min(now, max(transitions))
            jobs.append(
                RunningApplianceJob(
                    name=name,
                    energy_kwh=float(settings["energy_kwh"]),
                    duration_slots=int(settings["slots"]),
                    started_at=started_at,
                )
            )
        return jobs

    def _schedule_pending_jobs(
        self,
        slots: list[ForecastSlot],
        *,
        initial_planned_power_kw: list[float] | None = None,
    ) -> tuple[list[ForecastSlot], dict[str, str]]:
        jobs: list[PendingApplianceJob] = []
        for name, settings in self.config.appliances.items():
            paused = self.hass.states.get(settings["pause_entity"])
            if (
                paused is None
                or paused.state != "on"
                or self._override_active(settings)
            ):
                continue
            jobs.append(
                PendingApplianceJob(
                    name=name,
                    energy_kwh=float(settings["energy_kwh"]),
                    duration_slots=int(settings["slots"]),
                    earliest_start_hour=int(settings["start_hour"]),
                    latest_start_hour=int(settings["latest_start_hour"]),
                )
            )
        return schedule_pending_jobs(
            slots,
            jobs,
            export_eur_kwh=self.config.export_eur_kwh,
            max_combined_power_kw=MAX_COMBINED_APPLIANCE_AVERAGE_POWER_KW,
            initial_planned_power_kw=initial_planned_power_kw,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.now()
        if self._history_refresh_date != now.date():
            await self._async_refresh_history()
        if (
            self._weather_forecast_refreshed_at is None
            or now - self._weather_forecast_refreshed_at > timedelta(minutes=30)
        ):
            await self._async_refresh_weather_forecast()
        try:
            (
                slots,
                known_count,
                baseline_load_total,
                robust_load_total,
                pv_forecast_fallback,
                level_calibration,
                live,
            ) = self._build_slots(now)
            slots, running_jobs, running_power_kw = add_running_jobs(
                slots,
                self._running_appliance_jobs(now),
                observed_at=now,
                first_slot_uses_live_house_power=live is not None,
            )
            slots, schedules = self._schedule_pending_jobs(
                slots,
                initial_planned_power_kw=running_power_kw,
            )
            try:
                ev_plan = await self._ev_observation.async_plan(
                    now,
                    slots,
                    live,
                    self._ev_history_accounting_valid,
                )
            except Exception:  # noqa: BLE001 - isolate optional EV support
                _LOGGER.exception("Unexpected EV planning failure")
                ev_plan = disabled_ev_plan_payload()
                ev_plan.update(
                    {
                        "status": "awaiting_data",
                        "reason": "ev_planning_failed",
                        "suggested_mode": "degraded",
                    }
                )
            measured_soc = self._optional_numeric_state(self.config.soc_entity)
            soc_is_valid = measured_soc is not None and 0 <= measured_soc <= 100
            # Keep the diagnostic forecast available when the BMS emits its
            # known 65535/missing sentinel.  The command builder still sees
            # the invalid raw value and immediately selects DEGRADED; the
            # optimizer itself receives the hard minimum as a harmless input.
            soc = measured_soc if soc_is_valid else self.config.hard_min_soc
            price_ratio = known_count / len(slots)
            confidence = round(
                100
                * (
                    0.45 * min(1.0, self._history_hours / 4000)
                    + 0.35 * price_ratio
                    + (0.20 if not pv_forecast_fallback else 0.08)
                )
            )
            result = await self.hass.async_add_executor_job(
                partial(
                    optimize_battery,
                    slots,
                    soc,
                    capacity_kwh=self.config.battery_capacity_kwh,
                    hard_min_soc=self.config.hard_min_soc,
                    battery_wear_eur_kwh=self.config.battery_wear_eur_kwh,
                    export_eur_kwh=self.config.export_eur_kwh,
                    grid_charge_margin_eur_kwh=self.config.grid_charge_margin_eur_kwh,
                    pv_curtailment_penalty_eur_kwh=self.config.pv_curtailment_penalty_eur_kwh,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise UpdateFailed(f"Optimization failed: {error}") from error

        reason = result.reason
        if (
            result.action == "PV_SURPLUS"
            and live is not None
            and live["grid_w"] > LIVE_GRID_WARNING_W
        ):
            reason = (
                f"PV lädt die Batterie; noch {live['grid_w']:.0f} W Netzbezug – "
                "ESS-PV-Durchleitung wird nachgeregelt"
            )

        next_discharge = next(
            (
                now.isoformat() if index == 0 else str(item["start"])
                for index, item in enumerate(result.plan)
                if float(item["battery_to_load_kwh"]) > 0.005
            ),
            None,
        )
        next_grid_charge = next(
            (
                now.isoformat() if index == 0 else str(item["start"])
                for index, item in enumerate(result.plan)
                if float(item["grid_to_battery_kwh"]) > 0.005
            ),
            None,
        )
        quiet_hours_now = (
            now.hour >= self.config.quiet_hours_start
            or now.hour < self.config.quiet_hours_end
        )
        target_charge_current = (
            self.config.quiet_charge_current_a
            if result.action == "GRID_CHARGE" and quiet_hours_now
            else self.config.normal_charge_current_a
        )
        (
            requested_grid_setpoint_w,
            planned_grid_to_house_w,
            planned_grid_charge_w,
        ) = _first_slot_grid_setpoint_w(
            now=now,
            action=result.action,
            plan=result.plan,
            live=live,
            maximum_grid_setpoint_w=self.config.victron_grid_setpoint_max_w,
        )
        current_price_is_known = not slots[0].price_is_forecast
        command = build_control_command(
            now=now,
            action=result.action,
            model_minimum_soc=result.target_min_soc,
            current_soc=(measured_soc if measured_soc is not None else float("nan")),
            data_quality_percent=confidence,
            requested_charge_current_a=target_charge_current,
            requested_grid_setpoint_w=requested_grid_setpoint_w,
            current_price_is_known=current_price_is_known,
            reason=reason,
            hard_min_soc=self.config.hard_min_soc,
            normal_charge_current_a=self.config.normal_charge_current_a,
            maximum_grid_setpoint_w=self.config.victron_grid_setpoint_max_w,
        )
        pv_headroom_active = (
            result.action == "PV_SURPLUS"
            and command.action == "PV_SURPLUS"
            and command.minimum_soc < result.target_min_soc
        )
        if pv_headroom_active:
            reason = "PV versorgt das Haus; 4 % Speicherpuffer sind freigegeben"
            command = replace(command, reason=reason)
        scheduled_energy = sum(
            float(self.config.appliances[name]["energy_kwh"])
            for name in schedules
            if name in self.config.appliances
        )
        running_forecast_energy = sum(
            float(item["forecast_energy_added_kwh"])
            for item in running_jobs.values()
        )
        running_remaining_energy = sum(
            float(item["remaining_energy_kwh"])
            for item in running_jobs.values()
        )
        return {
            "recommendation": command.action,
            "model_recommendation": result.action,
            "reason": command.reason,
            "model_reason": result.reason,
            "target_min_soc": result.target_min_soc,
            "control_command": command.as_dict(),
            "pv_headroom_active": pv_headroom_active,
            "confidence": confidence,
            "forecast_load_48h": round(sum(slot.load_kwh for slot in slots), 2),
            "forecast_load_robust_48h": round(
                robust_load_total + scheduled_energy + running_forecast_energy,
                2,
            ),
            "forecast_load_baseline_48h": round(
                baseline_load_total + scheduled_energy + running_remaining_energy,
                2,
            ),
            "load_model_active": self._load_model_ready,
            "recent_base_daily_kwh": (
                round(float(level_calibration["recent_daily_kwh"]), 2)
                if level_calibration["recent_daily_kwh"] is not None
                else None
            ),
            "recent_base_days": int(level_calibration["days"] or 0),
            "forecast_base_ceiling_48h": (
                round(float(level_calibration["ceiling_48h_kwh"]), 2)
                if level_calibration["ceiling_48h_kwh"] is not None
                else None
            ),
            "robust_level_factor": round(
                float(level_calibration["robust_factor"] or 1.0), 3
            ),
            "weather_forecast_hours": self._weather_forecast_source_count,
            "weather_inputs_active": self._weather_forecast_source_count > 0,
            "rain_indicator_percent": (
                100
                if self.hass.states.is_state(self.config.weather_rain_binary_entity, "on")
                else 0
            ),
            "forecast_pv_48h": round(sum(slot.pv_kwh for slot in slots), 2),
            "pv_forecast_fallback": pv_forecast_fallback,
            "known_price_slots": known_count,
            "estimated_price_slots": len(slots) - known_count,
            "recent_price_average": (
                round(self._recent_price_average, 4)
                if self._recent_price_average is not None
                else None
            ),
            "recent_weekday_price_average": (
                round(self._recent_price_average_by_day_type[False], 4)
                if False in self._recent_price_average_by_day_type
                else None
            ),
            "recent_weekend_price_average": (
                round(self._recent_price_average_by_day_type[True], 4)
                if True in self._recent_price_average_by_day_type
                else None
            ),
            "history_hours": self._history_hours,
            "expected_cost": result.expected_cost_eur,
            "expected_grid_import": result.expected_grid_import_kwh,
            "expected_grid_import_first_24h": (
                result.expected_grid_import_first_24h_kwh
            ),
            "expected_grid_import_second_24h": (
                result.expected_grid_import_second_24h_kwh
            ),
            "expected_grid_charge": result.expected_grid_charge_kwh,
            "expected_export": result.expected_export_kwh,
            "expected_battery_discharge": result.expected_battery_discharge_kwh,
            "projected_min_soc": result.projected_min_soc,
            "pv_headroom_required_percent": result.pv_headroom_required_percent,
            "next_discharge": next_discharge,
            "next_grid_charge": next_grid_charge,
            "target_charge_current": command.charge_current_a,
            "target_grid_setpoint_w": command.grid_setpoint_w,
            "planned_grid_to_house_first_slot_w": planned_grid_to_house_w,
            "planned_grid_charge_first_slot_w": planned_grid_charge_w,
            "current_price_is_known": current_price_is_known,
            "quiet_grid_charge_active": (
                command.action == "GRID_CHARGE" and quiet_hours_now
            ),
            "dynamic_grid_charge_enabled": True,
            # Compatibility for the existing dashboard attribute.
            "winter_grid_charge_enabled": True,
            "scheduled_jobs": schedules,
            "running_jobs": running_jobs,
            "ev_plan": ev_plan,
            "calculated_at": now.isoformat(),
            "live_correction_active": live is not None,
            "live_pv_w": round(live["pv_w"], 1) if live is not None else None,
            "live_load_w": round(live["load_w"], 1) if live is not None else None,
            "live_grid_w": round(live["grid_w"], 1) if live is not None else None,
            "live_site_load_w": (
                round(live["site_load_w"], 1) if live is not None else None
            ),
            "live_house_load_w": (
                round(live["house_load_w"], 1)
                if live is not None and live.get("ev_accounting_valid") is True
                else None
            ),
            "live_ev_load_w": (
                round(live["ev_load_w"], 1)
                if live is not None and live.get("ev_load_w") is not None
                else None
            ),
        }
