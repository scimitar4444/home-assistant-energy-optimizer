"""Regression tests for optional, fail-closed EV configuration."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

_PACKAGE = "energy_optimizer"
_ROOT = Path(__file__).parents[1] / "custom_components" / _PACKAGE
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    sys.modules[_PACKAGE] = package
for module_name in ("const", "config"):
    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE}.{module_name}", _ROOT / f"{module_name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

const = sys.modules[f"{_PACKAGE}.const"]
EVConfig = sys.modules[f"{_PACKAGE}.config"].EVConfig
OptimizerConfig = sys.modules[f"{_PACKAGE}.config"].OptimizerConfig


def _enabled_ev_mapping() -> dict[str, object]:
    return {
        const.CONF_EV_ENABLED: True,
        const.CONF_SITE_METER_INCLUDES_EV: True,
        const.CONF_EV_LIVE_POWER_ENTITY: "sensor.wallbox_power",
        const.CONF_EV_ENERGY_ENTITY: "sensor.wallbox_energy",
        const.CONF_EV_CONNECTED_ENTITY: "binary_sensor.vehicle_connected",
        const.CONF_EV_VEHICLE_SOC_ENTITY: "sensor.vehicle_soc",
        const.CONF_EV_CALENDAR_ENTITY: "calendar.vehicle_departures",
    }


class EVConfigTests(unittest.TestCase):
    def test_legacy_entry_defaults_to_disabled_ev_planning(self) -> None:
        ev = OptimizerConfig.from_mapping({}).ev

        self.assertFalse(ev.enabled)
        self.assertEqual(ev.normal_charge_power_kw, 3.6)
        self.assertEqual(ev.boost_charge_power_kw, 11.0)
        self.assertEqual(ev.site_max_import_power_kw, 22.0)

    def test_enable_without_topology_confirmation_fails_closed(self) -> None:
        values = _enabled_ev_mapping()
        values[const.CONF_SITE_METER_INCLUDES_EV] = False

        self.assertFalse(OptimizerConfig.from_mapping(values).ev.enabled)

    def test_enable_without_every_required_entity_fails_closed(self) -> None:
        for key in (
            const.CONF_EV_LIVE_POWER_ENTITY,
            const.CONF_EV_ENERGY_ENTITY,
            const.CONF_EV_CONNECTED_ENTITY,
            const.CONF_EV_VEHICLE_SOC_ENTITY,
            const.CONF_EV_CALENDAR_ENTITY,
        ):
            with self.subTest(key=key):
                values = _enabled_ev_mapping()
                values.pop(key)
                self.assertFalse(OptimizerConfig.from_mapping(values).ev.enabled)

    def test_complete_confirmed_mapping_enables_ev_observation(self) -> None:
        ev = OptimizerConfig.from_mapping(_enabled_ev_mapping()).ev

        self.assertTrue(ev.enabled)
        self.assertTrue(ev.site_meter_includes_ev)
        self.assertEqual(ev.calendar_entity, "calendar.vehicle_departures")

    def test_site_sensor_reused_as_ev_submeter_fails_closed(self) -> None:
        values = _enabled_ev_mapping()
        values[const.CONF_LIVE_LOAD_POWER_ENTITIES] = (
            "sensor.phase_1",
            "sensor.wallbox_power",
        )
        self.assertFalse(OptimizerConfig.from_mapping(values).ev.enabled)

        values = _enabled_ev_mapping()
        values[const.CONF_GRID_IMPORT_ENERGY_ENTITY] = "sensor.wallbox_energy"
        self.assertFalse(OptimizerConfig.from_mapping(values).ev.enabled)

    def test_ev_input_sensor_roles_must_be_distinct(self) -> None:
        values = _enabled_ev_mapping()
        values[const.CONF_EV_VEHICLE_SOC_ENTITY] = "sensor.wallbox_power"

        self.assertFalse(OptimizerConfig.from_mapping(values).ev.enabled)

    def test_automatic_power_is_hard_capped_at_11_kw(self) -> None:
        values = _enabled_ev_mapping()
        values.update(
            {
                const.CONF_EV_NORMAL_CHARGE_POWER_KW: 22,
                const.CONF_EV_BOOST_CHARGE_POWER_KW: 22,
            }
        )

        ev = OptimizerConfig.from_mapping(values).ev

        self.assertEqual(ev.normal_charge_power_kw, 11.0)
        self.assertEqual(ev.boost_charge_power_kw, 11.0)

    def test_negative_reserve_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserve distance"):
            EVConfig(reserve_km=-0.5)

    def test_site_import_limit_matches_accounting_ceiling(self) -> None:
        with self.assertRaisesRegex(ValueError, "site import power"):
            EVConfig(site_max_import_power_kw=60.1)

    def test_ev_configuration_is_frozen(self) -> None:
        ev = EVConfig()

        with self.assertRaises(FrozenInstanceError):
            ev.enabled = True

    def test_options_override_legacy_entry_data(self) -> None:
        class Entry:
            data = _enabled_ev_mapping()
            options = {const.CONF_EV_ENABLED: False}

        self.assertFalse(OptimizerConfig.from_entry(Entry()).ev.enabled)


if __name__ == "__main__":
    unittest.main()
