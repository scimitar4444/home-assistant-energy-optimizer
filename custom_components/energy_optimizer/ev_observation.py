"""Home Assistant adapter for optional, observation-only EV planning.

This module deliberately stops at a recommendation payload.  It neither
writes a wallbox nor adds hypothetical EV energy to the authoritative site
optimizer.  The central coordinator therefore remains the only owner of
stationary-battery and grid dispatch.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .config import EVConfig
from .ev_planning import (
    EVChargeAllocation,
    EVSlot,
    TripRequirement,
    VehicleProfile,
    plan_ev_charging,
)
from .optimizer import ForecastSlot

_LOGGER = logging.getLogger(__name__)

_EV_SOC_MAX_AGE_SECONDS = 6 * 60 * 60
_EV_SOC_ACTIVE_CHARGE_MAX_AGE_SECONDS = 15 * 60
_EV_ACTIVE_CHARGE_W = 100.0
_EV_CALENDAR_LOOKAHEAD_DAYS = 14
_EV_EVENT_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_MAX_SCHEDULE_PREVIEW_GROUPS = 8

EVPlanPayload = dict[str, Any]


def disabled_ev_plan_payload() -> EVPlanPayload:
    """Return the stable sensor contract when optional EV support is off."""
    return {
        "status": "disabled",
        "reason": "ev_planning_disabled",
        "next_departure": None,
        "required_wallbox_kwh": None,
        "planned_wallbox_kwh": 0.0,
        "unmet_wallbox_kwh": None,
        "target_soc_percent": None,
        "suggested_power_w": 0,
        "suggested_mode": "idle",
        "suggested_duration_minutes": None,
        "suggested_valid_until": None,
        "next_charge_start": None,
        "next_charge_end": None,
        "next_charge_power_w": 0,
        "schedule": [],
        "schedule_truncated": False,
        "provisional": True,
        "complete": False,
        "data_quality_percent": 0,
        "connected": None,
        "observation_mode": True,
        "accounting_valid": False,
        "history_accounting_valid": False,
        "applied_to_site_optimizer": False,
        "current_command_blocked_reason": None,
        "vehicle_soc_age_minutes": None,
        "live_power_age_seconds": None,
    }


def _parse_event_numbers(description: str) -> dict[str, float]:
    """Parse optional, language-neutral ``key: value`` trip overrides."""
    values: dict[str, float] = {}
    for field in re.split(r"[\n;]+", description or ""):
        key, separator, raw_value = field.partition(":")
        if not separator:
            key, separator, raw_value = field.partition("=")
        canonical = key.strip().lower()
        match = _EV_EVENT_NUMBER.search(raw_value)
        if canonical not in {"distance_km", "reserve_km"} or not separator:
            continue
        if match is None:
            continue
        try:
            value = float(match.group(0).replace(",", "."))
        except ValueError:
            continue
        if isfinite(value) and value >= 0:
            values[canonical] = value
    return values


def _calendar_datetime(value: Any, *, fallback_tz: Any) -> datetime | None:
    """Accept a timed departure and normalize Home Assistant's naive form."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw_value = str(value)
        if "T" not in raw_value and " " not in raw_value:
            return None
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=fallback_tz)
    return parsed


def _trip_from_event(
    event: dict[str, Any],
    *,
    now: datetime,
    default_reserve_km: float,
) -> tuple[TripRequirement | None, str | None]:
    """Translate one generic calendar event into a trip requirement."""
    departure = _calendar_datetime(event.get("start"), fallback_tz=now.tzinfo)
    if departure is None:
        return None, "calendar_event_start_invalid"
    overrides = _parse_event_numbers(str(event.get("description", "")))
    distance_km = overrides.get("distance_km")
    if distance_km is None:
        return None, "trip_distance_missing"
    reserve_km = overrides.get("reserve_km", default_reserve_km)
    if not 0 < distance_km <= 2_000 or not 0 <= reserve_km <= 1_000:
        return None, "trip_values_out_of_range"
    return (
        TripRequirement(
            departure=departure,
            distance_km=distance_km,
            reserve_km=reserve_km,
            event_id=str(event.get("uid") or event.get("id") or ""),
            name=str(event.get("summary") or ""),
        ),
        None,
    )


def _quarter(value: datetime) -> datetime:
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)


def _allocation_end(allocation: EVChargeAllocation) -> datetime:
    return allocation.start + timedelta(minutes=allocation.duration_minutes)


def _schedule_preview(
    allocations: tuple[EVChargeAllocation, ...],
) -> tuple[list[dict[str, Any]], bool]:
    """Return at most eight contiguous schedule groups for recorder attributes."""
    groups: list[dict[str, Any]] = []
    group_energies: list[float] = []
    group_prices: list[list[tuple[float, float]]] = []
    for allocation in allocations:
        start = allocation.start
        end = _allocation_end(allocation)
        power_w = round(allocation.power_kw * 1000)
        can_extend = bool(
            groups
            and groups[-1]["end"] == start.isoformat()
            and groups[-1]["power_w"] == power_w
            and groups[-1]["price_is_known"] == allocation.price_is_known
            and groups[-1]["pv_preferred"] == allocation.pv_preferred
        )
        if can_extend:
            group = groups[-1]
            group["end"] = end.isoformat()
            group_energies[-1] += allocation.wallbox_energy_kwh
            group["energy_kwh"] = round(group_energies[-1], 2)
            if allocation.price_eur_kwh is not None:
                group_prices[-1].append(
                    (allocation.price_eur_kwh, allocation.wallbox_energy_kwh)
                )
            priced_energy = sum(energy for _price, energy in group_prices[-1])
            group["price_eur_kwh"] = (
                round(
                    sum(price * energy for price, energy in group_prices[-1])
                    / priced_energy,
                    5,
                )
                if priced_energy > 0
                else None
            )
            continue

        groups.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "power_w": power_w,
                "energy_kwh": round(allocation.wallbox_energy_kwh, 2),
                "price_eur_kwh": (
                    round(allocation.price_eur_kwh, 5)
                    if allocation.price_eur_kwh is not None
                    else None
                ),
                "price_is_known": allocation.price_is_known,
                "pv_preferred": allocation.pv_preferred,
            }
        )
        group_energies.append(allocation.wallbox_energy_kwh)
        group_prices.append(
            (
                [(allocation.price_eur_kwh, allocation.wallbox_energy_kwh)]
                if allocation.price_eur_kwh is not None
                else []
            )
        )

    return groups[:_MAX_SCHEDULE_PREVIEW_GROUPS], len(groups) > _MAX_SCHEDULE_PREVIEW_GROUPS


class EVObservationPlanner:
    """Adapt Home Assistant states and calendars to the pure EV planner."""

    def __init__(self, hass: HomeAssistant, config: EVConfig) -> None:
        self.hass = hass
        self.config = config

    def _connected_state(self) -> bool | None:
        """Return a conservative normalized connection state."""
        state = self.hass.states.get(self.config.connected_entity)
        if state is None:
            return None
        normalized = str(state.state).strip().lower()
        if normalized in {"on", "connected", "charging", "true", "1"}:
            return True
        if normalized in {"off", "disconnected", "false", "0"}:
            return False
        return None

    def _soc_state(self) -> tuple[float | None, float | None, str | None]:
        """Read a cloud-backed vehicle SoC with its deliberately longer age."""
        state = self.hass.states.get(self.config.vehicle_soc_entity)
        if state is None or state.state in {"unknown", "unavailable"}:
            return None, None, "vehicle_soc_unavailable"
        attributes = getattr(state, "attributes", {})
        if str(attributes.get("unit_of_measurement", "")).strip() != "%":
            return None, None, "vehicle_soc_unit_invalid"
        try:
            soc = float(state.state)
        except ValueError:
            return None, None, "vehicle_soc_invalid"
        if not isfinite(soc) or not 0 <= soc <= 100:
            return None, None, "vehicle_soc_out_of_range"
        age_seconds = (dt_util.utcnow() - state.last_updated).total_seconds()
        if age_seconds < 0:
            return None, age_seconds, "vehicle_soc_timestamp_invalid"
        if age_seconds > _EV_SOC_MAX_AGE_SECONDS:
            return None, age_seconds, "vehicle_soc_stale"
        return soc, age_seconds, None

    async def _async_next_trip(
        self,
        now: datetime,
        *,
        optimizer_horizon_end: datetime,
    ) -> tuple[TripRequirement | None, str, str]:
        """Read the next departure from a dedicated EV calendar."""
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "start_date_time": now.isoformat(),
                    "end_date_time": (
                        now + timedelta(days=_EV_CALENDAR_LOOKAHEAD_DAYS)
                    ).isoformat(),
                },
                target={"entity_id": self.config.calendar_entity},
                blocking=True,
                return_response=True,
            )
        except Exception:  # noqa: BLE001 - EV planning must fail independently
            _LOGGER.warning("EV departure calendar could not be read")
            return None, "awaiting_data", "calendar_unavailable"

        calendar_result = (
            response.get(self.config.calendar_entity)
            if isinstance(response, dict)
            else None
        )
        events = (
            calendar_result.get("events")
            if isinstance(calendar_result, dict)
            else None
        )
        if not isinstance(events, list):
            return None, "awaiting_data", "calendar_response_invalid"

        candidates: list[tuple[datetime, dict[str, Any]]] = []
        invalid_start_seen = False
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("cancelled") is True or str(
                event.get("status", "")
            ).lower() in {"cancelled", "canceled"}:
                continue
            departure = _calendar_datetime(event.get("start"), fallback_tz=now.tzinfo)
            if departure is None:
                invalid_start_seen = True
                continue
            departure = dt_util.as_local(departure)
            if departure <= now:
                continue
            candidates.append((departure, {**event, "start": departure}))

        if not candidates:
            if invalid_start_seen:
                return None, "awaiting_data", "calendar_event_start_invalid"
            return None, "awaiting_trip", "no_trip_in_lookahead"

        departure, event = min(candidates, key=lambda item: item[0])
        trip, parse_error = _trip_from_event(
            event,
            now=now,
            default_reserve_km=self.config.reserve_km,
        )
        if trip is None:
            return None, "awaiting_data", parse_error or "calendar_event_invalid"
        if departure > optimizer_horizon_end:
            return trip, "awaiting_horizon", "trip_outside_optimizer_horizon"
        return trip, "scheduled", "calendar_trip_available"

    def _forecast_slots(
        self,
        now: datetime,
        slots: list[ForecastSlot],
        live: dict[str, Any] | None,
        *,
        connected: bool | None,
    ) -> list[EVSlot]:
        """Build wallbox allowances after protecting predicted house demand."""
        ev_slots: list[EVSlot] = []
        live_accounting_valid = bool(
            live is not None and live.get("ev_accounting_valid") is True
        )
        for index, slot in enumerate(slots):
            slot_start = _calendar_datetime(slot.start, fallback_tz=now.tzinfo)
            if slot_start is None:
                continue
            if index == 0:
                duration_hours = max(
                    0.005,
                    min(
                        0.25,
                        ((slot_start + timedelta(minutes=15)) - now).total_seconds()
                        / 3600,
                    ),
                )
                ev_start = now
                if live_accounting_valid:
                    live_site_kwh = float(live["site_load_w"]) / 1000 * duration_hours
                    scheduled_house_kwh = max(0.0, slot.load_kwh - live_site_kwh)
                    house_load_kwh = (
                        float(live["house_load_w"]) / 1000 * duration_hours
                        + scheduled_house_kwh
                    )
                else:
                    house_load_kwh = slot.load_kwh
            else:
                duration_hours = 0.25
                ev_start = slot_start
                house_load_kwh = slot.load_kwh
            house_kw = house_load_kwh / duration_hours
            pv_kw = slot.pv_kwh / duration_hours
            available_kw = min(
                self.config.boost_charge_power_kw,
                max(0.0, self.config.site_max_import_power_kw + pv_kw - house_kw),
            )
            if index == 0 and (
                not live_accounting_valid or connected is not True
            ):
                # Future slots remain useful as an observation schedule, but
                # missing current evidence can never authorize/count charging
                # in the current interval.
                available_kw = 0.0
            ev_slots.append(
                EVSlot(
                    start=ev_start,
                    available_ev_power_kw=available_kw,
                    price_eur_kwh=slot.price_eur_kwh,
                    price_is_known=not slot.price_is_forecast,
                    residual_pv_kwh=max(0.0, slot.pv_kwh - house_load_kwh),
                    duration_hours=duration_hours,
                )
            )
        return ev_slots

    async def async_plan(
        self,
        now: datetime,
        slots: list[ForecastSlot],
        live: dict[str, Any] | None,
        history_accounting_valid: bool,
    ) -> EVPlanPayload:
        """Calculate a fail-closed EV recommendation without controlling it."""
        if not self.config.enabled:
            return disabled_ev_plan_payload()

        payload = disabled_ev_plan_payload()
        payload.update(
            {
                "status": "awaiting_data",
                "reason": "ev_data_incomplete",
                "data_quality_percent": 20,
                "suggested_mode": "degraded",
                "history_accounting_valid": history_accounting_valid,
            }
        )
        if not slots:
            payload["reason"] = "optimizer_horizon_unavailable"
            return payload
        horizon_start = _calendar_datetime(slots[-1].start, fallback_tz=now.tzinfo)
        if horizon_start is None:
            payload["reason"] = "optimizer_horizon_invalid"
            return payload
        horizon_end = horizon_start + timedelta(minutes=15)
        try:
            trip, trip_status, trip_reason = await self._async_next_trip(
                now,
                optimizer_horizon_end=horizon_end,
            )
        except Exception:  # noqa: BLE001 - never take down the house optimizer
            _LOGGER.exception("Unexpected EV calendar planning failure")
            payload["reason"] = "calendar_planning_failed"
            return payload

        payload["status"] = trip_status
        payload["reason"] = trip_reason
        payload["data_quality_percent"] = 40
        if trip is not None:
            payload["next_departure"] = trip.departure.isoformat()
        if trip_status in {"awaiting_trip", "awaiting_horizon"}:
            payload["suggested_mode"] = "idle"
            return payload
        if trip is None:
            return payload

        connected = self._connected_state()
        payload["connected"] = connected
        if connected is not None:
            payload["data_quality_percent"] += 10

        soc, soc_age_seconds, soc_error = self._soc_state()
        payload["vehicle_soc_age_minutes"] = (
            round(soc_age_seconds / 60, 1)
            if soc_age_seconds is not None and soc_age_seconds >= 0
            else None
        )
        if soc_error is not None:
            payload["reason"] = soc_error
            return payload
        payload["data_quality_percent"] += 20

        accounting_valid = bool(
            live is not None and live.get("ev_accounting_valid") is True
        )
        payload["accounting_valid"] = accounting_valid
        payload["live_power_age_seconds"] = (
            live.get("ev_power_age_seconds") if live is not None else None
        )
        if accounting_valid:
            payload["data_quality_percent"] += 30
        elif live is None or live.get("ev_load_w") is None:
            payload["current_command_blocked_reason"] = "ev_live_power_unavailable"
        else:
            payload["current_command_blocked_reason"] = "ev_accounting_invalid"
        if connected is False:
            payload["current_command_blocked_reason"] = "vehicle_not_connected"
        elif connected is None and not payload.get("current_command_blocked_reason"):
            payload["current_command_blocked_reason"] = "vehicle_connection_unknown"
        if (
            accounting_valid
            and float(live.get("ev_load_w") or 0) > _EV_ACTIVE_CHARGE_W
            and soc_age_seconds is not None
            and soc_age_seconds > _EV_SOC_ACTIVE_CHARGE_MAX_AGE_SECONDS
        ):
            payload.update(
                {
                    "status": "awaiting_data",
                    "reason": "vehicle_soc_stale_while_charging",
                    "current_command_blocked_reason": (
                        "vehicle_soc_stale_while_charging"
                    ),
                }
            )
            return payload

        try:
            profile = VehicleProfile(
                battery_capacity_kwh=self.config.battery_capacity_kwh,
                consumption_kwh_per_100km=self.config.consumption_kwh_per_100km,
                charging_efficiency=self.config.charge_efficiency,
                normal_power_kw=self.config.normal_charge_power_kw,
                boost_power_kw=self.config.boost_charge_power_kw,
            )
            plan = plan_ev_charging(
                profile,
                trip,
                self._forecast_slots(now, slots, live, connected=connected),
                current_soc_percent=soc,
            )
        except (TypeError, ValueError) as error:
            _LOGGER.warning("EV recommendation could not be calculated: %s", error)
            payload["reason"] = "ev_plan_invalid"
            return payload

        first_quarter = _quarter(now)
        current_allocation = next(
            (
                allocation
                for allocation in plan.allocations
                if _quarter(allocation.start) == first_quarter
            ),
            None,
        )
        suggested_power_w = (
            round(current_allocation.power_kw * 1000)
            if current_allocation is not None and connected is True
            else 0
        )
        suggested_duration_minutes = (
            round(current_allocation.duration_minutes, 1)
            if current_allocation is not None and suggested_power_w > 0
            else None
        )
        suggested_valid_until = (
            _allocation_end(current_allocation).isoformat()
            if current_allocation is not None and suggested_power_w > 0
            else None
        )
        schedule, schedule_truncated = _schedule_preview(plan.allocations)
        next_allocation = plan.allocations[0] if plan.allocations else None
        provisional = (
            connected is not True
            or not accounting_valid
            or any(
                not allocation.price_is_known or allocation.pv_preferred
                for allocation in plan.allocations
            )
        )
        suggested_mode = "idle"
        if not accounting_valid or connected is None:
            suggested_mode = "degraded"
        elif suggested_power_w > round(profile.automatic_normal_power_kw * 1000):
            suggested_mode = "boost"
        elif suggested_power_w > 0:
            suggested_mode = "normal"
        payload.update(
            {
                "status": plan.status,
                "reason": plan.reason,
                "required_wallbox_kwh": (
                    round(plan.required_wallbox_kwh, 2)
                    if plan.required_wallbox_kwh is not None
                    else None
                ),
                "planned_wallbox_kwh": round(plan.planned_wallbox_kwh, 2),
                "unmet_wallbox_kwh": (
                    round(plan.unmet_wallbox_kwh, 2)
                    if plan.unmet_wallbox_kwh is not None
                    else None
                ),
                "target_soc_percent": round(plan.target_soc_percent, 1),
                "suggested_power_w": suggested_power_w,
                "suggested_mode": suggested_mode,
                "suggested_duration_minutes": suggested_duration_minutes,
                "suggested_valid_until": suggested_valid_until,
                "next_charge_start": (
                    next_allocation.start.isoformat()
                    if next_allocation is not None
                    else None
                ),
                "next_charge_end": (
                    _allocation_end(next_allocation).isoformat()
                    if next_allocation is not None
                    else None
                ),
                "next_charge_power_w": (
                    round(next_allocation.power_kw * 1000)
                    if next_allocation is not None
                    else 0
                ),
                "schedule": schedule,
                "schedule_truncated": schedule_truncated,
                "provisional": provisional,
                "complete": plan.complete,
                "data_quality_percent": min(
                    85 if provisional else 100,
                    80
                    if not history_accounting_valid
                    else int(payload["data_quality_percent"]),
                ),
                # This beta has no writer.  A hypothetical load must not
                # influence a Victron command until a future adapter starts it
                # atomically.
                "applied_to_site_optimizer": False,
            }
        )
        return payload
