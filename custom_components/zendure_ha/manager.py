"""Coordinator for Zendure integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import sqrt
from pathlib import Path
from typing import Any

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers import homeassistant as auth_ha
from homeassistant.components import bluetooth
from homeassistant.components.number import NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration

from .api import Api
from .const import CONF_AUTO_MQTT_USER, CONF_P1METER, DOMAIN, DeviceState, ManagerMode, ManagerState, SmartMode
from .device import DeviceSettings, ZendureDevice, ZendureLegacy
from .entity import EntityDevice
from .fusegroup import FuseGroup
from .number import ZendureRestoreNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureSensor

SCAN_INTERVAL = timedelta(seconds=60)
PRIMARY_DEVICE_DISABLED = "__disabled__"

_LOGGER = logging.getLogger(__name__)

EMPTY_SOC_STATES = {
    DeviceState.SOCEMPTY,
    DeviceState.SOCRESERVE,
}

LOW_SOC_STATES = {
    *EMPTY_SOC_STATES,
    DeviceState.RESERVE_RECOVERY,
}

type ZendureConfigEntry = ConfigEntry[ZendureManager]


@dataclass(frozen=True, slots=True)
class _PowerRouteDevice:
    """Per-cycle routing facts for one device."""

    device: ZendureDevice
    selected_primary: bool
    charging: bool
    discharging: bool
    idle: bool
    produced_limit: int
    produced_home: int
    battery_home_output: int
    charge_floor: int
    charge_surplus: int
    bypass_passthrough: int
    available_discharge: int
    available_discharge_with_produced: int

    @property
    def active_produced_home(self) -> int:
        """Return current produced power already serving home output."""
        if not self.discharging or self.device.state == DeviceState.SOCFULL:
            return 0
        return self.produced_home

    @property
    def active_chargeable_produced_home(self) -> int:
        """
        Return produced home that should be preserved as a discharge target.

        SOCEMPTY, SOCRESERVE, and RESERVE_RECOVERY devices are passing solar to
        home but should have that solar redirected to their own battery instead;
        their contribution must not be treated as a floor to preserve in charge
        mode.
        """
        if not self.discharging or self.device.state in {DeviceState.SOCFULL, *LOW_SOC_STATES}:
            return 0
        return self.produced_home

    @property
    def home_output_is_only_produced(self) -> bool:
        """Return whether current home output is fully production-backed."""
        home_output = max(0, self.device.homeOutput.asInt)
        return home_output > 0 and self.produced_home >= home_output


@dataclass(frozen=True, slots=True)
class _PowerRoutingSnapshot:
    """Per-cycle routing view shared by primary-aware manager paths."""

    selected_primary: ZendureDevice | None
    primary_aware: bool
    charge_devices: tuple[ZendureDevice, ...]
    discharge_devices: tuple[ZendureDevice, ...]
    idle_devices: tuple[ZendureDevice, ...]
    devices: dict[ZendureDevice, _PowerRouteDevice]

    def route(self, device: ZendureDevice) -> _PowerRouteDevice:
        """Return routing facts for a device."""
        return self.devices[device]

    def produced_limit(self, device: ZendureDevice) -> int:
        """Return the production-backed output limit for a device."""
        return self.route(device).produced_limit

    def charge_surplus(self, device: ZendureDevice) -> int:
        """Return local production surplus that can remain on the device for charging."""
        return self.route(device).charge_surplus

    def chargeable_produced_home(self, device: ZendureDevice) -> int:
        """Return produced home output that can move into local charging."""
        route = self.route(device)
        if not route.discharging or not route.home_output_is_only_produced:
            return 0
        if route.device.state in {DeviceState.OFFLINE, DeviceState.SOCFULL} or route.device.charge_limit >= 0:
            return 0
        return min(route.produced_home, -route.device.charge_limit)

    @property
    def selected_primary_bypass_passthrough(self) -> int:
        """Return selected-primary production already passed through explicit bypass."""
        if not self.primary_aware or self.selected_primary is None:
            return 0
        if self.selected_primary not in self.discharge_devices:
            return 0
        route = self.route(self.selected_primary)
        if route.device.state == DeviceState.SOCFULL:
            return 0
        return route.bypass_passthrough

    @property
    def active_primary_produced_floor(self) -> int:
        """Return selected-primary produced home output that should be preserved."""
        if self.selected_primary is None or self.selected_primary not in self.discharge_devices:
            return 0
        return self.route(self.selected_primary).active_produced_home

    @property
    def active_non_primary_produced_floor(self) -> int:
        """Return non-primary produced home output that should be preserved."""
        return sum(
            self.route(device).active_produced_home
            for device in self.discharge_devices
            if device is not self.selected_primary
        )

    @property
    def active_serving_pv_floor(self) -> int:
        """Return all active produced home output that should be preserved."""
        return self.active_primary_produced_floor + self.active_non_primary_produced_floor

    @property
    def active_chargeable_serving_pv_floor(self) -> int:
        """
        Return produced home output from devices that are not blocked-low.

        SOCEMPTY, SOCRESERVE, and RESERVE_RECOVERY devices pass solar to home
        but should be redirected to charge their own battery; their solar floor
        must not block the charge-mode debounce.
        """
        return sum(
            self.route(device).active_chargeable_produced_home
            for device in self.discharge_devices
        )

    @property
    def preserves_produced_floor(self) -> bool:
        """Return whether current discharge output should be treated as a production floor."""
        return self.primary_aware or all(
            self.route(device).home_output_is_only_produced for device in self.discharge_devices
        )

    def active_produced_targets(self, devices: list[ZendureDevice]) -> dict[ZendureDevice, int]:
        """Return per-device active produced home targets."""
        return {device: self.route(device).active_produced_home for device in devices}

    def positive_demand_charge_lag(self, setpoint: int) -> bool:
        """Return whether positive P1 should reduce stale charge instead of obeying holdoff."""
        active_charge_floor_total = sum(self.route(device).charge_floor for device in self.charge_devices)
        return (
            setpoint < 0
            and active_charge_floor_total > 0
            and active_charge_floor_total + setpoint > 0
        )

    def discharge_candidates(
        self,
        active_devices: list[ZendureDevice],
        idle_devices: list[ZendureDevice],
        *,
        promote_idle_devices: bool,
    ) -> list[ZendureDevice]:
        """Return active and promotable idle discharge candidates."""
        candidates = list(active_devices)
        candidates.extend(
            device
            for device in idle_devices
            if self.route(device).produced_limit > 0
            or (promote_idle_devices and self.route(device).available_discharge > 0)
        )
        return sorted(candidates, key=lambda device: device.electricLevel.asInt, reverse=False)


class ZendureManager(DataUpdateCoordinator[None], EntityDevice):
    """Class to regular update devices."""

    devices: list[ZendureDevice] = []
    fuseGroups: list[FuseGroup] = []
    simulation: bool = False

    def __init__(self, hass: HomeAssistant, entry: ZendureConfigEntry) -> None:
        """Initialize Zendure Manager."""
        super().__init__(hass, _LOGGER, name="Zendure Manager", update_interval=SCAN_INTERVAL, config_entry=entry)
        EntityDevice.__init__(self, hass, "manager", "Zendure Manager", "", "", "")
        self.api = Api()
        self.operation: ManagerMode = ManagerMode.OFF
        self.zero_next = datetime.min
        self.zero_fast = datetime.min
        self.check_reset = datetime.min
        self.p1meterEvent: Callable[[], None] | None = None
        self.p1_history: deque[int] = deque([25, -25], maxlen=8)
        self.p1_factor = 1
        self.update_count = 0

        self.charge: list[ZendureDevice] = []
        self.charge_limit = 0
        self.charge_optimal = 0
        self.charge_time = datetime.max
        self.charge_last = datetime.min
        self.charge_debounce_since: datetime | None = None
        self.charge_weight = 0

        self.discharge: list[ZendureDevice] = []
        self.discharge_bypass = 0
        self.discharge_produced = 0
        self.discharge_limit = 0
        self.discharge_optimal = 0
        self.discharge_weight = 0
        self.idle: list[ZendureDevice] = []
        self.idle_lvlmax = 0
        self.idle_lvlmin = 0
        self.produced = 0
        self.pwr_low = 0

    def _reset_power_distribution_state(self) -> None:
        """Reset per-cycle distribution state before computing a new routing pass."""
        self.zero_fast = datetime.max
        self.charge.clear()
        self.charge_limit = 0
        self.charge_optimal = 0
        self.charge_weight = 0
        self.discharge.clear()
        self.discharge_bypass = 0
        self.discharge_limit = 0
        self.discharge_optimal = 0
        self.discharge_produced = 0
        self.discharge_weight = 0
        self.idle.clear()
        self.idle_lvlmax = 0
        self.idle_lvlmin = 100
        self.produced = 0
        for fg in self.fuseGroups:
            fg.initPower = True

    async def loadDevices(self) -> None:
        if (
            self.config_entry is None
            or (data := await Api.Connect(self.hass, dict(self.config_entry.data), True)) is None
        ):
            return
        if (mqtt := data.get("mqtt")) is None:
            return

        # get version number from integration
        integration = await async_get_integration(self.hass, DOMAIN)
        if integration is None:
            _LOGGER.error("Integration not found for domain: %s", DOMAIN)
            return
        self.attr_device_info["sw_version"] = integration.manifest.get("version", "unknown")

        self.operationmode = (
            ZendureRestoreSelect(
                self,
                "Operation",
                {
                    0: "off",
                    1: "manual",
                    2: "smart",
                    3: "smart_discharging",
                    4: "smart_charging",
                    5: "store_solar",
                    6: "manual_primary_aware",
                    7: "smart_primary_aware",
                    8: "smart_discharging_primary_aware",
                    9: "smart_charging_primary_aware",
                },
                self.update_operation,
            ),
        )
        self.primarydevice = ZendureRestoreSelect(
            self,
            "primary_device",
            {PRIMARY_DEVICE_DISABLED: "none"},
            self.update_primary_device,
            PRIMARY_DEVICE_DISABLED,
        )
        self.operationstate = ZendureSensor(self, "operation_state")
        self.manualpower = ZendureRestoreNumber(
            self, "manual_power", None, None, "W", "power", 12000, -12000, NumberMode.BOX, True
        )
        self.discharge_recovery_margin = ZendureRestoreNumber(
            self,
            "discharge_recovery_margin",
            self._refresh_discharge_recovery_margin,
            None,
            "%",
            "soc",
            100,
            0,
            NumberMode.BOX,
            True,
        )
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy_storage", None, 1)
        self.totalKwh = ZendureSensor(self, "total_kwh", None, "kWh", "energy_storage", None, 2)
        self.power = ZendureSensor(self, "power", None, "W", "power", "measurement", 0)

        # load devices
        for dev in data["deviceList"]:
            try:
                if (deviceId := dev["deviceKey"]) is None or (prodModel := dev["productModel"]) is None:
                    continue
                _LOGGER.info("Adding device: %s %s => %s", deviceId, prodModel, dev)

                init = Api.createdevice.get(prodModel.lower().strip(), None)
                if init is None:
                    _LOGGER.info("Device %s is not supported!", prodModel)
                    continue

                # create the device and mqtt server
                prodName = dev.get("deviceName") or prodModel
                device = init(self.hass, deviceId, prodName, dev)
                device.set_discharge_recovery_margin(self.discharge_recovery_margin.asNumber)
                device.discharge_start = device.discharge_limit // 10
                device.discharge_optimal = device.discharge_limit // 4
                Api.devices[deviceId] = device
                device.register_pending_entities()

                auto_mqtt = self.config_entry.data.get(CONF_AUTO_MQTT_USER, False)
                if auto_mqtt and Api.localServer is not None and Api.localServer != "":
                    try:
                        psw = hashlib.md5(deviceId.encode()).hexdigest().upper()[8:24]
                        provider: auth_ha.HassAuthProvider = auth_ha.async_get_provider(self.hass)
                        credentials = await provider.async_get_or_create_credentials({"username": deviceId.lower()})
                        user = await self.hass.auth.async_get_user_by_credentials(credentials)
                        if user is None:
                            user = await self.hass.auth.async_create_user(
                                deviceId, group_ids=[GROUP_ID_USER], local_only=True
                            )
                            await provider.async_add_auth(deviceId.lower(), psw)
                            await self.hass.auth.async_link_user(user, credentials)
                        else:
                            await provider.async_change_password(deviceId.lower(), psw)

                        _LOGGER.info("Managed MQTT user for device: %s", deviceId)

                    except Exception as err:
                        _LOGGER.error("Failed to manage MQTT user for %s: %s", deviceId, err)
                elif auto_mqtt:
                    _LOGGER.debug("Skipping auto MQTT user creation for %s: Local server not configured.", deviceId)

            except Exception as e:
                _LOGGER.error("Unable to create device %s!", e)
                _LOGGER.error(traceback.format_exc())

        self.devices = list(Api.devices.values())
        for device in self.devices:
            device.on_available_kwh_changed = self.refresh_available_kwh
        self._refresh_discharge_recovery_margin(None, None)
        self.refresh_available_kwh()
        _LOGGER.info("Loaded %s devices", len(self.devices))

        # initialize the api & p1 meter
        self.api.Init(self.config_entry.data, mqtt)
        await self.update_fusegroups()
        self.update_p1meter(self.config_entry.data.get(CONF_P1METER, "sensor.power_actual"))
        await asyncio.sleep(1)  # allow other tasks to run
        self.refresh_primary_device_options()
        self.register_pending_entities()

    async def update_fusegroups(self) -> None:
        _LOGGER.info("Update fusegroups")

        # updateFuseGroup callback
        async def updateFuseGroup(_entity: ZendureRestoreSelect, _value: Any) -> None:
            await self.update_fusegroups()

        fuseGroups: dict[str, FuseGroup] = {}
        for device in self.devices:
            try:
                if device.fuseGroup.onchanged is None:
                    device.fuseGroup.onchanged = updateFuseGroup

                fg: FuseGroup | None = None
                match device.fuseGroup.state:
                    case "owncircuit" | "group3600":
                        fg = FuseGroup(device.name, 3600, -3600)
                    case "group800":
                        fg = FuseGroup(device.name, 800, -1200)
                    case "group800_2400":
                        fg = FuseGroup(device.name, 800, -2400)
                    case "group1200":
                        fg = FuseGroup(device.name, 1200, -1200)
                    case "group2000":
                        fg = FuseGroup(device.name, 2000, -2000)
                    case "group2400":
                        fg = FuseGroup(device.name, 2400, -2400)
                    case "unused":
                        # only switch off, if Manager is used
                        if self.operation != ManagerMode.OFF:
                            await device.power_off()
                        continue
                    case _:
                        _LOGGER.debug(
                            "Device %s has unsupported fuseGroup state: %s", device.name, device.fuseGroup.state
                        )
                        continue

                if fg is not None:
                    fg.devices.append(device)
                    fuseGroups[device.deviceId] = fg
            except AttributeError as err:
                _LOGGER.error("Device %s missing fuseGroup attribute: %s", device.name, err)
            except Exception as err:
                _LOGGER.error(
                    "Unable to create fusegroup for device %s (%s): %s",
                    device.name,
                    device.deviceId,
                    err,
                    exc_info=True,
                )

        # Update the fusegroups and select optins for each device
        for device in self.devices:
            try:
                fusegroups: dict[Any, str] = {
                    0: "unused",
                    1: "owncircuit",
                    2: "group800",
                    3: "group800_2400",
                    4: "group1200",
                    5: "group2000",
                    6: "group2400",
                    7: "group3600",
                }
                for deviceId, fg in fuseGroups.items():
                    if deviceId != device.deviceId:
                        fusegroups[deviceId] = f"Part of {fg.name} fusegroup"
                device.fuseGroup.setDict(fusegroups)
            except AttributeError as err:
                _LOGGER.error("Device %s missing fuseGroup attribute: %s", device.name, err)
            except Exception as err:
                _LOGGER.error(
                    "Unable to update fusegroup options for device %s (%s): %s",
                    device.name,
                    device.deviceId,
                    err,
                    exc_info=True,
                )

        # Add devices to fusegroups
        for device in self.devices:
            if fg := fuseGroups.get(device.fuseGroup.value):
                device.fuseGrp = fg
                fg.devices.append(device)
            device.setStatus()

        # check if we can split fuse groups
        self.fuseGroups.clear()
        for fg in fuseGroups.values():
            if (
                len(fg.devices) > 1
                and fg.maxpower >= sum(d.discharge_limit for d in fg.devices)
                and fg.minpower <= sum(d.charge_limit for d in fg.devices)
            ):
                for d in fg.devices:
                    self.fuseGroups.append(FuseGroup(d.name, d.discharge_limit, d.charge_limit, [d]))
            else:
                for d in fg.devices:
                    d.fuseGrp = fg
                self.fuseGroups.append(fg)
        self.refresh_primary_device_options()

    async def update_operation(self, entity: ZendureSelect, _operation: Any) -> None:
        operation = ManagerMode(entity.value)
        _LOGGER.info("Update operation: %s from: %s", operation, self.operation)

        self.operation = operation
        if self.p1meterEvent is not None:
            if operation != ManagerMode.OFF and (len(self.devices) == 0 or all(not d.online for d in self.devices)):
                _LOGGER.warning("No devices online, not possible to start the operation")
                return

            match self.operation:
                case ManagerMode.OFF:
                    if len(self.devices) > 0:
                        for d in self.devices:
                            await d.power_off()

    async def update_primary_device(self, entity: ZendureSelect, _device_id: Any) -> None:
        """Handle updates to the selected primary device."""
        if entity is not None:
            entity.update_value(_device_id)
        _LOGGER.info("Update primary device: %s", _device_id if _device_id is not None else None)
        if self.operation != ManagerMode.MATCHING_PRIMARY_AWARE or self.config_entry is None:
            return

        p1meter = self.config_entry.data.get(CONF_P1METER, "sensor.power_actual")
        if (state := self.hass.states.get(p1meter)) is None:
            return

        try:
            p1 = int(self.p1_factor * float(state.state))
        except (TypeError, ValueError):
            return

        try:
            self._reset_power_distribution_state()
            await self.powerChanged(p1, False, datetime.now())
        finally:
            time = datetime.now()
            self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
            self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)

    def refresh_primary_device_options(self) -> None:
        """Refresh the selectable primary device list."""
        if not hasattr(self, "primarydevice"):
            return

        options = {PRIMARY_DEVICE_DISABLED: "none"}
        for device in sorted(self.devices, key=lambda dev: dev.name):
            options[device.deviceId] = device.name
        self.primarydevice.setDict(options)

    def resolve_primary_device(self) -> ZendureDevice | None:
        """Return the currently selected primary device."""
        if not hasattr(self, "primarydevice"):
            return None
        device_id = self.primarydevice.value
        if device_id in (None, PRIMARY_DEVICE_DISABLED):
            return None
        return next((device for device in self.devices if device.deviceId == device_id), None)

    def get_primary_device(self, charging: bool) -> ZendureDevice | None:
        """Return the selected primary device when it can participate in the requested direction."""
        if self.operation not in {
            ManagerMode.MATCHING_PRIMARY_AWARE,
            ManagerMode.MATCHING_DISCHARGE_PRIMARY_AWARE,
            ManagerMode.MATCHING_CHARGE_PRIMARY_AWARE,
            ManagerMode.MANUAL_PRIMARY_AWARE,
        }:
            return None

        if (device := self.resolve_primary_device()) is None or not device.online:
            return None

        if charging:
            return (
                device
                if device.state not in {DeviceState.OFFLINE, DeviceState.SOCFULL} and device.charge_limit < 0
                else None
            )
        return device if self._available_discharge_power(device, primary_aware=True) > 0 else None

    def _refresh_discharge_recovery_margin(self, _entity: Any, _value: Any) -> None:
        """Propagate the configured discharge recovery margin to all devices."""
        margin = max(0, self.discharge_recovery_margin.asNumber)
        for device in getattr(self, "devices", []):
            device.set_discharge_recovery_margin(margin)
        self.refresh_available_kwh()

    def refresh_available_kwh(self) -> None:
        """Refresh the manager aggregate for currently online device energy."""
        self.availableKwh.update_value(
            sum(max(0, device.actualKwh) for device in self.devices if device.state != DeviceState.OFFLINE)
        )

    @staticmethod
    def _is_discharge_capable(device: ZendureDevice) -> bool:
        """Return whether the device is generally capable of discharging."""
        return device.state not in {DeviceState.OFFLINE, *EMPTY_SOC_STATES} and device.discharge_limit > 0

    def _available_discharge_power(
        self,
        device: ZendureDevice,
        *,
        primary_aware: bool = False,
        allow_produced_only: bool = False,
    ) -> int:
        """Return the currently available discharge contribution for a device."""
        if not self._is_discharge_capable(device):
            return 0
        if device.is_discharge_blocked():
            return (
                self._current_produced_output_limit(device, primary_aware=primary_aware) if allow_produced_only else 0
            )
        if primary_aware:
            return self._primary_discharge_limit(device)
        return max(0, device.fuseGrp.discharge_limit(device))

    def _current_produced_output_limit(self, device: ZendureDevice, *, primary_aware: bool = False) -> int:
        """Return the solar/off-grid contribution currently available without draining the battery."""
        if not device.online or device.discharge_limit <= 0:
            return 0

        limit = max(0, -device.pwr_produced)
        if primary_aware:
            return min(limit, self._primary_discharge_limit(device))
        return min(limit, max(0, device.fuseGrp.discharge_limit(device)))

    def _primary_charge_limit(self, device: ZendureDevice) -> int:
        """Return the maximum charge power for the primary within its fusegroup."""
        other_input = sum(max(0, other.homeInput.asInt) for other in device.fuseGrp.devices if other is not device)
        return min(0, max(device.charge_limit, device.fuseGrp.minpower + other_input))

    def _primary_discharge_limit(self, device: ZendureDevice) -> int:
        """Return the maximum discharge power for the primary within its fusegroup."""
        other_output = sum(max(0, other.homeOutput.asInt) for other in device.fuseGrp.devices if other is not device)
        return max(0, min(device.discharge_limit, device.fuseGrp.maxpower - other_output))

    @staticmethod
    def _current_charge_surplus_limit(device: ZendureDevice) -> int:
        """Return current locally produced surplus that can stay on this device for charging."""
        if not device.online or device.charge_limit >= 0:
            return 0
        if device.state in {DeviceState.OFFLINE, DeviceState.SOCFULL}:
            return 0
        return min(-device.charge_limit, max(0, -device.pwr_produced - max(0, device.homeOutput.asInt)))

    def _power_routing_snapshot(
        self, selected_primary: ZendureDevice | None, *, primary_aware: bool
    ) -> _PowerRoutingSnapshot:
        """Build a per-cycle route snapshot from the current device classification."""
        devices = [*self.charge, *self.discharge, *self.idle]
        if selected_primary is not None and selected_primary not in devices:
            devices.append(selected_primary)

        route_devices: dict[ZendureDevice, _PowerRouteDevice] = {}
        for device in devices:
            produced_limit = (
                self._current_produced_output_limit(device, primary_aware=primary_aware)
                if primary_aware or device in self.discharge
                else 0
            )
            home_output = max(0, device.homeOutput.asInt)
            produced_home = min(home_output, produced_limit)
            bypass_passthrough = 0
            if (
                getattr(device, "byPass", None) is not None
                and device.byPass.is_on
                and home_output > 0
                and device.pwr_produced < 0
            ):
                bypass_passthrough = min(home_output, -device.pwr_produced)

            route_devices[device] = _PowerRouteDevice(
                device=device,
                selected_primary=device is selected_primary,
                charging=device in self.charge,
                discharging=device in self.discharge,
                idle=device in self.idle,
                produced_limit=produced_limit,
                produced_home=produced_home,
                battery_home_output=max(0, home_output - produced_home),
                charge_floor=max(0, device.homeInput.asInt - max(0, device.pwr_offgrid)),
                charge_surplus=self._current_charge_surplus_limit(device),
                bypass_passthrough=bypass_passthrough,
                available_discharge=(
                    self._available_discharge_power(device, primary_aware=primary_aware) if primary_aware else 0
                ),
                available_discharge_with_produced=self._available_discharge_power(
                    device, primary_aware=primary_aware, allow_produced_only=True
                )
                if primary_aware
                else 0,
            )

        return _PowerRoutingSnapshot(
            selected_primary=selected_primary,
            primary_aware=primary_aware,
            charge_devices=tuple(self.charge),
            discharge_devices=tuple(self.discharge),
            idle_devices=tuple(self.idle),
            devices=route_devices,
        )

    @staticmethod
    def _allocate_capped_targets(
        setpoint: int,
        devices: list[ZendureDevice],
        capacities: dict[ZendureDevice, int],
        *,
        direction: int,
        targets: dict[ZendureDevice, int] | None = None,
    ) -> tuple[dict[ZendureDevice, int], int]:
        """Allocate a positive or negative budget across ordered per-device caps."""
        allocated = {device: targets.get(device, 0) if targets is not None else 0 for device in devices}
        if setpoint * direction <= 0:
            return allocated, setpoint

        remaining = abs(setpoint)
        for device in devices:
            if remaining <= 0:
                break
            target = min(max(0, capacities.get(device, 0)), remaining)
            if target <= 0:
                continue
            allocated[device] = allocated.get(device, 0) + direction * target
            remaining -= target

        return allocated, direction * remaining

    def _charge_metrics(self, devices: list[ZendureDevice]) -> tuple[int, int, int]:
        """Recalculate charge metrics for the active fallback devices."""
        for fg in self.fuseGroups:
            fg.initPower = True

        limit = 0
        optimal = 0
        weight = 0
        for device in devices:
            limit += device.fuseGrp.charge_limit(device)
            optimal += device.charge_optimal
            weight += device.pwr_max * (100 - device.electricLevel.asInt)
        return limit, optimal, weight

    @staticmethod
    def _collect_charge_candidates(
        active_devices: list[ZendureDevice],
        idle_devices: list[ZendureDevice],
        *,
        promote_idle_devices: bool = False,
    ) -> tuple[list[ZendureDevice], list[ZendureDevice]]:
        """Promote blocked-low idle devices into the normal charge allocation set."""
        charge_devices = list(active_devices)
        remaining_idle: list[ZendureDevice] = []

        for device in idle_devices:
            if device.state in LOW_SOC_STATES or (promote_idle_devices and device.state != DeviceState.SOCFULL):
                charge_devices.append(device)
            else:
                remaining_idle.append(device)

        return charge_devices, remaining_idle

    @staticmethod
    def _idle_levels(devices: list[ZendureDevice]) -> tuple[int, int]:
        """Return idle SoC bounds for the provided devices."""
        if not devices:
            return 0, 100
        return (
            max(device.electricLevel.asInt for device in devices),
            min(device.electricLevel.asInt if device.state != DeviceState.SOCFULL else 100 for device in devices),
        )

    async def _power_discharge_primary_aware(self, device: ZendureDevice, power: int) -> int:
        """Use explicit bypass for eligible devices when a primary-aware path requests zero output."""
        if power == 0 and getattr(device, "byPass", None) is not None and device.byPass.is_on:
            return 0
        if power == 0 and device.can_bypass:
            return await device.power_bypass()
        return await device.power_discharge(power)

    def _allocate_produced_floor(
        self,
        setpoint: int,
        devices: list[ZendureDevice],
        *,
        primary_aware: bool = False,
    ) -> tuple[dict[ZendureDevice, int], int]:
        """Assign the current solar/off-grid contribution before any battery-backed discharge."""
        targets = dict.fromkeys(devices, 0)
        produced_caps = {
            device: self._current_produced_output_limit(device, primary_aware=primary_aware) for device in devices
        }
        produced_budget = min(setpoint, sum(produced_caps.values()))

        remaining = produced_budget
        weight = sum(produced_caps.values())
        limit = weight
        for d in sorted(devices, key=lambda d: d.electricLevel.asInt, reverse=False):
            cap = produced_caps[d]
            if cap == 0:
                continue
            pwr = int(remaining * cap / weight) if weight != 0 else 0
            weight -= cap
            limit -= cap
            if limit < remaining - pwr:
                pwr = max(remaining - limit, 0)
            pwr = min(pwr, remaining, cap)
            targets[d] += pwr
            remaining -= pwr

        return targets, setpoint - (produced_budget - remaining)

    def _add_battery_remainder(
        self,
        targets: dict[ZendureDevice, int],
        setpoint: int,
        devices: list[ZendureDevice],
        idle_lvlmax: int,
        *,
        primary_aware: bool = False,
    ) -> int:
        """Distribute the battery-backed remainder on top of the solar floor."""
        if setpoint <= 0:
            return 0

        battery_limit = 0
        battery_optimal = 0
        battery_weight = 0
        battery_caps: dict[ZendureDevice, int] = {}
        for d in devices:
            total_limit = self._available_discharge_power(d, primary_aware=primary_aware, allow_produced_only=True)
            cap = 0 if d.is_discharge_blocked() else max(0, total_limit - targets[d])
            battery_caps[d] = cap
            battery_limit += cap
            battery_weight += cap * d.electricLevel.asInt
            if cap > 0:
                battery_optimal += d.discharge_optimal

        setpoint = min(setpoint, battery_limit)
        dev_start = max(0, setpoint - battery_optimal * 2) if setpoint > SmartMode.POWER_START else 0
        limit = battery_limit
        for i, d in enumerate(devices):
            cap = battery_caps[d]
            if cap == 0:
                continue
            pwr = int(setpoint * (cap * d.electricLevel.asInt) / battery_weight) if battery_weight != 0 else 0
            battery_weight -= cap * d.electricLevel.asInt

            limit -= cap
            if limit < setpoint - pwr:
                pwr = max(setpoint - limit, 0)
            pwr = min(pwr, setpoint, cap)

            if len(devices) > 1 and i == 0 and d.state != DeviceState.SOCFULL:
                self.pwr_low = 0 if (delta := d.discharge_start * 1.5 - pwr) <= 0 else self.pwr_low + int(delta)
                pwr = 0 if self.pwr_low > d.discharge_optimal else pwr

            targets[d] += pwr
            setpoint -= pwr
            dev_start += 1 if pwr != 0 and d.electricLevel.asInt + 3 < idle_lvlmax else 0

        return dev_start

    def _collect_discharge_candidates(
        self,
        active_devices: list[ZendureDevice],
        idle_devices: list[ZendureDevice],
        *,
        primary_aware: bool = False,
        promote_idle_devices: bool = False,
    ) -> list[ZendureDevice]:
        """Include idle devices that can contribute current solar/off-grid power."""
        candidates = list(active_devices)
        candidates.extend(
            device
            for device in idle_devices
            if self._current_produced_output_limit(device, primary_aware=primary_aware) > 0
            or (promote_idle_devices and self._available_discharge_power(device, primary_aware=primary_aware) > 0)
        )
        return sorted(candidates, key=lambda d: d.electricLevel.asInt, reverse=False)

    async def _start_idle_discharge_devices(
        self, idle_devices: list[ZendureDevice], dev_start: int, *, primary_aware: bool = False
    ) -> None:
        """Start additional idle devices when the remainder distribution requires it."""
        if dev_start <= 0 or not idle_devices:
            return

        idle_devices.sort(key=lambda d: d.electricLevel.asInt, reverse=True)
        for d in idle_devices:
            if self._available_discharge_power(d, primary_aware=primary_aware) > 0:
                if primary_aware:
                    await self._power_discharge_primary_aware(d, SmartMode.POWER_START)
                else:
                    await d.power_discharge(SmartMode.POWER_START)
                if (dev_start := dev_start - d.discharge_optimal * 2) <= 0:
                    break
        self.pwr_low = 0

    async def _async_update_data(self) -> None:

        def isBleDevice(device: ZendureDevice, si: bluetooth.BluetoothServiceInfoBleak) -> bool:
            for d in si.manufacturer_data.values():
                try:
                    if d is None or len(d) <= 1:
                        continue
                    sn = d.decode("utf8")[:-1]
                    if device.snNumber.endswith(sn):
                        _LOGGER.info("Found Zendure Bluetooth device: %s", si)
                        device.attr_device_info["connections"] = {("bluetooth", str(si.address))}
                        return True
                except Exception:
                    continue
            return False

        time = datetime.now()
        kwh = 0
        for device in self.devices:
            kwh += device.kWh
            if isinstance(device, ZendureLegacy) and device.bleMac is None:
                for si in bluetooth.async_discovered_service_info(self.hass, False):
                    if isBleDevice(device, si):
                        break

            _LOGGER.debug("Update device: %s (%s)", device.name, device.deviceId)
            await device.dataRefresh(self.update_count)
            if device.hemsState.is_on and (time - device.hemsStateUpdated).total_seconds() > SmartMode.HEMSOFF_TIMEOUT:
                device.hemsState.update_value(0)
            device.setStatus()
            device.update_device_state()
        self.update_count += 1
        self.totalKwh.update_value(kwh)
        self.refresh_available_kwh()

        # Manually update the timer
        if self.hass and self.hass.loop.is_running():
            self._schedule_refresh()

    def update_p1meter(self, p1meter: str | None) -> None:
        """Update the P1 meter sensor."""
        _LOGGER.debug("Updating P1 meter to: %s", p1meter)
        if self.p1meterEvent:
            self.p1meterEvent()
        if p1meter:
            self.p1meterEvent = async_track_state_change_event(self.hass, [p1meter], self._p1_changed)
            if (entity := self.hass.states.get(p1meter)) is not None and entity.attributes.get(
                "unit_of_measurement", "W"
            ) in ("kW", "kilowatt", "kilowatts"):
                self.p1_factor = 1000
        else:
            self.p1meterEvent = None

    def writeSimulation(self, time: datetime, p1: int) -> None:
        if Path("simulation.csv").exists() is False:
            with Path("simulation.csv").open("w") as f:
                f.write(
                    "Time;P1;Operation;Battery;Solar;Home;SetPoint;--;"
                    + ";".join(
                        [
                            f"bat;Prod;Home;{
                                json.dumps(
                                    DeviceSettings(
                                        d.name,
                                        d.fuseGrp.name,
                                        d.charge_limit,
                                        d.discharge_limit,
                                        d.maxSolar,
                                        d.kWh,
                                        d.socSet.asNumber,
                                        d.minSoc.asNumber,
                                    ),
                                    default=vars,
                                )
                            }"
                            for d in self.devices
                        ]
                    )
                    + "\n"
                )

        with Path("simulation.csv").open("a") as f:
            data = ""
            tbattery = 0
            tsolar = 0
            thome = 0

            for d in self.devices:
                tbattery += (pwr_battery := d.batteryOutput.asInt - d.batteryInput.asInt)
                tsolar += (pwr_solar := d.solarInput.asInt)
                thome += (pwr_home := d.homeOutput.asInt - d.homeInput.asInt)
                data += f";{pwr_battery};{pwr_solar};{pwr_home};{d.electricLevel.asInt}"

            f.write(
                f"{time};{p1};{self.operation};{tbattery};{tsolar};{thome};{self.manualpower.asNumber};" + data + "\n"
            )

    async def _p1_changed(self, event: Event[EventStateChangedData]) -> None:
        # exit if there is nothing to do
        if not self.hass.is_running or not self.hass.is_running or (new_state := event.data["new_state"]) is None:
            return

        try:  # convert the state to a float
            p1 = int(self.p1_factor * float(new_state.state))
        except ValueError:
            return

        # Get time & update simulation
        time = datetime.now()
        if ZendureManager.simulation:
            self.writeSimulation(time, p1)

        # Check for fast delay
        if time < self.zero_fast:
            _LOGGER.debug("P1 update suppressed by fast-delay (zero_fast=%s)", self.zero_fast)
            self.p1_history.append(p1)
            return

        # calculate the standard deviation
        if len(self.p1_history) > 1:
            avg = int(sum(self.p1_history) / len(self.p1_history))
            stddev = SmartMode.P1_STDDEV_FACTOR * max(
                SmartMode.P1_STDDEV_MIN, sqrt(sum([pow(i - avg, 2) for i in self.p1_history]) / len(self.p1_history))
            )
            if isFast := abs(p1 - avg) > stddev or abs(p1 - self.p1_history[0]) > stddev:
                self.p1_history.clear()
        else:
            isFast = False
        self.p1_history.append(p1)

        # check minimal time between updates
        if isFast or time > self.zero_next:
            try:
                # prevent updates during power distribution changes
                self._reset_power_distribution_state()
                await self.powerChanged(p1, isFast, time)
            except Exception as err:
                _LOGGER.error(err)
                _LOGGER.error(traceback.format_exc())
            finally:
                time = datetime.now()
                self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
                self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)

    async def powerChanged(self, p1: int, isFast: bool, time: datetime) -> None:
        """Return the distribution setpoint."""
        setpoint = p1
        power = 0

        for d in self.devices:
            if await d.power_get():
                # get power production
                d.pwr_produced = min(
                    0, d.batteryOutput.asInt + d.homeInput.asInt - d.batteryInput.asInt - d.homeOutput.asInt
                )
                self.produced -= d.pwr_produced

                # only positive pwr_offgrid must be taken into account, negative values count a solarInput
                if (home := -d.homeInput.asInt + max(0, d.pwr_offgrid)) < 0:
                    self.charge.append(d)
                    self.charge_limit += d.fuseGrp.charge_limit(d)
                    self.charge_optimal += d.charge_optimal
                    self.charge_weight += d.pwr_max * (100 - d.electricLevel.asInt)
                    setpoint += home
                # Low-SOC states cannot discharge the battery, but can still
                # feed into the home using solar power or off-grid power.
                elif (home := d.homeOutput.asInt) > 0:
                    self.discharge.append(d)
                    self.discharge_bypass -= d.pwr_produced if d.state == DeviceState.SOCFULL else 0
                    self.discharge_limit += d.fuseGrp.discharge_limit(d)
                    self.discharge_optimal += d.discharge_optimal
                    self.discharge_produced -= d.pwr_produced
                    self.discharge_weight += d.pwr_max * d.electricLevel.asInt
                    setpoint += home

                else:
                    self.idle.append(d)
                    self.idle_lvlmax = max(self.idle_lvlmax, d.electricLevel.asInt)
                    self.idle_lvlmin = min(
                        self.idle_lvlmin, d.electricLevel.asInt if d.state != DeviceState.SOCFULL else 100
                    )

                power += d.pwr_offgrid + home + d.pwr_produced

        # Update the power entities
        self.power.update_value(power)
        self.refresh_available_kwh()

        primary_aware_mode = self.operation in {
            ManagerMode.MATCHING_PRIMARY_AWARE,
            ManagerMode.MATCHING_DISCHARGE_PRIMARY_AWARE,
            ManagerMode.MATCHING_CHARGE_PRIMARY_AWARE,
            ManagerMode.MANUAL_PRIMARY_AWARE,
        }
        selected_primary = self.resolve_primary_device()
        routing = self._power_routing_snapshot(selected_primary, primary_aware=primary_aware_mode)
        selected_primary_bypass_passthrough = routing.selected_primary_bypass_passthrough
        self.discharge_bypass += selected_primary_bypass_passthrough
        active_primary_produced_floor = routing.active_primary_produced_floor
        active_non_primary_produced_floor = routing.active_non_primary_produced_floor
        active_serving_pv_floor = routing.active_serving_pv_floor
        if p1 > 0 and self.charge and routing.preserves_produced_floor:
            self.discharge_bypass += max(0, active_serving_pv_floor - selected_primary_bypass_passthrough)

        # discharge_bypass accumulates already-served produced power from SOCFULL devices,
        # explicit bypass, and produced pass-through during positive charge lag.
        # Subtract it from setpoint to avoid over-discharging from grid. Clamp away a
        # negative setpoint only when the cycle has not already entered charge mode.
        gross_discharge_setpoint = setpoint
        if self.discharge_bypass > 0:
            net_setpoint = setpoint - self.discharge_bypass
            setpoint = max(0, net_setpoint) if p1 > 0 and not self.charge else net_setpoint
            if (
                self.operation == ManagerMode.MATCHING_PRIMARY_AWARE
                and p1 > 0
                and self.charge
                and active_serving_pv_floor > 0
                and setpoint >= 0
            ):
                setpoint = max(setpoint, gross_discharge_setpoint)
        discharge_candidate_setpoint = setpoint

        # In zero/negative-p1 cycles, treat non-bypass production as extra chargeable
        # surplus when the cycle is already in surplus or the selected primary can
        # keep a meaningful local surplus after current home pass-through.
        extra_surplus = self.produced - self.discharge_bypass
        charge_transition_would_zero = self.charge_time > time
        selected_charge_primary = self.get_primary_device(charging=True)
        active_non_primary_local_surplus = sum(
            routing.charge_surplus(device) for device in self.discharge if device is not selected_primary
        )
        # Non-primary low-SOC devices whose solar exactly covers homeOutput have
        # charge_surplus=0 but can still redirect that solar to their own battery.
        active_non_primary_empty_chargeable = sum(
            routing.chargeable_produced_home(device)
            for device in self.discharge
            if device is not selected_primary and device.state in LOW_SOC_STATES
        )
        primary_keeps_local_surplus = (
            selected_charge_primary is not None
            and not selected_charge_primary.is_discharge_blocked()
            and routing.charge_surplus(selected_charge_primary) > 0
            and (active_primary_produced_floor == 0 or not charge_transition_would_zero)
            and active_non_primary_produced_floor == 0
        )
        if p1 <= 0 and extra_surplus > 0:
            surplus_setpoint = setpoint - extra_surplus
            if (
                setpoint <= 0
                or active_non_primary_local_surplus > 0
                or active_non_primary_empty_chargeable > 0
                or (primary_keeps_local_surplus and surplus_setpoint < -SmartMode.POWER_START)
            ):
                setpoint = surplus_setpoint

        positive_demand_charge_lag = p1 > 0 and routing.positive_demand_charge_lag(setpoint)
        debounce_charge_flip = (
            self.operation == ManagerMode.MATCHING_PRIMARY_AWARE
            and routing.active_chargeable_serving_pv_floor > 0
            and charge_transition_would_zero
            and setpoint < 0
            and not positive_demand_charge_lag
        )
        if debounce_charge_flip:
            if self.charge_debounce_since is None:
                self.charge_debounce_since = time
            if time - self.charge_debounce_since < timedelta(seconds=SmartMode.TIMEZERO):
                setpoint = max(0, discharge_candidate_setpoint, active_serving_pv_floor)
        else:
            self.charge_debounce_since = None

        # Update power distribution.
        _LOGGER.info("P1 ======> p1:%s isFast:%s, setpoint:%sW stored:%sW", p1, isFast, setpoint, self.produced)
        match self.operation:
            case ManagerMode.MATCHING:
                if setpoint < 0:
                    await self.power_charge(setpoint, time)
                else:
                    await self.power_discharge(setpoint)

            case ManagerMode.MATCHING_PRIMARY_AWARE:
                if setpoint < 0:
                    await self.power_charge_primary_aware(setpoint, time)
                else:
                    await self.power_discharge_primary_aware(setpoint)

            case ManagerMode.MATCHING_DISCHARGE:
                # Only discharge, do nothing if setpoint is negative
                await self.power_discharge(max(0, setpoint))

            case ManagerMode.MATCHING_DISCHARGE_PRIMARY_AWARE:
                await self.power_discharge_primary_aware(max(0, setpoint))

            case ManagerMode.MATCHING_CHARGE | ManagerMode.STORE_SOLAR:
                # Feed current solar/off-grid production into the home first and only store true surplus.
                if setpoint > 0 and self.produced > SmartMode.POWER_START:
                    await self.power_discharge(min(self.produced, setpoint), produced_only=True)
                # send device into idle-mode
                elif setpoint > 0:
                    await self.power_discharge(0)
                else:
                    await self.power_charge(min(0, setpoint), time)

            case ManagerMode.MATCHING_CHARGE_PRIMARY_AWARE:
                if setpoint > 0 and self.produced > SmartMode.POWER_START:
                    await self.power_discharge_primary_aware(min(self.produced, setpoint), produced_only=True)
                elif setpoint > 0:
                    await self.power_discharge_primary_aware(0)
                else:
                    await self.power_charge_primary_aware(min(0, setpoint), time)

            case ManagerMode.MANUAL:
                # Manual power into or from home
                if (setpoint := int(self.manualpower.asNumber)) > 0:
                    await self.power_discharge(setpoint)
                else:
                    await self.power_charge(setpoint, time)

            case ManagerMode.MANUAL_PRIMARY_AWARE:
                if (setpoint := int(self.manualpower.asNumber)) > 0:
                    await self.power_discharge_primary_aware(setpoint)
                else:
                    await self.power_charge_primary_aware(setpoint, time)

            case ManagerMode.OFF:
                self.operationstate.update_value(ManagerState.OFF.value)

    async def power_charge(self, setpoint: int, time: datetime) -> None:
        """Charge devices."""
        _LOGGER.info("Charge => setpoint %sW", setpoint)

        # stop discharging devices
        for d in self.discharge:
            # avoid stopping bypassing devices
            if d.byPass.is_on:
                continue
            # avoid gridOff device to use power from the grid
            if d.pwr_offgrid == 0:
                await d.power_discharge(0)
            else:
                await d.power_discharge(-10)

        # prevent hysteria
        if self.charge_time > time:
            if self.charge_time == datetime.max:
                self.charge_time = time + timedelta(
                    seconds=2 if (time - self.charge_last).total_seconds() > 300 else 60
                )
                self.charge_last = self.charge_time
                self.pwr_low = 0
            setpoint = 0
        self.operationstate.update_value(ManagerState.CHARGE.value if setpoint < 0 else ManagerState.IDLE.value)

        charge_devices, idle_devices = self._collect_charge_candidates(
            self.charge,
            self.idle,
            promote_idle_devices=setpoint < -SmartMode.POWER_START and not self.charge,
        )
        _idle_lvlmax, idle_lvlmin = self._idle_levels(idle_devices)
        charge_limit, charge_optimal, charge_weight = self._charge_metrics(charge_devices)

        # distribute charging devices
        dev_start = min(0, setpoint - charge_optimal * 2) if setpoint < -SmartMode.POWER_START else 0
        limit = charge_limit
        setpoint = max(limit, setpoint)
        for i, d in enumerate(sorted(charge_devices, key=lambda d: d.electricLevel.asInt, reverse=True)):
            pwr = (
                int(setpoint * (d.pwr_max * (100 - d.electricLevel.asInt)) / charge_weight) if charge_weight != 0 else 0
            )
            charge_weight -= d.pwr_max * (100 - d.electricLevel.asInt)

            # adjust the limit, make sure we have 'enough' power to charge
            limit -= d.pwr_max
            pwr = max(pwr, setpoint, d.pwr_max)
            if limit > setpoint - pwr:
                pwr = max(setpoint - limit, setpoint, d.pwr_max)

            # make sure we have devices in optimal working range
            if len(charge_devices) > 1 and i == 0:
                self.pwr_low = 0 if (delta := d.charge_start * 1.5 - pwr) >= 0 else self.pwr_low + int(-delta)
                pwr = 0 if self.pwr_low < d.charge_optimal else pwr

            setpoint -= await d.power_charge(pwr)
            dev_start += -1 if pwr != 0 and d.electricLevel.asInt > idle_lvlmin + 3 else 0

        # start idle device if needed
        if dev_start < 0 and idle_devices:
            idle_devices.sort(key=lambda d: d.electricLevel.asInt, reverse=False)
            for d in idle_devices:
                # offGrid device need to be started with at least their offgrid power,
                # otherwise they will not be recognized as charging
                # but should not be started with more than pwr_offgrid if they are full
                # if a offGrid device need to be started, the output power is set to 0
                # and it take all offGrid power from grid
                start_pwr = SmartMode.POWER_START
                await d.power_charge(
                    -start_pwr - max(0, d.pwr_offgrid)
                    if d.state != DeviceState.SOCFULL
                    else -max(0, d.pwr_offgrid)
                )
                if (dev_start := dev_start - d.charge_optimal * 2) >= 0:
                    break
            self.pwr_low: int = 0

    async def power_charge_primary_aware(self, setpoint: int, time: datetime) -> None:
        """Charge devices using the selected primary first."""
        _LOGGER.info("Charge (primary-aware) => setpoint %sW", setpoint)

        selected_primary = self.resolve_primary_device()
        routing = self._power_routing_snapshot(selected_primary, primary_aware=True)
        active_discharge_targets = routing.active_produced_targets(self.discharge)
        requested_setpoint = setpoint
        positive_demand_charge_lag = routing.positive_demand_charge_lag(requested_setpoint)
        adjusts_active_charge = requested_setpoint < 0 and any(
            routing.route(device).charge_floor > 0 for device in self.charge
        )
        allow_home_pv_charge = not positive_demand_charge_lag
        # In surplus mode, low-SOC devices should redirect their solar to their
        # own battery instead of continuing to pass it to the home.  Zero their
        # discharge targets so the stop-discharge loop below actually stops them.
        if allow_home_pv_charge:
            active_discharge_targets = {
                d: (0 if d.state in LOW_SOC_STATES and routing.chargeable_produced_home(d) > 0 else t)
                for d, t in active_discharge_targets.items()
            }

        # prevent hysteria
        if self.charge_time > time:
            if self.charge_time == datetime.max:
                self.charge_time = time + timedelta(
                    seconds=2 if (time - self.charge_last).total_seconds() > 300 else 60
                )
                self.charge_last = self.charge_time
                self.pwr_low = 0
            if not adjusts_active_charge:
                setpoint = 0
        self.operationstate.update_value(ManagerState.CHARGE.value if setpoint < 0 else ManagerState.IDLE.value)

        primary = self.get_primary_device(charging=True)
        charge_devices, idle_devices = self._collect_charge_candidates(
            [device for device in self.charge if device is not primary],
            [device for device in self.idle if device is not primary],
            promote_idle_devices=setpoint <= -SmartMode.POWER_START
            and not any(device is not primary for device in self.charge),
        )
        active_secondary_charge_devices = [
            device
            for device in self.discharge
            if device is not selected_primary
            and (
                routing.charge_surplus(device) > 0
                or (allow_home_pv_charge and routing.chargeable_produced_home(device) > 0)
            )
        ]
        pure_secondary_charge_devices = [
            device
            for device in charge_devices
            if device in self.charge and device.homeOutput.asInt == 0 and routing.charge_surplus(device) > 0
        ]
        promoted_secondary_charge_devices = [
            device for device in charge_devices if device not in self.charge and routing.charge_surplus(device) > 0
        ]
        mixed_secondary_charge_devices = [
            device
            for device in charge_devices
            if device in self.charge and device.homeOutput.asInt > 0 and routing.charge_surplus(device) > 0
        ]
        idle_secondary_surplus_devices = [device for device in idle_devices if routing.charge_surplus(device) > 0]
        primary_local_surplus = routing.charge_surplus(primary) if primary is not None else 0
        move_primary_charge_to_secondary = (
            primary is not None
            and primary in self.charge
            and primary_local_surplus == 0
            and bool(
                pure_secondary_charge_devices
                or promoted_secondary_charge_devices
                or mixed_secondary_charge_devices
                or idle_secondary_surplus_devices
            )
        )
        if move_primary_charge_to_secondary and self.charge_time > time:
            setpoint = requested_setpoint
            self.operationstate.update_value(ManagerState.CHARGE.value if setpoint < 0 else ManagerState.IDLE.value)

        surplus_floor_devices = [*active_secondary_charge_devices, *charge_devices, *idle_secondary_surplus_devices]
        charge_targets, setpoint = self._allocate_capped_targets(
            setpoint,
            surplus_floor_devices,
            {
                device: max(
                    routing.charge_surplus(device),
                    routing.chargeable_produced_home(device) if allow_home_pv_charge else 0,
                )
                for device in surplus_floor_devices
            },
            direction=-1,
        )
        if move_primary_charge_to_secondary and not pure_secondary_charge_devices:
            setpoint = 0

        for d in self.discharge:
            if charge_targets.get(d, 0) != 0 or active_discharge_targets.get(d, 0) > 0:
                continue
            # avoid gridOff device to use power from the grid
            if d.pwr_offgrid == 0:
                await self._power_discharge_primary_aware(d, 0)
            else:
                await d.power_discharge(-10)

        if move_primary_charge_to_secondary and primary is not None:
            await primary.power_charge(0)
        elif primary is not None and setpoint < 0:
            primary_target = min(0, max(setpoint, self._primary_charge_limit(primary)))
            if primary_target != 0:
                setpoint -= await primary.power_charge(primary_target)
            elif active_discharge_targets.get(primary, 0) > 0:
                await self._power_discharge_primary_aware(primary, active_discharge_targets[primary])
        elif positive_demand_charge_lag and primary is not None and primary in self.charge:
            await primary.power_charge(0)
        elif selected_primary is not None and active_discharge_targets.get(selected_primary, 0) > 0:
            await self._power_discharge_primary_aware(selected_primary, active_discharge_targets[selected_primary])

        _idle_lvlmax, idle_lvlmin = self._idle_levels(idle_devices)
        charge_limit, charge_optimal, charge_weight = self._charge_metrics(charge_devices)

        # distribute charging devices
        dev_start = min(0, setpoint - charge_optimal * 2) if setpoint < -SmartMode.POWER_START else 0
        limit = charge_limit
        setpoint = max(limit, setpoint)
        for i, d in enumerate(sorted(charge_devices, key=lambda d: d.electricLevel.asInt, reverse=True)):
            pwr = (
                int(setpoint * (d.pwr_max * (100 - d.electricLevel.asInt)) / charge_weight) if charge_weight != 0 else 0
            )
            charge_weight -= d.pwr_max * (100 - d.electricLevel.asInt)

            # adjust the limit, make sure we have 'enough' power to charge
            limit -= d.pwr_max
            pwr = max(pwr, setpoint, d.pwr_max)
            if limit > setpoint - pwr:
                pwr = max(setpoint - limit, setpoint, d.pwr_max)

            # make sure we have devices in optimal working range
            if len(charge_devices) > 1 and i == 0:
                self.pwr_low = 0 if (delta := d.charge_start * 1.5 - pwr) >= 0 else self.pwr_low + int(-delta)
                pwr = 0 if self.pwr_low < d.charge_optimal else pwr

            target = charge_targets.get(d, 0) + pwr
            if move_primary_charge_to_secondary and d not in pure_secondary_charge_devices:
                target = charge_targets.get(d, 0)
                pwr = 0
            setpoint -= pwr
            dev_start += -1 if pwr != 0 and d.electricLevel.asInt > idle_lvlmin + 3 else 0
            if target != 0:
                await d.power_charge(target)

        for d in idle_secondary_surplus_devices:
            target = charge_targets.get(d, 0)
            if target != 0:
                await d.power_charge(target)

        for d in active_secondary_charge_devices:
            target = charge_targets.get(d, 0)
            if target != 0:
                await d.power_charge(target)
            elif active_discharge_targets.get(d, 0) > 0:
                await self._power_discharge_primary_aware(d, active_discharge_targets[d])

        for d, target in active_discharge_targets.items():
            if d is selected_primary or d in active_secondary_charge_devices:
                continue
            if target > 0:
                await self._power_discharge_primary_aware(d, target)

        # start idle device if needed
        if dev_start < 0 and idle_devices:
            idle_devices.sort(key=lambda d: d.electricLevel.asInt, reverse=False)
            for d in idle_devices:
                # offGrid device need to be started with at least their offgrid power,
                # otherwise they will not be recognized as charging
                # but should not be started with more than pwr_offgrid if they are full
                # if a offGrid device need to be started, the output power is set to 0
                # and it take all offGrid power from grid
                await d.power_charge(
                    -SmartMode.POWER_START - max(0, d.pwr_offgrid)
                    if d.state != DeviceState.SOCFULL
                    else -max(0, d.pwr_offgrid)
                )
                if (dev_start := dev_start - d.charge_optimal * 2) >= 0:
                    break
            self.pwr_low: int = 0

    async def power_discharge(self, setpoint: int, *, produced_only: bool = False) -> None:
        """Discharge devices."""
        _LOGGER.info("Discharge => setpoint %sW", setpoint)

        # reset hysteria time
        if self.charge_time != datetime.max:
            self.charge_time = datetime.max
            self.pwr_low = 0

        # stop charging devices
        for d in self.charge:
            # SF 2400 may show more gridInputPower than offGridPower and will be
            # recognized as charging, so set power to 10 instead of 0
            if max(0, d.pwr_offgrid) == 0:
                await d.power_discharge(0)
            else:
                await d.power_discharge(10)

        discharge_devices = self._collect_discharge_candidates(self.discharge, self.idle)
        idle_devices = [device for device in self.idle if device not in discharge_devices]
        idle_lvlmax, _idle_lvlmin = self._idle_levels(idle_devices)
        self.operationstate.update_value(
            ManagerState.DISCHARGE.value if setpoint > 0 and discharge_devices else ManagerState.IDLE.value
        )
        targets, setpoint = self._allocate_produced_floor(setpoint, discharge_devices)

        dev_start = (
            0 if produced_only else self._add_battery_remainder(targets, setpoint, discharge_devices, idle_lvlmax)
        )

        for d in discharge_devices:
            await d.power_discharge(targets[d])

        if produced_only:
            return

        await self._start_idle_discharge_devices(idle_devices, dev_start)

    async def power_discharge_primary_aware(self, setpoint: int, *, produced_only: bool = False) -> None:
        """Discharge devices using the selected primary first."""
        _LOGGER.info("Discharge (primary-aware) => setpoint %sW", setpoint)
        requested_setpoint = setpoint

        # reset hysteria time
        if self.charge_time != datetime.max:
            self.charge_time = datetime.max
            self.pwr_low = 0

        selected_primary = self.resolve_primary_device()
        routing = self._power_routing_snapshot(selected_primary, primary_aware=True)
        charge_produced_devices = [
            device for device in self.charge if device.is_discharge_blocked() and routing.produced_limit(device) > 0
        ]

        # stop charging devices
        for d in self.charge:
            if d in charge_produced_devices:
                continue
            # SF 2400 may show more gridInputPower than offGridPower and will be
            # recognized as charging, so set power to 10 instead of 0
            if max(0, d.pwr_offgrid) == 0:
                await self._power_discharge_primary_aware(d, 0)
            else:
                await d.power_discharge(10)

        primary_produced_cap = routing.produced_limit(selected_primary) if selected_primary is not None else 0
        if (
            not produced_only
            and selected_primary is not None
            and selected_primary.online
            and selected_primary in self.discharge
            and selected_primary.is_discharge_blocked()
            and primary_produced_cap == 0
        ):
            _LOGGER.info(
                "Primary device %s is currently blocked for discharge at floor %.1f%%; stopping discharge",
                selected_primary.name,
                selected_primary.discharge_floor_soc(),
            )
            await self._power_discharge_primary_aware(selected_primary, 0)

        primary = (
            selected_primary
            if selected_primary is not None
            and selected_primary.online
            and (
                primary_produced_cap > 0
                if produced_only
                else routing.route(selected_primary).available_discharge_with_produced > 0
            )
            else None
        )
        remaining_active = [device for device in self.discharge if device is not primary]
        idle_devices = [device for device in self.idle if device is not primary]
        idle_produced_devices = [device for device in idle_devices if routing.produced_limit(device) > 0]
        idle_battery_devices = [device for device in idle_devices if device not in idle_produced_devices]
        produced_devices = sorted(
            (
                device
                for device in [
                    *remaining_active,
                    *(device for device in charge_produced_devices if device is not primary),
                    *idle_produced_devices,
                ]
                if routing.produced_limit(device) > 0
            ),
            key=lambda device: device.electricLevel.asInt,
        )
        active_produced_floor, setpoint = self._allocate_capped_targets(
            setpoint,
            remaining_active,
            {
                device: min(max(0, device.homeOutput.asInt), routing.produced_limit(device))
                for device in remaining_active
            },
            direction=1,
        )

        primary_target = 0
        if primary is not None and primary_produced_cap > 0 and setpoint > 0:
            primary_target = min(setpoint, primary_produced_cap)
            setpoint -= primary_target

        produced_targets, setpoint = self._allocate_capped_targets(
            setpoint,
            produced_devices,
            {
                device: max(0, routing.produced_limit(device) - active_produced_floor.get(device, 0))
                for device in produced_devices
            },
            direction=1,
            targets=active_produced_floor,
        )

        if primary is not None and not produced_only and setpoint > 0:
            primary_cap = routing.route(primary).available_discharge_with_produced
            primary_battery_cap = max(0, primary_cap - primary_target)
            additional_primary = min(setpoint, primary_battery_cap)
            if additional_primary > 0:
                primary_target += additional_primary
                setpoint -= additional_primary

        discharge_devices = routing.discharge_candidates(
            remaining_active,
            idle_battery_devices,
            promote_idle_devices=not produced_only
            and (setpoint > SmartMode.POWER_START or (primary is None and requested_setpoint > SmartMode.POWER_START)),
        )
        self.operationstate.update_value(
            ManagerState.DISCHARGE.value
            if requested_setpoint > 0 and (primary is not None or produced_devices or discharge_devices)
            else ManagerState.IDLE.value
        )
        command_devices = sorted({*produced_devices, *discharge_devices}, key=lambda device: device.electricLevel.asInt)
        targets = {device: produced_targets.get(device, 0) for device in command_devices}
        idle_battery_devices = [device for device in idle_battery_devices if device not in discharge_devices]
        idle_lvlmax, _idle_lvlmin = self._idle_levels(idle_battery_devices)

        dev_start = (
            0
            if produced_only
            else self._add_battery_remainder(
                targets,
                setpoint,
                discharge_devices,
                idle_lvlmax,
                primary_aware=True,
            )
        )

        if primary is not None and primary_target > 0:
            await self._power_discharge_primary_aware(primary, primary_target)

        for d in command_devices:
            await self._power_discharge_primary_aware(d, targets[d])

        if produced_only:
            return

        await self._start_idle_discharge_devices(idle_battery_devices, dev_start, primary_aware=True)
