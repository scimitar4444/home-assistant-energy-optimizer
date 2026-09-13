"""Regression tests for the optional, pure EV charging planner."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PACKAGE = "energy_optimizer"
_ROOT = Path(__file__).parents[1] / "custom_components" / _PACKAGE
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    sys.modules[_PACKAGE] = package


def _load_module(name: str):
    module_name = f"{_PACKAGE}.{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_PLANNING = _load_module("ev_planning")
EVSlot = _PLANNING.EVSlot
TripRequirement = _PLANNING.TripRequirement
VehicleProfile = _PLANNING.VehicleProfile


START = datetime(2026, 9, 13, 8, tzinfo=timezone.utc)


def _profile(**overrides) -> VehicleProfile:
    values = {
        "battery_capacity_kwh": 50.0,
        "consumption_kwh_per_100km": 20.0,
        "charging_efficiency": 0.90,
        "normal_power_kw": 3.6,
        "boost_power_kw": 11.0,
        "material_price_difference_eur_kwh": 0.05,
    }
    values.update(overrides)
    return VehicleProfile(**values)


def _requirement(
    *,
    departure_slots: int = 8,
    distance_km: float = 75,
    reserve_km: float = 25,
) -> TripRequirement:
    return TripRequirement(
        departure=START + timedelta(minutes=15 * departure_slots),
        distance_km=distance_km,
        reserve_km=reserve_km,
        event_id="calendar-event",
    )


def _slots(
    prices: list[float | None],
    *,
    known: list[bool] | None = None,
    power_kw: float = 3.6,
) -> list[EVSlot]:
    known = known if known is not None else [price is not None for price in prices]
    return [
        EVSlot(
            start=START + timedelta(minutes=15 * index),
            available_ev_power_kw=power_kw,
            price_eur_kwh=price,
            price_is_known=known[index],
        )
        for index, price in enumerate(prices)
    ]


class EnergyRequirementTests(unittest.TestCase):
    """Range and SoC conversion must apply losses only to missing energy."""

    def test_energy_formula_includes_trip_and_remaining_reserve(self) -> None:
        required, target, shortfall = _PLANNING.required_wallbox_energy_kwh(
            _profile(),
            _requirement(distance_km=100, reserve_km=50),
            40.0,
        )

        # 150 km * 20 kWh/100 km = 30 kWh at departure.  The 40 %
        # battery already contains 20 kWh, leaving 10 / 0.9 at the wallbox.
        self.assertAlmostEqual(target, 30.0)
        self.assertAlmostEqual(required, 10.0 / 0.9)
        self.assertEqual(shortfall, 0.0)

    def test_impossible_range_is_reported_instead_of_overcharging(self) -> None:
        profile = _profile(consumption_kwh_per_100km=25.0)
        requirement = _requirement(distance_km=200, reserve_km=100)
        slots = _slots([0.10] * 24, power_kw=11.0)

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=90.0,
        )

        self.assertFalse(plan.complete)
        self.assertEqual(plan.reason, "trip_and_reserve_exceed_vehicle_capacity")
        self.assertAlmostEqual(plan.capacity_shortfall_kwh, 25.0)


class EconomicPlanningTests(unittest.TestCase):
    """Confirmed prices and explicit source budgets drive optional charging."""

    def test_cheapest_confirmed_slot_is_selected(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        # Target 10 kWh, current energy 9.1 kWh: exactly one normal slot.
        slots = _slots([0.40, 0.10, 0.30])

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=9.1 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertEqual(len(plan.allocations), 1)
        self.assertEqual(plan.allocations[0].start, START + timedelta(minutes=15))
        self.assertAlmostEqual(plan.allocations[0].wallbox_energy_kwh, 0.9)

    def test_residual_pv_is_used_before_cheaper_grid_energy(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        slots = _slots([0.50, 0.10])
        slots[0] = EVSlot(
            start=slots[0].start,
            available_ev_power_kw=3.6,
            price_eur_kwh=0.50,
            price_is_known=True,
            residual_pv_kwh=0.9,
        )

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=9.1 / 50 * 100,
        )

        self.assertEqual(len(plan.allocations), 1)
        self.assertEqual(plan.allocations[0].start, START)
        self.assertAlmostEqual(plan.allocations[0].wallbox_energy_kwh, 0.9)

    def test_slot_below_normal_mode_is_unusable_after_house_priority(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        slots = _slots([0.10, 0.20], power_kw=1.0)

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=9.5 / 50 * 100,
        )

        self.assertFalse(plan.complete)
        self.assertEqual(plan.reason, "deadline_capacity_insufficient")
        self.assertEqual(plan.allocations, ())

    def test_partial_last_interval_keeps_discrete_normal_command(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        slots = _slots([0.10])

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=9.55 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertAlmostEqual(plan.allocations[0].wallbox_energy_kwh, 0.45)
        self.assertAlmostEqual(plan.allocations[0].power_kw, 3.6)
        self.assertAlmostEqual(plan.allocations[0].slot_fraction, 0.5)
        self.assertAlmostEqual(plan.allocations[0].duration_minutes, 7.5)

    def test_current_partial_interval_cannot_offer_a_full_quarter_hour(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        slots = [
            EVSlot(
                start=START,
                available_ev_power_kw=3.6,
                price_eur_kwh=0.10,
                price_is_known=True,
                duration_hours=5 / 60,
            ),
            EVSlot(
                start=START + timedelta(minutes=15),
                available_ev_power_kw=3.6,
                price_eur_kwh=0.20,
                price_is_known=True,
            ),
        ]
        # The missing 0.9 kWh does not fit into the five minutes remaining in
        # the cheap slot, so the planner must also use the next interval.
        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=9.1 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertEqual(len(plan.allocations), 2)
        self.assertAlmostEqual(plan.allocations[0].wallbox_energy_kwh, 0.3)
        self.assertAlmostEqual(plan.allocations[0].duration_minutes, 5.0)
        self.assertAlmostEqual(plan.allocations[1].wallbox_energy_kwh, 0.6)

    def test_slot_duration_rejects_more_than_one_quarter_hour(self) -> None:
        with self.assertRaises(ValueError):
            EVSlot(
                start=START,
                available_ev_power_kw=3.6,
                duration_hours=0.5,
            )

    def test_slot_is_clipped_at_non_quarter_hour_departure(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = TripRequirement(
            departure=START + timedelta(minutes=7),
            distance_km=50,
            reserve_km=0,
        )
        # Only seven minutes are physically available. The remaining eight
        # minutes of the quarter hour must never be credited after departure.
        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            _slots([0.10], power_kw=3.6),
            current_soc_percent=9.4 / 50 * 100,
        )

        self.assertFalse(plan.complete)
        self.assertAlmostEqual(plan.planned_wallbox_kwh, 3.6 * 7 / 60)
        self.assertAlmostEqual(plan.allocations[0].duration_minutes, 7.0)


class DeadlineAndBoostTests(unittest.TestCase):
    """Unknown prices and automatic boost must remain tightly bounded."""

    def test_unknown_price_is_kept_as_latest_deadline_fallback(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        slots = _slots([None, None, None], known=[False, False, False])

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=9.1 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertEqual(plan.reason, "deadline_fallback_uses_unknown_price")
        self.assertEqual(plan.allocations[0].start, START + timedelta(minutes=30))
        self.assertTrue(plan.allocations[0].deadline_fallback)

    def test_positive_known_price_waits_for_future_tariff_publication(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        slots = _slots(
            [0.50, None, None, None, None, None, None, None],
            known=[True, False, False, False, False, False, False, False],
        )

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=9.1 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertEqual(len(plan.allocations), 1)
        self.assertEqual(
            plan.allocations[0].start,
            START + timedelta(minutes=15 * 7),
        )
        self.assertTrue(plan.allocations[0].deadline_fallback)

    def test_negative_known_price_is_not_lost_while_future_is_unknown(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        slots = _slots(
            [-0.01, None, None],
            known=[True, False, False],
        )

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=9.1 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertEqual(len(plan.allocations), 1)
        self.assertEqual(plan.allocations[0].start, START)

    def test_negative_price_precedes_forecast_pv_preference(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        slots = _slots([-0.01, 0.40])
        slots[1] = EVSlot(
            start=slots[1].start,
            available_ev_power_kw=3.6,
            price_eur_kwh=0.40,
            price_is_known=True,
            residual_pv_kwh=0.9,
        )

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=9.1 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertEqual(len(plan.allocations), 1)
        self.assertEqual(plan.allocations[0].start, START)

    def test_boost_is_used_when_normal_power_cannot_meet_deadline(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(
            departure_slots=1,
            distance_km=50,
            reserve_km=0,
        )
        slots = _slots([0.10], power_kw=22.0)
        # 7.25 -> 10 kWh requires a full 11-kW quarter hour.
        soc = 7.25 / 50 * 100

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=soc,
        )

        self.assertTrue(plan.complete)
        self.assertTrue(plan.automatic_boost_used)
        self.assertAlmostEqual(plan.allocations[0].power_kw, 11.0)
        self.assertEqual(plan.reason, "normal_power_cannot_meet_deadline")

    def test_confirmed_cheap_window_may_replace_later_expensive_slot(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(distance_km=50, reserve_km=0)
        slots = _slots([0.10, 0.50], power_kw=11.0)
        # Two normal-power slots would suffice, but concentrating 1.8 kWh in
        # the confirmed cheap slot avoids the materially dearer later one.
        soc = 8.2 / 50 * 100

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=soc,
        )

        self.assertTrue(plan.complete)
        self.assertTrue(plan.automatic_boost_used)
        self.assertEqual(plan.reason, "confirmed_cheap_window_avoids_high_price")
        self.assertEqual(len(plan.allocations), 1)
        self.assertAlmostEqual(plan.allocations[0].power_kw, 11.0)
        self.assertAlmostEqual(plan.allocations[0].slot_fraction, 1.8 / 2.75)
        self.assertAlmostEqual(
            plan.allocations[0].duration_minutes,
            15 * 1.8 / 2.75,
        )

    def test_deadline_boost_still_minimizes_confirmed_energy_cost(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(
            departure_slots=2,
            distance_km=50,
            reserve_km=0,
        )
        slots = _slots([0.10, 0.50], power_kw=11.0)

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=7.0 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertTrue(plan.automatic_boost_used)
        self.assertAlmostEqual(plan.allocations[0].wallbox_energy_kwh, 2.75)
        self.assertAlmostEqual(plan.allocations[1].wallbox_energy_kwh, 0.25)

    def test_later_confirmed_cheap_slot_may_replace_earlier_cost(self) -> None:
        profile = _profile(charging_efficiency=1.0)
        requirement = _requirement(
            departure_slots=2,
            distance_km=50,
            reserve_km=0,
        )
        slots = _slots([0.50, 0.10], power_kw=11.0)

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=8.2 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertEqual(len(plan.allocations), 1)
        self.assertEqual(plan.allocations[0].start, START + timedelta(minutes=15))
        self.assertAlmostEqual(plan.allocations[0].wallbox_energy_kwh, 1.8)
        self.assertAlmostEqual(plan.allocations[0].power_kw, 11.0)
        self.assertEqual(plan.reason, "confirmed_cheap_window_avoids_high_price")

    def test_22_kw_profile_setting_is_never_used_automatically(self) -> None:
        profile = _profile(charging_efficiency=1.0, boost_power_kw=22.0)
        requirement = _requirement(departure_slots=2, distance_km=50, reserve_km=0)
        slots = _slots([0.10, 0.20], power_kw=22.0)

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=4.5 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertTrue(plan.automatic_boost_used)
        self.assertLessEqual(max(item.power_kw for item in plan.allocations), 11.0)

    def test_even_misconfigured_normal_power_cannot_enable_22_kw(self) -> None:
        profile = _profile(
            charging_efficiency=1.0,
            normal_power_kw=22.0,
            boost_power_kw=22.0,
        )
        requirement = _requirement(departure_slots=1, distance_km=50, reserve_km=0)
        slots = _slots([0.10], power_kw=22.0)

        plan = _PLANNING.plan_ev_charging(
            profile,
            requirement,
            slots,
            current_soc_percent=7.25 / 50 * 100,
        )

        self.assertTrue(plan.complete)
        self.assertEqual(len(plan.allocations), 1)
        self.assertAlmostEqual(plan.allocations[0].power_kw, 11.0)

    def test_missing_soc_yields_no_command(self) -> None:
        plan = _PLANNING.plan_ev_charging(
            _profile(),
            _requirement(),
            _slots([0.10, None], known=[True, False]),
            current_soc_percent=None,
        )

        self.assertFalse(plan.complete)
        self.assertEqual(plan.status, "awaiting_data")
        self.assertEqual(plan.reason, "current_soc_unavailable")
        self.assertEqual(plan.allocations, ())
        self.assertIsNone(plan.required_wallbox_kwh)


if __name__ == "__main__":
    unittest.main()
