"""Sensor entities for the Home Assistant Energy Optimizer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnergyOptimizerCoordinator


@dataclass(frozen=True, kw_only=True)
class OptimizerSensorDescription(SensorEntityDescription):
    """Describe an optimizer sensor."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSORS = (
    OptimizerSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "discharge",
            "reserve",
            "pv_surplus",
            "pv_store",
            "grid_charge",
            "degraded",
        ],
        icon="mdi:home-lightning-bolt-outline",
        value_fn=lambda data: data["recommendation"].lower(),
    ),
    OptimizerSensorDescription(
        key="target_min_soc",
        translation_key="target_min_soc",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-lock",
        value_fn=lambda data: data["target_min_soc"],
    ),
    OptimizerSensorDescription(
        key="control_min_soc",
        translation_key="control_min_soc",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-lock-check",
        value_fn=lambda data: data["control_command"]["minimum_soc"],
    ),
    OptimizerSensorDescription(
        key="confidence",
        translation_key="confidence",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:check-decagram-outline",
        value_fn=lambda data: data["confidence"],
    ),
    OptimizerSensorDescription(
        key="forecast_load_48h",
        translation_key="forecast_load_48h",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda data: data["forecast_load_48h"],
    ),
    OptimizerSensorDescription(
        key="forecast_load_robust_48h",
        translation_key="forecast_load_robust_48h",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:shield-home-outline",
        value_fn=lambda data: data["forecast_load_robust_48h"],
    ),
    OptimizerSensorDescription(
        key="forecast_load_baseline_48h",
        translation_key="forecast_load_baseline_48h",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:chart-bell-curve-cumulative",
        value_fn=lambda data: data["forecast_load_baseline_48h"],
    ),
    OptimizerSensorDescription(
        key="weather_rain_indicator",
        translation_key="weather_rain_indicator",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-rainy",
        value_fn=lambda data: data.get("rain_indicator_percent", 0),
    ),
    OptimizerSensorDescription(
        key="forecast_pv_48h",
        translation_key="forecast_pv_48h",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:solar-power-variant",
        value_fn=lambda data: data["forecast_pv_48h"],
    ),
    OptimizerSensorDescription(
        key="expected_grid_import",
        translation_key="expected_grid_import",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:transmission-tower-import",
        value_fn=lambda data: data["expected_grid_import"],
    ),
    OptimizerSensorDescription(
        key="expected_grid_charge",
        translation_key="expected_grid_charge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-charging-medium",
        value_fn=lambda data: data["expected_grid_charge"],
    ),
    OptimizerSensorDescription(
        key="target_charge_current",
        translation_key="target_charge_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:current-ac",
        value_fn=lambda data: data["target_charge_current"],
    ),
    OptimizerSensorDescription(
        key="target_grid_setpoint",
        translation_key="target_grid_setpoint",
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:transmission-tower-import",
        value_fn=lambda data: data["control_command"]["grid_setpoint_w"],
    ),
    OptimizerSensorDescription(
        key="projected_min_soc",
        translation_key="projected_min_soc",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-arrow-down-outline",
        value_fn=lambda data: data["projected_min_soc"],
    ),
)


_EV_PLAN_STARTUP_DEFAULTS: dict[str, Any] = {
    "status": "disabled",
    "reason": "ev_plan_not_available",
    "next_departure": None,
    "required_wallbox_kwh": None,
    "planned_wallbox_kwh": 0.0,
    "unmet_wallbox_kwh": None,
    "target_soc_percent": None,
    "suggested_power_w": 0,
    "suggested_mode": "idle",
    "suggested_duration_minutes": None,
    "suggested_valid_until": None,
    "next_charge_start": None,
    "next_charge_end": None,
    "next_charge_power_w": 0,
    "schedule": [],
    "schedule_truncated": False,
    "provisional": True,
    "complete": False,
    "data_quality_percent": 0,
    "connected": None,
    "observation_mode": True,
    "applied_to_site_optimizer": False,
    "accounting_valid": False,
    "history_accounting_valid": False,
}


def _ev_plan(data: dict[str, Any]) -> dict[str, Any]:
    """Return the EV payload or a safe startup/migration fallback."""
    plan = data.get("ev_plan")
    return plan if isinstance(plan, dict) else _EV_PLAN_STARTUP_DEFAULTS


def _ev_value(data: dict[str, Any], key: str) -> Any:
    """Return one value from the optional EV plan."""
    return _ev_plan(data).get(key)


def _ev_departure(data: dict[str, Any]) -> datetime | None:
    """Return the next EV departure as a timestamp sensor value."""
    value = _ev_value(data, "next_departure")
    if not value:
        return None
    try:
        departure = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    # Home Assistant timestamp sensors require an unambiguous aware datetime.
    return departure if departure.tzinfo is not None else None


def _ev_feasibility(data: dict[str, Any]) -> str:
    """Expose feasibility without treating missing input as a failed plan."""
    plan = _ev_plan(data)
    if plan.get("status") == "infeasible":
        return "infeasible"
    if plan.get("status") == "ready" and plan.get("complete"):
        return "feasible"
    if (
        plan.get("status") == "scheduled"
        and plan.get("complete")
        and plan.get("connected") is True
        and not plan.get("provisional", True)
    ):
        return "feasible"
    return "unknown"


EV_SENSORS = (
    OptimizerSensorDescription(
        key="ev_status",
        translation_key="ev_status",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "disabled",
            "awaiting_trip",
            "awaiting_horizon",
            "awaiting_data",
            "ready",
            "scheduled",
            "infeasible",
        ],
        icon="mdi:car-electric",
        value_fn=lambda data: _ev_value(data, "status"),
    ),
    OptimizerSensorDescription(
        key="ev_next_departure",
        translation_key="ev_next_departure",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-clock",
        value_fn=_ev_departure,
    ),
    OptimizerSensorDescription(
        key="ev_required_wallbox_energy",
        translation_key="ev_required_wallbox_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-clock-outline",
        value_fn=lambda data: _ev_value(data, "required_wallbox_kwh"),
    ),
    OptimizerSensorDescription(
        key="ev_target_soc",
        translation_key="ev_target_soc",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-charging-high",
        value_fn=lambda data: _ev_value(data, "target_soc_percent"),
    ),
    OptimizerSensorDescription(
        key="ev_suggested_power",
        translation_key="ev_suggested_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:ev-station",
        value_fn=lambda data: _ev_value(data, "suggested_power_w"),
    ),
    OptimizerSensorDescription(
        key="ev_suggested_mode",
        translation_key="ev_suggested_mode",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "normal", "boost", "degraded"],
        icon="mdi:ev-plug-type2",
        value_fn=lambda data: _ev_value(data, "suggested_mode"),
    ),
    OptimizerSensorDescription(
        key="ev_plan_feasible",
        translation_key="ev_plan_feasible",
        device_class=SensorDeviceClass.ENUM,
        options=["unknown", "feasible", "infeasible"],
        icon="mdi:calendar-check-outline",
        value_fn=_ev_feasibility,
    ),
    OptimizerSensorDescription(
        key="ev_data_quality",
        translation_key="ev_data_quality",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:check-decagram-outline",
        value_fn=lambda data: _ev_value(data, "data_quality_percent"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up optimizer sensors."""
    coordinator: EnergyOptimizerCoordinator = entry.runtime_data
    descriptions = SENSORS
    if coordinator.config.ev.enabled:
        descriptions += EV_SENSORS
    async_add_entities(
        OptimizerSensor(coordinator, entry, description) for description in descriptions
    )


class OptimizerSensor(CoordinatorEntity[EnergyOptimizerCoordinator], SensorEntity):
    """One value produced by the optimizer."""

    entity_description: OptimizerSensorDescription

    def __init__(
        self,
        coordinator: EnergyOptimizerCoordinator,
        entry: ConfigEntry,
        description: OptimizerSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Home Assistant Energy Optimizer",
            manufacturer="Community",
            model="Deterministic 48-hour optimizer",
        )

    @property
    def native_value(self):
        """Return the current calculated value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose compact diagnostics on the main status sensor only."""
        if self.entity_description.key == "ev_status":
            plan = _ev_plan(self.coordinator.data)
            return {
                "reason": plan.get("reason"),
                "planned_wallbox_kwh": plan.get("planned_wallbox_kwh"),
                "unmet_wallbox_kwh": plan.get("unmet_wallbox_kwh"),
                "connected": plan.get("connected"),
                "observation_mode": plan.get("observation_mode", True),
                "ev_plan_applied_to_site_optimizer": plan.get(
                    "applied_to_site_optimizer", False
                ),
                "accounting_valid": plan.get("accounting_valid", False),
                "history_accounting_valid": plan.get(
                    "history_accounting_valid", False
                ),
                "current_recommendation_blocked": plan.get(
                    "current_command_blocked_reason"
                ),
                "vehicle_soc_age_minutes": plan.get("vehicle_soc_age_minutes"),
                "live_power_age_seconds": plan.get("live_power_age_seconds"),
                "next_charge_start": plan.get("next_charge_start"),
                "next_charge_end": plan.get("next_charge_end"),
                "next_charge_power_w": plan.get("next_charge_power_w", 0),
                "schedule": plan.get("schedule", []),
                "schedule_truncated": plan.get("schedule_truncated", False),
                "provisional": plan.get("provisional", True),
            }
        if self.entity_description.key == "ev_suggested_power":
            plan = _ev_plan(self.coordinator.data)
            return {
                "duration_minutes": plan.get("suggested_duration_minutes"),
                "valid_until": plan.get("suggested_valid_until"),
                "observation_only": True,
            }
        if self.entity_description.key != "status":
            return None
        data = self.coordinator.data
        command = data["control_command"]
        control_enable_entity = self.coordinator.config.control_enable_entity
        control_enabled = bool(control_enable_entity) and self.hass.states.is_state(
            control_enable_entity, "on"
        )
        return {
            # English canonical keys for new dashboards and automations.
            "reason": data["reason"],
            "calculated_at": data["calculated_at"],
            "command_action": command["action"],
            "command_valid_until": command["valid_until"],
            "command_quality_ok": command["quality_ok"],
            "command_minimum_soc": command["minimum_soc"],
            "command_charge_current_a": command["charge_current_a"],
            "command_grid_setpoint_w": command["grid_setpoint_w"],
            "control_enabled": control_enabled,
            "simulation_mode": not control_enabled,
            # German legacy keys remain for existing private dashboards.
            "grund": data["reason"],
            "berechnet_um": data["calculated_at"],
            "steuerbefehl_revision": command["revision"],
            "steuerbefehl_gueltig_bis": command["valid_until"],
            "steuerbefehl_qualitaet_ok": command["quality_ok"],
            "steuerbefehl_mindest_soc": command["minimum_soc"],
            "steuerbefehl_ladestrom_a": command["charge_current_a"],
            "steuerbefehl_netz_sollwert_w": command["grid_setpoint_w"],
            "steuerbefehl_aktion": command["action"],
            "steuerbefehl_grund": command["reason"],
            "naechste_entladung": data["next_discharge"],
            "naechste_netzladung": data.get("next_grid_charge"),
            "grid_charge_block_state": data.get(
                "grid_charge_session_state", "IDLE"
            ),
            "grid_charge_block_start": data.get("grid_charge_session_start"),
            "grid_charge_block_end": data.get("grid_charge_session_end"),
            "grid_charge_block_counter_baseline_kwh": data.get(
                "grid_charge_session_baseline_kwh"
            ),
            "grid_charge_block_target_kwh": data.get(
                "grid_charge_session_target_kwh"
            ),
            "grid_charge_block_delivered_kwh": data.get(
                "grid_charge_session_delivered_kwh"
            ),
            "grid_charge_block_stop_reason": data.get(
                "grid_charge_session_stop_reason", ""
            ),
            "battery_charge_counter_kwh": data.get("battery_charge_counter_kwh"),
            "bekannte_preisintervalle": data["known_price_slots"],
            "geschaetzte_preisintervalle": data["estimated_price_slots"],
            "historienstunden": data["history_hours"],
            "lastmodell_aktiv": data["load_model_active"],
            "lastmodell": "Robustes Kalender- und Verlaufsmodell",
            "medianprognose_48h_kwh": data["forecast_load_baseline_48h"],
            "robuste_grundlastprognose_48h_kwh": data.get(
                "forecast_load_robust_48h"
            ),
            "aktive_lastprognose": "robustes Kalender- und Verlaufsmodell",
            "grundlast_median_letzte_14_tage_kwh_pro_tag": data.get(
                "recent_base_daily_kwh"
            ),
            "grundlast_tage_verwendet": data.get("recent_base_days", 0),
            "grundlast_obergrenze_48h_kwh": data.get(
                "forecast_base_ceiling_48h"
            ),
            "robuste_niveaukorrektur": data.get("robust_level_factor", 1.0),
            "wetterprognosestunden": data.get("weather_forecast_hours", 0),
            "wettermerkmale_aktiv": data.get("weather_inputs_active", False),
            "pv_prognose_ersatzwert_aktiv": data.get(
                "pv_forecast_fallback", False
            ),
            "erwartete_kosten_eur": data["expected_cost"],
            "netzbedarf_erste_24h_kwh": data.get(
                "expected_grid_import_first_24h", 0
            ),
            "netzbedarf_zweite_24h_kwh": data.get(
                "expected_grid_import_second_24h", 0
            ),
            "ungenutzter_pv_ueberschuss_kwh": data["expected_export"],
            "benoetigter_pv_speicherplatz_prozent": data.get(
                "pv_headroom_required_percent", 0
            ),
            "erwartete_batterieentladung_kwh": data["expected_battery_discharge"],
            "erwartete_netzladung_kwh": data.get("expected_grid_charge", 0),
            "ziel_ladestrom_a": data.get("target_charge_current", 50),
            "ziel_netz_sollwert_w": data.get("target_grid_setpoint_w", 0),
            "geplanter_netzanteil_haus_aktueller_slot_w": data.get(
                "planned_grid_to_house_first_slot_w", 0
            ),
            "geplante_netzladung_aktueller_slot_w": data.get(
                "planned_grid_charge_first_slot_w", 0
            ),
            "aktueller_preis_sicher_bekannt": data.get(
                "current_price_is_known", False
            ),
            "leise_netzladung_aktiv": data.get("quiet_grid_charge_active", False),
            "winter_netzladen_freigegeben": data.get(
                "winter_grid_charge_enabled", False
            ),
            "dynamisches_netzladen_freigegeben": data.get(
                "dynamic_grid_charge_enabled", False
            ),
            "geraete_startplanung": data["scheduled_jobs"],
            "geraete_laufprognose": data.get("running_jobs", {}),
            "steuerung_aktiv": control_enabled,
            "simulationsmodus": not control_enabled,
            "live_korrektur_aktiv": data.get("live_correction_active", False),
            "live_pv_w": data.get("live_pv_w"),
            "live_hausverbrauch_w": data.get("live_load_w"),
            "live_netzbezug_w": data.get("live_grid_w"),
            "pv_ladepuffer_aktiv": data.get("pv_headroom_active", False),
            "preisersatz_7_tage_mittel_ct_kwh": (
                round(float(data["recent_price_average"]) * 100, 2)
                if data.get("recent_price_average") is not None
                else None
            ),
            "preisersatz_werktag_ct_kwh": (
                round(float(data["recent_weekday_price_average"]) * 100, 2)
                if data.get("recent_weekday_price_average") is not None
                else None
            ),
            "preisersatz_wochenende_ct_kwh": (
                round(float(data["recent_weekend_price_average"]) * 100, 2)
                if data.get("recent_weekend_price_average") is not None
                else None
            ),
        }
