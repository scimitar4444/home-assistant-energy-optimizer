"""Pure quarter-hour planning for an optional electric vehicle charger.

The planner deliberately knows nothing about Home Assistant, Victron or a
specific wallbox.  The caller keeps ownership of the house and connection
limits and passes only the power that remains available for the vehicle.  PV
surplus is only a preferred charging window.  The existing central optimizer
remains solely responsible for stationary-battery dispatch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime

SLOT_HOURS = 0.25
AUTOMATIC_POWER_CEILING_KW = 11.0
_EPSILON = 1e-9


@dataclass(frozen=True)
class VehicleProfile:
    """Energy and charging limits for one vehicle.

    ``boost_power_kw`` may describe a wallbox or vehicle capable of more than
    11 kW.  Automatic plans are nevertheless hard-capped at 11 kW; a 22-kW
    emergency mode remains a manual concern outside this module.
    """

    battery_capacity_kwh: float
    consumption_kwh_per_100km: float
    charging_efficiency: float = 0.90
    normal_power_kw: float = 3.6
    boost_power_kw: float = 11.0
    material_price_difference_eur_kwh: float = 0.05

    def __post_init__(self) -> None:
        if not _positive_finite(self.battery_capacity_kwh):
            raise ValueError("Battery capacity must be positive")
        if not _positive_finite(self.consumption_kwh_per_100km):
            raise ValueError("Vehicle consumption must be positive")
        if not _positive_finite(self.charging_efficiency) or self.charging_efficiency > 1:
            raise ValueError("Charging efficiency must be in the interval (0, 1]")
        if not _positive_finite(self.normal_power_kw):
            raise ValueError("Normal charging power must be positive")
        if not _positive_finite(self.boost_power_kw):
            raise ValueError("Boost charging power must be positive")
        if (
            not _non_negative_finite(self.material_price_difference_eur_kwh)
        ):
            raise ValueError("Material price difference must be non-negative")

    @property
    def automatic_normal_power_kw(self) -> float:
        """Return the normal limit after applying the automatic 11-kW guard."""
        return min(AUTOMATIC_POWER_CEILING_KW, self.normal_power_kw)

    @property
    def automatic_boost_power_kw(self) -> float:
        """Return the boost limit after applying the automatic 11-kW guard."""
        return min(
            AUTOMATIC_POWER_CEILING_KW,
            max(self.automatic_normal_power_kw, self.boost_power_kw),
        )


@dataclass(frozen=True)
class TripRequirement:
    """One generic calendar-like departure and its required remaining range."""

    departure: datetime
    distance_km: float
    reserve_km: float
    event_id: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        if not _non_negative_finite(self.distance_km):
            raise ValueError("Trip distance must be non-negative")
        if not _non_negative_finite(self.reserve_km):
            raise ValueError("Reserve distance must be non-negative")


@dataclass(frozen=True)
class EVSlot:
    """Inputs for one 15-minute interval.

    ``available_ev_power_kw`` is the externally calculated wallbox allowance
    after house loads and the grid-connection limit have been protected.
    ``residual_pv_kwh`` is PV left after the house has been supplied.  It
    influences timing but does not grant this module control of the stationary
    battery.
    """

    start: datetime
    available_ev_power_kw: float | None
    price_eur_kwh: float | None = None
    price_is_known: bool = False
    residual_pv_kwh: float | None = 0.0
    duration_hours: float = SLOT_HOURS

    def __post_init__(self) -> None:
        if not _positive_finite(self.duration_hours) or self.duration_hours > SLOT_HOURS:
            raise ValueError("Slot duration must be in the interval (0, 0.25]")


@dataclass(frozen=True)
class EVChargeAllocation:
    """Discrete wallbox command and its required part of one interval."""

    start: datetime
    power_kw: float
    wallbox_energy_kwh: float
    slot_fraction: float
    duration_minutes: float
    price_eur_kwh: float | None
    price_is_known: bool
    pv_preferred: bool
    deadline_fallback: bool


@dataclass(frozen=True)
class EVPlan:
    """Deterministic plan for satisfying one departure requirement."""

    requirement: TripRequirement
    target_battery_kwh: float
    target_soc_percent: float
    required_wallbox_kwh: float | None
    planned_wallbox_kwh: float
    unmet_wallbox_kwh: float | None
    capacity_shortfall_kwh: float
    complete: bool
    status: str
    reason: str
    automatic_boost_used: bool
    allocations: tuple[EVChargeAllocation, ...]


@dataclass
class _WorkingSlot:
    source: EVSlot
    normal_power_kw: float
    boost_power_kw: float
    normal_capacity_kwh: float
    boost_capacity_kwh: float
    planned_kwh: float = 0.0
    pv_preference_kwh: float = 0.0
    price_planned_kwh: float = 0.0
    deadline_fallback: bool = False

    def headroom(self, capacity_kwh: float) -> float:
        return max(0.0, capacity_kwh - self.planned_kwh)

    @property
    def known_price(self) -> bool:
        return self.source.price_is_known and _finite(self.source.price_eur_kwh)


def required_wallbox_energy_kwh(
    profile: VehicleProfile,
    requirement: TripRequirement,
    current_soc_percent: float,
) -> tuple[float, float, float]:
    """Return wallbox energy, target battery energy and capacity shortfall.

    Distance and remaining-range reserve are converted to usable battery
    energy.  Charging losses are applied only to the missing energy, not to the
    energy already present in the vehicle.
    """
    if not _finite(current_soc_percent) or not 0 <= current_soc_percent <= 100:
        raise ValueError("Current SoC must be between 0 and 100 percent")
    requested_battery_kwh = (
        (requirement.distance_km + requirement.reserve_km)
        * profile.consumption_kwh_per_100km
        / 100
    )
    target_battery_kwh = min(profile.battery_capacity_kwh, requested_battery_kwh)
    capacity_shortfall_kwh = max(
        0.0,
        requested_battery_kwh - profile.battery_capacity_kwh,
    )
    current_battery_kwh = profile.battery_capacity_kwh * current_soc_percent / 100
    missing_battery_kwh = max(0.0, target_battery_kwh - current_battery_kwh)
    return (
        missing_battery_kwh / profile.charging_efficiency,
        target_battery_kwh,
        capacity_shortfall_kwh,
    )


def plan_ev_charging(
    profile: VehicleProfile,
    requirement: TripRequirement,
    slots: list[EVSlot],
    *,
    current_soc_percent: float | None,
) -> EVPlan:
    """Plan interruptible quarter-hour charging before one departure.

    Residual PV windows are consumed first.  Remaining optional charging uses
    the cheapest confirmed prices.  Unknown-price intervals are kept until the
    end of the available horizon and used only as a conservative deadline
    fallback.  Source dispatch, including any stationary-battery contribution,
    remains a single later decision of the central energy optimizer.

    Automatic 11-kW charging is permitted only when the normal 3.6-kW path
    cannot physically meet the deadline, or when extra power in an earlier
    confirmed cheap interval displaces confirmed, materially dearer energy.
    """
    requested_battery_kwh = (
        (requirement.distance_km + requirement.reserve_km)
        * profile.consumption_kwh_per_100km
        / 100
    )
    target_battery_kwh = min(profile.battery_capacity_kwh, requested_battery_kwh)
    target_soc_percent = target_battery_kwh / profile.battery_capacity_kwh * 100
    capacity_shortfall_kwh = max(
        0.0,
        requested_battery_kwh - profile.battery_capacity_kwh,
    )
    if (
        current_soc_percent is None
        or not _finite(current_soc_percent)
        or not 0 <= current_soc_percent <= 100
    ):
        return EVPlan(
            requirement=requirement,
            target_battery_kwh=target_battery_kwh,
            target_soc_percent=target_soc_percent,
            required_wallbox_kwh=None,
            planned_wallbox_kwh=0.0,
            unmet_wallbox_kwh=None,
            capacity_shortfall_kwh=capacity_shortfall_kwh,
            complete=False,
            status="awaiting_data",
            reason="current_soc_unavailable",
            automatic_boost_used=False,
            allocations=(),
        )

    required_kwh, target_battery_kwh, capacity_shortfall_kwh = (
        required_wallbox_energy_kwh(profile, requirement, current_soc_percent)
    )
    if required_kwh <= _EPSILON:
        complete = capacity_shortfall_kwh <= _EPSILON
        return EVPlan(
            requirement=requirement,
            target_battery_kwh=target_battery_kwh,
            target_soc_percent=target_soc_percent,
            required_wallbox_kwh=0.0,
            planned_wallbox_kwh=0.0,
            unmet_wallbox_kwh=0.0,
            capacity_shortfall_kwh=capacity_shortfall_kwh,
            complete=complete,
            status="ready" if complete else "infeasible",
            reason=(
                "target_already_met"
                if complete
                else "trip_and_reserve_exceed_vehicle_capacity"
            ),
            automatic_boost_used=False,
            allocations=(),
        )

    working = _prepare_slots(profile, requirement, slots)
    remaining_kwh = required_kwh

    # Normal-power plan: use guaranteed non-positive prices and residual PV,
    # then reserve unknown future capacity before buying in a positive-price
    # confirmed slot. This lets a rolling caller wait for the next tariff
    # publication whenever the departure deadline still allows it.
    remaining_kwh = _allocate_non_positive_price(
        working, remaining_kwh, boost=False
    )
    remaining_kwh = _allocate_pv(working, remaining_kwh, boost=False)
    remaining_kwh = _allocate_required_known_before_unknown(
        working, remaining_kwh, boost=False
    )
    remaining_kwh = _allocate_unknown_price(working, remaining_kwh, boost=False)

    deadline_boost_required = remaining_kwh > _EPSILON
    if deadline_boost_required:
        remaining_kwh = _allocate_non_positive_price(
            working, remaining_kwh, boost=True
        )
        remaining_kwh = _allocate_pv(working, remaining_kwh, boost=True)
        remaining_kwh = _allocate_required_known_before_unknown(
            working, remaining_kwh, boost=True
        )
        remaining_kwh = _allocate_unknown_price(working, remaining_kwh, boost=True)
    _move_expensive_energy_to_cheap_boost(profile, working)

    allocations = _build_allocations(working)
    planned_kwh = sum(item.wallbox_energy_kwh for item in allocations)
    unmet_kwh = max(0.0, required_kwh - planned_kwh)
    boost_used = any(
        allocation.power_kw > profile.automatic_normal_power_kw + _EPSILON
        for allocation in allocations
    )
    energy_complete = unmet_kwh <= 1e-7
    complete = energy_complete and capacity_shortfall_kwh <= _EPSILON
    if capacity_shortfall_kwh > _EPSILON:
        status = "infeasible"
        reason = "trip_and_reserve_exceed_vehicle_capacity"
    elif not energy_complete:
        status = "infeasible"
        reason = "deadline_capacity_insufficient"
    elif any(item.deadline_fallback for item in allocations):
        status = "scheduled"
        reason = "deadline_fallback_uses_unknown_price"
    elif boost_used and deadline_boost_required:
        status = "scheduled"
        reason = "normal_power_cannot_meet_deadline"
    elif boost_used:
        status = "scheduled"
        reason = "confirmed_cheap_window_avoids_high_price"
    elif any(item.pv_preferred for item in allocations):
        status = "scheduled"
        reason = "pv_preference"
    else:
        status = "scheduled"
        reason = "lowest_confirmed_cost"

    return EVPlan(
        requirement=requirement,
        target_battery_kwh=target_battery_kwh,
        target_soc_percent=target_soc_percent,
        required_wallbox_kwh=required_kwh,
        planned_wallbox_kwh=planned_kwh,
        unmet_wallbox_kwh=unmet_kwh,
        capacity_shortfall_kwh=capacity_shortfall_kwh,
        complete=complete,
        status=status,
        reason=reason,
        automatic_boost_used=boost_used,
        allocations=allocations,
    )


def _prepare_slots(
    profile: VehicleProfile,
    requirement: TripRequirement,
    slots: list[EVSlot],
) -> list[_WorkingSlot]:
    unique: dict[datetime, EVSlot] = {}
    for slot in slots:
        try:
            before_departure = slot.start < requirement.departure
        except TypeError:
            # Mixing timezone-aware and naive calendar data is unsafe.  Ignore
            # that interval instead of inventing chargeable time.
            continue
        if not before_departure or slot.start in unique:
            continue
        seconds_before_departure = (
            requirement.departure - slot.start
        ).total_seconds()
        usable_duration_hours = min(
            slot.duration_hours,
            max(0.0, seconds_before_departure / 3600),
        )
        if usable_duration_hours <= _EPSILON:
            continue
        unique[slot.start] = replace(
            slot,
            duration_hours=usable_duration_hours,
        )

    working: list[_WorkingSlot] = []
    for slot in sorted(unique.values(), key=lambda item: item.start):
        allowance_kw = _non_negative_value(slot.available_ev_power_kw)
        normal_kw = profile.automatic_normal_power_kw
        boost_kw = profile.automatic_boost_power_kw
        normal_capacity_kwh = (
            normal_kw * slot.duration_hours
            if allowance_kw + _EPSILON >= normal_kw
            else 0.0
        )
        boost_capacity_kwh = (
            boost_kw * slot.duration_hours
            if allowance_kw + _EPSILON >= boost_kw
            else normal_capacity_kwh
        )
        working.append(
            _WorkingSlot(
                source=slot,
                normal_power_kw=normal_kw,
                boost_power_kw=boost_kw,
                normal_capacity_kwh=normal_capacity_kwh,
                boost_capacity_kwh=boost_capacity_kwh,
            )
        )
    return working


def _capacity(slot: _WorkingSlot, *, boost: bool) -> float:
    return slot.boost_capacity_kwh if boost else slot.normal_capacity_kwh


def _allocate_pv(
    slots: list[_WorkingSlot],
    remaining_kwh: float,
    *,
    boost: bool,
) -> float:
    for slot in slots:
        headroom = slot.headroom(_capacity(slot, boost=boost))
        budget = max(
            0.0,
            _non_negative_value(slot.source.residual_pv_kwh)
            - slot.pv_preference_kwh,
        )
        amount = min(remaining_kwh, headroom, budget)
        slot.pv_preference_kwh += amount
        slot.planned_kwh += amount
        remaining_kwh -= amount
        if remaining_kwh <= _EPSILON:
            return 0.0
    return remaining_kwh


def _allocate_known_price(
    slots: list[_WorkingSlot],
    remaining_kwh: float,
    *,
    boost: bool,
    maximum_kwh: float | None = None,
    price_ceiling_eur_kwh: float | None = None,
) -> float:
    ranked = sorted(
        (
            slot
            for slot in slots
            if slot.known_price
            and (
                price_ceiling_eur_kwh is None
                or float(slot.source.price_eur_kwh)
                <= price_ceiling_eur_kwh + _EPSILON
            )
        ),
        key=lambda item: (float(item.source.price_eur_kwh), item.source.start),
    )
    allocation_budget_kwh = (
        remaining_kwh if maximum_kwh is None else max(0.0, maximum_kwh)
    )
    for slot in ranked:
        amount = min(
            remaining_kwh,
            allocation_budget_kwh,
            slot.headroom(_capacity(slot, boost=boost)),
        )
        slot.price_planned_kwh += amount
        slot.planned_kwh += amount
        remaining_kwh -= amount
        allocation_budget_kwh -= amount
        if remaining_kwh <= _EPSILON or allocation_budget_kwh <= _EPSILON:
            return max(0.0, remaining_kwh)
    return remaining_kwh


def _allocate_non_positive_price(
    slots: list[_WorkingSlot],
    remaining_kwh: float,
    *,
    boost: bool,
) -> float:
    """Preserve a guaranteed free/negative confirmed charging opportunity."""
    return _allocate_known_price(
        slots,
        remaining_kwh,
        boost=boost,
        price_ceiling_eur_kwh=0.0,
    )


def _allocate_required_known_before_unknown(
    slots: list[_WorkingSlot],
    remaining_kwh: float,
    *,
    boost: bool,
) -> float:
    """Buy now only when unknown future capacity cannot meet the deadline."""
    unknown_headroom_kwh = sum(
        slot.headroom(_capacity(slot, boost=boost))
        for slot in slots
        if not slot.known_price
    )
    required_known_kwh = max(0.0, remaining_kwh - unknown_headroom_kwh)
    if required_known_kwh <= _EPSILON:
        return remaining_kwh
    return _allocate_known_price(
        slots,
        remaining_kwh,
        boost=boost,
        maximum_kwh=required_known_kwh,
    )


def _allocate_unknown_price(
    slots: list[_WorkingSlot],
    remaining_kwh: float,
    *,
    boost: bool,
) -> float:
    # Backwards allocation is deliberate: a rolling caller waits for firm
    # prices and uses an unknown interval only when it becomes the last chance.
    for slot in reversed([item for item in slots if not item.known_price]):
        amount = min(remaining_kwh, slot.headroom(_capacity(slot, boost=boost)))
        slot.price_planned_kwh += amount
        slot.planned_kwh += amount
        if amount > _EPSILON:
            slot.deadline_fallback = True
        remaining_kwh -= amount
        if remaining_kwh <= _EPSILON:
            return 0.0
    return remaining_kwh


def _move_expensive_energy_to_cheap_boost(
    profile: VehicleProfile,
    slots: list[_WorkingSlot],
) -> None:
    """Use >normal power only to preserve a confirmed cheap opportunity."""
    for cheap in slots:
        if not cheap.known_price:
            continue
        boost_headroom = cheap.headroom(cheap.boost_capacity_kwh)
        if boost_headroom <= _EPSILON:
            continue
        cheap_price = float(cheap.source.price_eur_kwh)
        donors = sorted(
            (
                candidate
                for candidate in slots
                if candidate is not cheap
                and candidate.known_price
                and candidate.price_planned_kwh > _EPSILON
                and float(candidate.source.price_eur_kwh) - cheap_price
                >= profile.material_price_difference_eur_kwh - _EPSILON
            ),
            key=lambda item: (-float(item.source.price_eur_kwh), item.source.start),
        )
        for donor in donors:
            amount = min(boost_headroom, donor.price_planned_kwh)
            donor.price_planned_kwh -= amount
            donor.planned_kwh -= amount
            # Any still unused residual PV in the cheap slot is preferred
            # before its confirmed grid price.
            amount_left = amount
            pv_budget = max(
                0.0,
                _non_negative_value(cheap.source.residual_pv_kwh)
                - cheap.pv_preference_kwh,
            )
            pv_amount = min(amount_left, pv_budget)
            cheap.pv_preference_kwh += pv_amount
            amount_left -= pv_amount
            cheap.price_planned_kwh += amount_left
            cheap.planned_kwh += amount
            boost_headroom -= amount
            if boost_headroom <= _EPSILON:
                break


def _build_allocations(slots: list[_WorkingSlot]) -> tuple[EVChargeAllocation, ...]:
    allocations: list[EVChargeAllocation] = []
    for slot in slots:
        if slot.planned_kwh <= _EPSILON:
            continue
        uses_boost = slot.planned_kwh > slot.normal_capacity_kwh + _EPSILON
        power_kw = slot.boost_power_kw if uses_boost else slot.normal_power_kw
        full_slot_energy_kwh = power_kw * slot.source.duration_hours
        slot_fraction = min(1.0, slot.planned_kwh / full_slot_energy_kwh)
        allocations.append(
            EVChargeAllocation(
                start=slot.source.start,
                power_kw=power_kw,
                wallbox_energy_kwh=slot.planned_kwh,
                slot_fraction=slot_fraction,
                duration_minutes=slot_fraction * slot.source.duration_hours * 60,
                price_eur_kwh=(
                    float(slot.source.price_eur_kwh)
                    if slot.known_price
                    else None
                ),
                price_is_known=slot.known_price,
                pv_preferred=slot.pv_preference_kwh > _EPSILON,
                deadline_fallback=slot.deadline_fallback,
            )
        )
    return tuple(allocations)


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _positive_finite(value: float | None) -> bool:
    return _finite(value) and float(value) > 0


def _non_negative_finite(value: float | None) -> bool:
    return _finite(value) and float(value) >= 0


def _non_negative_value(value: float | None) -> float:
    return float(value) if _non_negative_finite(value) else 0.0
