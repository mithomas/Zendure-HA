"""Shared helpers for Zendure integration tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import Mock, patch

from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha import manager as manager_module
from custom_components.zendure_ha.const import (
    CONF_APPTOKEN,
    CONF_MQTTLOCAL,
    CONF_MQTTLOG,
    CONF_P1METER,
    DOMAIN,
    AcMode,
    ManagerMode,
)
from custom_components.zendure_ha.device import ZendureDevice
from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800
from custom_components.zendure_ha.fusegroup import FuseGroup
from custom_components.zendure_ha.manager import ZendureManager
from custom_components.zendure_ha.number import ZendureRestoreNumber
from custom_components.zendure_ha.select import ZendureRestoreSelect
from custom_components.zendure_ha.sensor import ZendureSensor
from custom_components.zendure_ha.switch import ZendureSwitch

PRIMARY_DEVICE_DISABLED = getattr(manager_module, "PRIMARY_DEVICE_DISABLED", "__disabled__")


def config_entry_data() -> dict[str, Any]:
    """Return a minimal valid config-entry payload."""
    return {
        CONF_APPTOKEN: "Zm9vLmJhcg==",
        CONF_P1METER: "sensor.power_actual",
        CONF_MQTTLOG: False,
        CONF_MQTTLOCAL: False,
    }


def make_config_entry(data: dict[str, Any] | None = None) -> MockConfigEntry:
    """Create a mock config entry for the integration."""
    return MockConfigEntry(domain=DOMAIN, data=data or config_entry_data())


def make_p1_event(value: float | str) -> Mock:
    """Create a HomeAssistant-style P1 state change event."""
    state = Mock()
    state.state = str(value)
    event = Mock()
    event.data = {"new_state": state, "old_state": None, "entity_id": "sensor.power_actual"}
    return event


def make_device_definition(
    *,
    device_id: str,
    device_name: str,
    product_model: str,
    product_key: str = "PK",
    serial_number: str = "SN123456",
) -> dict[str, str]:
    """Build a device definition matching the integration constructors."""
    return {
        "deviceKey": device_id,
        "deviceName": device_name,
        "productKey": product_key,
        "productModel": product_model,
        "snNumber": serial_number,
        "ip": "",
    }


def make_device(
    hass: HomeAssistant,
    *,
    device_cls: Callable[..., ZendureDevice] = SolarFlow800,
    device_id: str = "device-1",
    device_name: str = "test device",
    product_model: str = "SolarFlow 800",
    level: int = 50,
    min_soc: int = 10,
    reserve: int = 10,
    soc_set: int = 80,
    kwh: float = 2.0,
    ac_mode: int = AcMode.OUTPUT,
    input_limit: float | None = None,
    output_limit: float | None = None,
    home_input: int = 0,
    home_output: int = 0,
    battery_input: int = 0,
    battery_output: int = 0,
) -> ZendureDevice:
    """Create a real device instance with a stable online baseline."""
    definition = make_device_definition(
        device_id=device_id,
        device_name=device_name,
        product_model=product_model,
        serial_number=f"{device_id[-3:]:0>3}SN123456",
    )
    with patch(
        "custom_components.zendure_ha.device.async_get_clientsession",
        return_value=Mock(),
    ):
        device = device_cls(hass, device_id, device_name, definition)
    device.fuseGroup.update_value(1)
    device.fuseGrp = FuseGroup(
        device.name,
        device.discharge_limit or 800,
        device.charge_limit or -1000,
        [device],
    )
    device.socStatus.update_value(0)
    device.socLimit.update_value(0)
    device.minSoc._attr_native_value = min_soc
    device.socReserve._attr_native_value = reserve
    device.socSet._attr_native_value = soc_set
    device.acMode.update_value(ac_mode)
    device.electricLevel.update_value(level)
    device.homeInput.update_value(home_input)
    device.homeOutput.update_value(home_output)
    device.batteryInput.update_value(battery_input)
    device.batteryOutput.update_value(battery_output)
    if input_limit is not None:
        device.limitInput.update_value(input_limit)
    if output_limit is not None:
        device.limitOutput.update_value(output_limit)
    device.lastseen = datetime.now() + timedelta(minutes=5)
    device.kWh = kwh
    device.totalKwh.update_value(kwh)
    device.setStatus()
    device.refresh_discharge_state()
    return device


def make_manager(
    hass: HomeAssistant,
    *,
    devices: tuple[ZendureDevice, ...] | list[ZendureDevice] | None = None,
    operation: ManagerMode = ManagerMode.OFF,
    manual_power: float = 0,
    discharge_recovery_margin: float = 0,
    primary_device_id: str | None = None,
    charge_time: datetime | None = None,
    charge_devices: tuple[ZendureDevice, ...] | list[ZendureDevice] | None = None,
    discharge_devices: tuple[ZendureDevice, ...] | list[ZendureDevice] | None = None,
    idle_devices: tuple[ZendureDevice, ...] | list[ZendureDevice] | None = None,
) -> ZendureManager:
    """Create a manager instance with the entities needed by the tests."""
    entry = make_config_entry()
    manager = ZendureManager(hass, entry)
    manager.primarydevice = ZendureRestoreSelect(
        manager,
        "primary_device",
        {PRIMARY_DEVICE_DISABLED: "none"},
        manager.update_primary_device,
        PRIMARY_DEVICE_DISABLED,
    )
    manager.operationstate = ZendureSensor(manager, "operation_state")
    manager.manualpower = ZendureRestoreNumber(
        manager,
        "manual_power",
        manager.update_manual_power,
        None,
        "W",
        "power",
        12000,
        -12000,
        NumberMode.BOX,
        True,
    )
    manager.discharge_recovery_margin = ZendureRestoreNumber(
        manager,
        "discharge_recovery_margin",
        manager._refresh_discharge_recovery_margin,
        None,
        "%",
        "soc",
        100,
        0,
        NumberMode.BOX,
        True,
    )
    manager.spike_filter = ZendureSwitch(manager, "spike_filter", manager._update_spike_filter, value=False)
    manager.spike_filter_threshold = ZendureRestoreNumber(
        manager,
        "spike_filter_threshold",
        None,
        None,
        "W",
        "power",
        3000,
        0,
        NumberMode.BOX,
        True,
        initial_value=800,
    )
    manager.spike_filter_duration = ZendureRestoreNumber(
        manager,
        "spike_filter_duration",
        None,
        None,
        "s",
        "duration",
        60,
        0,
        NumberMode.BOX,
        True,
        initial_value=3,
    )
    manager.availableKwh = ZendureSensor(manager, "available_kwh", None, "kWh", "energy", None, 1)
    manager.totalAvailableKwh = ZendureSensor(manager, "total_available_kwh", None, "kWh", "energy", None, 1)
    manager.totalKwh = ZendureSensor(manager, "total_kwh", None, "kWh", "energy", None, 2)
    manager.power = ZendureSensor(manager, "power", None, "W", "power", "measurement", 0)
    manager.operation = operation
    manager.manualpower._attr_native_value = manual_power
    manager.discharge_recovery_margin._attr_native_value = discharge_recovery_margin
    if charge_time is not None:
        manager.charge_time = charge_time
    if devices is not None:
        attach_devices(manager, *devices)
    if primary_device_id is not None:
        manager.primarydevice.update_value(primary_device_id)
    if charge_devices is not None:
        manager.charge = list(charge_devices)
    if discharge_devices is not None:
        manager.discharge = list(discharge_devices)
    if idle_devices is not None:
        manager.idle = list(idle_devices)
    if devices is not None and discharge_recovery_margin:
        manager._refresh_discharge_recovery_margin(None, None)
    return manager


def attach_devices(manager: ZendureManager, *devices: ZendureDevice) -> None:
    """Attach devices to a manager and refresh its derived entities."""
    manager.devices = list(devices)
    manager.fuseGroups = list(
        {id(device.fuseGrp): device.fuseGrp for device in devices if hasattr(device, "fuseGrp")}.values(),
    )
    for device in devices:
        device.on_available_kwh_changed = manager.refresh_energy_kwh
    manager.refresh_primary_device_options()
    manager.refresh_energy_kwh()
