"""Tests for the optional EV observation sensors."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

_PACKAGE = "energy_optimizer"
_ROOT = Path(__file__).parents[1] / "custom_components" / _PACKAGE


def _module(name: str) -> ModuleType:
    module = ModuleType(name)
    sys.modules[name] = module
    return module


def _load_sensor_module() -> ModuleType:
    """Load the sensor platform with a small Home Assistant API stub."""
    package = sys.modules.get(_PACKAGE) or _module(_PACKAGE)
    package.__path__ = [str(_ROOT)]

    for name in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.helpers",
    ):
        if name not in sys.modules:
            module = _module(name)
            module.__path__ = []

    sensor_api = _module("homeassistant.components.sensor")

    class SensorDeviceClass:
        ENUM = "enum"
        TIMESTAMP = "timestamp"

    class SensorStateClass:
        MEASUREMENT = "measurement"

    @dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str
        translation_key: str | None = None
        device_class: str | None = None
        options: list[str] | None = None
        icon: str | None = None
        native_unit_of_measurement: str | None = None
        state_class: str | None = None
        entity_category: str | None = None

    class SensorEntity:
        pass

    sensor_api.SensorDeviceClass = SensorDeviceClass
    sensor_api.SensorEntity = SensorEntity
    sensor_api.SensorEntityDescription = SensorEntityDescription
    sensor_api.SensorStateClass = SensorStateClass

    config_entries = _module("homeassistant.config_entries")
    config_entries.ConfigEntry = type("ConfigEntry", (), {})

    constants = _module("homeassistant.const")
    constants.PERCENTAGE = "%"
    constants.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
    constants.UnitOfElectricCurrent = SimpleNamespace(AMPERE="A")
    constants.UnitOfEnergy = SimpleNamespace(KILO_WATT_HOUR="kWh")
    constants.UnitOfPower = SimpleNamespace(WATT="W")
    constants.UnitOfTime = SimpleNamespace(SECONDS="s")

    core = _module("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})

    device_registry = _module("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = lambda **kwargs: kwargs

    entity_platform = _module("homeassistant.helpers.entity_platform")
    entity_platform.AddConfigEntryEntitiesCallback = object

    update_coordinator = _module("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = SimpleNamespace(
                states=SimpleNamespace(is_state=lambda *_args: False)
            )

        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    update_coordinator.CoordinatorEntity = CoordinatorEntity

    const_spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE}.const", _ROOT / "const.py"
    )
    assert const_spec is not None and const_spec.loader is not None
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const_module
    const_spec.loader.exec_module(const_module)

    coordinator = _module(f"{_PACKAGE}.coordinator")
    coordinator.EnergyOptimizerCoordinator = type(
        "EnergyOptimizerCoordinator", (), {}
    )

    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE}.sensor", _ROOT / "sensor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sensor = _load_sensor_module()


def _ev_data(**overrides):
    plan = {
        "status": "scheduled",
        "reason": "lowest_confirmed_cost",
        "next_departure": "2026-09-15T12:00:00+02:00",
        "required_wallbox_kwh": 12.34,
        "target_soc_percent": 63.5,
        "suggested_power_w": 3600,
        "suggested_mode": "normal",
        "provisional": False,
        "complete": True,
        "data_quality_percent": 91,
        "planned_wallbox_kwh": 12.34,
        "unmet_wallbox_kwh": 0.0,
        "connected": True,
        "observation_mode": True,
        "applied_to_site_optimizer": False,
        "accounting_valid": True,
    }
    plan.update(overrides)
    return {"ev_plan": plan}


class EVSensorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.descriptions = {item.key: item for item in sensor.EV_SENSORS}

    def test_optimizer_diagnostics_are_visible_sensors(self) -> None:
        descriptions = {item.key: item for item in sensor.SENSORS}
        self.assertEqual(
            {
                "calculation_duration",
                "optimizer_passes",
                "pv_forecast_source",
            },
            set(descriptions)
            & {
                "calculation_duration",
                "optimizer_passes",
                "pv_forecast_source",
            },
        )
        data = {
            "calculation_duration_seconds": 2.34,
            "optimizer_passes": 2,
            "pv_forecast_source": "mixed",
        }
        self.assertEqual(
            descriptions["calculation_duration"].value_fn(data), 2.34
        )
        self.assertEqual(descriptions["optimizer_passes"].value_fn(data), 2)
        self.assertEqual(
            descriptions["pv_forecast_source"].value_fn(data), "mixed"
        )

    def test_ev_sensor_surface_is_complete_and_concise(self) -> None:
        self.assertEqual(
            set(self.descriptions),
            {
                "ev_status",
                "ev_next_departure",
                "ev_required_wallbox_energy",
                "ev_target_soc",
                "ev_suggested_power",
                "ev_suggested_mode",
                "ev_plan_feasible",
                "ev_data_quality",
            },
        )

    def test_ev_values_use_confirmed_coordinator_contract(self) -> None:
        data = _ev_data()
        self.assertEqual(
            self.descriptions["ev_status"].value_fn(data), "scheduled"
        )
        self.assertEqual(
            self.descriptions["ev_suggested_power"].value_fn(data), 3600
        )
        self.assertEqual(
            self.descriptions["ev_suggested_mode"].value_fn(data), "normal"
        )
        self.assertEqual(
            self.descriptions["ev_plan_feasible"].value_fn(data), "feasible"
        )
        departure = self.descriptions["ev_next_departure"].value_fn(data)
        self.assertEqual(departure.isoformat(), "2026-09-15T12:00:00+02:00")

    def test_missing_data_is_not_reported_as_infeasible(self) -> None:
        data = _ev_data(
            status="awaiting_data",
            complete=False,
            next_departure=None,
            required_wallbox_kwh=None,
        )
        self.assertEqual(
            self.descriptions["ev_plan_feasible"].value_fn(data), "unknown"
        )
        self.assertIsNone(
            self.descriptions["ev_next_departure"].value_fn(data)
        )

    def test_disconnected_future_schedule_is_only_provisional(self) -> None:
        data = _ev_data(connected=False, suggested_power_w=0)

        self.assertEqual(
            self.descriptions["ev_plan_feasible"].value_fn(data), "unknown"
        )

    def test_naive_departure_is_rejected_for_timestamp_sensor(self) -> None:
        self.assertIsNone(
            self.descriptions["ev_next_departure"].value_fn(
                _ev_data(next_departure="2026-09-15T12:00:00")
            )
        )
        self.assertIsNone(
            self.descriptions["ev_next_departure"].value_fn(
                _ev_data(next_departure=datetime(2026, 9, 15, 12))
            )
        )

    def test_status_enum_covers_every_coordinator_state(self) -> None:
        self.assertEqual(
            self.descriptions["ev_status"].options,
            [
                "disabled",
                "awaiting_trip",
                "awaiting_horizon",
                "awaiting_data",
                "ready",
                "scheduled",
                "infeasible",
            ],
        )

    def test_missing_ev_payload_uses_safe_startup_state(self) -> None:
        self.assertEqual(
            self.descriptions["ev_status"].value_fn({}), "disabled"
        )
        self.assertEqual(
            self.descriptions["ev_suggested_power"].value_fn({}), 0
        )
        self.assertEqual(
            self.descriptions["ev_plan_feasible"].value_fn({}), "unknown"
        )

    def test_ev_entities_are_created_only_when_enabled(self) -> None:
        async def setup(enabled: bool):
            coordinator = SimpleNamespace(
                config=SimpleNamespace(ev=SimpleNamespace(enabled=enabled)),
                data=_ev_data(),
            )
            entry = SimpleNamespace(runtime_data=coordinator, entry_id="entry")
            entities = []
            await sensor.async_setup_entry(
                SimpleNamespace(), entry, lambda values: entities.extend(values)
            )
            return entities

        without_ev = asyncio.run(setup(False))
        with_ev = asyncio.run(setup(True))
        self.assertEqual(len(without_ev), len(sensor.SENSORS))
        self.assertEqual(
            len(with_ev), len(sensor.SENSORS) + len(sensor.EV_SENSORS)
        )
        self.assertFalse(
            any(entity.entity_description.key.startswith("ev_") for entity in without_ev)
        )

    def test_ev_status_attributes_make_observation_mode_explicit(self) -> None:
        coordinator = SimpleNamespace(data=_ev_data())
        entry = SimpleNamespace(entry_id="entry")
        entity = sensor.OptimizerSensor(
            coordinator, entry, self.descriptions["ev_status"]
        )
        attributes = entity.extra_state_attributes
        self.assertTrue(attributes["observation_mode"])
        self.assertFalse(attributes["ev_plan_applied_to_site_optimizer"])
        self.assertTrue(attributes["accounting_valid"])

    def test_power_attributes_expose_duration_and_expiry(self) -> None:
        coordinator = SimpleNamespace(
            data=_ev_data(
                suggested_duration_minutes=7.5,
                suggested_valid_until="2026-09-15T12:07:30+02:00",
            )
        )
        entry = SimpleNamespace(entry_id="entry")
        entity = sensor.OptimizerSensor(
            coordinator, entry, self.descriptions["ev_suggested_power"]
        )

        self.assertEqual(entity.extra_state_attributes["duration_minutes"], 7.5)
        self.assertEqual(
            entity.extra_state_attributes["valid_until"],
            "2026-09-15T12:07:30+02:00",
        )
        self.assertTrue(entity.extra_state_attributes["observation_only"])


if __name__ == "__main__":
    unittest.main()
