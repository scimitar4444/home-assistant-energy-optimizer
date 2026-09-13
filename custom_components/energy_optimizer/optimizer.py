"""Battery dispatch calculation for the Home Assistant Energy Optimizer."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

ENERGY_STEP_KWH = 0.02
FLOW_STEP_KWH = 0.01


@dataclass(frozen=True)
class ForecastSlot:
    """One quarter-hour forecast interval."""

    start: str
    price_eur_kwh: float
    load_kwh: float
    pv_kwh: float
    price_is_forecast: bool = False
    max_grid_charge_kw: float = 0.0


@dataclass(frozen=True)
class OptimizationResult:
    """Relevant optimizer output and the reconstructed schedule."""

    target_min_soc: int
    action: str
    reason: str
    expected_cost_eur: float
    expected_grid_import_kwh: float
    expected_grid_import_first_24h_kwh: float
    expected_grid_import_second_24h_kwh: float
    expected_grid_charge_kwh: float
    expected_export_kwh: float
    expected_battery_discharge_kwh: float
    projected_min_soc: float
    pv_headroom_required_percent: float
    plan: list[dict[str, float | str | bool]]


def _percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    position = max(0.0, min(1.0, fraction)) * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def pv_headroom_required_percent(
    slots: list[ForecastSlot],
    *,
    capacity_kwh: float,
    charge_efficiency: float = 0.94,
    discharge_efficiency: float = 0.94,
    horizon_slots: int = 96,
) -> float:
    """Return storage space needed for the next 24 hours' net PV rise.

    Loads before and during the solar window, including scheduled appliances,
    create storage space and are therefore subtracted chronologically.  The
    maximum cumulative rise is the amount the battery must be able to accept
    without curtailing PV.
    """
    if capacity_kwh <= 0:
        raise ValueError("Battery capacity must be positive")
    cumulative_kwh = 0.0
    maximum_rise_kwh = 0.0
    for slot in slots[:horizon_slots]:
        net_pv_kwh = slot.pv_kwh - slot.load_kwh
        if net_pv_kwh >= 0:
            cumulative_kwh += net_pv_kwh * charge_efficiency
        else:
            cumulative_kwh += net_pv_kwh / discharge_efficiency
        maximum_rise_kwh = max(maximum_rise_kwh, cumulative_kwh)
    return max(0.0, min(100.0, maximum_rise_kwh / capacity_kwh * 100))


def _optimize_battery_core(
    slots: list[ForecastSlot],
    current_soc: float,
    *,
    capacity_kwh: float,
    hard_min_soc: float,
    battery_wear_eur_kwh: float,
    export_eur_kwh: float,
    charge_efficiency: float = 0.94,
    discharge_efficiency: float = 0.94,
    max_charge_kw: float = 1.9,
    max_discharge_kw: float = 2.5,
    grid_charge_margin_eur_kwh: float = 0.03,
    min_grid_charge_kwh: float = 0.10,
    pv_curtailment_penalty_eur_kwh: float = 0.001,
    allow_active_storage: bool = True,
    safe_boundary_slot: int | None = None,
    safe_boundary_energy_kwh: float | None = None,
    energy_trace: list[float] | None = None,
) -> OptimizationResult:
    """Minimize predicted import cost, including permitted grid charging."""
    if not slots:
        raise ValueError("At least one forecast slot is required")

    maximum_soc = 100.0
    slot_hours = 0.25
    # A 20-Wh lattice is 0.4 percentage points on the installed 5-kWh
    # battery.  It halves each state vector and roughly halves the nested
    # state/action work, which makes the two-pass firm-price guard practical
    # on the Home Assistant Pi without materially changing the ESS target.
    energy_step = ENERGY_STEP_KWH
    flow_step = FLOW_STEP_KWH
    minimum_energy = capacity_kwh * hard_min_soc / 100
    maximum_energy = capacity_kwh
    state_count = round((maximum_energy - minimum_energy) / energy_step) + 1
    energies = [minimum_energy + index * energy_step for index in range(state_count)]

    def state_index(energy: float) -> int:
        value = round(
            (max(minimum_energy, min(maximum_energy, energy)) - minimum_energy)
            / energy_step
        )
        return max(0, min(state_count - 1, value))

    def interpolated_cost(costs: list[float], energy: float) -> float:
        """Value a sub-state-step battery level without inventing energy."""
        position = (
            max(minimum_energy, min(maximum_energy, energy)) - minimum_energy
        ) / energy_step
        lower = max(0, min(state_count - 1, int(position)))
        upper = min(state_count - 1, lower + 1)
        weight = position - lower
        # Avoid ``inf * 0 -> nan`` next to a hard boundary.  Exact lattice
        # states must retain their finite value even when the adjacent state
        # is infeasible.
        if weight <= 1e-12:
            return costs[lower]
        if weight >= 1 - 1e-12:
            return costs[upper]
        return costs[lower] * (1 - weight) + costs[upper] * weight

    # Only firm prices may assign a value to energy left beyond the rolling
    # 48-hour horizon.  Using statistically estimated prices here made the
    # optimizer buy real grid energy merely to preserve a largely full battery
    # for an imaginary period after the forecast ended.
    terminal_prices = [
        slot.price_eur_kwh
        for slot in slots[-min(32, len(slots)):]
        if not slot.price_is_forecast
    ]
    terminal_value = max(
        export_eur_kwh,
        (
            _percentile(terminal_prices, 0.30) * discharge_efficiency
            - battery_wear_eur_kwh
            if terminal_prices
            else export_eur_kwh
        ),
    )
    future_cost = [
        -(energy - minimum_energy) * terminal_value for energy in energies
    ]
    # Policy tuple fields are, in order: next state, battery-to-load,
    # total grid import, curtailed/exported PV, grid-to-battery,
    # PV-to-battery and PV-to-load.
    policies: list[
        list[tuple[int, float, float, float, float, float, float]]
    ] = []

    for slot_index in reversed(range(len(slots))):
        slot = slots[slot_index]
        at_safe_boundary = (
            safe_boundary_slot is not None
            and safe_boundary_energy_kwh is not None
            and slot_index == safe_boundary_slot - 1
        )
        current_cost: list[float] = []
        current_policy: list[
            tuple[int, float, float, float, float, float, float]
        ] = []
        for energy in energies:
                pv_direct = min(slot.load_kwh, slot.pv_kwh)
                unused_pv = max(0.0, slot.pv_kwh - pv_direct)
                net_load = max(0.0, slot.load_kwh - pv_direct)
                maximum_delivered = min(
                    net_load,
                    max_discharge_kw * slot_hours,
                    max(0.0, energy - minimum_energy) * discharge_efficiency,
                )
                step_count = int(maximum_delivered / flow_step)
                candidates = [
                    candidate * flow_step for candidate in range(step_count + 1)
                ]
                if maximum_delivered - candidates[-1] > 0.002:
                    candidates.append(maximum_delivered)
                best: tuple[
                    float,
                    int,
                    float,
                    float,
                    float,
                    float,
                    float,
                    float,
                ] | None = None
                for delivered in candidates:
                    next_energy = energy - delivered / discharge_efficiency
                    if (
                        at_safe_boundary
                        and next_energy > safe_boundary_energy_kwh + 1e-9
                    ):
                        continue
                    next_index = state_index(next_energy)
                    grid_import = max(0.0, net_load - delivered)
                    cost = (
                        grid_import * slot.price_eur_kwh
                        + delivered * battery_wear_eur_kwh
                        + unused_pv
                        * (pv_curtailment_penalty_eur_kwh - export_eur_kwh)
                        + interpolated_cost(future_cost, next_energy)
                    )
                    candidate = (
                        cost,
                        next_index,
                        delivered,
                        grid_import,
                        unused_pv,
                        0.0,
                        0.0,
                        pv_direct,
                    )
                    if (
                        best is None
                        or cost < best[0] - 1e-10
                        or (
                            abs(cost - best[0]) <= 1e-10
                            and delivered > best[2] + 0.005
                        )
                    ):
                        best = candidate

                # A charging interval may allocate PV either directly to the
                # house or to the battery.  Diverting PV from the house means
                # the grid supplies that same load.  This is deliberately a
                # real economic choice: at a sufficiently cheap daytime price
                # it can preserve PV energy for a more expensive evening.
                pv_surplus = max(0.0, slot.pv_kwh - slot.load_kwh)
                # Statistical prices are useful for a broad reserve tendency,
                # but are not firm enough to trigger an active energy purchase
                # or to divert PV away from the house.  In estimated intervals
                # PV therefore follows the ordinary priority: house first,
                # only the physical surplus may charge the battery.
                if slot.price_is_forecast or not allow_active_storage:
                    grid_charge_limit = 0.0
                    pv_charge_limit = pv_surplus
                else:
                    grid_charge_limit = (
                        max(0.0, slot.max_grid_charge_kw) * slot_hours
                    )
                    pv_charge_limit = max(0.0, slot.pv_kwh)
                maximum_charge_input = min(
                    max(0.0, max_charge_kw) * slot_hours,
                    max(0.0, maximum_energy - energy) / charge_efficiency,
                    pv_charge_limit + grid_charge_limit,
                )
                charge_step_count = int(maximum_charge_input / flow_step)
                charge_candidates = [
                    candidate * flow_step
                    for candidate in range(1, charge_step_count + 1)
                ]
                if (
                    maximum_charge_input > 0.0
                    and (
                        not charge_candidates
                        or maximum_charge_input - charge_candidates[-1] > 0.002
                    )
                ):
                    charge_candidates.append(maximum_charge_input)
                for charge_input in charge_candidates:
                    # For a fixed total charge, only three PV allocations can
                    # be optimal because the immediate cost is piecewise
                    # linear: either boundary, or exactly the PV surplus where
                    # further PV charging begins to displace direct use.
                    minimum_pv_charge = max(0.0, charge_input - grid_charge_limit)
                    maximum_pv_charge = min(pv_charge_limit, charge_input)
                    pv_charge_candidates = {
                        minimum_pv_charge,
                        maximum_pv_charge,
                        max(minimum_pv_charge, min(maximum_pv_charge, pv_surplus)),
                        max(
                            minimum_pv_charge,
                            min(
                                maximum_pv_charge,
                                charge_input - min_grid_charge_kwh,
                            ),
                        ),
                    }
                    for pv_charge in pv_charge_candidates:
                        grid_charge = max(0.0, charge_input - pv_charge)
                        if grid_charge <= 1e-9:
                            grid_charge = 0.0
                        elif grid_charge < min_grid_charge_kwh:
                            continue
                        remaining_pv = max(0.0, slot.pv_kwh - pv_charge)
                        pv_to_load = min(slot.load_kwh, remaining_pv)
                        grid_to_load = max(0.0, slot.load_kwh - pv_to_load)
                        exported = max(
                            0.0,
                            remaining_pv - pv_to_load,
                        )
                        stored = charge_input * charge_efficiency
                        next_energy = energy + stored
                        if (
                            at_safe_boundary
                            and next_energy > safe_boundary_energy_kwh + 1e-9
                        ):
                            continue
                        next_index = state_index(next_energy)
                        grid_import = grid_to_load + grid_charge
                        # A genuinely negative all-in price needs no additional
                        # arbitrage hurdle. Battery wear and conversion losses
                        # remain part of the future dispatch calculation.
                        charge_margin = (
                            0.0
                            if slot.price_eur_kwh < 0
                            else grid_charge_margin_eur_kwh
                        )
                        cost = (
                            grid_import * slot.price_eur_kwh
                            + grid_charge * charge_margin
                            + exported
                            * (pv_curtailment_penalty_eur_kwh - export_eur_kwh)
                            + interpolated_cost(future_cost, next_energy)
                        )
                        candidate = (
                            cost,
                            next_index,
                            0.0,
                            grid_import,
                            exported,
                            grid_charge,
                            pv_charge,
                            pv_to_load,
                        )
                        if best is None or cost < best[0] - 1e-10:
                            best = candidate
                if best is None:
                    # This state cannot shed enough energy in the boundary
                    # slot to satisfy the firm-price guard.  It remains in the
                    # lattice for interpolation, but no earlier optimal path
                    # may select it.
                    current_cost.append(float("inf"))
                    current_policy.append(
                        (state_index(energy), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
                    )
                    continue
                current_cost.append(best[0])
                current_policy.append(
                    (
                        best[1],
                        best[2],
                        best[3],
                        best[4],
                        best[5],
                        best[6],
                        best[7],
                    )
                )
        future_cost = current_cost
        policies.append(current_policy)

    policies.reverse()
    initial_energy = capacity_kwh * max(hard_min_soc, min(maximum_soc, current_soc)) / 100
    index = state_index(initial_energy)
    plan: list[dict[str, float | str | bool]] = []
    planned_soc_starts: list[float] = []
    planned_soc_ends: list[float] = []
    total_import = total_export = total_discharge = total_grid_charge = 0.0
    first_24h_import = 0.0
    minimum_planned_energy = energies[index]
    for slot, policy in zip(slots, policies, strict=True):
        start_energy = energies[index]
        (
            next_index,
            discharge,
            grid_import,
            exported,
            grid_charge,
            pv_charge,
            pv_to_load,
        ) = policy[index]
        end_energy = energies[next_index]
        start_soc = start_energy / capacity_kwh * 100
        end_soc = end_energy / capacity_kwh * 100
        planned_soc_starts.append(start_soc)
        planned_soc_ends.append(end_soc)
        total_import += grid_import
        if len(plan) < 96:
            first_24h_import += grid_import
        total_export += exported
        total_discharge += discharge
        total_grid_charge += grid_charge
        minimum_planned_energy = min(minimum_planned_energy, end_energy)
        plan.append(
            {
                "start": slot.start,
                "price_ct_kwh": round(slot.price_eur_kwh * 100, 2),
                "load_kwh": round(slot.load_kwh, 3),
                "pv_kwh": round(slot.pv_kwh, 3),
                "soc_start": round(start_soc, 1),
                "soc_end": round(end_soc, 1),
                "battery_to_load_kwh": round(discharge, 3),
                "grid_to_battery_kwh": round(grid_charge, 3),
                "pv_to_battery_kwh": round(pv_charge, 3),
                "pv_to_load_kwh": round(pv_to_load, 3),
                "pv_export_kwh": round(exported, 3),
                "grid_import_kwh": round(grid_import, 3),
                "price_is_forecast": slot.price_is_forecast,
            }
        )
        if energy_trace is not None:
            energy_trace.append(end_energy)
        index = next_index

    first_deficit_index = next(
        (
            index
            for index, item in enumerate(plan)
            if float(item["battery_to_load_kwh"]) > 0.005
            or float(item["grid_import_kwh"])
            - float(item["grid_to_battery_kwh"])
            > 0.005
        ),
        0,
    )
    first_deficit = plan[first_deficit_index]
    first_deficit_discharge = float(first_deficit["battery_to_load_kwh"])
    # Use the full-precision model state for the Victron threshold.  The plan
    # shown in diagnostics is rounded for readability and must not accidentally
    # lock the battery at its current whole-percentage SoC.
    reserve_after = planned_soc_ends[first_deficit_index]
    if first_deficit_discharge <= 0.005:
        reserve_after = planned_soc_starts[first_deficit_index]
    else:
        # Keep the live threshold responsive to loads smaller than the 20-Wh
        # state lattice.  The economic DP uses interpolation, while Victron's
        # immediate reserve should reflect the actual first discharge.
        reserve_after = (
            energies[state_index(
                capacity_kwh
                * planned_soc_starts[first_deficit_index]
                / 100
            )]
            - first_deficit_discharge / discharge_efficiency
        ) / capacity_kwh * 100

    first_grid_charge = float(plan[0]["grid_to_battery_kwh"])
    first_battery_discharge = float(plan[0]["battery_to_load_kwh"])
    first_pv_charge = float(plan[0]["pv_to_battery_kwh"])
    first_pv_surplus = max(0.0, slots[0].pv_kwh - slots[0].load_kwh)
    pv_is_diverted_from_load = first_pv_charge > first_pv_surplus + 0.005
    if first_grid_charge > 0.005:
        action = "GRID_CHARGE"
        reason = "Günstigen Strom für spätere teure Stunden laden"
    elif first_battery_discharge > 0.005:
        action = "DISCHARGE"
        reason = "Batterieeinsatz ist jetzt wirtschaftlich"
    elif pv_is_diverted_from_load:
        action = "PV_STORE"
        reason = "Günstiges Netz versorgt das Haus; PV lädt für teure Stunden"
    elif slots[0].pv_kwh >= slots[0].load_kwh:
        action = "PV_SURPLUS"
        reason = "PV versorgt das Haus; Überschuss lädt die Batterie"
    elif first_battery_discharge <= 0.005:
        action = "RESERVE"
        reason = "Batterie für wertvollere Stunden halten"
    else:  # pragma: no cover - guarded by the exhaustive branches above
        raise AssertionError("Unreachable optimizer action")

    # The Victron register supports finer values than the UI slider suggests.
    # Whole percentages keep the live reserve close to the planned end of the
    # current interval instead of releasing up to another 4.9 % of the battery.
    if action == "GRID_CHARGE":
        charge_target_soc = current_soc
        for item in plan:
            if float(item["grid_to_battery_kwh"]) <= 0.005:
                break
            charge_target_soc = max(charge_target_soc, float(item["soc_end"]))
        target_min_soc = ceil(charge_target_soc)
    else:
        target_min_soc = int(max(hard_min_soc, min(current_soc, reserve_after)))
    target_min_soc = max(round(hard_min_soc), min(95, target_min_soc))
    expected_cost = sum(
        float(item["grid_import_kwh"]) * slot.price_eur_kwh
        + float(item["battery_to_load_kwh"]) * battery_wear_eur_kwh
        for item, slot in zip(plan, slots, strict=True)
    ) - total_export * export_eur_kwh
    required_pv_headroom = pv_headroom_required_percent(
        slots,
        capacity_kwh=capacity_kwh,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
    )

    return OptimizationResult(
        target_min_soc=target_min_soc,
        action=action,
        reason=reason,
        expected_cost_eur=round(expected_cost, 3),
        expected_grid_import_kwh=round(total_import, 3),
        expected_grid_import_first_24h_kwh=round(first_24h_import, 3),
        expected_grid_import_second_24h_kwh=round(
            max(0.0, total_import - first_24h_import), 3
        ),
        expected_grid_charge_kwh=round(total_grid_charge, 3),
        expected_export_kwh=round(total_export, 3),
        expected_battery_discharge_kwh=round(total_discharge, 3),
        projected_min_soc=round(minimum_planned_energy / capacity_kwh * 100, 1),
        pv_headroom_required_percent=round(required_pv_headroom, 1),
        plan=plan,
    )


def optimize_battery(
    slots: list[ForecastSlot],
    current_soc: float,
    *,
    capacity_kwh: float,
    hard_min_soc: float,
    battery_wear_eur_kwh: float,
    export_eur_kwh: float,
    charge_efficiency: float = 0.94,
    discharge_efficiency: float = 0.94,
    max_charge_kw: float = 1.9,
    max_discharge_kw: float = 2.5,
    grid_charge_margin_eur_kwh: float = 0.03,
    min_grid_charge_kwh: float = 0.10,
    pv_curtailment_penalty_eur_kwh: float = 0.001,
) -> OptimizationResult:
    """Optimize dispatch while containing active storage to firm prices.

    A passive reference (no AC charging and no PV diversion from the house)
    determines the battery energy naturally carried beyond the contiguous firm
    Tibber-price prefix.  The active optimization may buy grid energy or send
    PV to the battery while the grid supplies the house, but must reach that
    boundary with no more battery energy than the reference.  Consequently all
    actively stored energy is consumed while prices are known and cannot
    acquire value from estimated intervals.
    """
    if not slots:
        raise ValueError("At least one forecast slot is required")
    firm_price_end = next(
        (
            index
            for index, slot in enumerate(slots)
            if slot.price_is_forecast
        ),
        len(slots),
    )
    common = {
        "capacity_kwh": capacity_kwh,
        "hard_min_soc": hard_min_soc,
        "battery_wear_eur_kwh": battery_wear_eur_kwh,
        "export_eur_kwh": export_eur_kwh,
        "charge_efficiency": charge_efficiency,
        "discharge_efficiency": discharge_efficiency,
        "max_charge_kw": max_charge_kw,
        "max_discharge_kw": max_discharge_kw,
        "grid_charge_margin_eur_kwh": grid_charge_margin_eur_kwh,
        "min_grid_charge_kwh": min_grid_charge_kwh,
        "pv_curtailment_penalty_eur_kwh": pv_curtailment_penalty_eur_kwh,
    }
    active_storage_possible = any(
        not slot.price_is_forecast
        and (
            slot.max_grid_charge_kw > 0
            or (slot.pv_kwh > 0 and slot.load_kwh > 0)
        )
        for slot in slots[:firm_price_end]
    )
    if (
        firm_price_end == 0
        or not active_storage_possible
    ):
        return _optimize_battery_core(slots, current_soc, **common)

    reference_energy_trace: list[float] = []
    _optimize_battery_core(
        slots,
        current_soc,
        allow_active_storage=False,
        energy_trace=reference_energy_trace,
        **common,
    )
    safe_boundary_energy = reference_energy_trace[firm_price_end - 1]
    return _optimize_battery_core(
        slots,
        current_soc,
        safe_boundary_slot=firm_price_end,
        safe_boundary_energy_kwh=safe_boundary_energy,
        **common,
    )
