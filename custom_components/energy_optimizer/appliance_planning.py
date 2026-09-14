"""Deterministic planning helpers for flexible household appliances."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .optimizer import ForecastSlot

SLOT_DURATION = timedelta(minutes=15)


@dataclass(frozen=True)
class PendingApplianceJob:
    """One appliance cycle waiting for an economic start time."""

    name: str
    energy_kwh: float
    duration_slots: int
    earliest_start_hour: int
    latest_start_hour: int
    requested_at: datetime | None = None
    finish_by: datetime | None = None
    latest_finish_hour: float | None = None

    @property
    def average_power_kw(self) -> float:
        """Return the cycle's average power used for the overlap limit."""
        return self.energy_kwh / (self.duration_slots * 0.25)


@dataclass(frozen=True)
class RunningApplianceJob:
    """One appliance cycle which is already running."""

    name: str
    energy_kwh: float
    duration_slots: int
    started_at: datetime

    @property
    def average_power_kw(self) -> float:
        """Return the cycle's average power for its remaining run time."""
        return self.energy_kwh / (self.duration_slots * 0.25)


@dataclass(frozen=True)
class InterruptibleLoadRequest:
    """Generic future request for a controllable thermal or charging load.

    A climate adapter can derive ``energy_budget_kwh`` from room temperature,
    target range and weather. The common site optimizer can then choose among
    these eligible slots without inventing appliance-specific exceptions.
    """

    name: str
    energy_budget_kwh: float
    earliest_start: datetime
    finish_by: datetime
    minimum_power_kw: float
    maximum_power_kw: float
    minimum_run_slots: int = 1


def interruptible_candidate_slots(
    slots: list[ForecastSlot],
    request: InterruptibleLoadRequest,
    *,
    not_before: datetime | None = None,
) -> list[int]:
    """Return physically eligible slots for a future interruptible load."""
    if request.energy_budget_kwh <= 0:
        return []
    if request.minimum_power_kw <= 0:
        raise ValueError("Minimum power must be positive")
    if request.maximum_power_kw < request.minimum_power_kw:
        raise ValueError("Maximum power must not be below minimum power")
    if request.minimum_run_slots <= 0:
        raise ValueError("Minimum run time must contain at least one slot")
    eligible: list[int] = []
    for index, slot in enumerate(slots):
        start = datetime.fromisoformat(slot.start)
        end = start + SLOT_DURATION
        if _after(request.earliest_start, start):
            continue
        if not_before is not None and _after(not_before, start):
            continue
        if not _at_or_before(end, request.finish_by):
            continue
        eligible.append(index)
    return eligible


def fixed_request_timestamp(
    stored_requested_at: datetime | None,
    *,
    pause_active: bool,
    observed_transition_at: datetime,
) -> datetime | None:
    """Retain one request time until the pause mode is explicitly cleared.

    ``last_changed`` is suitable only for seeding a newly observed request.  A
    restored Home Assistant helper can receive a new state timestamp during a
    restart, so an already persisted request always wins.
    """
    if not pause_active:
        return None
    return stored_requested_at or observed_transition_at


def marginal_load_cost(
    slot: ForecastSlot,
    additional_energy_kwh: float,
    *,
    export_eur_kwh: float,
) -> float:
    """Value extra load using only the PV surplus it can actually consume."""
    pv_surplus_kwh = max(0.0, slot.pv_kwh - slot.load_kwh)
    pv_energy_kwh = min(additional_energy_kwh, pv_surplus_kwh)
    residual_energy_kwh = max(0.0, additional_energy_kwh - pv_energy_kwh)
    return (
        pv_energy_kwh * export_eur_kwh
        + residual_energy_kwh * slot.price_eur_kwh
    )


def _is_aware(value: datetime) -> bool:
    """Return whether a datetime carries a usable UTC offset."""
    return value.tzinfo is not None and value.utcoffset() is not None


def _comparable_pair(
    first: datetime,
    second: datetime,
) -> tuple[datetime, datetime]:
    """Make two datetimes comparable without discarding real UTC offsets.

    Home Assistant state timestamps and production forecast slots are aware.
    Accepting a legacy naive timestamp is nevertheless useful for tests and
    restored data: it is interpreted as wall time in the aware peer's zone.
    """
    first_aware = _is_aware(first)
    second_aware = _is_aware(second)
    if first_aware and second_aware:
        return first.astimezone(timezone.utc), second.astimezone(timezone.utc)
    if not first_aware and not second_aware:
        return first, second
    if first_aware:
        return first, second.replace(tzinfo=first.tzinfo)
    return first.replace(tzinfo=second.tzinfo), second


def _at_or_before(value: datetime, limit: datetime) -> bool:
    """Compare timestamps correctly even when they use different offsets."""
    comparable_value, comparable_limit = _comparable_pair(value, limit)
    return comparable_value <= comparable_limit


def _after(value: datetime, limit: datetime) -> bool:
    """Compare ordered timestamps correctly across UTC offsets."""
    comparable_value, comparable_limit = _comparable_pair(value, limit)
    return comparable_value > comparable_limit


def _deadline_sort_value(value: datetime | None) -> tuple[int, datetime]:
    """Return a stable key which puts bounded jobs before legacy jobs."""
    if value is None:
        return (1, datetime.max)
    if _is_aware(value):
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return (0, value)


def _start_hour_allowed(job: PendingApplianceJob, start: datetime) -> bool:
    """Return whether a local start hour belongs to the configured window."""
    if job.earliest_start_hour <= job.latest_start_hour:
        return job.earliest_start_hour <= start.hour <= job.latest_start_hour
    # Also support a future window which crosses midnight.
    return start.hour >= job.earliest_start_hour or start.hour <= job.latest_start_hour


def _finishes_within_deadline(
    job: PendingApplianceJob,
    start: datetime,
) -> bool:
    """Return whether the whole cycle, not merely its start, meets the limit."""
    if job.finish_by is None:
        return True
    finish = start + job.duration_slots * SLOT_DURATION
    return _at_or_before(finish, job.finish_by)


def _cycle_window_allowed(job: PendingApplianceJob, start: datetime) -> bool:
    """Return whether the complete cycle fits into its permitted local window."""
    if not _start_hour_allowed(job, start):
        return False
    if job.latest_finish_hour is None:
        return True
    finish = start + job.duration_slots * SLOT_DURATION
    finish_hour = finish.hour + finish.minute / 60 + finish.second / 3600
    return finish.date() == start.date() and finish_hour <= job.latest_finish_hour


def _percentile(values: list[float], fraction: float) -> float:
    """Return a linearly interpolated percentile for a small price series."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _battery_aware_candidate_cost(
    slots: list[ForecastSlot],
    indices: list[int],
    energy_per_slot: float,
    *,
    export_eur_kwh: float,
    baseline_dispatch_plan: list[dict[str, Any]],
    battery_capacity_kwh: float,
    hard_min_soc: float,
    discharge_efficiency: float,
    battery_wear_eur_kwh: float,
    battery_buffer_already_used_kwh: float,
    pv_buffer_already_used_kwh: list[float],
) -> tuple[float, float, list[float]]:
    """Estimate a cycle's incremental whole-system cost.

    The authoritative battery optimizer is intentionally not called once per
    possible appliance start.  Instead its baseline dispatch supplies the two
    scarce resources which matter for a flexible job: PV that would otherwise
    be curtailed and battery energy genuinely left above the hard minimum over
    the remaining horizon.  The final combined load is optimized again once.
    """
    if len(baseline_dispatch_plan) != len(slots):
        raise ValueError("Baseline dispatch must match the forecast horizon")
    if len(pv_buffer_already_used_kwh) != len(slots):
        raise ValueError("PV allocation state must match the forecast horizon")

    residual: list[tuple[float, float]] = []
    pv_used = [0.0] * len(slots)
    cost = 0.0
    for index in indices:
        plan_slot = baseline_dispatch_plan[index]
        free_pv = max(
            0.0,
            float(plan_slot.get("pv_export_kwh", 0.0))
            - pv_buffer_already_used_kwh[index],
        )
        direct_pv = min(energy_per_slot, free_pv)
        pv_used[index] = direct_pv
        cost += direct_pv * export_eur_kwh
        remainder = max(0.0, energy_per_slot - direct_pv)
        if remainder > 0:
            residual.append((slots[index].price_eur_kwh, remainder))

    first_index = indices[0]
    firm_price_end = next(
        (
            index
            for index, slot in enumerate(slots[first_index:], start=first_index)
            if slot.price_is_forecast
        ),
        len(slots),
    )
    future_soc = [
        float(item.get("soc_end", hard_min_soc))
        for item in baseline_dispatch_plan[first_index:firm_price_end]
    ]
    minimum_future_soc = min(future_soc, default=hard_min_soc)
    physical_buffer = max(
        0.0,
        (minimum_future_soc - hard_min_soc)
        / 100
        * battery_capacity_kwh
        * discharge_efficiency,
    )
    battery_buffer = max(0.0, physical_buffer - battery_buffer_already_used_kwh)

    # Remaining battery energy still has a value at the rolling horizon.  Use
    # the same conservative price region as the battery optimizer rather than
    # pretending that this energy is free merely because SoC is high now.
    firm_prices = [
        slot.price_eur_kwh
        for slot in slots[first_index:firm_price_end]
        if not slot.price_is_forecast
    ]
    battery_floor = max(
        export_eur_kwh,
        _percentile(firm_prices[-32:], 0.30) if firm_prices else export_eur_kwh,
    )
    battery_unit_cost = battery_floor + battery_wear_eur_kwh
    battery_used = 0.0
    # Spend the genuinely spare buffer only where it replaces energy more
    # expensive than its retained value.  This preserves it for the highest
    # price portions of a peaky appliance cycle.
    for price, energy in sorted(residual, reverse=True):
        from_battery = min(
            energy,
            battery_buffer,
        ) if price > battery_unit_cost else 0.0
        battery_buffer -= from_battery
        battery_used += from_battery
        cost += from_battery * battery_unit_cost
        cost += (energy - from_battery) * price
    return cost, battery_used, pv_used


def schedule_pending_jobs(
    slots: list[ForecastSlot],
    jobs: list[PendingApplianceJob],
    *,
    export_eur_kwh: float,
    max_combined_power_kw: float,
    max_start_slots: int = 144,
    initial_planned_power_kw: list[float] | None = None,
    decision_diagnostics: dict[str, dict[str, Any]] | None = None,
    not_before: datetime | None = None,
    baseline_dispatch_plan: list[dict[str, Any]] | None = None,
    battery_capacity_kwh: float = 0.0,
    hard_min_soc: float = 0.0,
    discharge_efficiency: float = 1.0,
    battery_wear_eur_kwh: float = 0.0,
) -> tuple[list[ForecastSlot], dict[str, str]]:
    """Place pending cycles without imposing a blanket no-overlap rule.

    The scheduler remains deliberately small and deterministic. Appliances may
    overlap while their combined average cycle power stays within the planning
    limit. Earlier completion deadlines are placed first; equal deadlines then
    use the narrowest start window so an all-day dishwasher cannot take the
    only legal slot of a more constrained cycle. Cost is considered only among
    candidates which finish on time. When a baseline battery dispatch is
    supplied, candidate cost includes the current SoC buffer, future battery
    reserve and PV which would otherwise be curtailed. If the deadline is
    already impossible, the first permitted complete-cycle fallback is returned
    and explicitly diagnosed.
    """
    planned_slots = list(slots)
    schedules: dict[str, str] = {}
    planned_power_kw = (
        list(initial_planned_power_kw)
        if initial_planned_power_kw is not None
        else [0.0] * len(planned_slots)
    )
    if len(planned_power_kw) != len(planned_slots):
        raise ValueError("Initial power profile must match the forecast horizon")
    use_system_cost = baseline_dispatch_plan is not None
    if use_system_cost and battery_capacity_kwh <= 0:
        raise ValueError("Battery capacity must be positive for system planning")
    battery_buffer_used_kwh = 0.0
    pv_buffer_used_kwh = [0.0] * len(planned_slots)

    def candidate_count(job: PendingApplianceJob) -> int:
        last_start = min(
            max_start_slots,
            len(planned_slots) - job.duration_slots + 1,
        )
        return sum(
            _cycle_window_allowed(
                job,
                datetime.fromisoformat(planned_slots[index].start),
            )
            and (
                not_before is None
                or not _after(
                    not_before,
                    datetime.fromisoformat(planned_slots[index].start),
                )
            )
            and _finishes_within_deadline(
                job,
                datetime.fromisoformat(planned_slots[index].start),
            )
            for index in range(max(0, last_start))
        )

    for job in sorted(
        jobs,
        key=lambda item: (
            _deadline_sort_value(item.finish_by),
            candidate_count(item),
            item.name,
        ),
    ):
        if job.duration_slots <= 0 or job.energy_kwh <= 0:
            if decision_diagnostics is not None:
                decision_diagnostics[job.name] = {
                    "status": "invalid_job",
                    "deadline_forced": False,
                    "deadline_risk": True,
                }
            continue
        energy_per_slot = job.energy_kwh / job.duration_slots
        best: tuple[float, int, float, list[float]] | None = None
        earliest_late_fallback: int | None = None
        on_time_start_indices: list[int] = []
        last_start = min(
            max_start_slots,
            len(planned_slots) - job.duration_slots + 1,
        )
        for start_index in range(max(0, last_start)):
            start = datetime.fromisoformat(planned_slots[start_index].start)
            if not _cycle_window_allowed(job, start):
                continue
            if not_before is not None and _after(not_before, start):
                continue
            indices = list(range(start_index, start_index + job.duration_slots))
            if any(
                planned_power_kw[index] + job.average_power_kw
                > max_combined_power_kw + 1e-9
                for index in indices
            ):
                continue
            if not _finishes_within_deadline(job, start):
                if earliest_late_fallback is None:
                    earliest_late_fallback = start_index
                continue
            on_time_start_indices.append(start_index)
            if baseline_dispatch_plan is not None:
                score, battery_used, pv_used = _battery_aware_candidate_cost(
                    planned_slots,
                    indices,
                    energy_per_slot,
                    export_eur_kwh=export_eur_kwh,
                    baseline_dispatch_plan=baseline_dispatch_plan,
                    battery_capacity_kwh=battery_capacity_kwh,
                    hard_min_soc=hard_min_soc,
                    discharge_efficiency=discharge_efficiency,
                    battery_wear_eur_kwh=battery_wear_eur_kwh,
                    battery_buffer_already_used_kwh=battery_buffer_used_kwh,
                    pv_buffer_already_used_kwh=pv_buffer_used_kwh,
                )
            else:
                score = sum(
                    marginal_load_cost(
                        planned_slots[index],
                        energy_per_slot,
                        export_eur_kwh=export_eur_kwh,
                    )
                    for index in indices
                )
                battery_used = 0.0
                pv_used = [0.0] * len(planned_slots)
            candidate = (score, start_index, battery_used, pv_used)
            if (
                best is None
                or score < best[0] - 1e-9
                or (abs(score - best[0]) <= 1e-9 and start_index < best[1])
            ):
                best = candidate

        # Once the deadline cannot be met, cost is no longer the deciding
        # factor.  Pick the earliest permitted complete cycle and report the
        # missed deadline through ``appliance_deadline_diagnostics``.
        if best is None and earliest_late_fallback is not None:
            start_index = earliest_late_fallback
        elif best is not None:
            start_index = best[1]
        else:
            if decision_diagnostics is not None:
                decision_diagnostics[job.name] = {
                    "status": (
                        "deadline_unmet_no_feasible_start"
                        if job.finish_by is not None
                        else "not_scheduled"
                    ),
                    "deadline_forced": False,
                    "deadline_risk": True,
                }
            continue
        deadline_forced = bool(
            job.finish_by is not None
            and best is not None
            and not any(index > start_index for index in on_time_start_indices)
        )
        if decision_diagnostics is not None:
            if best is None:
                status = "deadline_unmet_late_fallback"
                deadline_risk = True
            elif deadline_forced:
                status = "deadline_forced"
                deadline_risk = False
            elif job.finish_by is not None:
                status = "scheduled_on_time"
                deadline_risk = False
            else:
                status = "scheduled_without_deadline"
                deadline_risk = False
            decision_diagnostics[job.name] = {
                "status": status,
                "deadline_forced": deadline_forced,
                "deadline_risk": deadline_risk,
                "system_cost_active": use_system_cost,
                "estimated_incremental_cost_eur": (
                    round(best[0], 4) if best is not None else None
                ),
                "battery_buffer_used_kwh": (
                    round(best[2], 3) if best is not None else 0.0
                ),
            }
        if best is not None:
            battery_buffer_used_kwh += best[2]
            for index, energy in enumerate(best[3]):
                pv_buffer_used_kwh[index] += energy
        schedules[job.name] = planned_slots[start_index].start
        for index in range(start_index, start_index + job.duration_slots):
            planned_power_kw[index] += job.average_power_kw
            planned_slots[index] = replace(
                planned_slots[index],
                load_kwh=planned_slots[index].load_kwh + energy_per_slot,
            )
    return planned_slots, schedules


def appliance_deadline_diagnostics(
    slots: list[ForecastSlot],
    jobs: list[PendingApplianceJob],
    schedules: dict[str, str],
    *,
    max_start_slots: int = 144,
    scheduler_decisions: dict[str, dict[str, bool | str | None]] | None = None,
) -> dict[str, dict[str, bool | str | None]]:
    """Describe whether every pending cycle can still meet its fixed limit."""
    diagnostics: dict[str, dict[str, bool | str | None]] = {}
    for job in jobs:
        scheduled_start_raw = schedules.get(job.name)
        scheduled_start = (
            datetime.fromisoformat(scheduled_start_raw)
            if scheduled_start_raw is not None
            else None
        )
        scheduled_finish = (
            scheduled_start + job.duration_slots * SLOT_DURATION
            if scheduled_start is not None and job.duration_slots > 0
            else None
        )
        last_start = min(
            max_start_slots,
            len(slots) - job.duration_slots + 1,
        )
        later_on_time_start_exists = bool(
            scheduled_start is not None
            and job.finish_by is not None
            and any(
                _after(candidate_start, scheduled_start)
                and _start_hour_allowed(job, candidate_start)
                and _finishes_within_deadline(job, candidate_start)
                for candidate_start in (
                    datetime.fromisoformat(slots[index].start)
                    for index in range(max(0, last_start))
                )
            )
        )
        approximate_deadline_forced = bool(
            job.finish_by is not None
            and scheduled_finish is not None
            and _at_or_before(scheduled_finish, job.finish_by)
            and not later_on_time_start_exists
        )
        scheduler_decision = (
            scheduler_decisions.get(job.name, {})
            if scheduler_decisions is not None
            else {}
        )
        deadline_forced = bool(
            scheduler_decision.get(
                "deadline_forced",
                approximate_deadline_forced,
            )
        )

        if scheduler_decision:
            status = str(scheduler_decision["status"])
            at_risk = bool(scheduler_decision["deadline_risk"])
        elif job.finish_by is None:
            status = "scheduled_without_deadline" if scheduled_start else "not_scheduled"
            at_risk = scheduled_start is None
        elif scheduled_finish is None:
            status = "deadline_unmet_no_feasible_start"
            at_risk = True
        elif _at_or_before(scheduled_finish, job.finish_by):
            status = "deadline_forced" if deadline_forced else "scheduled_on_time"
            at_risk = False
        else:
            status = "deadline_unmet_late_fallback"
            at_risk = True

        diagnostics[job.name] = {
            "status": status,
            "at_risk": at_risk,
            # This is deliberately independent of price confirmation.  The
            # actuator may use it only at the final allowed start, while normal
            # economic starts continue to require firm prices.
            "deadline_forced": deadline_forced,
            "deadline_risk": at_risk,
            "requested_at": (
                job.requested_at.isoformat() if job.requested_at is not None else None
            ),
            "finish_by": job.finish_by.isoformat() if job.finish_by is not None else None,
            "scheduled_start": scheduled_start_raw,
            "scheduled_finish": (
                scheduled_finish.isoformat() if scheduled_finish is not None else None
            ),
        }
    return diagnostics


def scheduled_job_price_confirmation(
    slots: list[ForecastSlot],
    jobs: list[PendingApplianceJob],
    schedules: dict[str, str],
) -> dict[str, bool]:
    """Return whether each scheduled cycle has a firm price basis.

    Only the contiguous confirmed-price prefix may authorize a physical
    appliance start.  Historic replacement prices remain useful for showing a
    tentative later plan, but a confirmed interval after an estimated gap must
    not make that plan executable.
    """
    confirmed_prefix_slots = 0
    for slot in slots:
        if slot.price_is_forecast:
            break
        confirmed_prefix_slots += 1

    slot_indices = {slot.start: index for index, slot in enumerate(slots)}
    jobs_by_name = {job.name: job for job in jobs}
    confirmation: dict[str, bool] = {}
    for name, scheduled_start in schedules.items():
        job = jobs_by_name.get(name)
        start_index = slot_indices.get(scheduled_start)
        confirmation[name] = bool(
            job is not None
            and job.duration_slots > 0
            and start_index is not None
            and start_index + job.duration_slots <= confirmed_prefix_slots
        )
    return confirmation


def add_running_jobs(
    slots: list[ForecastSlot],
    jobs: list[RunningApplianceJob],
    *,
    observed_at: datetime,
    first_slot_uses_live_house_power: bool,
) -> tuple[
    list[ForecastSlot],
    dict[str, dict[str, bool | float | str]],
    list[float],
]:
    """Add the estimated remaining energy of cycles already in progress.

    If a cycle has exceeded its nominal duration but Home Assistant still says
    it is running, one rolling future interval is retained. This is preferable
    to silently dropping a real load while still keeping the estimate bounded.
    """
    planned_slots = list(slots)
    diagnostics: dict[str, dict[str, bool | float | str]] = {}
    running_power_kw = [0.0] * len(planned_slots)
    if not planned_slots:
        return planned_slots, diagnostics, running_power_kw

    first_slot_start = datetime.fromisoformat(planned_slots[0].start)
    for job in jobs:
        if job.duration_slots <= 0 or job.energy_kwh <= 0:
            continue
        started_at = min(job.started_at, observed_at)
        estimated_end = started_at + job.duration_slots * SLOT_DURATION
        overdue = estimated_end <= observed_at
        if overdue:
            # Retain one complete interval beyond the current one until the
            # authoritative run-state reports the actual end.
            estimated_end = max(
                observed_at + SLOT_DURATION,
                first_slot_start + 2 * SLOT_DURATION,
            )

        remaining_start = max(started_at, observed_at)
        remaining_energy_kwh = max(
            0.0,
            job.average_power_kw
            * (estimated_end - remaining_start).total_seconds()
            / 3600,
        )
        added_energy_kwh = 0.0
        for index, slot in enumerate(planned_slots):
            slot_start = datetime.fromisoformat(slot.start)
            slot_end = slot_start + SLOT_DURATION
            overlap_start = max(slot_start, remaining_start)
            overlap_end = min(slot_end, estimated_end)
            if overlap_end <= overlap_start:
                continue
            running_power_kw[index] += job.average_power_kw
            # The live total-house measurement already contains this appliance
            # for the unelapsed part of the first forecast interval.
            if index == 0 and first_slot_uses_live_house_power:
                continue
            energy_kwh = (
                job.average_power_kw
                * (overlap_end - overlap_start).total_seconds()
                / 3600
            )
            planned_slots[index] = replace(
                planned_slots[index],
                load_kwh=planned_slots[index].load_kwh + energy_kwh,
            )
            added_energy_kwh += energy_kwh

        diagnostics[job.name] = {
            "estimated_end": estimated_end.isoformat(),
            "remaining_energy_kwh": round(remaining_energy_kwh, 3),
            "forecast_energy_added_kwh": round(added_energy_kwh, 3),
            "overdue": overdue,
        }
    return planned_slots, diagnostics, running_power_kw
