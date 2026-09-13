"""Focused tests for the coordinator's optional EV observation path."""

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
    components = types.ModuleType("homeassistant.components")
    recorder = types.ModuleType("homeassistant.components.recorder")
    statistics = types.ModuleType("homeassistant.components.recorder.statistics")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    class UpdateFailed(Exception):
        pass

    recorder.get_instance = lambda _hass: None
    statistics.statistics_during_period = lambda *_args, **_kwargs: {}
    core.HomeAssistant = object
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed
    dt.as_local = lambda value: value
    dt.utcnow = lambda: datetime.now(timezone.utc)
    dt.utc_from_timestamp = lambda value: datetime.fromtimestamp(value, timezone.utc)
    dt.now = lambda: datetime.now(timezone.utc)

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.recorder": recorder,
        "homeassistant.components.recorder.statistics": statistics,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.update_coordinator": update_coordinator,
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
    f"{_PACKAGE}.coordinator_ev_under_test", _ROOT / "coordinator.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


class FutureFlowBlockTests(unittest.TestCase):
    @staticmethod
    def _plan(*flows: float):
        return [
            {"start": f"slot-{index}", "flow_kwh": flow}
            for index, flow in enumerate(flows)
        ]

    def test_running_block_is_not_reported_as_a_future_start(self) -> None:
        self.assertIsNone(
            _MODULE._next_flow_block_start(
                self._plan(0.2, 0.2, 0.0),
                "flow_kwh",
            )
        )

    def test_new_block_after_running_block_uses_later_transition(self) -> None:
        self.assertEqual(
            _MODULE._next_flow_block_start(
                self._plan(0.2, 0.0, 0.3),
                "flow_kwh",
            ),
            "slot-2",
        )

    def test_first_future_transition_is_returned_when_current_slot_is_idle(
        self,
    ) -> None:
        self.assertEqual(
            _MODULE._next_flow_block_start(
                self._plan(0.0, 0.3, 0.3),
                "flow_kwh",
            ),
            "slot-1",
        )


class FirmPriceBasisTests(unittest.TestCase):
    @staticmethod
    def _slot(price: float, *, forecast: bool = False):
        return _MODULE.ForecastSlot("slot", price, 0.1, 0.0, forecast)

    @staticmethod
    def _plan(*discharges: float):
        return [
            {"battery_to_load_kwh": discharge}
            for discharge in discharges
        ]

    def test_later_higher_firm_discharge_is_a_valid_basis(self) -> None:
        self.assertTrue(
            _MODULE._action_has_firm_price_basis(
                "RESERVE",
                [self._slot(0.20), self._slot(0.30)],
                self._plan(0.0, 0.1),
            )
        )

    def test_equal_or_lower_firm_price_is_not_a_valid_basis(self) -> None:
        self.assertFalse(
            _MODULE._action_has_firm_price_basis(
                "GRID_CHARGE",
                [self._slot(0.20), self._slot(0.20), self._slot(0.10)],
                self._plan(0.0, 0.1, 0.1),
            )
        )

    def test_search_stops_at_first_estimated_price(self) -> None:
        self.assertFalse(
            _MODULE._action_has_firm_price_basis(
                "PV_STORE",
                [
                    self._slot(0.20),
                    self._slot(0.25, forecast=True),
                    self._slot(0.40),
                ],
                self._plan(0.0, 0.0, 0.1),
            )
        )

    def test_nonpositive_current_price_can_justify_reserve_only(self) -> None:
        slots = [self._slot(0.0)]
        plan = self._plan(0.0)
        self.assertTrue(
            _MODULE._action_has_firm_price_basis("RESERVE", slots, plan)
        )
        self.assertFalse(
            _MODULE._action_has_firm_price_basis("GRID_CHARGE", slots, plan)
        )


class LiveAccountingTests(unittest.TestCase):
    @staticmethod
    def _coordinator(*, ev_age_seconds: float):
        now = datetime.now(timezone.utc)
        states = {
            "sun.sun": types.SimpleNamespace(state="above_horizon"),
            "sensor.pv": types.SimpleNamespace(
                state="0",
                last_updated=now,
                attributes={"unit_of_measurement": "W"},
            ),
            "sensor.grid": types.SimpleNamespace(
                state="4600",
                last_updated=now,
                attributes={"unit_of_measurement": "W"},
            ),
            "sensor.phase_1": types.SimpleNamespace(
                state="4600",
                last_updated=now,
                attributes={"unit_of_measurement": "W"},
            ),
            "sensor.phase_2": types.SimpleNamespace(
                state="0",
                last_updated=now,
                attributes={"unit_of_measurement": "W"},
            ),
            "sensor.phase_3": types.SimpleNamespace(
                state="0",
                last_updated=now,
                attributes={"unit_of_measurement": "W"},
            ),
            "sensor.ev": types.SimpleNamespace(
                state="3600",
                last_updated=now - timedelta(seconds=ev_age_seconds),
                attributes={"unit_of_measurement": "W"},
            ),
        }
        coordinator = object.__new__(_MODULE.EnergyOptimizerCoordinator)
        coordinator.hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=states.get)
        )
        coordinator.config = types.SimpleNamespace(
            live_pv_power_entity="sensor.pv",
            live_grid_power_entity="sensor.grid",
            live_load_power_entities=(
                "sensor.phase_1",
                "sensor.phase_2",
                "sensor.phase_3",
            ),
            ev=types.SimpleNamespace(
                enabled=True,
                live_power_entity="sensor.ev",
                site_meter_includes_ev=True,
                site_max_import_power_kw=22.0,
            ),
        )
        return coordinator

    def test_wrong_ev_power_unit_cannot_be_subtracted(self) -> None:
        coordinator = self._coordinator(ev_age_seconds=2)
        coordinator.hass.states.get("sensor.ev").attributes[
            "unit_of_measurement"
        ] = "kW"

        live = coordinator._live_measurements()

        self.assertEqual(live["site_load_w"], 4600)
        self.assertIsNone(live["ev_load_w"])
        self.assertFalse(live["ev_accounting_valid"])

    def test_site_remains_authoritative_while_house_diagnostic_is_cleaned(self) -> None:
        live = self._coordinator(ev_age_seconds=2)._live_measurements()

        self.assertEqual(live["load_w"], 4600)
        self.assertEqual(live["site_load_w"], 4600)
        self.assertEqual(live["ev_load_w"], 3600)
        self.assertEqual(live["house_load_w"], 1000)
        self.assertTrue(live["ev_accounting_valid"])

    def test_stale_ev_submeter_never_hides_manual_site_load(self) -> None:
        live = self._coordinator(ev_age_seconds=61)._live_measurements()

        self.assertEqual(live["load_w"], 4600)
        self.assertEqual(live["site_load_w"], 4600)
        self.assertIsNone(live["ev_load_w"])
        self.assertFalse(live["ev_accounting_valid"])


class HistoryAccountingTests(unittest.TestCase):
    @staticmethod
    def _coordinator(result, *, ev_enabled: bool):
        class Recorder:
            async def async_add_executor_job(self, _function, *_args):
                return result

        coordinator = object.__new__(_MODULE.EnergyOptimizerCoordinator)
        energy_state = types.SimpleNamespace(
            attributes={"unit_of_measurement": "kWh"}
        )
        coordinator.hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=lambda _entity_id: energy_state)
        )
        coordinator.config = types.SimpleNamespace(
            energy_history_entities=("grid", "export", "pv", "charge", "discharge"),
            price_history_entity="price",
            tv_light_energy_entity="",
            device_energy_entities={},
            ev=types.SimpleNamespace(
                enabled=ev_enabled,
                energy_entity="ev_energy",
                site_meter_includes_ev=True,
            ),
        )
        return coordinator, Recorder()

    def test_high_site_import_is_split_before_small_house_limit(self) -> None:
        timestamp = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).replace(minute=0, second=0, microsecond=0).timestamp()
        changes = {
            "grid": 23.0,
            "export": 0.0,
            "pv": 0.0,
            "charge": 0.0,
            "discharge": 0.0,
            "ev_energy": 22.0,
        }
        result = {
            entity: [{"start": timestamp, "change": value}]
            for entity, value in changes.items()
        }
        result["price"] = [{"start": timestamp, "mean": 0.2}]
        coordinator, recorder = self._coordinator(result, ev_enabled=True)
        original_get_instance = _MODULE.get_instance
        _MODULE.get_instance = lambda _hass: recorder
        try:
            asyncio.run(coordinator._async_refresh_history())
        finally:
            _MODULE.get_instance = original_get_instance

        samples = [
            value
            for values in coordinator._load_samples.values()
            for value in values
        ]
        self.assertEqual(samples, [1.0])
        self.assertEqual(coordinator._recent_base_daily_kwh, 1.0)

    def test_missing_ev_statistics_fail_closed(self) -> None:
        timestamp = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).replace(minute=0, second=0, microsecond=0).timestamp()
        result = {
            entity: [{"start": timestamp, "change": value}]
            for entity, value in {
                "grid": 1.0,
                "export": 0.0,
                "pv": 0.0,
                "charge": 0.0,
                "discharge": 0.0,
            }.items()
        }
        result["price"] = [{"start": timestamp, "mean": 0.2}]
        coordinator, recorder = self._coordinator(result, ev_enabled=True)
        original_get_instance = _MODULE.get_instance
        _MODULE.get_instance = lambda _hass: recorder
        try:
            asyncio.run(coordinator._async_refresh_history())
        finally:
            _MODULE.get_instance = original_get_instance

        self.assertEqual(coordinator._history_hours, 0)
        self.assertFalse(coordinator._load_model_ready)
        self.assertEqual(coordinator._recent_base_days, 0)
        samples = [
            value
            for values in coordinator._load_samples.values()
            for value in values
        ]
        self.assertEqual(samples, [])

    def test_history_before_first_ev_statistic_preserves_house_model(self) -> None:
        timestamp = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).replace(minute=0, second=0, microsecond=0).timestamp()
        result = {
            entity: [{"start": timestamp, "change": value}]
            for entity, value in {
                "grid": 1.0,
                "export": 0.0,
                "pv": 0.0,
                "charge": 0.0,
                "discharge": 0.0,
            }.items()
        }
        result["ev_energy"] = [
            {"start": timestamp + 24 * 3600, "change": 0.0}
        ]
        result["price"] = [{"start": timestamp, "mean": 0.2}]
        coordinator, recorder = self._coordinator(result, ev_enabled=True)
        original_get_instance = _MODULE.get_instance
        _MODULE.get_instance = lambda _hass: recorder
        try:
            asyncio.run(coordinator._async_refresh_history())
        finally:
            _MODULE.get_instance = original_get_instance

        self.assertEqual(coordinator._history_hours, 1)
        self.assertEqual(coordinator._recent_base_days, 1)
        samples = [
            value
            for values in coordinator._load_samples.values()
            for value in values
        ]
        self.assertEqual(samples, [1.0])

    def test_gap_after_first_ev_statistic_is_not_learned_as_house_load(self) -> None:
        first = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
        second = first + 3600
        result = {
            entity: [
                {"start": first, "change": first_value},
                {"start": second, "change": second_value},
            ]
            for entity, first_value, second_value in (
                ("grid", 1.0, 4.0),
                ("export", 0.0, 0.0),
                ("pv", 0.0, 0.0),
                ("charge", 0.0, 0.0),
                ("discharge", 0.0, 0.0),
            )
        }
        result["ev_energy"] = [{"start": first, "change": 0.0}]
        result["price"] = [{"start": first, "mean": 0.2}]
        coordinator, recorder = self._coordinator(result, ev_enabled=True)
        original_get_instance = _MODULE.get_instance
        _MODULE.get_instance = lambda _hass: recorder
        try:
            asyncio.run(coordinator._async_refresh_history())
        finally:
            _MODULE.get_instance = original_get_instance

        samples = [
            value
            for values in coordinator._load_samples.values()
            for value in values
        ]
        self.assertEqual(samples, [1.0])
        self.assertEqual(coordinator._recent_base_days, 0)

    def test_daily_ev_coverage_checks_union_of_site_counter_hours(self) -> None:
        first = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
        second = first + 3600
        result = {
            "grid": [
                {"start": first, "change": 1.0},
                {"start": second, "change": 3.6},
            ],
            "export": [{"start": first, "change": 0.0}],
            "pv": [{"start": first, "change": 0.0}],
            "charge": [{"start": first, "change": 0.0}],
            "discharge": [{"start": first, "change": 0.0}],
            "ev_energy": [{"start": first, "change": 0.0}],
            "price": [{"start": first, "mean": 0.2}],
        }
        coordinator, recorder = self._coordinator(result, ev_enabled=True)
        original_get_instance = _MODULE.get_instance
        _MODULE.get_instance = lambda _hass: recorder
        try:
            asyncio.run(coordinator._async_refresh_history())
        finally:
            _MODULE.get_instance = original_get_instance

        self.assertEqual(coordinator._recent_base_days, 0)


if __name__ == "__main__":
    unittest.main()
