"""Focused tests for the optional EV config-flow forms and validation."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

_PACKAGE = "energy_optimizer"
_ROOT = Path(__file__).parents[1] / "custom_components" / _PACKAGE
_DEPENDENCY_MODULES = (
    "voluptuous",
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.selector",
)
_MISSING = object()
_ORIGINAL_DEPENDENCIES = {
    name: sys.modules.get(name, _MISSING) for name in _DEPENDENCY_MODULES
}


def _install_dependency_stubs() -> None:
    """Install the small API surface needed to import ``config_flow``."""
    voluptuous = types.ModuleType("voluptuous")

    class Marker:
        def __init__(self, schema, *, default=None) -> None:
            self.schema = schema
            self.default = default

    class Required(Marker):
        pass

    class Optional(Marker):
        pass

    class Schema:
        def __init__(self, schema) -> None:
            self.schema = schema

    voluptuous.Optional = Optional
    voluptuous.Required = Required
    voluptuous.Schema = Schema

    homeassistant = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    selector = types.ModuleType("homeassistant.helpers.selector")

    class ConfigFlow:
        def __init_subclass__(cls, *, domain=None, **kwargs) -> None:
            super().__init_subclass__(**kwargs)
            cls.domain = domain

        async def async_set_unique_id(self, unique_id) -> None:
            self.unique_id = unique_id

        def _abort_if_unique_id_configured(self) -> None:
            return None

        def async_show_form(self, *, step_id, data_schema, errors=None):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
            }

        def async_create_entry(self, *, title, data):
            return {"type": "create_entry", "title": title, "data": data}

    class OptionsFlowWithReload(ConfigFlow):
        pass

    class ConfigEntry:
        pass

    class SelectorConfig:
        def __init__(self, **kwargs) -> None:
            self.provided = dict(kwargs)
            for key, value in kwargs.items():
                setattr(self, key, value)

    class Selector:
        def __init__(self, config=None) -> None:
            self.config = config

    class BooleanSelector(Selector):
        pass

    class EntitySelector(Selector):
        pass

    class EntitySelectorConfig(SelectorConfig):
        pass

    class NumberSelector(Selector):
        pass

    class NumberSelectorConfig(SelectorConfig):
        pass

    class TextSelector(Selector):
        pass

    class TextSelectorConfig(SelectorConfig):
        pass

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlowWithReload = OptionsFlowWithReload
    core.HomeAssistant = object
    core.callback = lambda function: function
    selector.BooleanSelector = BooleanSelector
    selector.EntitySelector = EntitySelector
    selector.EntitySelectorConfig = EntitySelectorConfig
    selector.NumberSelector = NumberSelector
    selector.NumberSelectorConfig = NumberSelectorConfig
    selector.NumberSelectorMode = types.SimpleNamespace(BOX="box")
    selector.TextSelector = TextSelector
    selector.TextSelectorConfig = TextSelectorConfig
    selector.TextSelectorType = types.SimpleNamespace(TEXT="text")
    helpers.selector = selector

    modules = {
        "voluptuous": voluptuous,
        "homeassistant": homeassistant,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.selector": selector,
    }
    for name, module in modules.items():
        sys.modules[name] = module


_install_dependency_stubs()
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    sys.modules[_PACKAGE] = package
for module_name in ("const", "config"):
    qualified_name = f"{_PACKAGE}.{module_name}"
    if qualified_name in sys.modules:
        continue
    spec = importlib.util.spec_from_file_location(
        qualified_name, _ROOT / f"{module_name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)

_SPEC = importlib.util.spec_from_file_location(
    f"{_PACKAGE}.config_flow_under_test", _ROOT / "config_flow.py"
)
assert _SPEC is not None and _SPEC.loader is not None
config_flow = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = config_flow
_SPEC.loader.exec_module(config_flow)
const = sys.modules[f"{_PACKAGE}.const"]
for _module_name, _original in _ORIGINAL_DEPENDENCIES.items():
    if _original is _MISSING:
        sys.modules.pop(_module_name, None)
    else:
        sys.modules[_module_name] = _original


class _States:
    def __init__(self, units: dict[str, str]) -> None:
        self._states = {
            entity_id: types.SimpleNamespace(
                attributes={"unit_of_measurement": unit}
            )
            for entity_id, unit in units.items()
        }

    def get(self, entity_id: str):
        return self._states.get(entity_id)


def _hass_with_units(**overrides: str):
    units = {
        "sensor.ev_power": "W",
        "sensor.ev_energy": "kWh",
        "sensor.ev_soc": "%",
    }
    units.update(overrides)
    return types.SimpleNamespace(states=_States(units))


def _ev_details(**overrides):
    details = {
        const.CONF_SITE_METER_INCLUDES_EV: True,
        const.CONF_EV_LIVE_POWER_ENTITY: "sensor.ev_power",
        const.CONF_EV_ENERGY_ENTITY: "sensor.ev_energy",
        const.CONF_EV_CONNECTED_ENTITY: "binary_sensor.ev_connected",
        const.CONF_EV_VEHICLE_SOC_ENTITY: "sensor.ev_soc",
        const.CONF_EV_CALENDAR_ENTITY: "calendar.ev_departures",
        const.CONF_EV_NORMAL_CHARGE_POWER_KW: 3.6,
        const.CONF_EV_BOOST_CHARGE_POWER_KW: 11.0,
        const.CONF_EV_BATTERY_CAPACITY_KWH: 64.0,
        const.CONF_EV_CONSUMPTION_KWH_PER_100KM: 18.0,
        const.CONF_EV_RESERVE_KM: 45.0,
        const.CONF_EV_CHARGE_EFFICIENCY: 0.9,
        const.CONF_SITE_MAX_IMPORT_POWER_KW: 22.0,
    }
    details.update(overrides)
    return details


class EVSchemaTests(unittest.TestCase):
    def test_unitless_number_schema_omits_none_unit(self) -> None:
        schema = config_flow._ev_details_schema({})
        efficiency_selector = next(
            value
            for marker, value in schema.schema.items()
            if marker.schema == const.CONF_EV_CHARGE_EFFICIENCY
        )

        self.assertNotIn(
            "unit_of_measurement", efficiency_selector.config.provided
        )


class EVConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_ev_finishes_setup_without_detail_form(self) -> None:
        flow = config_flow.EnergyOptimizerConfigFlow()
        flow._data = {"existing_setup_value": "kept"}

        result = await flow.async_step_ev({const.CONF_EV_ENABLED: False})

        self.assertEqual(result["type"], "create_entry")
        self.assertFalse(result["data"][const.CONF_EV_ENABLED])
        self.assertEqual(result["data"]["existing_setup_value"], "kept")

    async def test_unconfirmed_site_topology_stays_on_details_form(self) -> None:
        flow = config_flow.EnergyOptimizerConfigFlow()
        flow.hass = _hass_with_units()

        result = await flow.async_step_ev_details(
            _ev_details(**{const.CONF_SITE_METER_INCLUDES_EV: False})
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "ev_details")
        self.assertEqual(result["errors"]["base"], "ev_topology_not_confirmed")

    async def test_ev_sensor_roles_must_be_distinct(self) -> None:
        flow = config_flow.EnergyOptimizerConfigFlow()
        flow.hass = _hass_with_units()

        result = await flow.async_step_ev_details(
            _ev_details(
                **{const.CONF_EV_ENERGY_ENTITY: "sensor.ev_power"}
            )
        )

        self.assertEqual(
            result["errors"]["base"], "ev_input_entities_conflict"
        )

    async def test_ev_submeter_must_not_reuse_site_meter(self) -> None:
        flow = config_flow.EnergyOptimizerConfigFlow()
        flow.hass = _hass_with_units()
        flow._data = {
            const.CONF_LIVE_LOAD_POWER_ENTITIES: ["sensor.ev_power"]
        }

        result = await flow.async_step_ev_details(_ev_details())

        self.assertEqual(
            result["errors"]["base"], "ev_submeter_conflicts_site"
        )

    async def test_power_energy_and_soc_units_are_validated(self) -> None:
        cases = (
            (
                const.CONF_EV_LIVE_POWER_ENTITY,
                "sensor.ev_power",
                "kW",
                "ev_live_power_unit",
            ),
            (
                const.CONF_EV_ENERGY_ENTITY,
                "sensor.ev_energy",
                "Wh",
                "ev_energy_unit",
            ),
            (
                const.CONF_EV_VEHICLE_SOC_ENTITY,
                "sensor.ev_soc",
                "ratio",
                "ev_soc_unit",
            ),
        )
        for key, entity_id, invalid_unit, error_code in cases:
            with self.subTest(key=key):
                flow = config_flow.EnergyOptimizerConfigFlow()
                flow.hass = _hass_with_units(**{entity_id: invalid_unit})

                result = await flow.async_step_ev_details(_ev_details())

                self.assertEqual(result["errors"][key], error_code)

        valid_flow = config_flow.EnergyOptimizerConfigFlow()
        valid_flow.hass = _hass_with_units()
        valid_result = await valid_flow.async_step_ev_details(_ev_details())
        self.assertEqual(valid_result["type"], "create_entry")

    async def test_options_update_preserves_unrelated_existing_options(self) -> None:
        flow = config_flow.EnergyOptimizerOptionsFlow()
        flow.config_entry = types.SimpleNamespace(
            data={const.CONF_LIVE_LOAD_POWER_ENTITIES: ["sensor.house"]},
            options={
                const.CONF_ALLOW_GRID_CHARGING: False,
                const.CONF_EV_RESERVE_KM: 80.0,
            },
        )
        flow.hass = _hass_with_units()

        next_step = await flow.async_step_init({const.CONF_EV_ENABLED: True})
        result = await flow.async_step_ev_details(_ev_details())

        self.assertEqual(next_step["type"], "form")
        self.assertEqual(next_step["step_id"], "ev_details")
        self.assertEqual(result["type"], "create_entry")
        self.assertFalse(result["data"][const.CONF_ALLOW_GRID_CHARGING])
        self.assertEqual(result["data"][const.CONF_EV_RESERVE_KM], 45.0)
        self.assertTrue(result["data"][const.CONF_EV_ENABLED])


if __name__ == "__main__":
    unittest.main()
