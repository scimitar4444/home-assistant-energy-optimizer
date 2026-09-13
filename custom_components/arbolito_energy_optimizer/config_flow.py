"""Config flow for Arbolito Energy Optimizer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_ALLOW_GRID_CHARGING,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
    CONF_CONTROL_ENABLE_ENTITY,
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
    CONF_SOC_ENTITY,
    CONF_VICTRON_MODBUS_HOST,
    CONF_WEATHER_CLOUD_ENTITY,
    CONF_WEATHER_FORECAST_ENTITY,
    CONF_WEATHER_ILLUMINANCE_ENTITY,
    CONF_WEATHER_RAIN_BINARY_ENTITY,
    CONF_WEATHER_TEMPERATURE_ENTITY,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_EXPORT_EUR_KWH,
    DEFAULT_HARD_MIN_SOC,
    DOMAIN,
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
    unit: str,
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


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
            return self.async_create_entry(title=NAME, data=self._data)
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
