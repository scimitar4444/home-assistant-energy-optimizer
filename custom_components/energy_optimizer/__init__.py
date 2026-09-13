"""Home Assistant Energy Optimizer integration."""

from __future__ import annotations

from functools import partial
import socket
import struct

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .config import OptimizerConfig
from .const import DOMAIN
from .coordinator import EnergyOptimizerCoordinator

PLATFORMS = [Platform.SENSOR]
SERVICE_SET_CHARGE_CURRENT = "set_victron_charge_current"
SERVICE_SET_GRID_SETPOINT = "set_victron_grid_setpoint"
ATTR_CURRENT = "current"
ATTR_POWER = "power"


def _require_modbus(config: OptimizerConfig) -> None:
    if not config.victron_modbus_host:
        raise HomeAssistantError("Victron Modbus host is not configured")


def _receive_exact(connection: socket.socket, length: int) -> bytes:
    response = b""
    while len(response) < length:
        chunk = connection.recv(length - len(response))
        if not chunk:
            raise HomeAssistantError("Victron closed the Modbus connection")
        response += chunk
    return response


def _modbus_exchange(
    connection: socket.socket,
    transaction_id: int,
    pdu: bytes,
    *,
    unit_id: int,
) -> bytes:
    request = (
        struct.pack(">HHHB", transaction_id, 0, len(pdu) + 1, unit_id) + pdu
    )
    connection.sendall(request)
    header = _receive_exact(connection, 7)
    response_transaction, protocol, response_length, response_unit = struct.unpack(
        ">HHHB", header
    )
    if (
        response_transaction != transaction_id
        or protocol != 0
        or response_unit != unit_id
        or response_length < 2
        or response_length > 254
    ):
        raise HomeAssistantError("Invalid Victron Modbus TCP response header")
    response_pdu = _receive_exact(connection, response_length - 1)
    if response_pdu[0] & 0x80:
        code = response_pdu[1] if len(response_pdu) > 1 else -1
        raise HomeAssistantError(f"Victron Modbus exception {code}")
    return response_pdu


def _write_victron_charge_current(
    current: int,
    config: OptimizerConfig,
) -> None:
    """Write and verify the Victron maximum charge-current register."""
    _require_modbus(config)
    register = config.victron_max_charge_current_register
    payload = struct.pack(">BHH", 6, register, current)
    request = (
        struct.pack(
            ">HHHB",
            1,
            0,
            len(payload) + 1,
            config.victron_settings_unit_id,
        )
        + payload
    )
    try:
        with socket.create_connection(
            (config.victron_modbus_host, config.victron_modbus_port), timeout=5
        ) as connection:
            connection.sendall(request)
            response = _receive_exact(connection, 12)
    except OSError as error:
        raise HomeAssistantError(
            f"Victron charge current could not be set: {error}"
        ) from error
    if (
        response[6] != config.victron_settings_unit_id
        or response[7] != 6
    ):
        raise HomeAssistantError("Invalid response from Victron Modbus")
    response_register, written_current = struct.unpack(">HH", response[-4:])
    if response_register != register or written_current != current:
        raise HomeAssistantError("Victron did not confirm the charge current")


def _write_victron_grid_setpoint_once(
    power: int,
    config: OptimizerConfig,
) -> None:
    _require_modbus(config)
    register = config.victron_grid_setpoint_register
    write_pdu = struct.pack(">BHHB", 16, register, 2, 4) + struct.pack(">i", power)
    with socket.create_connection(
        (config.victron_modbus_host, config.victron_modbus_port), timeout=5
    ) as connection:
        connection.settimeout(5)
        write_response = _modbus_exchange(
            connection,
            1,
            write_pdu,
            unit_id=config.victron_settings_unit_id,
        )
        if write_response != struct.pack(">BHH", 16, register, 2):
            raise HomeAssistantError("Victron did not confirm the grid setpoint write")

        read_response = _modbus_exchange(
            connection,
            2,
            struct.pack(">BHH", 3, register, 2),
            unit_id=config.victron_settings_unit_id,
        )
        if len(read_response) != 6 or read_response[:2] != b"\x03\x04":
            raise HomeAssistantError("Invalid Victron grid setpoint readback")
        observed_power = struct.unpack(">i", read_response[2:])[0]
        if observed_power != power:
            raise HomeAssistantError(
                f"Victron returned {observed_power} W instead of {power} W"
            )


def _write_victron_grid_setpoint(power: int, config: OptimizerConfig) -> None:
    """Write and verify the volatile grid setpoint with one bounded retry."""
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            _write_victron_grid_setpoint_once(power, config)
            return
        except (HomeAssistantError, OSError) as error:
            last_error = error
    raise HomeAssistantError(
        f"Victron grid setpoint failed after two attempts: {last_error}"
    ) from last_error


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one configured optimizer instance."""
    config = OptimizerConfig.from_entry(entry)
    coordinator = EnergyOptimizerCoordinator(hass, config)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    async def _async_set_charge_current(call: ServiceCall) -> None:
        current = int(call.data[ATTR_CURRENT])
        await hass.async_add_executor_job(
            partial(_write_victron_charge_current, current, config)
        )

    async def _async_set_grid_setpoint(call: ServiceCall) -> None:
        power = int(call.data[ATTR_POWER])
        await hass.async_add_executor_job(
            partial(_write_victron_grid_setpoint, power, config)
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_CHARGE_CURRENT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_CHARGE_CURRENT,
            _async_set_charge_current,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_CURRENT): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=0, max=config.normal_charge_current_a),
                    )
                }
            ),
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_GRID_SETPOINT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_GRID_SETPOINT,
            _async_set_grid_setpoint,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_POWER): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=0, max=config.victron_grid_setpoint_max_w),
                    )
                }
            ),
        )

    @callback
    def _request_refresh(_event: Event) -> None:
        entry.async_create_task(hass, coordinator.async_request_refresh())

    watched_entities = {
        config.soc_entity,
        config.price_history_entity,
        config.price_timeline_entity,
        config.pv_today_remaining_entity,
        config.pv_tomorrow_entity,
        *config.live_load_power_entities,
    }
    watched_entities.update(
        entity_id
        for settings in config.appliances.values()
        for entity_id in (
            settings.get("pause_entity"),
            settings.get("override_entity"),
            settings.get("status_entity"),
            *settings.get("additional_override_entities", ()),
        )
        if entity_id
    )
    if config.control_enable_entity:
        watched_entities.add(config.control_enable_entity)
    entry.async_on_unload(
        async_track_state_change_event(
            hass,
            sorted(entity for entity in watched_entities if entity),
            _request_refresh,
        )
    )

    @callback
    def _refresh_at_quarter_hour(now) -> None:
        calculated_at = dt_util.parse_datetime(
            str((coordinator.data or {}).get("calculated_at", ""))
        )
        if calculated_at is not None:
            calculated_at = dt_util.as_local(calculated_at)
            local_now = dt_util.as_local(now)
            if (
                calculated_at.date() == local_now.date()
                and calculated_at.hour == local_now.hour
                and calculated_at.minute // 15 == local_now.minute // 15
            ):
                return
        entry.async_create_task(hass, coordinator.async_request_refresh())

    entry.async_on_unload(
        async_track_time_change(
            hass,
            _refresh_at_quarter_hour,
            minute=[0, 15, 30, 45],
            second=[2, 45],
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the optimizer."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and hass.services.has_service(DOMAIN, SERVICE_SET_CHARGE_CURRENT):
        hass.services.async_remove(DOMAIN, SERVICE_SET_CHARGE_CURRENT)
    if unloaded and hass.services.has_service(DOMAIN, SERVICE_SET_GRID_SETPOINT):
        hass.services.async_remove(DOMAIN, SERVICE_SET_GRID_SETPOINT)
    return unloaded
