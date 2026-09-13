"""Deterministic planning helpers for flexible household appliances."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

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


def schedule_pending_jobs(
    slots: list[ForecastSlot],
    jobs: list[PendingApplianceJob],
    *,
    export_eur_kwh: float,
    max_combined_power_kw: float,
    max_start_slots: int = 144,
    initial_planned_power_kw: list[float] | None = None,
) -> tuple[list[ForecastSlot], dict[str, str]]:
    """Place pending cycles without imposing a blanket no-overlap rule.

    The scheduler remains deliberately small and deterministic. Appliances may
    overlap while their combined average cycle power stays within the planning
    limit. Jobs with the narrowest start window are placed first so an all-day
    dishwasher cannot take the only legal slot of a more constrained cycle.
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

    def candidate_count(job: PendingApplianceJob) -> int:
        return sum(
            job.earliest_start_hour
            <= datetime.fromisoformat(slot.start).hour
            <= job.latest_start_hour
            for slot in planned_slots[:max_start_slots]
        )

    for job in sorted(jobs, key=lambda item: (candidate_count(item), item.name)):
        if job.duration_slots <= 0 or job.energy_kwh <= 0:
            continue
        energy_per_slot = job.energy_kwh / job.duration_slots
        best: tuple[float, int] | None = None
        last_start = min(
            max_start_slots,
            len(planned_slots) - job.duration_slots + 1,
        )
        for start_index in range(max(0, last_start)):
            start = datetime.fromisoformat(planned_slots[start_index].start)
            if not (
                job.earliest_start_hour
                <= start.hour
                <= job.latest_start_hour
            ):
                continue
            indices = range(start_index, start_index + job.duration_slots)
            if any(
                planned_power_kw[index] + job.average_power_kw
                > max_combined_power_kw + 1e-9
                for index in indices
            ):
                continue
            score = sum(
                marginal_load_cost(
                    planned_slots[index],
                    energy_per_slot,
                    export_eur_kwh=export_eur_kwh,
                )
                for index in indices
            )
            candidate = (score, start_index)
            if best is None or candidate < best:
                best = candidate

        if best is None:
            continue
        start_index = best[1]
        schedules[job.name] = planned_slots[start_index].start
        for index in range(start_index, start_index + job.duration_slots):
            planned_power_kw[index] += job.average_power_kw
            planned_slots[index] = replace(
                planned_slots[index],
                load_kwh=planned_slots[index].load_kwh + energy_per_slot,
            )
    return planned_slots, schedules


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
