"""Validated runtime configuration for the optimizer."""

# ruff: noqa: F405 - configuration keys/defaults intentionally come from const.

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from .const import *  # noqa: F403 - config keys/defaults are intentionally centralized


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key, "")
    return str(value).strip() if value is not None else ""


def ev_submeter_conflicts_site(data: Mapping[str, Any]) -> bool:
    """Reject selecting a site/PV/grid counter again as the EV submeter."""
    load_entities = data.get(CONF_LIVE_LOAD_POWER_ENTITIES, ())  # noqa: F405
    if isinstance(load_entities, str):
        load_entities = (load_entities,)
    site_power_entities = {
        _text(data, CONF_LIVE_PV_POWER_ENTITY),  # noqa: F405
        _text(data, CONF_LIVE_GRID_POWER_ENTITY),  # noqa: F405
        *(str(entity).strip() for entity in load_entities),
    }
    site_energy_entities = {
        _text(data, CONF_GRID_IMPORT_ENERGY_ENTITY),  # noqa: F405
        _text(data, CONF_GRID_EXPORT_ENERGY_ENTITY),  # noqa: F405
        _text(data, CONF_PV_ENERGY_ENTITY),  # noqa: F405
        _text(data, CONF_BATTERY_CHARGE_ENERGY_ENTITY),  # noqa: F405
        _text(data, CONF_BATTERY_DISCHARGE_ENERGY_ENTITY),  # noqa: F405
    }
    ev_power = _text(data, CONF_EV_LIVE_POWER_ENTITY)  # noqa: F405
    ev_energy = _text(data, CONF_EV_ENERGY_ENTITY)  # noqa: F405
    return bool(
        (ev_power and ev_power in site_power_entities)
        or (ev_energy and ev_energy in site_energy_entities)
    )


def ev_input_entities_conflict(data: Mapping[str, Any]) -> bool:
    """Require distinct power, energy and vehicle-SoC sensor entities."""
    entities = [
        _text(data, CONF_EV_LIVE_POWER_ENTITY),  # noqa: F405
        _text(data, CONF_EV_ENERGY_ENTITY),  # noqa: F405
        _text(data, CONF_EV_VEHICLE_SOC_ENTITY),  # noqa: F405
    ]
    selected = [entity for entity in entities if entity]
    return len(selected) != len(set(selected))


@dataclass(frozen=True)
class EVConfig:
    """Optional vendor-neutral electric-vehicle planning settings.

    EV planning is fail-closed: a stored enable flag only becomes effective
    after the installer has explicitly confirmed that the site/main meter also
    measures the wallbox. This beta is always observation-only.
    """

    enabled: bool = DEFAULT_EV_ENABLED  # noqa: F405
    site_meter_includes_ev: bool = DEFAULT_SITE_METER_INCLUDES_EV  # noqa: F405
    live_power_entity: str = ""
    energy_entity: str = ""
    connected_entity: str = ""
    vehicle_soc_entity: str = ""
    calendar_entity: str = ""
    normal_charge_power_kw: float = DEFAULT_EV_NORMAL_CHARGE_POWER_KW  # noqa: F405
    boost_charge_power_kw: float = DEFAULT_EV_BOOST_CHARGE_POWER_KW  # noqa: F405
    battery_capacity_kwh: float = DEFAULT_EV_BATTERY_CAPACITY_KWH  # noqa: F405
    consumption_kwh_per_100km: float = DEFAULT_EV_CONSUMPTION_KWH_PER_100KM  # noqa: F405
    reserve_km: float = DEFAULT_EV_RESERVE_KM  # noqa: F405
    charge_efficiency: float = DEFAULT_EV_CHARGE_EFFICIENCY  # noqa: F405
    site_max_import_power_kw: float = DEFAULT_SITE_MAX_IMPORT_POWER_KW  # noqa: F405

    def __post_init__(self) -> None:
        if self.enabled and not self.site_meter_includes_ev:
            raise ValueError("EV planning requires a site meter that includes the EV")
        required_entities = (
            self.live_power_entity,
            self.energy_entity,
            self.connected_entity,
            self.vehicle_soc_entity,
            self.calendar_entity,
        )
        if self.enabled and not all(required_entities):
            raise ValueError("Enabled EV planning requires all EV input entities")
        numeric_limits = (
            ("normal charging power", self.normal_charge_power_kw, 0, 11),
            ("boost charging power", self.boost_charge_power_kw, 0, 11),
            ("battery capacity", self.battery_capacity_kwh, 0, None),
            ("vehicle consumption", self.consumption_kwh_per_100km, 0, None),
            ("charging efficiency", self.charge_efficiency, 0, 1),
            (
                "site import power",
                self.site_max_import_power_kw,
                0,
                MAX_SITE_IMPORT_POWER_KW,  # noqa: F405
            ),
        )
        for name, value, minimum, maximum in numeric_limits:
            if (
                not math.isfinite(value)
                or value <= minimum
                or (maximum is not None and value > maximum)
            ):
                raise ValueError(f"Invalid EV {name}: {value}")
        if not math.isfinite(self.reserve_km) or self.reserve_km < 0:
            raise ValueError(f"Invalid EV reserve distance: {self.reserve_km}")
        if self.boost_charge_power_kw < self.normal_charge_power_kw:
            raise ValueError("EV boost charging power must not be below normal power")
        if self.site_max_import_power_kw < self.normal_charge_power_kw:
            raise ValueError("Site import power must support normal EV charging")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EVConfig":
        """Build safe EV settings from flat config-entry keys."""
        site_meter_includes_ev = bool(
            data.get(
                CONF_SITE_METER_INCLUDES_EV,  # noqa: F405
                DEFAULT_SITE_METER_INCLUDES_EV,  # noqa: F405
            )
        )
        required_entities = (
            _text(data, CONF_EV_LIVE_POWER_ENTITY),  # noqa: F405
            _text(data, CONF_EV_ENERGY_ENTITY),  # noqa: F405
            _text(data, CONF_EV_CONNECTED_ENTITY),  # noqa: F405
            _text(data, CONF_EV_VEHICLE_SOC_ENTITY),  # noqa: F405
            _text(data, CONF_EV_CALENDAR_ENTITY),  # noqa: F405
        )
        normal_power = min(
            MAX_EV_AUTOMATIC_CHARGE_POWER_KW,  # noqa: F405
            float(
                data.get(
                    CONF_EV_NORMAL_CHARGE_POWER_KW,  # noqa: F405
                    DEFAULT_EV_NORMAL_CHARGE_POWER_KW,  # noqa: F405
                )
            ),
        )
        boost_power = min(
            MAX_EV_AUTOMATIC_CHARGE_POWER_KW,  # noqa: F405
            max(
                normal_power,
                float(
                    data.get(
                        CONF_EV_BOOST_CHARGE_POWER_KW,  # noqa: F405
                        DEFAULT_EV_BOOST_CHARGE_POWER_KW,  # noqa: F405
                    )
                ),
            ),
        )
        return cls(
            enabled=(
                bool(data.get(CONF_EV_ENABLED, DEFAULT_EV_ENABLED))  # noqa: F405
                and site_meter_includes_ev
                and all(required_entities)
                and not ev_submeter_conflicts_site(data)
                and not ev_input_entities_conflict(data)
            ),
            site_meter_includes_ev=site_meter_includes_ev,
            live_power_entity=required_entities[0],
            energy_entity=required_entities[1],
            connected_entity=required_entities[2],
            vehicle_soc_entity=required_entities[3],
            calendar_entity=required_entities[4],
            normal_charge_power_kw=normal_power,
            boost_charge_power_kw=boost_power,
            battery_capacity_kwh=float(
                data.get(
                    CONF_EV_BATTERY_CAPACITY_KWH,  # noqa: F405
                    DEFAULT_EV_BATTERY_CAPACITY_KWH,  # noqa: F405
                )
            ),
            consumption_kwh_per_100km=float(
                data.get(
                    CONF_EV_CONSUMPTION_KWH_PER_100KM,  # noqa: F405
                    DEFAULT_EV_CONSUMPTION_KWH_PER_100KM,  # noqa: F405
                )
            ),
            reserve_km=float(
                data.get(CONF_EV_RESERVE_KM, DEFAULT_EV_RESERVE_KM)  # noqa: F405
            ),
            charge_efficiency=float(
                data.get(
                    CONF_EV_CHARGE_EFFICIENCY,  # noqa: F405
                    DEFAULT_EV_CHARGE_EFFICIENCY,  # noqa: F405
                )
            ),
            site_max_import_power_kw=float(
                data.get(
                    CONF_SITE_MAX_IMPORT_POWER_KW,  # noqa: F405
                    DEFAULT_SITE_MAX_IMPORT_POWER_KW,  # noqa: F405
                )
            ),
        )


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
    quiet_hours_start: float = DEFAULT_QUIET_HOURS_START  # noqa: F405
    quiet_hours_end: float = DEFAULT_QUIET_HOURS_END  # noqa: F405
    quiet_hours_weekend_end: float = DEFAULT_QUIET_HOURS_WEEKEND_END  # noqa: F405
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
    ev: EVConfig = field(default_factory=EVConfig)

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
            battery_capacity_kwh=float(
                data.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH)
            ),  # noqa: F405
            hard_min_soc=float(data.get(CONF_HARD_MIN_SOC, DEFAULT_HARD_MIN_SOC)),  # noqa: F405
            battery_wear_eur_kwh=float(
                data.get(CONF_BATTERY_WEAR_EUR_KWH, DEFAULT_BATTERY_WEAR_EUR_KWH)
            ),  # noqa: F405
            export_eur_kwh=float(data.get(CONF_EXPORT_EUR_KWH, DEFAULT_EXPORT_EUR_KWH)),  # noqa: F405
            grid_charge_margin_eur_kwh=float(
                data.get(CONF_GRID_CHARGE_MARGIN_EUR_KWH, DEFAULT_GRID_CHARGE_MARGIN_EUR_KWH)
            ),  # noqa: F405
            allow_grid_charging=bool(data.get(CONF_ALLOW_GRID_CHARGING, True)),  # noqa: F405
            quiet_hours_start=float(data.get(CONF_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_START)),  # noqa: F405
            quiet_hours_end=float(data.get(CONF_QUIET_HOURS_END, DEFAULT_QUIET_HOURS_END)),  # noqa: F405
            quiet_hours_weekend_end=float(
                data.get(
                    CONF_QUIET_HOURS_WEEKEND_END,  # noqa: F405
                    DEFAULT_QUIET_HOURS_WEEKEND_END,  # noqa: F405
                )
            ),
            quiet_grid_charge_kw=float(
                data.get(CONF_QUIET_GRID_CHARGE_KW, DEFAULT_QUIET_GRID_CHARGE_KW)
            ),  # noqa: F405
            day_grid_charge_kw=float(data.get(CONF_DAY_GRID_CHARGE_KW, DEFAULT_DAY_GRID_CHARGE_KW)),  # noqa: F405
            quiet_charge_current_a=int(
                data.get(CONF_QUIET_CHARGE_CURRENT_A, DEFAULT_QUIET_CHARGE_CURRENT_A)
            ),  # noqa: F405
            normal_charge_current_a=int(
                data.get(CONF_NORMAL_CHARGE_CURRENT_A, DEFAULT_NORMAL_CHARGE_CURRENT_A)
            ),  # noqa: F405
            control_enable_entity=_text(data, CONF_CONTROL_ENABLE_ENTITY),  # noqa: F405
            weather_temperature_entity=_text(data, CONF_WEATHER_TEMPERATURE_ENTITY),  # noqa: F405
            weather_cloud_entity=_text(data, CONF_WEATHER_CLOUD_ENTITY),  # noqa: F405
            weather_illuminance_entity=_text(data, CONF_WEATHER_ILLUMINANCE_ENTITY),  # noqa: F405
            weather_rain_binary_entity=_text(data, CONF_WEATHER_RAIN_BINARY_ENTITY),  # noqa: F405
            weather_forecast_entity=_text(data, CONF_WEATHER_FORECAST_ENTITY),  # noqa: F405
            victron_modbus_host=_text(data, CONF_VICTRON_MODBUS_HOST),  # noqa: F405
            victron_modbus_port=int(
                data.get(CONF_VICTRON_MODBUS_PORT, DEFAULT_VICTRON_MODBUS_PORT)
            ),  # noqa: F405
            victron_settings_unit_id=int(
                data.get(CONF_VICTRON_SETTINGS_UNIT_ID, DEFAULT_VICTRON_SETTINGS_UNIT_ID)
            ),  # noqa: F405
            victron_max_charge_current_register=int(
                data.get(
                    CONF_VICTRON_MAX_CHARGE_CURRENT_REGISTER,
                    DEFAULT_VICTRON_MAX_CHARGE_CURRENT_REGISTER,
                )
            ),  # noqa: F405
            victron_grid_setpoint_register=int(
                data.get(
                    CONF_VICTRON_GRID_SETPOINT_REGISTER, DEFAULT_VICTRON_GRID_SETPOINT_REGISTER
                )
            ),  # noqa: F405
            victron_grid_setpoint_max_w=int(
                data.get(CONF_VICTRON_GRID_SETPOINT_MAX_W, DEFAULT_VICTRON_GRID_SETPOINT_MAX_W)
            ),  # noqa: F405
            ev=EVConfig.from_mapping(data),
        )

    @classmethod
    def from_entry(cls, entry: Any) -> "OptimizerConfig":
        """Build settings from config entry data plus user options."""
        return cls.from_mapping({**entry.data, **entry.options})
