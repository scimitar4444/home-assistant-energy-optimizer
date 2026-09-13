"""State machine for one stable, metered grid-charge block."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import isfinite
from typing import Mapping, Sequence

GRID_CHARGE_FLOW_THRESHOLD_KWH = 0.005
GRID_CHARGE_COUNTER_TOLERANCE_KWH = 0.005
GRID_CHARGE_START_GRACE_SECONDS = 120
GRID_CHARGE_NO_PROGRESS_SECONDS = 360
SLOT_DURATION = timedelta(minutes=15)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and isfinite(float(value))


def _parse_aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class GridChargeSlice:
    """One frozen quarter-hour slice inside a charge block."""

    start: datetime
    end: datetime
    grid_setpoint_w: int
    grid_to_battery_ac_kwh: float


@dataclass(frozen=True)
class GridChargeSession:
    """Persistent state for a contiguous sequence of charge slices."""

    state: str
    start: datetime
    activated_at: datetime
    end: datetime
    baseline_charge_counter_kwh: float
    last_charge_counter_kwh: float
    last_progress_at: datetime
    target_stored_kwh: float
    delivered_stored_kwh: float
    target_minimum_soc: int
    reason: str
    stop_reason: str
    slices: tuple[GridChargeSlice, ...]

    @property
    def active(self) -> bool:
        """Return whether this session may request a positive grid setpoint."""
        return self.state == "ACTIVE"

    def slice_at(self, now: datetime) -> GridChargeSlice | None:
        """Return the frozen slice containing ``now``."""
        return next(
            (item for item in self.slices if item.start <= now < item.end),
            None,
        )


def grid_charge_start_allowed(
    now: datetime,
    slot_start: datetime,
    *,
    grace_seconds: int = GRID_CHARGE_START_GRACE_SECONDS,
) -> bool:
    """Allow a new block only directly after its quarter-hour boundary."""
    if (
        now.tzinfo is None
        or slot_start.tzinfo is None
        or slot_start.minute % 15 != 0
        or slot_start.second != 0
        or slot_start.microsecond != 0
    ):
        return False
    delay = (now - slot_start).total_seconds()
    return 0 <= delay <= max(0, grace_seconds)


def build_grid_charge_session(
    *,
    now: datetime,
    plan: Sequence[Mapping[str, object]],
    baseline_charge_counter_kwh: float,
    first_grid_setpoint_w: int,
    target_minimum_soc: int,
    reason: str,
    charge_efficiency: float,
    maximum_grid_setpoint_w: int,
) -> GridChargeSession | None:
    """Freeze the contiguous firm-price charge prefix of a new plan."""
    if (
        not plan
        or not _finite(baseline_charge_counter_kwh)
        or float(baseline_charge_counter_kwh) < 0
        or not _finite(charge_efficiency)
        or not 0 < float(charge_efficiency) <= 1
    ):
        return None

    first_start = _parse_aware_datetime(plan[0].get("start"))
    if first_start is None or not grid_charge_start_allowed(now, first_start):
        return None

    slices: list[GridChargeSlice] = []
    previous_end: datetime | None = None
    total_ac_kwh = 0.0
    ceiling_w = max(0, int(maximum_grid_setpoint_w))
    for index, item in enumerate(plan):
        start = _parse_aware_datetime(item.get("start"))
        flow = item.get("grid_to_battery_kwh")
        if (
            start is None
            or not _finite(flow)
            or float(flow) <= GRID_CHARGE_FLOW_THRESHOLD_KWH
            or bool(item.get("price_is_forecast", False))
            or (previous_end is not None and start != previous_end)
        ):
            break
        end = start + SLOT_DURATION
        if index == 0:
            setpoint_w = int(first_grid_setpoint_w)
        else:
            grid_import = item.get("grid_import_kwh")
            if not _finite(grid_import) or float(grid_import) < float(flow):
                return None
            setpoint_w = round(float(grid_import) * 1000 / 0.25)
        setpoint_w = max(0, min(ceiling_w, setpoint_w))
        if setpoint_w <= 0:
            return None
        slices.append(
            GridChargeSlice(
                start=start,
                end=end,
                grid_setpoint_w=setpoint_w,
                grid_to_battery_ac_kwh=float(flow),
            )
        )
        total_ac_kwh += float(flow)
        previous_end = end

    target_stored_kwh = total_ac_kwh * float(charge_efficiency)
    if not slices or target_stored_kwh <= GRID_CHARGE_COUNTER_TOLERANCE_KWH:
        return None

    baseline = float(baseline_charge_counter_kwh)
    return GridChargeSession(
        state="ACTIVE",
        start=first_start,
        activated_at=now,
        end=slices[-1].end,
        baseline_charge_counter_kwh=baseline,
        last_charge_counter_kwh=baseline,
        last_progress_at=now,
        target_stored_kwh=target_stored_kwh,
        delivered_stored_kwh=0.0,
        target_minimum_soc=max(0, min(100, round(target_minimum_soc))),
        reason=reason,
        stop_reason="",
        slices=tuple(slices),
    )


def evaluate_grid_charge_session(
    session: GridChargeSession,
    *,
    now: datetime,
    charge_counter_kwh: float | None,
    battery_soc: float | None,
    bms_max_charge_current_a: float | None = None,
    require_bms_current: bool = False,
    control_enabled: bool = True,
    no_progress_seconds: int = GRID_CHARGE_NO_PROGRESS_SECONDS,
) -> GridChargeSession:
    """Advance a block using physical measurements, never a new forecast."""
    if not session.active:
        return session
    if not control_enabled:
        return replace(session, state="ABORTED", stop_reason="control_disabled")
    if now.tzinfo is None or now >= session.end:
        return replace(session, state="ABORTED", stop_reason="block_ended")
    if not _finite(charge_counter_kwh):
        return replace(session, state="ABORTED", stop_reason="charge_counter_invalid")
    counter = float(charge_counter_kwh)
    if counter + GRID_CHARGE_COUNTER_TOLERANCE_KWH < session.last_charge_counter_kwh:
        return replace(session, state="ABORTED", stop_reason="charge_counter_reset")
    if not _finite(battery_soc):
        return replace(session, state="ABORTED", stop_reason="battery_soc_invalid")
    if float(battery_soc) >= 100:
        return replace(session, state="SATISFIED", stop_reason="battery_full")
    bms_current_valid = _finite(bms_max_charge_current_a) and (
        0 <= float(bms_max_charge_current_a) < 65535
    )
    if require_bms_current and not bms_current_valid:
        return replace(session, state="ABORTED", stop_reason="bms_current_invalid")
    if bms_current_valid and float(bms_max_charge_current_a) <= 0:
        return replace(session, state="SATISFIED", stop_reason="bms_charge_blocked")

    delivered = max(0.0, counter - session.baseline_charge_counter_kwh)
    last_progress_at = session.last_progress_at
    last_counter = session.last_charge_counter_kwh
    if counter > last_counter + GRID_CHARGE_COUNTER_TOLERANCE_KWH / 2:
        last_progress_at = now
        last_counter = counter
    updated = replace(
        session,
        delivered_stored_kwh=delivered,
        last_charge_counter_kwh=last_counter,
        last_progress_at=last_progress_at,
    )
    if delivered + GRID_CHARGE_COUNTER_TOLERANCE_KWH >= session.target_stored_kwh:
        return replace(updated, state="SATISFIED", stop_reason="energy_target_reached")
    if (now - last_progress_at).total_seconds() > max(0, no_progress_seconds):
        return replace(updated, state="ABORTED", stop_reason="charge_counter_stalled")
    if session.slice_at(now) is None:
        return replace(updated, state="ABORTED", stop_reason="outside_frozen_block")
    return updated


def terminal_session_may_clear(
    session: GridChargeSession,
    *,
    proposed_grid_charge_now: bool,
    now: datetime,
) -> bool:
    """Release the restart lock only after the old plan has a real gap."""
    if session.active:
        return False
    if now < session.end:
        return False
    return not proposed_grid_charge_now
