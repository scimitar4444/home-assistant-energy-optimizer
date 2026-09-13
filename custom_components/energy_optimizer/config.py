"""Validated runtime configuration for the optimizer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .const import *  # noqa: F403 - config keys/defaults are intentionally centralized


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key, "")
    return str(value).strip() if value is not None else ""


@dataclass(frozen=True)
class OptimizerConfig:
    """All installation-specific inputs used by one optimizer instance."""

    price_timeline_entity: str
    price_history_entity: str
    soc_entity: str
    pv_today_remaining_entity: str
    pv_tomorrow_entity: str
    live_pv_power_entity: str
    live_grid_power_entity: str
    live_load_power_entities: tuple[str, ...]
    energy_history_entities: tuple[str, str, str, str, str]
    battery_capacity_kwh: float = DEFAULT_BATTERY_CAPACITY_KWH  # noqa: F405
    hard_min_soc: float = DEFAULT_HARD_MIN_SOC  # noqa: F405
    battery_wear_eur_kwh: float = DEFAULT_BATTERY_WEAR_EUR_KWH  # noqa: F405
    export_eur_kwh: float = DEFAULT_EXPORT_EUR_KWH  # noqa: F405
    pv_curtailment_penalty_eur_kwh: float = DEFAULT_PV_CURTAILMENT_PENALTY_EUR_KWH  # noqa: F405
    grid_charge_margin_eur_kwh: float = DEFAULT_GRID_CHARGE_MARGIN_EUR_KWH  # noqa: F405
    allow_grid_charging: bool = True
    quiet_hours_start: int = DEFAULT_QUIET_HOURS_START  # noqa: F405
    quiet_hours_end: int = DEFAULT_QUIET_HOURS_END  # noqa: F405
    quiet_grid_charge_kw: float = DEFAULT_QUIET_GRID_CHARGE_KW  # noqa: F405
    day_grid_charge_kw: float = DEFAULT_DAY_GRID_CHARGE_KW  # noqa: F405
    quiet_charge_current_a: int = DEFAULT_QUIET_CHARGE_CURRENT_A  # noqa: F405
    normal_charge_current_a: int = DEFAULT_NORMAL_CHARGE_CURRENT_A  # noqa: F405
    control_enable_entity: str = ""
    weather_temperature_entity: str = ""
    weather_cloud_entity: str = ""
    weather_illuminance_entity: str = ""
    weather_rain_binary_entity: str = ""
    weather_forecast_entity: str = ""
    victron_modbus_host: str = ""
    victron_modbus_port: int = DEFAULT_VICTRON_MODBUS_PORT  # noqa: F405
    victron_settings_unit_id: int = DEFAULT_VICTRON_SETTINGS_UNIT_ID  # noqa: F405
    victron_max_charge_current_register: int = DEFAULT_VICTRON_MAX_CHARGE_CURRENT_REGISTER  # noqa: F405
    victron_grid_setpoint_register: int = DEFAULT_VICTRON_GRID_SETPOINT_REGISTER  # noqa: F405
    victron_grid_setpoint_max_w: int = DEFAULT_VICTRON_GRID_SETPOINT_MAX_W  # noqa: F405
    tv_light_energy_entity: str = ""
    device_energy_entities: Mapping[str, str] = field(default_factory=dict)
    appliances: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "OptimizerConfig":
        """Build normalized settings from config-entry data."""
        load_entities = tuple(
            str(entity).strip()
            for entity in data.get(CONF_LIVE_LOAD_POWER_ENTITIES, ())  # noqa: F405
            if str(entity).strip()
        )
        energy_entities = (
            _text(data, CONF_GRID_IMPORT_ENERGY_ENTITY),  # noqa: F405
            _text(data, CONF_GRID_EXPORT_ENERGY_ENTITY),  # noqa: F405
            _text(data, CONF_PV_ENERGY_ENTITY),  # noqa: F405
            _text(data, CONF_BATTERY_CHARGE_ENERGY_ENTITY),  # noqa: F405
            _text(data, CONF_BATTERY_DISCHARGE_ENERGY_ENTITY),  # noqa: F405
        )
        return cls(
            price_timeline_entity=_text(data, CONF_PRICE_TIMELINE_ENTITY),  # noqa: F405
            price_history_entity=_text(data, CONF_PRICE_HISTORY_ENTITY),  # noqa: F405
            soc_entity=_text(data, CONF_SOC_ENTITY),  # noqa: F405
            pv_today_remaining_entity=_text(data, CONF_PV_TODAY_REMAINING_ENTITY),  # noqa: F405
            pv_tomorrow_entity=_text(data, CONF_PV_TOMORROW_ENTITY),  # noqa: F405
            live_pv_power_entity=_text(data, CONF_LIVE_PV_POWER_ENTITY),  # noqa: F405
            live_grid_power_entity=_text(data, CONF_LIVE_GRID_POWER_ENTITY),  # noqa: F405
            live_load_power_entities=load_entities,
            energy_history_entities=energy_entities,
            battery_capacity_kwh=float(data.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH)),  # noqa: F405
            hard_min_soc=float(data.get(CONF_HARD_MIN_SOC, DEFAULT_HARD_MIN_SOC)),  # noqa: F405
            battery_wear_eur_kwh=float(data.get(CONF_BATTERY_WEAR_EUR_KWH, DEFAULT_BATTERY_WEAR_EUR_KWH)),  # noqa: F405
            export_eur_kwh=float(data.get(CONF_EXPORT_EUR_KWH, DEFAULT_EXPORT_EUR_KWH)),  # noqa: F405
            grid_charge_margin_eur_kwh=float(data.get(CONF_GRID_CHARGE_MARGIN_EUR_KWH, DEFAULT_GRID_CHARGE_MARGIN_EUR_KWH)),  # noqa: F405
            allow_grid_charging=bool(data.get(CONF_ALLOW_GRID_CHARGING, True)),  # noqa: F405
            quiet_hours_start=int(data.get(CONF_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_START)),  # noqa: F405
            quiet_hours_end=int(data.get(CONF_QUIET_HOURS_END, DEFAULT_QUIET_HOURS_END)),  # noqa: F405
            quiet_grid_charge_kw=float(data.get(CONF_QUIET_GRID_CHARGE_KW, DEFAULT_QUIET_GRID_CHARGE_KW)),  # noqa: F405
            day_grid_charge_kw=float(data.get(CONF_DAY_GRID_CHARGE_KW, DEFAULT_DAY_GRID_CHARGE_KW)),  # noqa: F405
            quiet_charge_current_a=int(data.get(CONF_QUIET_CHARGE_CURRENT_A, DEFAULT_QUIET_CHARGE_CURRENT_A)),  # noqa: F405
            normal_charge_current_a=int(data.get(CONF_NORMAL_CHARGE_CURRENT_A, DEFAULT_NORMAL_CHARGE_CURRENT_A)),  # noqa: F405
            control_enable_entity=_text(data, CONF_CONTROL_ENABLE_ENTITY),  # noqa: F405
            weather_temperature_entity=_text(data, CONF_WEATHER_TEMPERATURE_ENTITY),  # noqa: F405
            weather_cloud_entity=_text(data, CONF_WEATHER_CLOUD_ENTITY),  # noqa: F405
            weather_illuminance_entity=_text(data, CONF_WEATHER_ILLUMINANCE_ENTITY),  # noqa: F405
            weather_rain_binary_entity=_text(data, CONF_WEATHER_RAIN_BINARY_ENTITY),  # noqa: F405
            weather_forecast_entity=_text(data, CONF_WEATHER_FORECAST_ENTITY),  # noqa: F405
            victron_modbus_host=_text(data, CONF_VICTRON_MODBUS_HOST),  # noqa: F405
            victron_modbus_port=int(data.get(CONF_VICTRON_MODBUS_PORT, DEFAULT_VICTRON_MODBUS_PORT)),  # noqa: F405
            victron_settings_unit_id=int(data.get(CONF_VICTRON_SETTINGS_UNIT_ID, DEFAULT_VICTRON_SETTINGS_UNIT_ID)),  # noqa: F405
            victron_max_charge_current_register=int(data.get(CONF_VICTRON_MAX_CHARGE_CURRENT_REGISTER, DEFAULT_VICTRON_MAX_CHARGE_CURRENT_REGISTER)),  # noqa: F405
            victron_grid_setpoint_register=int(data.get(CONF_VICTRON_GRID_SETPOINT_REGISTER, DEFAULT_VICTRON_GRID_SETPOINT_REGISTER)),  # noqa: F405
            victron_grid_setpoint_max_w=int(data.get(CONF_VICTRON_GRID_SETPOINT_MAX_W, DEFAULT_VICTRON_GRID_SETPOINT_MAX_W)),  # noqa: F405
        )

    @classmethod
    def from_entry(cls, entry: Any) -> "OptimizerConfig":
        """Build settings from config entry data plus user options."""
        return cls.from_mapping({**entry.data, **entry.options})
