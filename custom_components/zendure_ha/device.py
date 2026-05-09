"""Zendure Integration device."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from aiohttp import ClientTimeout, ServerDisconnectedError
from bleak import BleakClient
from bleak.exc import BleakError

try:
    from bleak_retry_connector import establish_connection
except ImportError:
    establish_connection = None

from homeassistant.components import bluetooth, persistent_notification
from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from paho.mqtt import client as mqtt_client
from stringcase import camelcase

from .binary_sensor import ZendureBinarySensor
from .button import ZendureButton
from .const import ConnectionMode, DeviceState, SmartMode, SocLimitState
from .entity import EntityDevice, EntityZendure
from .number import ZendureNumber, ZendureRestoreNumber
from .select import ZendureRestoreSelect, ZendureSelect
from .sensor import ZendureRestoreSensor, ZendureSensor
from .switch import ZendureSwitch

_LOGGER = logging.getLogger(__name__)

CONST_HEADER = {"content-type": "application/json; charset=UTF-8"}
CONST_HEADER_CLOSE = {**CONST_HEADER, "Connection": "close"}
CONST_TIMEOUT = ClientTimeout(total=4)
FULL_SOC_PERCENT = 100
SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"
SOC_LIMIT_BY_DEVICE_STATE = {
    DeviceState.SOCFULL: SocLimitState.FULL,
    DeviceState.SOCNEARLYFULL: SocLimitState.NEARLY_FULL,
    DeviceState.SOCEMPTY: SocLimitState.EMPTY,
    DeviceState.RESERVE_RECOVERY: SocLimitState.RESERVE_RECOVERY,
    DeviceState.SOCRESERVE: SocLimitState.RESERVE,
}


class ZendureBattery(EntityDevice):
    """Zendure Battery class for devices."""

    @staticmethod
    def get_battery_type(sn: str) -> tuple[str, str, float]:
        model = "???"
        match sn[0]:
            case "A":
                if sn[3] == "3":
                    model = "AIO2400"
                    kWh = 2.4
                else:
                    model = "AB1000"
                    kWh = 0.96
            case "B":
                model = "AB1000S"
                kWh = 0.96
            case "C":
                # External AB2000X and internal AB2000X of SF800+/SF800Pro/SF1600AC+
                # starting with CO4A. They are also described as additional battery
                # in the Zendure App, even when they are integrated into the device.
                model = "AB2000" + ("S" if sn[3] == "F" else "X" if sn[3] == "E" else "")
                kWh = 1.92
            case "F":
                model = "AB3000"
                kWh = 2.88
            case "G":
                model = "AB3000L"
                kWh = 2.88
            case "J":
                # JO2A => internal battery of SF2400AC pro
                # JO4A => internal battery of SF2400AC+
                model = "I2400"
                kWh = 2.4
            case _:
                model = "Unknown"
                kWh = 0.0

        name = f"{model} {sn[-5:]}".strip()
        return name, model, kWh

    def __init__(self, hass: HomeAssistant, sn: str, parent: EntityDevice) -> None:
        """Initialize Device."""
        name, model, self.kWh = ZendureBattery.get_battery_type(sn)
        super().__init__(hass, sn, name, model, "", sn, parent.deviceId)
        self.attr_device_info["serial_number"] = sn


class ZendureDevice(EntityDevice):
    """Zendure Device class for devices integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        deviceId: str,
        name: str,
        model: str,
        definition: dict[str, str],
        parent: str | None = None,
    ) -> None:
        """Initialize Device."""
        from .fusegroup import FuseGroup

        """Initialize Device."""
        self.prodkey = definition["productKey"]
        super().__init__(hass, deviceId, name, model, self.prodkey, definition["snNumber"], parent)
        self.snNumber = definition["snNumber"]
        self.definition = definition
        self.fuseGrp: FuseGroup

        self.mqtt: mqtt_client.Client | None = None
        self.zendure: mqtt_client.Client | None = None
        self.ipAddress = (
            definition.get("ip", "")
            if definition.get("ip", "") != ""
            else f"zendure-{definition['productModel'].replace(' ', '')}-{self.snNumber}.local"
        )

        self.topic_read = f"iot/{self.prodkey}/{self.deviceId}/properties/read"
        self.topic_write = f"iot/{self.prodkey}/{self.deviceId}/properties/write"
        self.topic_function = f"iot/{self.prodkey}/{self.deviceId}/function/invoke"

        self.batteries: dict[str, ZendureBattery | None] = {}
        self.lastseen = datetime.min
        self._messageid = 0
        self.kWh = 0.0

        self.charge_limit: int = 0
        self.charge_optimal: int = 0
        self.charge_start: int = 0
        self.charge_max_limit_cap: int = 0
        self.discharge_limit: int = 0
        self.discharge_optimal: int = 0
        self.discharge_start: int = 0
        self.maxSolar = 0
        self.pwr_max: int = 0
        self.pwr_produced: int = 0
        self.actualKwh: float = 0.0
        self.on_available_kwh_changed: Callable[[], None] | None = None
        self.discharge_recovery_margin_soc: float = 0.0
        self._discharge_recovery_active: bool = False
        self._raw_soc_limit: int = 0
        self.state: DeviceState = DeviceState.OFFLINE

        self.create_entities()
        self.initialize_derived_state()

    def initialize_derived_state(self) -> None:
        """Initialize derived entity state after entity creation completes."""
        self.refresh_discharge_state()

    @property
    def discharge_recovery_active(self) -> bool:
        """Return whether discharge recovery is currently blocking planned discharge."""
        return self._discharge_recovery_active

    @discharge_recovery_active.setter
    def discharge_recovery_active(self, value: bool) -> None:
        self._discharge_recovery_active = bool(value)

    def create_entities(self) -> None:
        """Create the device entities."""
        self.limitOutput = ZendureNumber(
            self, "outputLimit", self.entityWrite, None, "W", "power", self.discharge_limit, 0, NumberMode.SLIDER
        )
        self.limitInput = ZendureNumber(
            self, "inputLimit", self.entityWrite, None, "W", "power", self.charge_limit, 0, NumberMode.SLIDER
        )
        self.chargeMaxLimit = ZendureNumber(
            self, "chargeMaxLimit", self.entityWrite, None, "W", "power", self.charge_limit, 0, NumberMode.SLIDER
        )
        self.minSoc = ZendureNumber(self, "minSoc", self.entityWrite, None, "%", "soc", 100, 5, NumberMode.SLIDER, 10)
        self.socSet = ZendureNumber(self, "socSet", self.entityWrite, None, "%", "soc", 100, 70, NumberMode.SLIDER, 10)
        self.socReserve = ZendureRestoreNumber(
            self, "socReserve", self.refresh_recovery_state, None, "%", "soc", 100, 5, NumberMode.SLIDER, True
        )
        self.socStatus = ZendureSensor(self, "socStatus", state=0)
        self.socLimit = ZendureSensor(self, "socLimit", state=SocLimitState.NORMAL.value)
        self.deviceState = ZendureSensor(self, "state", state=self.state.value, translation_key="device_state")
        self.byPass = ZendureBinarySensor(self, "pass")

        fuseGroups = {
            0: "unused",
            1: "owncircuit",
            2: "group800",
            3: "group800_2400",
            4: "group1200",
            5: "group2000",
            6: "group2400",
            7: "group3600",
        }
        self.fuseGroup = ZendureRestoreSelect(self, "fuseGroup", fuseGroups, None)
        self.acMode = ZendureSelect(self, "acMode", {1: "input", 2: "output"}, self.entityWrite, 1)
        self.electricLevel = ZendureSensor(self, "electricLevel", None, "%", "battery", "measurement")
        self.homeInput = ZendureSensor(self, "gridInputPower", None, "W", "power", "measurement")
        self.solarInput = ZendureSensor(
            self, "solarInputPower", None, "W", "power", "measurement", icon="mdi:solar-panel"
        )
        self.batteryInput = ZendureSensor(self, "outputPackPower", None, "W", "power", "measurement")
        self.batteryOutput = ZendureSensor(self, "packInputPower", None, "W", "power", "measurement")
        self.homeOutput = ZendureSensor(self, "outputHomePower", None, "W", "power", "measurement")
        self.batInOut = ZendureSensor(self, "batInOut", None, "W", "power", "measurement", 0)
        self.heatState = ZendureBinarySensor(self, "heatState")
        self.hemsState = ZendureBinarySensor(self, "hemsState")
        self.hemsStateUpdated = datetime.min
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy_storage", None, 1)
        self.totalKwh = ZendureSensor(self, "total_kwh", None, "kWh", "energy_storage", None, 2)
        self.connectionStatus = ZendureSensor(self, "connectionStatus")
        self.connection: ZendureRestoreSelect
        self.bleAdapter: ZendureRestoreSelect | None = None
        self.remainingTime = ZendureSensor(self, "remainingTime", None, "h", "duration", "measurement")
        self.nextCalibration = ZendureRestoreSensor(self, "nextCalibration", None, None, "timestamp", None)
        self.lastHttpReport = ZendureRestoreSensor(self, "lastHttpReport", None, None, "timestamp", None)
        self.lastMqttReport = ZendureRestoreSensor(self, "lastMqttReport", None, None, "timestamp", None)

        self.aggrCharge = ZendureRestoreSensor(self, "aggrCharge", None, "kWh", "energy", "total_increasing", 2)
        self.aggrDischarge = ZendureRestoreSensor(self, "aggrDischarge", None, "kWh", "energy", "total_increasing", 2)
        self.aggrHomeInput = ZendureRestoreSensor(
            self, "aggrGridInputPower", None, "kWh", "energy", "total_increasing", 2
        )
        self.aggrHomeOut = ZendureRestoreSensor(self, "aggrOutputHome", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSolar = ZendureRestoreSensor(self, "aggrSolar", None, "kWh", "energy", "total_increasing", 2)
        self.aggrSwitchCount = ZendureRestoreSensor(self, "switchCount", None, None, None, "total_increasing", 0)

    def setLimits(self, charge: int, discharge: int) -> None:
        """Set the device limits."""
        try:
            self.charge_limit = charge
            self.charge_optimal = charge // 4
            self.charge_start = charge // 10
            if self.charge_max_limit_cap == 0:
                self.charge_max_limit_cap = abs(charge)
            self.limitInput.update_range(0, abs(charge))
            self.chargeMaxLimit.update_range(0, self.charge_max_limit_cap)

            self.discharge_limit = discharge
            self.discharge_optimal = discharge // 4
            self.discharge_start = discharge // 10
            self.limitOutput.update_range(0, discharge)
        except Exception:
            _LOGGER.error("SetLimits error %s %s %s!", self.name, charge, discharge)

    def setStatus(self) -> None:
        from .api import Api

        try:
            if self.lastseen == datetime.min:
                self.connectionStatus.update_value(0)
            elif self.socStatus.asInt == 1:
                self.connectionStatus.update_value(1)
            elif self.hemsState.is_on:
                self.connectionStatus.update_value(2)
            elif self.fuseGroup.value == 0:
                self.connectionStatus.update_value(3)
            elif self.connection.value == ConnectionMode.ZENSDK:
                self.connectionStatus.update_value(12)
            elif self.mqtt is not None and self.mqtt.host == Api.localServer:
                self.connectionStatus.update_value(11)
            else:
                self.connectionStatus.update_value(10)
        except Exception:
            self.connectionStatus.update_value(0)

    def entityUpdate(self, key: Any, value: Any) -> bool:
        # update entity state
        if key in {"remainOutTime", "remainInputTime"}:
            self.remainingTime.update_value(self.calcRemainingTime())
            return True
        if key == "state":
            self.update_device_state()
            return False

        previous_soc_limit = self.socLimit.asInt if key == "socLimit" and hasattr(self, "socLimit") else None
        if key == "socLimit":
            previous_raw_soc_limit = self._raw_soc_limit
            self._raw_soc_limit = self._coerce_soc_limit(value, previous_raw_soc_limit)
            changed = self._raw_soc_limit != previous_raw_soc_limit
        else:
            changed = super().entityUpdate(key, value)
        try:
            if changed or key == "socLimit":
                match key:
                    case "packState":
                        if value == 0:
                            self.aggrSwitchCount.update_value(1 + self.aggrSwitchCount.asNumber)
                    case "outputPackPower":
                        if not self.heatState.is_on:
                            self.aggrCharge.aggregate(dt_util.now(), value)
                        self.aggrDischarge.aggregate(dt_util.now(), 0)
                        self.batInOut.update_value(self.batteryOutput.asInt - self.batteryInput.asInt)
                    case "packInputPower":
                        self.aggrCharge.aggregate(dt_util.now(), 0)
                        self.aggrDischarge.aggregate(dt_util.now(), value)
                        self.batInOut.update_value(self.batteryOutput.asInt - self.batteryInput.asInt)
                    case "solarPower1" | "solarPower2" | "solarPower3" | "solarPower4" | "solarPower5" | "solarPower6":
                        pv_num = key[10:]
                        aggr_entity = self.entities.get(f"aggrSolar{pv_num}")
                        if isinstance(aggr_entity, ZendureRestoreSensor):
                            aggr_entity.aggregate(dt_util.now(), value)
                    case "solarInputPower":
                        self.aggrSolar.aggregate(dt_util.now(), value)
                    case "gridInputPower":
                        self.aggrHomeInput.aggregate(dt_util.now(), value)
                    case "outputHomePower":
                        self.aggrHomeOut.aggregate(dt_util.now(), value)
                    case "gridOffPower":
                        self.aggrOffGrid.aggregate(dt_util.now(), value)
                    case "inverseMaxPower":
                        self.setLimits(self.charge_limit, value)
                    case "chargeLimit" | "chargeMaxLimit":
                        self.setLimits(-value, self.discharge_limit)
                    case "hemsState" | "socStatus":
                        self.setStatus()
                        if key == "socStatus" and self.socStatus.asInt == 0:
                            self.nextCalibration.update_value(dt_util.now() + timedelta(days=30))
                    case "electricLevel" | "minSoc" | "socLimit" | "socReserve" | "socSet":
                        if changed and self.electricLevel.asInt == FULL_SOC_PERCENT:
                            self.nextCalibration.update_value(dt_util.now() + timedelta(days=30))
                        self.refresh_recovery_state()
        except Exception as e:
            _LOGGER.error("EntityUpdate error %s %s %s!", self.name, key, e)
            _LOGGER.error(traceback.format_exc())

        return changed or (previous_soc_limit is not None and self.socLimit.asInt != previous_soc_limit)

    @staticmethod
    def _coerce_soc_limit(value: Any, default: int = 0) -> int:
        """Return the raw device-reported SoC limit value."""
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _report_has_device_data(payload: Any) -> bool:
        """Return whether a report payload contains usable device data."""
        if not isinstance(payload, dict):
            return False
        if properties := payload.get("properties"):
            return len(properties) > 0
        if pack_data := payload.get("packData"):
            return len(pack_data) > 0
        return False

    def _mark_report_success(self, source: Literal["http", "mqtt"]) -> None:
        """Record the last successfully applied report timestamp for a source."""
        sensor = self.lastHttpReport if source == "http" else self.lastMqttReport
        sensor.update_value(dt_util.utcnow())

    def update_available_kwh(self, _entity: EntityZendure | None = None, _value: Any = None) -> None:
        """Refresh the available energy sensor from the current discharge baseline."""
        if not hasattr(self, "availableKwh") or not hasattr(self, "electricLevel") or not hasattr(self, "minSoc"):
            return

        baseline = self.available_discharge_baseline_soc(_entity, _value)
        self.availableKwh.update_value((self.electricLevel.asNumber - baseline) / 100 * self.kWh)

    def update_device_state(self, _entity: EntityZendure | None = None, _value: Any = None) -> None:
        """Refresh the cached runtime state from the current device values."""
        min_soc = self.minSoc.asNumber
        reserve = getattr(getattr(self, "socReserve", None), "asNumber", 0)
        # During restore, this callback can fire before self.socReserve assignment completes.
        if (
            _entity is not None
            and getattr(_entity, "translation_key", None) == "soc_reserve"
            and isinstance(_value, (int, float))
        ):
            reserve = _value
        baseline = self.available_discharge_baseline_soc(_entity, _value)
        level = self.electricLevel.asNumber

        if not self.online or self.socSet.asNumber == 0 or self.kWh == 0:
            self.state = DeviceState.OFFLINE
        elif self._raw_soc_limit == SocLimitState.FULL or self.electricLevel.asInt >= self.socSet.asNumber:
            self.state = DeviceState.SOCFULL
        elif self.taper_charge_limit is not None:
            self.state = DeviceState.SOCNEARLYFULL
        elif self._raw_soc_limit == SocLimitState.EMPTY or level <= min_soc:
            self.state = DeviceState.SOCEMPTY
        elif min_soc < level <= reserve:
            self.state = DeviceState.SOCRESERVE
        elif self.discharge_recovery_active and level < baseline:
            self.state = DeviceState.RESERVE_RECOVERY
        else:
            self.state = DeviceState.INACTIVE
        soc_limit_state = self._soc_limit_value_for_state()
        if soc_limit_state is not None:
            self.socLimit.update_value(soc_limit_state.value)
        self.deviceState.update_value(self.state.value)

    @property
    def taper_charge_limit(self) -> int | None:
        """Return the maximum charge rate in watts when near-full, or None if not tapering."""
        return None

    @property
    def effective_charge_limit(self) -> int:
        """Return the effective charge limit, respecting any near-full taper."""
        taper = self.taper_charge_limit
        if taper is not None:
            return max(self.charge_limit, -taper)
        return self.charge_limit

    def _soc_limit_value_for_state(self) -> SocLimitState | None:
        """Return the public SoC Limit sensor value for the effective device state."""
        if self.state is DeviceState.OFFLINE:
            return None
        return SOC_LIMIT_BY_DEVICE_STATE.get(self.state, SocLimitState.NORMAL)

    def refresh_discharge_state(self, _entity: EntityZendure | None = None, _value: Any = None) -> None:
        """Refresh all device-local state derived from the current discharge baseline."""
        previous_actual_kwh = self.actualKwh
        self.update_available_kwh(_entity, _value)
        if hasattr(self, "remainingTime"):
            self.remainingTime.update_value(self.calcRemainingTime(_entity, _value))
        self.update_device_state(_entity, _value)
        self.actualKwh = self.availableKwh.asNumber
        if previous_actual_kwh != self.actualKwh and self.on_available_kwh_changed is not None:
            self.on_available_kwh_changed()

    def discharge_floor_soc(self, _entity: EntityZendure | None = None, _value: Any = None) -> float:
        """Return the effective SoC floor for planned discharge."""
        reserve = getattr(getattr(self, "socReserve", None), "asNumber", 0)
        # During restore, this callback can fire before self.socReserve assignment completes.
        if (
            _entity is not None
            and getattr(_entity, "translation_key", None) == "soc_reserve"
            and isinstance(_value, (int, float))
        ):
            reserve = _value
        return max(self.minSoc.asNumber, reserve)

    def set_discharge_recovery_margin(self, margin: float) -> None:
        """Update the configured recovery margin and refresh dependent state."""
        self.discharge_recovery_margin_soc = max(0, margin)
        self.refresh_recovery_state(activate_margin_window=True)

    def refresh_recovery_state(
        self,
        _entity: EntityZendure | None = None,
        _value: Any = None,
        *,
        activate_margin_window: bool = False,
    ) -> None:
        """Refresh discharge recovery state and all dependent device-local values."""
        self.is_discharge_blocked(_entity, _value, activate_margin_window=activate_margin_window)

    def available_discharge_baseline_soc(self, _entity: EntityZendure | None = None, _value: Any = None) -> float:
        """Return the SoC baseline that counts as available for planned discharge."""
        baseline = self.discharge_floor_soc(_entity, _value)
        if self.discharge_recovery_active and self.discharge_recovery_margin_soc > 0:
            baseline = min(100, baseline + self.discharge_recovery_margin_soc)
        return baseline

    def is_discharge_blocked(
        self,
        _entity: EntityZendure | None = None,
        _value: Any = None,
        *,
        activate_margin_window: bool = False,
    ) -> bool:
        """Return whether planned battery discharge is currently blocked."""
        floor = self.discharge_floor_soc(_entity, _value)
        level = self.electricLevel.asNumber
        margin = max(0, self.discharge_recovery_margin_soc)
        recovery_active = self.discharge_recovery_active

        if activate_margin_window and margin > 0 and floor <= level < floor + margin:
            recovery_active = True

        if margin == 0:
            recovery_active = False
            blocked = level <= floor
        elif level <= floor:
            recovery_active = True
            blocked = True
        elif recovery_active:
            if level < floor + margin:
                blocked = True
            else:
                recovery_active = False
                blocked = False
        else:
            blocked = False

        self.discharge_recovery_active = recovery_active
        self.refresh_discharge_state(_entity, _value)
        return blocked

    def calcRemainingTime(self, _entity: EntityZendure | None = None, _value: Any = None) -> float:
        """Calculate the remaining time."""
        level = self.electricLevel.asInt
        power = self.batteryOutput.asInt - self.batteryInput.asInt

        if power == 0:
            return 0

        if power < 0:
            soc = self.socSet.asNumber
            return 0 if level >= soc else min(999, self.kWh * 10 / -power * (soc - level))

        soc = self.available_discharge_baseline_soc(_entity, _value)
        return 0 if level <= soc else min(999, self.kWh * 10 / power * (level - soc))

    async def entityWrite(self, entity: EntityZendure, value: Any) -> None:
        if entity.translation_key is None:
            _LOGGER.error("Entity %s has no translation_key, cannot write property %s", entity.name, self.name)
            return

        _LOGGER.info("Writing property %s %s => %s", self.name, entity.propertyName, value)
        self._messageid += 1
        payload = json.dumps(
            {
                "deviceId": self.deviceId,
                "messageId": self._messageid,
                "timestamp": int(datetime.now().timestamp()),
                "properties": {entity.propertyName: value},
            },
            default=lambda o: o.__dict__,
        )
        if self.mqtt is not None:
            self.mqtt.publish(self.topic_write, payload)

    async def button_press(self, _key: str) -> None:
        return

    def mqttPublish(self, topic: str, command: Any, client: mqtt_client.Client | None = None) -> None:
        command["messageId"] = self._messageid
        command["deviceId"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        payload = json.dumps(command, default=lambda o: o.__dict__)

        if client is not None:
            client.publish(topic, payload)
        elif self.mqtt is not None:
            self.mqtt.publish(topic, payload)

    def mqttInvoke(self, command: Any) -> None:
        self._messageid += 1
        command["messageId"] = self._messageid
        command["deviceKey"] = self.deviceId
        command["timestamp"] = int(datetime.now().timestamp())
        self.mqttPublish(self.topic_function, command)

    async def mqttProperties(self, payload: Any, source: Literal["http", "mqtt"] | None = None) -> bool:
        report_applied = self._report_has_device_data(payload)

        if report_applied:
            was_offline = self.lastseen == datetime.min
            self.lastseen = datetime.now() + timedelta(minutes=5)
            if was_offline:
                self.setStatus()

        if isinstance(payload, dict) and (properties := payload.get("properties", None)) and len(properties) > 0:
            for key, value in properties.items():
                self.entityUpdate(key, value)

        # update the battery properties
        if isinstance(payload, dict) and (batprops := payload.get("packData", None)):
            for b in batprops:
                if (sn := b.get("sn", None)) is None:
                    continue

                if (bat := self.batteries.get(sn, None)) is None:
                    self.batteries[sn] = ZendureBattery(self.hass, sn, self)

                elif bat and b:
                    for key, value in b.items():
                        if key != "sn":
                            bat.entityUpdate(key, value)

            # Recalculate total capacity after every packData update
            # (covers both new batteries and potential pack changes)
            self.kWh = sum(0 if b is None else b.kWh for b in self.batteries.values())
            self.totalKwh.update_value(self.kWh)
            self.refresh_discharge_state()

        if report_applied and source is not None:
            self._mark_report_success(source)

        return report_applied

    def mqttMessage(self, topic: str, payload: Any) -> bool:
        try:
            match topic:
                case "properties/report":
                    asyncio.run_coroutine_threadsafe(self.mqttProperties(payload, "mqtt"), self.hass.loop)

                case "register/replay":
                    _LOGGER.info("Register replay for %s => %s", self.name, payload)
                    if self.mqtt is not None:
                        self.mqtt.publish(f"iot/{self.prodkey}/{self.deviceId}/register/replay", None, 1, True)

                case "time-sync":
                    return True

                case "properties/energy":
                    self.hemsState.update_value(1)
                    self.hemsStateUpdated = datetime.now()
                    self.setStatus()
                    return True

                case "event/device" | "event/error":
                    return True

                case (
                    "properties/read"
                    | "function/invoke/reply"
                    | "properties/read/reply"
                    | "config"
                    | "log"
                    | "function/invoke"
                ):
                    return False

                case _:
                    return False
        except Exception as err:
            _LOGGER.error(err)

        return True

    async def mqttSelect(self, _select: ZendureRestoreSelect, _value: Any) -> None:
        from .api import Api

        self.mqtt = None
        Api.update_cloud_mqtt_state()
        if self.lastseen != datetime.min:
            if self.connection.value == ConnectionMode.CLOUD:
                await self.bleMqtt(Api.mqttCloud)
            elif self.connection.value == ConnectionMode.LOCAL:
                await self.bleMqtt(Api.mqttLocal)

        _LOGGER.debug("Mqtt selected %s", self.name)

    @property
    def bleMac(self) -> str | None:
        if (conn := self.attr_device_info.get("connections", None)) is not None:
            for connection_type, mac_address in conn:
                if connection_type == "bluetooth":
                    return mac_address
        return None

    @staticmethod
    def _scanner_source(scanner_device: Any) -> str | None:
        """Extract scanner source identifier from a BluetoothScannerDevice-like object."""
        source = getattr(scanner_device, "source", None)
        if source:
            return str(source)

        if scanner := getattr(scanner_device, "scanner", None):
            source = getattr(scanner, "source", None)
            if source:
                return str(source)

        if service_info := getattr(scanner_device, "service_info", None):
            source = getattr(service_info, "source", None)
            if source:
                return str(source)

        return None

    @staticmethod
    def _scanner_ble_device(scanner_device: Any) -> Any | None:
        """Extract BLEDevice from a BluetoothScannerDevice-like object."""
        device = getattr(scanner_device, "ble_device", None)
        if device is not None:
            return device

        device = getattr(scanner_device, "device", None)
        if device is not None:
            return device

        if service_info := getattr(scanner_device, "service_info", None):
            device = getattr(service_info, "device", None)
            if device is not None:
                return device

        return None

    def ble_sources(self) -> list[str]:
        """Get available Bluetooth source identifiers from Home Assistant."""
        sources: set[str] = set()
        ble_mac = self.bleMac

        # Prefer scanner sources for this specific device.
        try:
            if ble_mac and (scanner_devices_by_address := getattr(bluetooth, "async_scanner_devices_by_address", None)):
                for scanner_device in scanner_devices_by_address(self.hass, ble_mac, True):
                    if source := self._scanner_source(scanner_device):
                        sources.add(source)
        except Exception as err:
            _LOGGER.debug("Could not read bluetooth scanner sources for %s: %s", self.name, err)

        # Fallback: derive sources from all discovered connectable advertisements.
        try:
            if discovered_service_info := getattr(bluetooth, "async_discovered_service_info", None):
                for info in discovered_service_info(self.hass, True):
                    if source := getattr(info, "source", None):
                        sources.add(str(source))
        except Exception as err:
            _LOGGER.debug("Could not derive bluetooth sources for %s: %s", self.name, err)

        return sorted(sources)

    def ble_device_from_source(self, ble_mac: str, source: str) -> Any | None:
        """Return a BLEDevice for an address constrained to a specific scanner source."""
        if scanner_devices_by_address := getattr(bluetooth, "async_scanner_devices_by_address", None):
            try:
                for scanner_device in scanner_devices_by_address(self.hass, ble_mac, True):
                    if self._scanner_source(scanner_device) != source:
                        continue
                    if device := self._scanner_ble_device(scanner_device):
                        return device
            except Exception as err:
                _LOGGER.debug("Could not get BLE device for %s on source %s: %s", self.name, source, err)

        return None

    def ble_adapter_options(self) -> dict[int, str]:
        """Build selectable BLE adapter/source options for this device."""
        return {0: "auto", **dict(enumerate(self.ble_sources(), start=1))}

    def selected_ble_source(self) -> str | None:
        """Return configured BLE source for this device or None for auto selection."""
        if self.bleAdapter is None:
            return None

        self.bleAdapter.setDict(self.ble_adapter_options())
        source = self.bleAdapter.current_option
        return None if source in (None, "", "auto") else str(source)

    async def bleMqtt(self, mqtt: mqtt_client.Client) -> bool:
        """Set the MQTT server for the device via BLE."""
        from .api import Api

        msg: str | None = None
        try:
            if Api.wifipsw == "" or Api.wifissid == "":
                msg = "No WiFi credentials or connections found"
                return False

            if (ble_mac := self.bleMac) is None:
                msg = "No BLE MAC address available"
                return False

            # get the bluetooth device
            ble_source = self.selected_ble_source()
            device = None
            if ble_source is not None:
                device = self.ble_device_from_source(ble_mac, ble_source)

            if device is None:
                device = bluetooth.async_ble_device_from_address(self.hass, ble_mac, True)

            if device is None:
                msg = f"BLE device {ble_mac} not found"
                if ble_source is not None:
                    msg += f" on source {ble_source}"
                return False

            try:
                _LOGGER.info("Set mqtt %s to %s", self.name, mqtt.host)
                if establish_connection is not None:
                    client = await establish_connection(BleakClient, device, self.name)
                else:
                    client = BleakClient(device)
                    await client.connect()

                try:
                    await self.bleCommand(
                        client,
                        {
                            "iotUrl": mqtt.host,
                            "messageId": 1002,
                            "method": "token",
                            "password": Api.wifipsw,
                            "ssid": Api.wifissid,
                            "timeZone": "GMT+01:00",
                            "token": "abcdefgh",
                        },
                    )

                    await self.bleCommand(
                        client,
                        {
                            "messageId": 1003,
                            "method": "station",
                        },
                    )
                finally:
                    # Ensure stale BLE sessions do not leak if command execution fails unexpectedly.
                    if client.is_connected:
                        await client.disconnect()
            except TimeoutError:
                msg = "Timeout when trying to connect to the BLE device"
                _LOGGER.warning(msg)
            except (AttributeError, BleakError) as err:
                msg = f"Could not connect to {self.name}: {err}"
                _LOGGER.warning(msg)
            except Exception as err:
                msg = f"BLE error: {err}"
                _LOGGER.warning(msg)
            else:
                self.mqtt = mqtt
                if self.zendure is not None:
                    self.zendure.loop_stop()
                    self.zendure.disconnect()
                    self.zendure = None

                self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, self.mqtt)
                self.setStatus()

                return True
            return False

        finally:
            if msg is not None:
                msg = f"Error setting the MQTT server on {self.name} to {mqtt.host}, {msg}"
            else:
                msg = f"Changing the MQTT server on {self.name} to {mqtt.host} was successful"

            persistent_notification.async_create(self.hass, (msg), "Zendure", "zendure_ha")

            _LOGGER.info("BLE update ready")

    async def bleCommand(self, client: BleakClient, command: Any) -> None:
        try:
            self._messageid += 1
            payload = json.dumps(command, default=lambda o: o.__dict__)
            b = bytearray()
            b.extend(map(ord, payload))
            _LOGGER.info("BLE command: %s => %s", self.name, payload)
            await client.write_gatt_char(SF_COMMAND_CHAR, b, response=False)
        except Exception as err:
            _LOGGER.warning("BLE error: %s", err)

    async def power_get(self) -> bool:
        if self.lastseen < datetime.now():
            self.lastseen = datetime.min
            self.setStatus()

        self.actualKwh = self.availableKwh.asNumber
        self.update_device_state()
        return self.state != DeviceState.OFFLINE

    async def charge(self, _power: int) -> int:
        """Set the power output/input."""
        return 0

    async def power_charge(self, power: int) -> int:
        """Set charge power."""
        power = min(0, max(power, self.effective_charge_limit))
        if abs(power - self.homeInput.asInt + self.homeOutput.asInt) <= SmartMode.POWER_TOLERANCE:
            _LOGGER.info("Power charge %s => no action [power %s]", self.name, power)
            return power
        return await self.charge(power)

    async def discharge(self, _power: int) -> int:
        """Set the power output/input."""
        return 0

    async def power_discharge(self, power: int) -> int:
        """Set discharge power."""
        power = max(0, min(power, self.discharge_limit))
        if abs(power - self.homeOutput.asInt + self.homeInput.asInt) <= SmartMode.POWER_TOLERANCE:
            _LOGGER.info("Power discharge %s => no action [power %s]", self.name, power)
            return self.homeOutput.asInt
        return await self.discharge(power)

    async def power_off(self) -> None:
        """Set the power off."""

    @property
    def supports_bypass(self) -> bool:
        """Return whether the device supports explicit bypass mode."""
        return False

    @property
    def can_bypass(self) -> bool:
        """Return whether the device can enter bypass right now."""
        return self.supports_bypass and self.state == DeviceState.SOCFULL

    async def power_bypass(self) -> int:
        """Put the device into explicit bypass mode."""
        await self.power_off()
        return 0

    @property
    def online(self) -> bool:
        """Check if device is online."""
        return self.connectionStatus.asInt >= SmartMode.CONNECTED

    @property
    def pwr_offgrid(self) -> int:
        """Get the offgrid power."""
        return 0


class ZendureLegacy(ZendureDevice):
    """Zendure Legacy class for devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        deviceId: str,
        name: str,
        model: str,
        definition: dict[str, str],
        parent: str | None = None,
    ) -> None:
        """Initialize Device."""
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.connection = ZendureRestoreSelect(
            self,
            "connection",
            {ConnectionMode.CLOUD: "cloud", ConnectionMode.LOCAL: "local"},
            self.mqttSelect,
            ConnectionMode.CLOUD,
        )
        self.mqttReset = ZendureButton(self, "mqttReset", self.button_press)
        self.bleAdapter = ZendureRestoreSelect(self, "bleAdapter", self.ble_adapter_options(), self.bleAdapterSelect, 0)

    async def bleAdapterSelect(self, _select: ZendureRestoreSelect, _value: Any) -> None:
        # Refresh available sources whenever selection changes or is restored.
        if self.bleAdapter is not None:
            self.bleAdapter.setDict(self.ble_adapter_options())

    async def button_press(self, button: ZendureButton) -> None:
        from .api import Api

        match button.translation_key:
            case "mqtt_reset":
                _LOGGER.info("Resetting MQTT for %s", self.name)
                await self.bleMqtt(Api.mqttCloud if self.connection.value == ConnectionMode.CLOUD else Api.mqttLocal)

    async def dataRefresh(self, _update_count: int) -> None:
        """Refresh the device data."""
        from .api import Api

        if self.lastseen != datetime.min:
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, self.mqtt)
        else:
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, Api.mqttCloud)
            self.mqttPublish(self.topic_read, {"properties": ["getAll"]}, Api.mqttLocal)

    def mqttMessage(self, topic: str, payload: Any) -> bool:
        if topic == "register/replay":
            _LOGGER.info("Register replay for %s => %s", self.name, payload)
            return True

        return super().mqttMessage(topic, payload)


class ZendureZenSdk(ZendureDevice):
    """Zendure Zen SDK class for devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        deviceId: str,
        name: str,
        model: str,
        definition: dict[str, str],
        parent: str | None = None,
    ) -> None:
        """Initialize Device."""
        self.session = async_get_clientsession(hass, verify_ssl=False)
        self._http_failures = 0
        self._http_block_until = datetime.min
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.connection = ZendureRestoreSelect(
            self,
            "connection",
            {ConnectionMode.CLOUD: "cloud", ConnectionMode.ZENSDK: "zenSDK"},
            self.mqttSelect,
            ConnectionMode.CLOUD,
        )
        self.httpid = 0

    async def mqttSelect(self, select: Any, _value: Any) -> None:
        from .api import Api

        self.mqtt = None
        Api.update_cloud_mqtt_state()
        match select.value:
            case ConnectionMode.CLOUD:
                Api.mqttCloud.unsubscribe(f"/{self.prodkey}/{self.deviceId}/#")
                Api.mqttCloud.unsubscribe(f"iot/{self.prodkey}/{self.deviceId}/#")

            case ConnectionMode.ZENSDK:
                Api.mqttCloud.unsubscribe(f"/{self.prodkey}/{self.deviceId}/#")
                Api.mqttCloud.unsubscribe(f"iot/{self.prodkey}/{self.deviceId}/#")

        _LOGGER.debug("Mqtt selected %s", self.name)

    async def entityWrite(self, entity: EntityZendure, value: Any) -> None:
        if entity.translation_key is None:
            _LOGGER.error("Entity %s has no translation_key, cannot write property %s", entity.name, self.name)
            return

        if self.online and self.connection.value == ConnectionMode.CLOUD:
            await super().entityWrite(entity, value)
        else:
            _LOGGER.info("Writing property %s %s => %s", self.name, entity.propertyName, value)
            await self.httpPost("properties/write", {"properties": {entity.propertyName: value}})

    async def dataRefresh(self, update_count: int) -> None:
        if self.connection.value == ConnectionMode.ZENSDK or (update_count == 0 and not self.online):
            await self._refresh_http_report()

    async def power_get(self) -> bool:
        """Get the current power."""
        if self.connection.value != ConnectionMode.CLOUD:
            await self._refresh_http_report()

        return await super().power_get()

    async def _refresh_http_report(self) -> bool:
        """Fetch the latest HTTP report and apply it as an HTTP-sourced update."""
        json = await self.httpGet("properties/report")
        return await self.mqttProperties(json, "http")

    async def charge(self, power: int, _off: bool = False) -> int:
        """Set charge power."""
        _LOGGER.info("Power charge %s => %s", self.name, power)
        if (
            power == -SmartMode.POWER_START
            and self.limitInput.asInt == -SmartMode.POWER_START
            and self.homeInput.asInt == 0
        ):
            power -= 10
        await self.doCommand(
            {
                "properties": {
                    "smartMode": 0 if power == 0 and self.pwr_offgrid == 0 else 1,
                    "acMode": 1,
                    "outputLimit": 0,
                    "inputLimit": -power,
                }
            }
        )
        return power

    async def discharge(self, power: int) -> int:
        _LOGGER.info("Power discharge %s => %s", self.name, power)
        if (
            power == SmartMode.POWER_START
            and self.limitOutput.asInt == SmartMode.POWER_START
            and self.homeOutput.asInt == 0
        ):
            power += 10
        await self.doCommand(
            {
                "properties": {
                    "smartMode": 0 if power == 0 and self.pwr_offgrid == 0 else 1,
                    "acMode": 2,
                    "outputLimit": power,
                    "inputLimit": 0,
                }
            }
        )
        return power

    async def power_off(self) -> None:
        """Set the power off."""
        await self.doCommand(
            {
                "properties": {
                    "smartMode": 0 if self.pwr_offgrid == 0 else 1,
                    "acMode": 2,
                    "outputLimit": 0,
                    "inputLimit": 0,
                }
            }
        )

    async def doCommand(self, command: Any) -> None:
        if self.connection.value != ConnectionMode.CLOUD:
            await self.httpPost("properties/write", command)
        else:
            self.mqttPublish(self.topic_write, command, self.mqtt)

    async def httpGet(self, url: str, key: str | None = None) -> dict[str, Any]:
        if datetime.now() < self._http_block_until:
            return {}
        try:
            url = f"http://{self.ipAddress}/{url}"
            headers = CONST_HEADER_CLOSE if self._http_failures > 0 else CONST_HEADER
            response = await self.session.get(url, headers=headers, timeout=CONST_TIMEOUT)
            response.raise_for_status()
            payload = json.loads(await response.text())
            self.lastseen = datetime.now()
            self._http_failures = 0
            self._http_block_until = datetime.min
            return payload if key is None else payload.get(key, {})
        except Exception as e:
            self._http_failures += 1
            if self._http_failures >= 3:
                delay = min(80, 5 * (2 ** min(4, self._http_failures - 3)))
                self._http_block_until = datetime.now() + timedelta(seconds=delay)
                self.lastseen = datetime.min
            log = (
                _LOGGER.info
                if isinstance(e, (TimeoutError, asyncio.TimeoutError, ServerDisconnectedError))
                else _LOGGER.error
            )
            log(
                "%s for %s during httpGet%s [failures=%s, retry_after=%s]",
                type(e).__name__,
                self.name,
                f": {e}" if str(e) else "!",
                self._http_failures,
                self._http_block_until if self._http_block_until != datetime.min else "immediate",
            )
        return {}

    async def httpPost(self, url: str, command: Any) -> bool:
        if datetime.now() < self._http_block_until:
            return False
        try:
            self.httpid += 1
            command["id"] = self.httpid
            command["sn"] = self.snNumber
            url = f"http://{self.ipAddress}/{url}"
            headers = CONST_HEADER_CLOSE if self._http_failures > 0 else CONST_HEADER
            response = await self.session.post(url, json=command, headers=headers, timeout=CONST_TIMEOUT)
            response.raise_for_status()
            self._http_failures = 0
            self._http_block_until = datetime.min
        except Exception as e:
            self._http_failures += 1
            if self._http_failures >= 3:
                delay = min(80, 5 * (2 ** min(4, self._http_failures - 3)))
                self._http_block_until = datetime.now() + timedelta(seconds=delay)
                self.lastseen = datetime.min
            log = (
                _LOGGER.info
                if isinstance(e, (TimeoutError, asyncio.TimeoutError, ServerDisconnectedError))
                else _LOGGER.error
            )
            log(
                "%s for %s during httpPost%s [failures=%s, retry_after=%s]",
                type(e).__name__,
                self.name,
                f": {e}" if str(e) else "!",
                self._http_failures,
                self._http_block_until if self._http_block_until != datetime.min else "immediate",
            )
            return False
        return True


class ZendureZenSDKWithLocalMQTT(ZendureZenSdk):
    """Hybrid mode: local MQTT for reads/writes with ZenSDK HTTP fallback."""

    _http_only_entities: set[str] = {"Fanmode", "Fanspeed", "chargeMaxLimit"}
    _mqtt_select_mappings: dict[str, dict[int, str]] = {
        "gridOffMode": {0: "Normal mode", 1: "Economic mode", 2: "OFF"},
        "acMode": {1: "Input mode", 2: "Output mode"},
        "gridReverse": {1: "Allow backflow", 2: "Disallow backflow"},
    }

    def __init__(
        self,
        hass: HomeAssistant,
        deviceId: str,
        name: str,
        model: str,
        definition: dict[str, str],
        parent: str | None = None,
    ) -> None:
        """Initialize the hybrid local-MQTT device variant."""
        # Initialize before super().__init__ because restore callbacks may invoke mqttSelect early.
        self._mqtt_subscribed = False
        self._mqtt_entities_received: set[str] = set()
        super().__init__(hass, deviceId, name, model, definition, parent)
        self.connection.setDict(
            {
                ConnectionMode.CLOUD: "cloud",
                ConnectionMode.ZENSDK: "zenSDK",
                ConnectionMode.ZENSDK_WITH_LOCAL_MQTT: "localMqtt+zenSDK",
            }
        )

    async def mqttSelect(self, select: Any, value: Any) -> None:
        from .api import Api

        self._mqtt_entities_received.clear()
        Api.update_cloud_mqtt_state()
        if select.value == ConnectionMode.ZENSDK_WITH_LOCAL_MQTT:
            Api.mqttCloud.unsubscribe(f"/{self.prodkey}/{self.deviceId}/#")
            Api.mqttCloud.unsubscribe(f"iot/{self.prodkey}/{self.deviceId}/#")
            self.mqtt = None
            if Api.mqttLocal.is_connected() and not self._mqtt_subscribed:
                Api.mqttLocal.subscribe("Zendure/+/+/+")
                self._mqtt_subscribed = True
        else:
            self._mqtt_subscribed = False
            await super().mqttSelect(select, value)

    async def entityWrite(self, entity: EntityZendure, value: Any) -> None:
        from .api import Api

        if entity.translation_key is None:
            _LOGGER.error(
                "Entity %s has no translation_key, cannot write property %s",
                entity.name,
                self.name,
            )
            return

        property_name = camelcase(entity.translation_key)

        if (
            self.connection.value == ConnectionMode.ZENSDK_WITH_LOCAL_MQTT
            and property_name not in self._http_only_entities
        ):
            if isinstance(entity, ZendureSwitch):
                entity_type = "switch"
            elif isinstance(entity, ZendureSelect):
                entity_type = "select"
                mapping = self._mqtt_select_mappings.get(property_name)
                if mapping and value in mapping:
                    value = mapping[value]
            elif isinstance(entity, ZendureNumber):
                entity_type = "number"
                value = int(value) // entity.factor
            else:
                entity_type = "sensor"

            topic = f"Zendure/{entity_type}/{self.snNumber}/{property_name}/set"
            mqtt_value = self._format_mqtt_value(entity_type, value)
            Api.mqttLocal.publish(topic, mqtt_value)
            _LOGGER.info("Writing via MQTT %s %s => %s", self.name, topic, mqtt_value)
            return

        await super().entityWrite(entity, value)

    def localMqttMessage(self, entity_type: str, entity_name: str, value: str) -> tuple[bool, bool]:
        try:
            if entity_name.endswith(("/availability", "/set")):
                return True, False

            parsed_value = self._parse_mqtt_value(entity_type, value)
            if entity_name in {"socStatus", "socState", "packState"} and isinstance(parsed_value, str):
                parsed_value = {
                    "idle": 0,
                    "standby": 0,
                    "charging": 1,
                    "discharging": 2,
                }.get(parsed_value.strip().lower(), parsed_value)
            if isinstance(entity := self.entities.get(entity_name), ZendureNumber):
                parsed_value = parsed_value * entity.factor
            elif (spec := self.createEntity.get(entity_name)) is not None and (
                spec if isinstance(spec, str) else spec[0]
            ) == "°C":
                # MQTT sends temperature already in °C; reverse the Kelvin template so it round-trips correctly
                parsed_value = float(parsed_value) * 10 + 2731
            self._mqtt_entities_received.add(entity_name)
            self.entityUpdate(entity_name, parsed_value)
            self.lastseen = datetime.now() + timedelta(minutes=5)
            self.setStatus()
        except Exception as e:
            _LOGGER.error(
                "Error handling local MQTT message for %s: %s=%s, error: %s",
                self.name,
                entity_name,
                value,
                e,
            )
            return False, False
        else:
            return True, True

    def handleLocalMqttMessage(self, client: Any, entity_type: str, entity_name: str, value: str) -> None:
        """Handle local MQTT entity updates on Home Assistant loop."""
        handled, report_applied = self.localMqttMessage(entity_type, entity_name, value)
        if report_applied:
            self._mark_report_success("mqtt")
        if handled and self.mqtt != client:
            self.mqtt = client
            self.setStatus()

    def localMqttBatteryMessage(self, battery_id: str, entity_name: str, value: str) -> bool:
        try:
            if entity_name.endswith(("/availability", "/set")):
                return False

            actual_entity_name = entity_name
            if entity_name.startswith(f"{battery_id}_"):
                actual_entity_name = entity_name[len(battery_id) + 1 :]

            if (battery := self.batteries.get(battery_id, None)) is None:
                self.batteries[battery_id] = ZendureBattery(self.hass, battery_id, self)
                self.kWh = sum(0 if b is None else b.kWh for b in self.batteries.values())
                self.refresh_discharge_state()
                battery = self.batteries[battery_id]

            if battery is not None:
                parsed_value = self._parse_mqtt_value("sensor", value)
                if actual_entity_name == "state" and isinstance(parsed_value, str):
                    parsed_value = {
                        "standby": 0,
                        "charging": 1,
                        "discharging": 2,
                    }.get(parsed_value.strip().lower(), parsed_value)
                battery.entityUpdate(actual_entity_name, parsed_value)
        except Exception as e:
            _LOGGER.error(
                "Error handling local MQTT battery message: %s/%s=%s, error: %s",
                battery_id,
                entity_name,
                value,
                e,
            )
            return False
        else:
            return True

    def handleLocalMqttBatteryMessage(self, battery_id: str, entity_name: str, value: str) -> None:
        """Handle local MQTT battery updates on Home Assistant loop."""
        if self.localMqttBatteryMessage(battery_id, entity_name, value):
            self._mark_report_success("mqtt")

    def _parse_mqtt_value(self, entity_type: str, value: str) -> Any:
        if entity_type == "switch":
            return 1 if value.upper() in ("ON", "1", "TRUE", "YES") else 0
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            return value

    def _format_mqtt_value(self, entity_type: str, value: Any) -> str:
        if entity_type == "switch":
            return "ON" if value in (1, True, "1", "on", "ON") else "OFF"
        return str(value)

    async def dataRefresh(self, update_count: int) -> None:
        if self.connection.value == ConnectionMode.ZENSDK_WITH_LOCAL_MQTT:
            await self._refresh_http_report()
            return
        await super().dataRefresh(update_count)

    async def power_get(self) -> bool:
        if self.connection.value == ConnectionMode.ZENSDK_WITH_LOCAL_MQTT:
            if not self._mqtt_entities_received:
                await self._refresh_http_report()
        elif self.connection.value != ConnectionMode.CLOUD:
            await self._refresh_http_report()

        return await ZendureDevice.power_get(self)

    async def doCommand(self, command: Any) -> None:
        from .api import Api

        if self.connection.value != ConnectionMode.ZENSDK_WITH_LOCAL_MQTT:
            await super().doCommand(command)
            return

        props = command.get("properties")
        if not props:
            await self.httpPost("properties/write", command)
            return

        http_props: dict[str, Any] = {}
        for prop_name, prop_value in props.items():
            if prop_name in self._http_only_entities:
                http_props[prop_name] = prop_value
                continue

            entity = self.entities.get(prop_name)
            if isinstance(entity, ZendureSwitch):
                entity_type = "switch"
            elif isinstance(entity, ZendureSelect):
                entity_type = "select"
            else:
                entity_type = "number"

            if entity_type == "select":
                mapping = self._mqtt_select_mappings.get(prop_name)
                if mapping and prop_value in mapping:
                    prop_value = mapping[prop_value]

            topic = f"Zendure/{entity_type}/{self.snNumber}/{prop_name}/set"
            mqtt_value = self._format_mqtt_value(entity_type, prop_value)
            Api.mqttLocal.publish(topic, mqtt_value)

        if http_props:
            await self.httpPost("properties/write", {"properties": http_props})


@dataclass
class DeviceSettings:
    device_id: str
    fuseGroup: str
    limitCharge: int
    limitDischarge: int
    maxSolar: int
    kWh: float = 0.0
    socSet: float = 100
    minSoc: float = 0
