"""Regression tests for appliance load and start planning."""

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


_OPTIMIZER = _load_module("optimizer")
_PLANNING = _load_module("appliance_planning")
ForecastSlot = _OPTIMIZER.ForecastSlot
PendingApplianceJob = _PLANNING.PendingApplianceJob
RunningApplianceJob = _PLANNING.RunningApplianceJob
InterruptibleLoadRequest = _PLANNING.InterruptibleLoadRequest


def _slots(
    count: int,
    *,
    start: datetime | None = None,
    price: float = 0.30,
    load: float = 0.10,
    pv: float = 0.0,
) -> list:
    start = start or datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
    return [
        ForecastSlot(
            (start + timedelta(minutes=15 * index)).isoformat(),
            price,
            load,
            pv,
        )
        for index in range(count)
    ]


class PendingApplianceTests(unittest.TestCase):
    """PV and overlap decisions must use marginal energy, not flags."""

    def test_tiny_pv_surplus_does_not_make_whole_cycle_free(self) -> None:
        slots = _slots(2)
        slots[0] = ForecastSlot(slots[0].start, 0.50, 0.09, 0.10)
        slots[1] = ForecastSlot(slots[1].start, 0.20, 0.10, 0.0)
        job = PendingApplianceJob("test", 0.10, 1, 0, 23)

        planned, schedules = _PLANNING.schedule_pending_jobs(
            slots,
            [job],
            export_eur_kwh=0.0,
            max_combined_power_kw=2.5,
            max_start_slots=2,
        )

        self.assertEqual(schedules["test"], slots[1].start)
        self.assertAlmostEqual(planned[0].load_kwh, 0.09)
        self.assertAlmostEqual(planned[1].load_kwh, 0.20)

    def test_two_small_cycles_may_share_the_cheapest_slot(self) -> None:
        slots = _slots(2)
        slots[0] = ForecastSlot(slots[0].start, 0.10, 0.10, 0.0)
        slots[1] = ForecastSlot(slots[1].start, 0.50, 0.10, 0.0)
        jobs = [
            PendingApplianceJob("first", 0.10, 1, 0, 23),
            PendingApplianceJob("second", 0.10, 1, 0, 23),
        ]

        planned, schedules = _PLANNING.schedule_pending_jobs(
            slots,
            jobs,
            export_eur_kwh=0.0,
            max_combined_power_kw=1.0,
            max_start_slots=2,
        )

        self.assertEqual(schedules["first"], slots[0].start)
        self.assertEqual(schedules["second"], slots[0].start)
        self.assertAlmostEqual(planned[0].load_kwh, 0.30)

    def test_overlap_is_limited_by_combined_average_power(self) -> None:
        slots = _slots(2)
        slots[0] = ForecastSlot(slots[0].start, 0.10, 0.10, 0.0)
        slots[1] = ForecastSlot(slots[1].start, 0.50, 0.10, 0.0)
        jobs = [
            PendingApplianceJob("first", 0.20, 1, 0, 23),
            PendingApplianceJob("second", 0.20, 1, 0, 23),
        ]

        _, schedules = _PLANNING.schedule_pending_jobs(
            slots,
            jobs,
            export_eur_kwh=0.0,
            max_combined_power_kw=1.0,
            max_start_slots=2,
        )

        self.assertNotEqual(schedules["first"], schedules["second"])


class FlexibleLoadPlanningTests(unittest.TestCase):
    """Flexible loads share the battery-aware quarter-hour horizon."""

    def test_joint_cost_uses_real_battery_buffer_for_earlier_start(self) -> None:
        slots = _slots(3, price=0.50, load=0.10, pv=0.0)
        baseline_plan = [
            {"soc_end": 50.0, "pv_export_kwh": 0.0} for _ in slots
        ]
        job = PendingApplianceJob("dishwasher", 0.20, 1, 0, 23)

        _, schedules = _PLANNING.schedule_pending_jobs(
            slots,
            [job],
            export_eur_kwh=0.0,
            max_combined_power_kw=2.5,
            max_start_slots=2,
            baseline_dispatch_plan=baseline_plan,
            battery_capacity_kwh=5.0,
            hard_min_soc=12.0,
            discharge_efficiency=0.94,
        )

        self.assertEqual(schedules["dishwasher"], slots[0].start)

    def test_joint_cost_uses_only_dispatchable_pv_surplus(self) -> None:
        slots = _slots(2, price=0.50, load=0.10, pv=0.0)
        slots[1] = ForecastSlot(slots[1].start, 0.10, 0.10, 0.0)
        baseline_plan = [
            {"soc_end": 12.0, "pv_export_kwh": 0.20},
            {"soc_end": 12.0, "pv_export_kwh": 0.0},
        ]
        job = PendingApplianceJob("dishwasher", 0.20, 1, 0, 23)

        _, schedules = _PLANNING.schedule_pending_jobs(
            slots,
            [job],
            export_eur_kwh=0.0,
            max_combined_power_kw=2.5,
            max_start_slots=2,
            baseline_dispatch_plan=baseline_plan,
            battery_capacity_kwh=5.0,
            hard_min_soc=12.0,
            discharge_efficiency=0.94,
        )

        self.assertEqual(schedules["dishwasher"], slots[0].start)

    def test_calendar_deadline_bounds_interruptible_load_slots(self) -> None:
        slots = _slots(8)
        start = datetime.fromisoformat(slots[0].start)
        request = InterruptibleLoadRequest(
            name="room_climate",
            energy_budget_kwh=0.8,
            earliest_start=start + timedelta(minutes=15),
            finish_by=start + timedelta(hours=1),
            minimum_power_kw=0.3,
            maximum_power_kw=1.2,
            minimum_run_slots=2,
        )

        candidates = _PLANNING.interruptible_candidate_slots(slots, request)

        self.assertEqual(candidates, [1, 2, 3])

    def test_fixed_cycle_must_finish_by_calendar_deadline(self) -> None:
        slots = _slots(8, price=0.50)
        slots[1] = ForecastSlot(slots[1].start, 0.20, 0.10, 0.0)
        slots[2] = ForecastSlot(slots[2].start, 0.01, 0.10, 0.0)
        start = datetime.fromisoformat(slots[0].start)
        job = PendingApplianceJob(
            "washer",
            0.20,
            2,
            0,
            23,
            requested_at=start,
            finish_by=start + timedelta(minutes=45),
        )

        _, schedules = _PLANNING.schedule_pending_jobs(
            slots,
            [job],
            export_eur_kwh=0.0,
            max_combined_power_kw=2.5,
            max_start_slots=8,
        )

        self.assertEqual(schedules["washer"], slots[1].start)

    def test_complete_cycle_can_be_kept_out_of_quiet_hours(self) -> None:
        slots = _slots(
            8,
            start=datetime(2026, 9, 14, 20, tzinfo=timezone.utc),
            price=0.50,
        )
        job = PendingApplianceJob(
            "dryer",
            0.8,
            8,
            6,
            20,
            latest_finish_hour=21.0,
        )

        _, schedules = _PLANNING.schedule_pending_jobs(
            slots,
            [job],
            export_eur_kwh=0.0,
            max_combined_power_kw=2.5,
            max_start_slots=8,
        )

        self.assertNotIn("dryer", schedules)

    def test_estimated_price_never_becomes_confirmed_by_deadline(self) -> None:
        slots = [
            ForecastSlot(
                slot.start,
                slot.price_eur_kwh,
                slot.load_kwh,
                slot.pv_kwh,
                price_is_forecast=True,
            )
            for slot in _slots(2)
        ]
        job = PendingApplianceJob("dishwasher", 0.10, 1, 0, 23)

        confirmation = _PLANNING.scheduled_job_price_confirmation(
            slots,
            [job],
            {"dishwasher": slots[0].start},
        )

        self.assertEqual(confirmation, {"dishwasher": False})

    def test_persisted_request_time_survives_restart_timestamp(self) -> None:
        requested = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        restored = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

        self.assertEqual(
            _PLANNING.fixed_request_timestamp(
                requested,
                pause_active=True,
                observed_transition_at=restored,
            ),
            requested,
        )


class RunningApplianceTests(unittest.TestCase):
    """An active cycle must remain in the load forecast until it ends."""

    def test_nominal_remaining_energy_is_added_until_estimated_end(self) -> None:
        observed_at = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        slots = _slots(4, start=observed_at)
        job = RunningApplianceJob(
            "washer",
            energy_kwh=1.0,
            duration_slots=4,
            started_at=observed_at - timedelta(minutes=15),
        )

        planned, diagnostics, power = _PLANNING.add_running_jobs(
            slots,
            [job],
            observed_at=observed_at,
            first_slot_uses_live_house_power=False,
        )

        self.assertAlmostEqual(sum(slot.load_kwh for slot in planned), 0.4 + 0.75)
        self.assertEqual(diagnostics["washer"]["remaining_energy_kwh"], 0.75)
        self.assertEqual(power[:3], [1.0, 1.0, 1.0])
        self.assertEqual(power[3], 0.0)

    def test_live_first_slot_is_not_double_counted(self) -> None:
        observed_at = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        slots = _slots(4, start=observed_at)
        job = RunningApplianceJob(
            "washer",
            energy_kwh=1.0,
            duration_slots=4,
            started_at=observed_at - timedelta(minutes=15),
        )

        planned, diagnostics, _ = _PLANNING.add_running_jobs(
            slots,
            [job],
            observed_at=observed_at,
            first_slot_uses_live_house_power=True,
        )

        self.assertAlmostEqual(sum(slot.load_kwh for slot in planned), 0.4 + 0.50)
        self.assertEqual(
            diagnostics["washer"]["forecast_energy_added_kwh"],
            0.5,
        )

    def test_overdue_active_state_keeps_one_future_interval(self) -> None:
        observed_at = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        slots = _slots(4, start=observed_at)
        job = RunningApplianceJob(
            "washer",
            energy_kwh=1.0,
            duration_slots=4,
            started_at=observed_at - timedelta(hours=3),
        )

        planned, diagnostics, _ = _PLANNING.add_running_jobs(
            slots,
            [job],
            observed_at=observed_at,
            first_slot_uses_live_house_power=True,
        )

        self.assertTrue(diagnostics["washer"]["overdue"])
        self.assertAlmostEqual(planned[1].load_kwh, 0.35)


if __name__ == "__main__":
    unittest.main()
