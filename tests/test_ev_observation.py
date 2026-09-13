"""Tests for the isolated Home Assistant EV observation adapter."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _install_home_assistant_stubs() -> None:
    homeassistant = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    core.HomeAssistant = object
    dt.as_local = lambda value: value
    dt.utcnow = lambda: datetime.now(timezone.utc)
    modules = {
        "homeassistant": homeassistant,
        "homeassistant.core": core,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
    }
    for name, module in modules.items():
        sys.modules.setdefault(name, module)


_install_home_assistant_stubs()
_PACKAGE = "energy_optimizer"
_ROOT = Path(__file__).parents[1] / "custom_components" / _PACKAGE
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    sys.modules[_PACKAGE] = package

_SPEC = importlib.util.spec_from_file_location(
    f"{_PACKAGE}.ev_observation_under_test", _ROOT / "ev_observation.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
_EV_PLANNING = sys.modules[f"{_PACKAGE}.ev_planning"]


class CalendarParsingTests(unittest.TestCase):
    def test_canonical_overrides_accept_decimal_comma_and_ignore_aliases(self) -> None:
        values = _MODULE._parse_event_numbers(
            "distance_km: 80,5\nreserve_km=40;strecke_km: 999"
        )

        self.assertEqual(values, {"distance_km": 80.5, "reserve_km": 40.0})

    def test_timed_naive_start_uses_ha_timezone_but_all_day_is_rejected(self) -> None:
        tz = timezone(timedelta(hours=2))
        now = datetime(2026, 9, 13, 8, tzinfo=tz)

        trip, error = _MODULE._trip_from_event(
            {
                "start": "2026-09-13 18:00:00",
                "summary": "EV departure",
                "description": "distance_km: 80",
            },
            now=now,
            default_reserve_km=40,
        )

        self.assertIsNone(error)
        self.assertEqual(trip.departure, datetime(2026, 9, 13, 18, tzinfo=tz))
        invalid, error = _MODULE._trip_from_event(
            {"start": "2026-09-13", "description": "distance_km: 80"},
            now=now,
            default_reserve_km=40,
        )
        self.assertIsNone(invalid)
        self.assertEqual(error, "calendar_event_start_invalid")

    def test_trip_values_are_bounded(self) -> None:
        trip, error = _MODULE._trip_from_event(
            {
                "start": "2026-09-13T18:00:00+02:00",
                "description": "distance_km: 9000\nreserve_km: 40",
            },
            now=datetime(2026, 9, 13, 8, tzinfo=timezone.utc),
            default_reserve_km=40,
        )

        self.assertIsNone(trip)
        self.assertEqual(error, "trip_values_out_of_range")


class SchedulePreviewTests(unittest.TestCase):
    @staticmethod
    def _allocation(start: datetime, *, pv: bool = False):
        return _EV_PLANNING.EVChargeAllocation(
            start=start,
            power_kw=3.6,
            wallbox_energy_kwh=0.9,
            slot_fraction=1.0,
            duration_minutes=15.0,
            price_eur_kwh=0.2,
            price_is_known=True,
            pv_preferred=pv,
            deadline_fallback=False,
        )

    def test_contiguous_allocations_are_compacted(self) -> None:
        start = datetime(2026, 9, 13, 10, tzinfo=timezone.utc)
        allocations = tuple(
            self._allocation(start + timedelta(minutes=15 * index))
            for index in range(12)
        )

        schedule, truncated = _MODULE._schedule_preview(allocations)

        self.assertEqual(len(schedule), 1)
        self.assertFalse(truncated)
        self.assertEqual(schedule[0]["start"], start.isoformat())
        self.assertEqual(
            schedule[0]["end"], (start + timedelta(hours=3)).isoformat()
        )
        self.assertAlmostEqual(schedule[0]["energy_kwh"], 10.8)

    def test_preview_never_exposes_more_than_eight_groups(self) -> None:
        start = datetime(2026, 9, 13, 10, tzinfo=timezone.utc)
        allocations = tuple(
            self._allocation(
                start + timedelta(minutes=15 * index),
                pv=bool(index % 2),
            )
            for index in range(12)
        )

        schedule, truncated = _MODULE._schedule_preview(allocations)

        self.assertEqual(len(schedule), 8)
        self.assertTrue(truncated)


class ObservationPlanningTests(unittest.TestCase):
    @staticmethod
    def _planner(
        *,
        connected_state: str = "on",
        departure_delta: timedelta = timedelta(hours=1, minutes=30),
        soc_age: timedelta = timedelta(0),
    ):
        now = datetime.now(timezone.utc)

        class States:
            def get(self, entity_id):
                if entity_id == "binary_sensor.ev_connected":
                    return types.SimpleNamespace(state=connected_state)
                if entity_id == "sensor.ev_soc":
                    return types.SimpleNamespace(
                        state="0",
                        last_updated=now - soc_age,
                        attributes={"unit_of_measurement": "%"},
                    )
                return None

        event_start = now + departure_delta

        class Services:
            def __init__(self):
                self.call = None

            async def async_call(self, domain, service, data, **kwargs):
                self.call = (domain, service, data, kwargs)
                return {
                    "calendar.ev": {
                        "events": [
                            {
                                "start": event_start.isoformat(),
                                "summary": "EV departure",
                                "description": "distance_km: 10\nreserve_km: 0",
                            }
                        ]
                    }
                }

        hass = types.SimpleNamespace(states=States(), services=Services())
        config = types.SimpleNamespace(
            enabled=True,
            site_meter_includes_ev=True,
            calendar_entity="calendar.ev",
            connected_entity="binary_sensor.ev_connected",
            vehicle_soc_entity="sensor.ev_soc",
            reserve_km=0.0,
            battery_capacity_kwh=60.0,
            consumption_kwh_per_100km=18.0,
            charge_efficiency=0.9,
            normal_charge_power_kw=3.6,
            boost_charge_power_kw=11.0,
            site_max_import_power_kw=22.0,
        )
        planner = _MODULE.EVObservationPlanner(hass, config)
        slots = [
            _MODULE.ForecastSlot(
                (_MODULE._quarter(now) + timedelta(minutes=15 * index)).isoformat(),
                0.20 + index / 100,
                0.15,
                0.0,
                False,
            )
            for index in range(12)
        ]
        return planner, now, slots

    def test_calendar_uses_target_and_future_plan_survives_missing_live_split(self) -> None:
        planner, now, slots = self._planner()

        payload = asyncio.run(planner.async_plan(now, slots, None, True))

        call = planner.hass.services.call
        self.assertEqual(call[0:2], ("calendar", "get_events"))
        self.assertNotIn("entity_id", call[2])
        self.assertEqual(call[3]["target"], {"entity_id": "calendar.ev"})
        self.assertEqual(payload["status"], "scheduled")
        self.assertGreater(payload["planned_wallbox_kwh"], 0)
        self.assertFalse(payload["accounting_valid"])
        self.assertTrue(payload["history_accounting_valid"])
        self.assertEqual(payload["suggested_power_w"], 0)
        self.assertEqual(payload["suggested_mode"], "degraded")
        self.assertFalse(payload["applied_to_site_optimizer"])
        self.assertTrue(payload["provisional"])
        self.assertTrue(payload["schedule"])
        self.assertLessEqual(len(payload["schedule"]), 8)
        self.assertIn("end", payload["schedule"][0])
        self.assertIsNotNone(payload["next_charge_start"])
        self.assertIsNotNone(payload["next_charge_end"])
        self.assertLessEqual(payload["next_charge_power_w"], 11_000)

    def test_current_slot_is_partial_and_unavailable_when_disconnected(self) -> None:
        planner, now, slots = self._planner(connected_state="off")
        live = {
            "ev_accounting_valid": True,
            "site_load_w": 500.0,
            "house_load_w": 500.0,
            "ev_load_w": 0.0,
            "ev_power_age_seconds": 2.0,
        }

        ev_slots = planner._forecast_slots(now, slots, live, connected=False)

        self.assertEqual(ev_slots[0].start, now)
        self.assertLessEqual(ev_slots[0].duration_hours, 0.25)
        self.assertEqual(ev_slots[0].available_ev_power_kw, 0.0)
        self.assertGreaterEqual(ev_slots[1].available_ev_power_kw, 3.6)

    def test_trip_beyond_optimizer_window_waits_without_false_infeasible(self) -> None:
        planner, now, slots = self._planner(departure_delta=timedelta(days=3))

        payload = asyncio.run(planner.async_plan(now, slots, None, True))

        self.assertEqual(payload["status"], "awaiting_horizon")
        self.assertEqual(payload["reason"], "trip_outside_optimizer_horizon")
        self.assertIsNotNone(payload["next_departure"])
        self.assertEqual(payload["suggested_power_w"], 0)

    def test_stale_vehicle_soc_during_active_charge_blocks_replanning(self) -> None:
        planner, now, slots = self._planner(soc_age=timedelta(minutes=20))
        live = {
            "ev_accounting_valid": True,
            "site_load_w": 4600.0,
            "house_load_w": 1000.0,
            "ev_load_w": 3600.0,
            "ev_power_age_seconds": 2.0,
        }

        payload = asyncio.run(planner.async_plan(now, slots, live, True))

        self.assertEqual(payload["status"], "awaiting_data")
        self.assertEqual(payload["reason"], "vehicle_soc_stale_while_charging")
        self.assertEqual(payload["suggested_power_w"], 0)

    def test_disconnected_schedule_is_provisional_even_with_firm_prices(self) -> None:
        planner, now, slots = self._planner(connected_state="off")
        live = {
            "ev_accounting_valid": True,
            "site_load_w": 500.0,
            "house_load_w": 500.0,
            "ev_load_w": 0.0,
            "ev_power_age_seconds": 2.0,
        }

        payload = asyncio.run(planner.async_plan(now, slots, live, True))

        self.assertTrue(payload["provisional"])
        self.assertEqual(payload["suggested_power_w"], 0)

    def test_observation_plan_never_mutates_authoritative_site_slots(self) -> None:
        planner, now, slots = self._planner()
        original_slots = list(slots)

        asyncio.run(planner.async_plan(now, slots, None, True))

        self.assertEqual(slots, original_slots)

    def test_adapter_retains_eleven_kw_automatic_ceiling(self) -> None:
        planner, now, slots = self._planner()
        planner.config.boost_charge_power_kw = 22.0
        planner.config.consumption_kwh_per_100km = 100.0
        live = {
            "ev_accounting_valid": True,
            "site_load_w": 500.0,
            "house_load_w": 500.0,
            "ev_load_w": 0.0,
            "ev_power_age_seconds": 2.0,
        }

        payload = asyncio.run(planner.async_plan(now, slots, live, True))

        self.assertTrue(payload["schedule"])
        self.assertLessEqual(
            max(group["power_w"] for group in payload["schedule"]),
            11_000,
        )


if __name__ == "__main__":
    unittest.main()
