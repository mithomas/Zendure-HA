"""Shared helpers for Zendure integration tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zendure_ha.const import (
    CONF_APPTOKEN,
    CONF_MQTTLOCAL,
    CONF_MQTTLOG,
    CONF_P1METER,
    DOMAIN,
    DeviceState,
)
from custom_components.zendure_ha.device import ZendureDevice
from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800
from custom_components.zendure_ha.fusegroup import FuseGroup
from custom_components.zendure_ha.manager import ZendureManager
from custom_components.zendure_ha.number import ZendureRestoreNumber
from custom_components.zendure_ha.sensor import ZendureSensor


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
    device_cls: type[ZendureDevice] = SolarFlow800,
    device_id: str = "device-1",
    device_name: str = "test device",
    product_model: str = "SolarFlow 800",
    level: int = 50,
    min_soc: int = 10,
    reserve: int = 10,
    soc_set: int = 80,
    kwh: float = 2.0,
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
        device = device_cls(hass, device_id, product_model, definition)
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
    device.electricLevel.update_value(level)
    device.homeInput.update_value(0)
    device.homeOutput.update_value(0)
    device.batteryInput.update_value(0)
    device.batteryOutput.update_value(0)
    device.lastseen = datetime.now() + timedelta(minutes=5)
    device.kWh = kwh
    device.totalKwh.update_value(kwh)
    device.setStatus()
    device.refresh_discharge_state()
    return device


def make_manager(hass: HomeAssistant) -> ZendureManager:
    """Create a manager instance with the entities needed by the tests."""
    entry = make_config_entry()
    manager = ZendureManager(hass, entry)
    manager.operationstate = ZendureSensor(manager, "operation_state")
    manager.manualpower = ZendureRestoreNumber(
        manager,
        "manual_power",
        None,
        None,
        "W",
        "power",
        12000,
        -12000,
        NumberMode.BOX,
        True,
    )
    if hasattr(manager, "_refresh_discharge_recovery_margin"):
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
    manager.availableKwh = ZendureSensor(
        manager, "available_kwh", None, "kWh", "energy", None, 1
    )
    manager.totalKwh = ZendureSensor(
        manager, "total_kwh", None, "kWh", "energy", None, 2
    )
    manager.power = ZendureSensor(
        manager, "power", None, "W", "power", "measurement", 0
    )
    return manager


def attach_devices(manager: ZendureManager, *devices: ZendureDevice) -> None:
    """Attach devices to a manager and refresh its derived entities."""
    manager.devices = list(devices)
    manager.fuseGroups = list(
        {
            id(device.fuseGrp): device.fuseGrp
            for device in devices
            if hasattr(device, "fuseGrp")
        }.values()
    )
    for device in devices:
        device.on_available_kwh_changed = manager.refresh_available_kwh
    manager.refresh_available_kwh()


@dataclass
class StubValue:
    """Simple numeric wrapper matching the integration sensor API."""

    value: int | float = 0

    @property
    def asInt(self) -> int:
        return int(self.value)

    @property
    def asNumber(self) -> int | float:
        return self.value


@dataclass
class StubFuseGroup:
    """Simple fuse-group model for manager tests."""

    maxpower: int
    minpower: int
    discharge_limit_value: int
    charge_limit_value: int
    devices: list[Any] = field(default_factory=list)
    initPower: bool = False

    def discharge_limit(self, _device: Any) -> int:
        return self.discharge_limit_value

    def charge_limit(self, _device: Any) -> int:
        return self.charge_limit_value


@dataclass(eq=False)
class StubDevice:
    """Small device double for manager allocation tests."""

    name: str
    deviceId: str
    state: DeviceState = DeviceState.INACTIVE
    level: int = 50
    online: bool = True
    blocked: bool = False
    floor: float = 10.0
    discharge_limit: int = 800
    charge_limit: int = -1000
    discharge_optimal: int = 200
    discharge_start: int = 50
    charge_optimal: int = 200
    charge_start: int = 50
    pwr_max: int = 800
    home_input: int = 0
    home_output: int = 0
    battery_input: int = 0
    battery_output: int = 0
    pwr_produced: int = 0
    pwr_offgrid_value: int = 0
    actualKwh: float = 0.0
    bypass_capable: bool = False
    fuse_discharge_limit: int | None = None
    fuse_charge_limit: int | None = None
    power_discharge_mock: AsyncMock = field(default_factory=AsyncMock, repr=False)
    power_charge_mock: AsyncMock = field(default_factory=AsyncMock, repr=False)
    power_bypass_mock: AsyncMock = field(default_factory=AsyncMock, repr=False)

    def __post_init__(self) -> None:
        """Initialize stub values."""
        self.electricLevel = StubValue(self.level)
        self.homeInput = StubValue(self.home_input)
        self.homeOutput = StubValue(self.home_output)
        self.batteryInput = StubValue(self.battery_input)
        self.batteryOutput = StubValue(self.battery_output)
        self.fuseGrp = StubFuseGroup(
            maxpower=self.fuse_discharge_limit or self.discharge_limit,
            minpower=self.fuse_charge_limit or self.charge_limit,
            discharge_limit_value=self.fuse_discharge_limit or self.discharge_limit,
            charge_limit_value=self.fuse_charge_limit or self.charge_limit,
            devices=[self],
        )

    @property
    def can_bypass(self) -> bool:
        return self.bypass_capable and self.state == DeviceState.SOCFULL

    @property
    def pwr_offgrid(self) -> int:
        return self.pwr_offgrid_value

    def is_discharge_blocked(self, *_args: Any, **_kwargs: Any) -> bool:
        return self.blocked

    def discharge_floor_soc(self, *_args: Any, **_kwargs: Any) -> float:
        return self.floor

    async def power_discharge(self, power: int) -> int:
        await self.power_discharge_mock(power)
        return power

    async def power_charge(self, power: int) -> int:
        await self.power_charge_mock(power)
        return power

    async def power_bypass(self) -> int:
        await self.power_bypass_mock()
        return 0
