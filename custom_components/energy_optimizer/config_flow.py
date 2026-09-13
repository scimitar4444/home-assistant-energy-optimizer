"""Config flow for Home Assistant Energy Optimizer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers import selector

from .config import ev_input_entities_conflict, ev_submeter_conflicts_site
from .const import (
    CONF_ALLOW_GRID_CHARGING,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
    CONF_CONTROL_ENABLE_ENTITY,
    CONF_EV_BATTERY_CAPACITY_KWH,
    CONF_EV_BOOST_CHARGE_POWER_KW,
    CONF_EV_CALENDAR_ENTITY,
    CONF_EV_CHARGE_EFFICIENCY,
    CONF_EV_CONNECTED_ENTITY,
    CONF_EV_CONSUMPTION_KWH_PER_100KM,
    CONF_EV_ENABLED,
    CONF_EV_ENERGY_ENTITY,
    CONF_EV_LIVE_POWER_ENTITY,
    CONF_EV_NORMAL_CHARGE_POWER_KW,
    CONF_EV_RESERVE_KM,
    CONF_EV_VEHICLE_SOC_ENTITY,
    CONF_EXPORT_EUR_KWH,
    CONF_GRID_EXPORT_ENERGY_ENTITY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_HARD_MIN_SOC,
    CONF_LIVE_GRID_POWER_ENTITY,
    CONF_LIVE_LOAD_POWER_ENTITIES,
    CONF_LIVE_PV_POWER_ENTITY,
    CONF_PRICE_HISTORY_ENTITY,
    CONF_PRICE_TIMELINE_ENTITY,
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_TODAY_REMAINING_ENTITY,
    CONF_PV_TOMORROW_ENTITY,
    CONF_SITE_MAX_IMPORT_POWER_KW,
    CONF_SITE_METER_INCLUDES_EV,
    CONF_SOC_ENTITY,
    CONF_VICTRON_MODBUS_HOST,
    CONF_WEATHER_CLOUD_ENTITY,
    CONF_WEATHER_FORECAST_ENTITY,
    CONF_WEATHER_ILLUMINANCE_ENTITY,
    CONF_WEATHER_RAIN_BINARY_ENTITY,
    CONF_WEATHER_TEMPERATURE_ENTITY,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_EV_BATTERY_CAPACITY_KWH,
    DEFAULT_EV_BOOST_CHARGE_POWER_KW,
    DEFAULT_EV_CHARGE_EFFICIENCY,
    DEFAULT_EV_CONSUMPTION_KWH_PER_100KM,
    DEFAULT_EV_ENABLED,
    DEFAULT_EV_NORMAL_CHARGE_POWER_KW,
    DEFAULT_EV_RESERVE_KM,
    DEFAULT_EXPORT_EUR_KWH,
    DEFAULT_HARD_MIN_SOC,
    DEFAULT_SITE_MAX_IMPORT_POWER_KW,
    DEFAULT_SITE_METER_INCLUDES_EV,
    DOMAIN,
    MAX_SITE_IMPORT_POWER_KW,
    NAME,
)


def _entity(domain: str, *, multiple: bool = False) -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain, multiple=multiple)
    )


def _number(
    minimum: float,
    maximum: float,
    step: float,
    unit: str | None,
) -> selector.NumberSelector:
    config: dict[str, Any] = {
        "min": minimum,
        "max": maximum,
        "step": step,
        "mode": selector.NumberSelectorMode.BOX,
    }
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(
        selector.NumberSelectorConfig(**config)
    )


def _stored(data: Mapping[str, Any], key: str, default: Any) -> Any:
    """Return a form default without mutating the source mapping."""
    value = data.get(key, default)
    return default if value is None else value


def _ev_enable_schema(data: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_EV_ENABLED,
                default=bool(_stored(data, CONF_EV_ENABLED, DEFAULT_EV_ENABLED)),
            ): selector.BooleanSelector(),
        }
    )


def _ev_details_schema(data: Mapping[str, Any]) -> vol.Schema:
    """Build the generic EV input form with safe automatic limits."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SITE_METER_INCLUDES_EV,
                default=bool(
                    _stored(
                        data,
                        CONF_SITE_METER_INCLUDES_EV,
                        DEFAULT_SITE_METER_INCLUDES_EV,
                    )
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_EV_LIVE_POWER_ENTITY,
                default=_stored(data, CONF_EV_LIVE_POWER_ENTITY, ""),
            ): _entity("sensor"),
            vol.Required(
                CONF_EV_ENERGY_ENTITY,
                default=_stored(data, CONF_EV_ENERGY_ENTITY, ""),
            ): _entity("sensor"),
            vol.Required(
                CONF_EV_CONNECTED_ENTITY,
                default=_stored(data, CONF_EV_CONNECTED_ENTITY, ""),
            ): _entity("binary_sensor"),
            vol.Required(
                CONF_EV_VEHICLE_SOC_ENTITY,
                default=_stored(data, CONF_EV_VEHICLE_SOC_ENTITY, ""),
            ): _entity("sensor"),
            vol.Required(
                CONF_EV_CALENDAR_ENTITY,
                default=_stored(data, CONF_EV_CALENDAR_ENTITY, ""),
            ): _entity("calendar"),
            vol.Required(
                CONF_EV_NORMAL_CHARGE_POWER_KW,
                default=float(
                    _stored(
                        data,
                        CONF_EV_NORMAL_CHARGE_POWER_KW,
                        DEFAULT_EV_NORMAL_CHARGE_POWER_KW,
                    )
                ),
            ): _number(0.1, 11, 0.1, "kW"),
            vol.Required(
                CONF_EV_BOOST_CHARGE_POWER_KW,
                default=float(
                    _stored(
                        data,
                        CONF_EV_BOOST_CHARGE_POWER_KW,
                        DEFAULT_EV_BOOST_CHARGE_POWER_KW,
                    )
                ),
            ): _number(0.1, 11, 0.1, "kW"),
            vol.Required(
                CONF_EV_BATTERY_CAPACITY_KWH,
                default=float(
                    _stored(
                        data,
                        CONF_EV_BATTERY_CAPACITY_KWH,
                        DEFAULT_EV_BATTERY_CAPACITY_KWH,
                    )
                ),
            ): _number(1, 250, 0.1, "kWh"),
            vol.Required(
                CONF_EV_CONSUMPTION_KWH_PER_100KM,
                default=float(
                    _stored(
                        data,
                        CONF_EV_CONSUMPTION_KWH_PER_100KM,
                        DEFAULT_EV_CONSUMPTION_KWH_PER_100KM,
                    )
                ),
            ): _number(1, 100, 0.1, "kWh/100 km"),
            vol.Required(
                CONF_EV_RESERVE_KM,
                default=float(
                    _stored(data, CONF_EV_RESERVE_KM, DEFAULT_EV_RESERVE_KM)
                ),
            ): _number(0, 1000, 1, "km"),
            vol.Required(
                CONF_EV_CHARGE_EFFICIENCY,
                default=float(
                    _stored(
                        data,
                        CONF_EV_CHARGE_EFFICIENCY,
                        DEFAULT_EV_CHARGE_EFFICIENCY,
                    )
                ),
            ): _number(0.5, 1, 0.01, None),
            vol.Required(
                CONF_SITE_MAX_IMPORT_POWER_KW,
                default=float(
                    _stored(
                        data,
                        CONF_SITE_MAX_IMPORT_POWER_KW,
                        DEFAULT_SITE_MAX_IMPORT_POWER_KW,
                    )
                ),
            ): _number(1, MAX_SITE_IMPORT_POWER_KW, 0.1, "kW"),
        }
    )


def _configured_unit(hass: Any, entity_id: str) -> str | None:
    """Return an entity unit when a selected state is already available."""
    states = getattr(hass, "states", None)
    state = states.get(entity_id) if states is not None and entity_id else None
    if state is None:
        return None
    return str(state.attributes.get("unit_of_measurement", "")).strip()


def _ev_validation_errors(
    data: Mapping[str, Any], *, hass: Any | None = None
) -> dict[str, str]:
    """Validate relationships which cannot be expressed by selectors."""
    errors: dict[str, str] = {}
    if not data.get(CONF_SITE_METER_INCLUDES_EV, False):
        errors["base"] = "ev_topology_not_confirmed"
    elif ev_submeter_conflicts_site(data):
        errors["base"] = "ev_submeter_conflicts_site"
    elif ev_input_entities_conflict(data):
        errors["base"] = "ev_input_entities_conflict"
    normal_power = float(data[CONF_EV_NORMAL_CHARGE_POWER_KW])
    if float(data[CONF_EV_BOOST_CHARGE_POWER_KW]) < normal_power:
        errors[CONF_EV_BOOST_CHARGE_POWER_KW] = "ev_boost_below_normal"
    if float(data[CONF_SITE_MAX_IMPORT_POWER_KW]) < normal_power:
        errors[CONF_SITE_MAX_IMPORT_POWER_KW] = "ev_site_limit_below_normal"
    if hass is not None:
        unit_checks = (
            (CONF_EV_LIVE_POWER_ENTITY, "W", "ev_live_power_unit"),
            (CONF_EV_ENERGY_ENTITY, "kWh", "ev_energy_unit"),
            (CONF_EV_VEHICLE_SOC_ENTITY, "%", "ev_soc_unit"),
        )
        for key, required_unit, error_code in unit_checks:
            unit = _configured_unit(hass, str(data.get(key, "")))
            if unit is not None and unit != required_unit:
                errors[key] = error_code
    return errors


class EnergyOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Collect all installation-specific entity mappings."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input=None):
        """Collect battery and tariff policy settings."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_prices()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BATTERY_CAPACITY_KWH,
                        default=DEFAULT_BATTERY_CAPACITY_KWH,
                    ): _number(0.5, 200, 0.1, "kWh"),
                    vol.Required(
                        CONF_HARD_MIN_SOC,
                        default=DEFAULT_HARD_MIN_SOC,
                    ): _number(0, 90, 1, "%"),
                    vol.Required(
                        CONF_EXPORT_EUR_KWH,
                        default=DEFAULT_EXPORT_EUR_KWH,
                    ): _number(-1, 2, 0.001, "EUR/kWh"),
                    vol.Required(
                        CONF_ALLOW_GRID_CHARGING,
                        default=False,
                    ): selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_prices(self, user_input=None):
        """Collect price, SoC and PV forecast entities."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_power()
        return self.async_show_form(
            step_id="prices",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRICE_TIMELINE_ENTITY): _entity("sensor"),
                    vol.Required(CONF_PRICE_HISTORY_ENTITY): _entity("sensor"),
                    vol.Required(CONF_SOC_ENTITY): _entity("sensor"),
                    vol.Required(CONF_PV_TODAY_REMAINING_ENTITY): _entity("sensor"),
                    vol.Required(CONF_PV_TOMORROW_ENTITY): _entity("sensor"),
                }
            ),
        )

    async def async_step_power(self, user_input=None):
        """Collect live power entities."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_energy()
        return self.async_show_form(
            step_id="power",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIVE_PV_POWER_ENTITY): _entity("sensor"),
                    vol.Required(CONF_LIVE_GRID_POWER_ENTITY): _entity("sensor"),
                    vol.Required(CONF_LIVE_LOAD_POWER_ENTITIES): _entity(
                        "sensor", multiple=True
                    ),
                }
            ),
        )

    async def async_step_energy(self, user_input=None):
        """Collect ordered long-term energy counters."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_optional()
        return self.async_show_form(
            step_id="energy",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GRID_IMPORT_ENERGY_ENTITY): _entity("sensor"),
                    vol.Required(CONF_GRID_EXPORT_ENERGY_ENTITY): _entity("sensor"),
                    vol.Required(CONF_PV_ENERGY_ENTITY): _entity("sensor"),
                    vol.Required(CONF_BATTERY_CHARGE_ENERGY_ENTITY): _entity("sensor"),
                    vol.Required(CONF_BATTERY_DISCHARGE_ENERGY_ENTITY): _entity(
                        "sensor"
                    ),
                }
            ),
        )

    async def async_step_optional(self, user_input=None):
        """Collect optional weather and advanced-control inputs."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()
        return self.async_show_form(
            step_id="optional",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_CONTROL_ENABLE_ENTITY): _entity(
                        "input_boolean"
                    ),
                    vol.Optional(CONF_WEATHER_TEMPERATURE_ENTITY): _entity("sensor"),
                    vol.Optional(CONF_WEATHER_CLOUD_ENTITY): _entity("sensor"),
                    vol.Optional(CONF_WEATHER_ILLUMINANCE_ENTITY): _entity("sensor"),
                    vol.Optional(CONF_WEATHER_RAIN_BINARY_ENTITY): _entity(
                        "binary_sensor"
                    ),
                    vol.Optional(CONF_WEATHER_FORECAST_ENTITY): _entity("weather"),
                    vol.Optional(CONF_VICTRON_MODBUS_HOST): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT
                        )
                    ),
                }
            ),
        )

    async def async_step_ev(self, user_input=None):
        """Offer the optional EV planner without changing legacy defaults."""
        if user_input is not None:
            self._data.update(user_input)
            if not user_input[CONF_EV_ENABLED]:
                return self.async_create_entry(title=NAME, data=self._data)
            return await self.async_step_ev_details()
        return self.async_show_form(
            step_id="ev",
            data_schema=_ev_enable_schema(self._data),
        )

    async def async_step_ev_details(self, user_input=None):
        """Collect EV entities, vehicle assumptions and the topology consent."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _ev_validation_errors(
                {**self._data, **user_input}, hass=self.hass
            )
            if not errors:
                self._data.update(user_input)
                self._data[CONF_EV_ENABLED] = True
                return self.async_create_entry(title=NAME, data=self._data)
        return self.async_show_form(
            step_id="ev_details",
            data_schema=_ev_details_schema({**self._data, **(user_input or {})}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Allow existing installations to opt into EV observation."""
        return EnergyOptimizerOptionsFlow()


class EnergyOptimizerOptionsFlow(OptionsFlowWithReload):
    """Edit the optional EV planner and reload the integration safely."""

    def __init__(self) -> None:
        self._ev_data: dict[str, Any] = {}

    @property
    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(self, user_input=None):
        """Enable, disable or continue to the EV settings."""
        if user_input is not None:
            self._ev_data = {
                CONF_EV_ENABLED: bool(user_input[CONF_EV_ENABLED]),
            }
            if not self._ev_data[CONF_EV_ENABLED]:
                return self.async_create_entry(
                    title="",
                    data={**self.config_entry.options, **self._ev_data},
                )
            return await self.async_step_ev_details()
        return self.async_show_form(
            step_id="init",
            data_schema=_ev_enable_schema(self._current),
        )

    async def async_step_ev_details(self, user_input=None):
        """Update EV observation settings for an existing config entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _ev_validation_errors(
                {**self._current, **self._ev_data, **user_input},
                hass=self.hass,
            )
            if not errors:
                self._ev_data.update(user_input)
                self._ev_data[CONF_EV_ENABLED] = True
                return self.async_create_entry(
                    title="",
                    data={**self.config_entry.options, **self._ev_data},
                )
        return self.async_show_form(
            step_id="ev_details",
            data_schema=_ev_details_schema(
                {**self._current, **self._ev_data, **(user_input or {})}
            ),
            errors=errors,
        )
