"""Pure construction of one authoritative Victron control command."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from math import isfinite

from .const import (
    CONTROL_COMMAND_VALID_MINUTES,
    CONTROL_DATA_QUALITY_MIN_PERCENT,
    HARD_MIN_SOC,
    MAX_CONTROL_SOC,
    NORMAL_CHARGE_CURRENT_A,
    PV_STORE_GRID_HEADROOM_W,
    VICTRON_GRID_SETPOINT_MAX_W,
    VICTRON_RELEASE_HEADROOM_PERCENT,
)

VALID_ACTIONS = {
    "DISCHARGE",
    "RESERVE",
    "PV_SURPLUS",
    "PV_STORE",
    "GRID_CHARGE",
}
POSITIVE_GRID_SETPOINT_ACTIONS = {"PV_STORE", "GRID_CHARGE"}


@dataclass(frozen=True)
class ControlCommand:
    """One immutable command consumed atomically by the HA automation."""

    revision: str
    valid_until: str
    quality_ok: bool
    action: str
    minimum_soc: int
    charge_current_a: int
    grid_setpoint_w: int
    reason: str

    def as_dict(self) -> dict[str, str | bool | int]:
        """Return a Home-Assistant attribute friendly representation."""
        return asdict(self)


def _finite_number(value: float) -> bool:
    return isinstance(value, (int, float)) and isfinite(float(value))


def bounded_pv_store_grid_setpoint_w(
    *,
    planned_grid_to_house_w: float,
    live_house_w: float,
    live_pv_w: float,
    maximum_grid_setpoint_w: int = VICTRON_GRID_SETPOINT_MAX_W,
    headroom_w: int = PV_STORE_GRID_HEADROOM_W,
) -> int:
    """Bound PV_STORE to real house demand with a small import headroom.

    The unavoidable deficit is supplied first.  Only the simultaneous
    house/PV overlap may additionally be shifted from PV-direct-use to the
    battery.  The final 30 W headroom prevents measurement noise from turning
    the intended house import into unintended grid-to-battery charging.
    """
    if not all(
        _finite_number(value)
        for value in (planned_grid_to_house_w, live_house_w, live_pv_w)
    ):
        return 0
    house_w = max(0.0, float(live_house_w))
    pv_w = max(0.0, float(live_pv_w))
    planned_w = max(0.0, float(planned_grid_to_house_w))
    unavoidable_deficit_w = max(0.0, house_w - pv_w)
    divertible_pv_w = min(house_w, pv_w)
    safe_live_limit_w = max(
        0.0,
        unavoidable_deficit_w
        + divertible_pv_w
        - headroom_w,
    )
    requested_w = min(
        safe_live_limit_w,
        max(unavoidable_deficit_w, planned_w),
    )
    return round(min(float(maximum_grid_setpoint_w), requested_w))


def build_control_command(
    *,
    now: datetime,
    action: str,
    model_minimum_soc: float,
    current_soc: float,
    data_quality_percent: float,
    requested_charge_current_a: float,
    requested_grid_setpoint_w: float,
    current_price_is_known: bool,
    reason: str,
    hard_min_soc: float = HARD_MIN_SOC,
    max_control_soc: float = MAX_CONTROL_SOC,
    normal_charge_current_a: int = NORMAL_CHARGE_CURRENT_A,
    release_headroom_percent: float = VICTRON_RELEASE_HEADROOM_PERCENT,
    maximum_grid_setpoint_w: int = VICTRON_GRID_SETPOINT_MAX_W,
) -> ControlCommand:
    """Translate the economic result once into the effective Victron command.

    The four-point release is a measured BatteryLife adapter characteristic,
    not another economic decision.  Invalid or weak input produces an explicit
    fail-open command instead of silently retaining an old high reserve.
    """
    valid_inputs = (
        action in VALID_ACTIONS
        and _finite_number(model_minimum_soc)
        and hard_min_soc <= float(model_minimum_soc) <= 100
        and _finite_number(current_soc)
        and 0 <= float(current_soc) <= 100
        and _finite_number(data_quality_percent)
        and float(data_quality_percent) >= CONTROL_DATA_QUALITY_MIN_PERCENT
        and _finite_number(requested_charge_current_a)
        and 0 < float(requested_charge_current_a) <= normal_charge_current_a
        and _finite_number(requested_grid_setpoint_w)
        and float(requested_grid_setpoint_w) >= 0
    )

    if not valid_inputs:
        effective_action = "DEGRADED"
        effective_soc = round(hard_min_soc)
        effective_current = normal_charge_current_a
        effective_grid_setpoint = 0
        effective_reason = "Ersatzbetrieb wegen unvollständiger oder unsicherer Daten"
    else:
        effective_action = action
        effective_soc = float(model_minimum_soc)
        if action == "DISCHARGE":
            effective_soc -= release_headroom_percent
        elif action == "PV_SURPLUS":
            effective_soc = min(
                effective_soc,
                float(current_soc) - release_headroom_percent,
            )
        effective_soc = round(
            max(hard_min_soc, min(max_control_soc, effective_soc))
        )
        effective_current = round(float(requested_charge_current_a))
        requested_grid_setpoint = round(
            min(
                maximum_grid_setpoint_w,
                max(0.0, float(requested_grid_setpoint_w)),
            )
        )
        if action in POSITIVE_GRID_SETPOINT_ACTIONS and (
            not current_price_is_known or requested_grid_setpoint <= 0
        ):
            # Without a firm current tariff and a positive, physically bounded
            # target, reserving is safe but deliberately storing/charging is not.
            effective_action = "RESERVE"
            effective_soc = round(
                max(
                    hard_min_soc,
                    min(max_control_soc, model_minimum_soc, current_soc),
                )
            )
            effective_current = normal_charge_current_a
            effective_grid_setpoint = 0
            effective_reason = (
                "Netzgestützten Betrieb ausgesetzt: aktueller Preis oder "
                "Netzsollwert ist nicht sicher"
            )
        else:
            effective_grid_setpoint = (
                requested_grid_setpoint
                if action in POSITIVE_GRID_SETPOINT_ACTIONS
                else 0
            )
            effective_reason = reason

    revision = now.isoformat(timespec="seconds")
    current_quarter = now.replace(
        minute=(now.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    # A command calculated from one tariff/PV interval must never survive into
    # the next one.  This is especially important on the small HA host, where a
    # full 48-hour optimization can finish shortly after a quarter boundary.
    valid_until = min(
        now + timedelta(minutes=CONTROL_COMMAND_VALID_MINUTES),
        current_quarter + timedelta(minutes=15),
    )
    return ControlCommand(
        revision=revision,
        valid_until=valid_until.isoformat(timespec="seconds"),
        quality_ok=valid_inputs,
        action=effective_action,
        minimum_soc=effective_soc,
        charge_current_a=effective_current,
        grid_setpoint_w=effective_grid_setpoint,
        reason=effective_reason,
    )
