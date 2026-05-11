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
from enum import Enum
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
from .switch import ZendureSwitch

SCAN_INTERVAL = timedelta(seconds=60)
PRIMARY_DEVICE_DISABLED = "__disabled__"

_LOGGER = logging.getLogger(__name__)

EMPTY_SOC_STATES = {
    DeviceState.SOCEMPTY,
    DeviceState.SOCRESERVE,
}

PV_CHARGE_FIRST_STATES = {
    DeviceState.SOCEMPTY,
}

PV_CHARGE_FIRST_OPERATIONS = {
    ManagerMode.MATCHING,
    ManagerMode.MATCHING_CHARGE,
}

P1_CHARGE_LAG_FAST_DEVIATION = 20
CHARGE_HOLDOFF_IDLE_SECONDS = 2
CHARGE_HOLDOFF_RECENT_SECONDS = 60
CHARGE_HOLDOFF_RECENT_WINDOW_SECONDS = 300

P1_CHARGE_LAG_FAST_OPERATIONS = {
    ManagerMode.MATCHING,
    ManagerMode.MATCHING_CHARGE,
}

LOW_SOC_STATES = {
    *EMPTY_SOC_STATES,
    DeviceState.RESERVE_RECOVERY,
}

type ZendureConfigEntry = ConfigEntry[ZendureManager]


class _OutputClamp(Enum):
    """How a manager mode may route power into home output."""

    NONE = "none"
    PRODUCED_ONLY = "produced_only"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class _RoutingPolicy:
    """
    Per-mode routing clamps applied after setpoint shaping.

    A charge path is device input. Home output is the inverter output into the
    house: FULL may use battery and PV, PRODUCED_ONLY may use only current PV or
    off-grid production, and NONE forces the output floor to zero.
    """

    charge_allowed: bool
    output_clamp: _OutputClamp
    selected_primary_charge: bool
    selected_primary_output: bool
    strict_output_stop: bool = False
    zero_uses_charge_path: bool = False


@dataclass(frozen=True, slots=True)
class _PowerRoutingIntent:
    """
    Per-cycle input and home-output intent after mode clamps are applied.

    Input is device charging. Home output is power commanded to the house and
    may be PV-only or battery-backed depending on the output clamp.
    """

    # Signed charging target. Negative watts mean device input; zero when not using the input path.
    input_budget: int
    # Non-negative home-output target after mode output clamps.
    home_output_budget: int
    # True when this cycle must use the input/charge executor, including zero-charge hold modes.
    route_input: bool
    # True when home output may use only current PV/off-grid production, not battery power.
    produced_only_output: bool
    # True when switching to input must stop current home output even if bypass is active.
    strict_home_output_stop: bool
    # True when charging should use the selected-primary executor ordering.
    selected_primary_input: bool
    # True when home output should use the selected-primary executor ordering.
    selected_primary_home_output: bool


DEFAULT_ROUTING_POLICY = _RoutingPolicy(
    charge_allowed=False,
    output_clamp=_OutputClamp.NONE,
    selected_primary_charge=False,
    selected_primary_output=False,
)

ROUTING_POLICIES = {
    ManagerMode.MATCHING: _RoutingPolicy(
        charge_allowed=True,
        output_clamp=_OutputClamp.FULL,
        selected_primary_charge=True,
        selected_primary_output=True,
    ),
    ManagerMode.MATCHING_DISCHARGE: _RoutingPolicy(
        charge_allowed=False,
        output_clamp=_OutputClamp.FULL,
        selected_primary_charge=False,
        selected_primary_output=True,
    ),
    ManagerMode.MATCHING_CHARGE: _RoutingPolicy(
        charge_allowed=True,
        output_clamp=_OutputClamp.PRODUCED_ONLY,
        selected_primary_charge=True,
        selected_primary_output=True,
        zero_uses_charge_path=True,
    ),
    ManagerMode.STORE_SOLAR: _RoutingPolicy(
        charge_allowed=True,
        output_clamp=_OutputClamp.NONE,
        selected_primary_charge=True,
        selected_primary_output=False,
        strict_output_stop=True,
        zero_uses_charge_path=True,
    ),
    ManagerMode.MANUAL: _RoutingPolicy(
        charge_allowed=True,
        output_clamp=_OutputClamp.FULL,
        selected_primary_charge=True,
        selected_primary_output=True,
        zero_uses_charge_path=True,
    ),
    ManagerMode.OFF: DEFAULT_ROUTING_POLICY,
}


def _pv_evidence_for_charge_replacement(device: ZendureDevice) -> int:
    """Return PV evidence safe for reducing selected-primary charge."""
    return max(0, device.solarInput.asInt, -device.pwr_produced)


def _pv_evidence_for_output_replacement(device: ZendureDevice) -> int:
    """Return explicit PV evidence safe for increasing selected-primary output."""
    return max(0, device.solarInput.asInt)


@dataclass(frozen=True, slots=True)
class _PowerRoutingDevice:
    """
    Per-cycle routing facts for one device.

    A produced floor is PV/off-grid power already serving home output. A
    charge floor is power already flowing into a device and should be reduced
    before switching direction. Charge surplus is local PV left for the same
    device's battery after current home output is covered.
    """

    device: ZendureDevice
    # Whether this cycle should send low-SOC PV to charging before preserving home output.
    pv_charge_first: bool
    # Whether polling classified the device as currently serving home output.
    discharging: bool
    # Production-backed output limit allowed for this device in this cycle.
    produced_limit: int
    # Portion of current home output already covered by PV/off-grid production.
    produced_home: int
    # Current device input that should be reduced before switching direction.
    charge_floor: int
    # Local production left for this device's own battery after current home output.
    charge_surplus: int
    # Explicit bypass production already passing through to home.
    bypass_passthrough: int
    # Discharge capacity available after device, fusegroup, and primary-aware limits.
    available_discharge: int
    # Discharge capacity including production that can output even when battery discharge is blocked.
    available_discharge_with_produced: int

    @property
    def active_produced_home(self) -> int:
        """Return current produced power that should remain serving home output."""
        if not self.discharging or self.device.state == DeviceState.SOCFULL:
            return 0
        if self.pv_charge_first and self.device.state in PV_CHARGE_FIRST_STATES:
            return 0
        return self.produced_home

    @property
    def home_output_is_only_produced(self) -> bool:
        """Return whether current home output is fully production-backed."""
        home_output = max(0, self.device.homeOutput.asInt)
        return home_output > 0 and self.produced_home >= home_output


@dataclass(frozen=True, slots=True)
class _PvFloorSummary:
    """Grouped PV floor facts used before a cycle is allowed to switch direction."""

    active_primary_produced_floor: int
    active_non_primary_produced_floor: int
    active_serving_pv_floor: int
    replaceable_non_primary_serving_pv: int
    uncovered_chargeable_serving_pv_floor: int


@dataclass(frozen=True, slots=True)
class _LocalChargeSummary:
    """Grouped local-PV charge facts shared by setpoint shaping and charge execution."""

    active_charge_local_surplus: int
    active_non_primary_local_surplus: int
    idle_non_primary_local_surplus: int
    active_pv_charge_first_home: int
    active_non_primary_empty_chargeable: int
    non_primary_local_chargeable_surplus: int
    selected_primary_local_surplus: int


@dataclass(frozen=True, slots=True)
class _PowerRoutingSnapshot:
    """
    Per-cycle routing view shared by primary-aware manager paths.

    Replaceable secondary PV is secondary home-serving PV that may move to the
    secondary battery because selected-primary PV can cover that home load.
    Uncovered floor is home-serving PV that cannot be replaced yet and blocks
    charge-mode debounce.
    """

    selected_primary: ZendureDevice | None
    primary_aware: bool
    charge_devices: tuple[ZendureDevice, ...]
    discharge_devices: tuple[ZendureDevice, ...]
    idle_devices: tuple[ZendureDevice, ...]
    devices: dict[ZendureDevice, _PowerRoutingDevice]

    def route(self, device: ZendureDevice) -> _PowerRoutingDevice:
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
        return min(route.produced_home, -route.device.effective_charge_limit)

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
    def selected_primary_output_replacement_capacity(self) -> int:
        """Return extra selected-primary PV output that can replace non-primary home PV."""
        if not self.primary_aware or self.selected_primary is None:
            return 0
        if self.selected_primary not in self.discharge_devices:
            return 0

        route = self.route(self.selected_primary)
        device = route.device
        if not device.online or device.state in {DeviceState.OFFLINE, DeviceState.SOCFULL}:
            return 0

        pv_evidence = _pv_evidence_for_output_replacement(device)
        available_output = min(pv_evidence, route.available_discharge_with_produced)
        return max(0, available_output - route.produced_home)

    def pv_floor_summary(self) -> _PvFloorSummary:
        """Return floor and replacement facts for one routing cycle."""
        if self.selected_primary is not None and self.selected_primary in self.discharge_devices:
            active_primary = self.route(self.selected_primary).active_produced_home
        else:
            active_primary = 0
        active_non_primary = sum(
            self.route(device).active_produced_home
            for device in self.discharge_devices
            if device is not self.selected_primary
        )
        active_serving = active_primary + active_non_primary
        primary_charge_replacement = 0
        if (
            self.primary_aware
            and self.selected_primary is not None
            and self.selected_primary in self.charge_devices
        ):
            route = self.route(self.selected_primary)
            device = route.device
            if (
                device.online
                and device.state not in {DeviceState.OFFLINE, DeviceState.SOCFULL}
                and device.effective_charge_limit < 0
            ):
                pv_evidence = _pv_evidence_for_charge_replacement(device)
                primary_charge_replacement = min(route.charge_floor, pv_evidence, -device.effective_charge_limit)
        replaceable_non_primary = min(
            active_non_primary,
            primary_charge_replacement + self.selected_primary_output_replacement_capacity,
        )
        return _PvFloorSummary(
            active_primary_produced_floor=active_primary,
            active_non_primary_produced_floor=active_non_primary,
            active_serving_pv_floor=active_serving,
            replaceable_non_primary_serving_pv=replaceable_non_primary,
            uncovered_chargeable_serving_pv_floor=max(0, active_serving - replaceable_non_primary),
        )

    def local_charge_summary(
        self,
        selected_charge_primary: ZendureDevice | None,
        *,
        pv_charge_first_mode: bool,
        include_charge_first_home: bool,
    ) -> _LocalChargeSummary:
        """Return local-PV charge facts using the same policy for all primary-aware paths."""
        active_charge_local_surplus = sum(
            self.charge_surplus(device) for device in self.charge_devices if device is not self.selected_primary
        )
        active_non_primary_local_surplus = sum(
            self.charge_surplus(device) for device in self.discharge_devices if device is not self.selected_primary
        )
        idle_non_primary_local_surplus = sum(
            self.charge_surplus(device) for device in self.idle_devices if device is not self.selected_primary
        )
        active_pv_charge_first_home = sum(
            self.chargeable_produced_home(device)
            for device in self.discharge_devices
            if pv_charge_first_mode and device.state in PV_CHARGE_FIRST_STATES
        )
        active_non_primary_empty_chargeable = (
            sum(
                self.chargeable_produced_home(device)
                for device in self.discharge_devices
                if (
                    pv_charge_first_mode
                    and include_charge_first_home
                    and device is not self.selected_primary
                    and device.state in PV_CHARGE_FIRST_STATES
                )
            )
            if include_charge_first_home
            else 0
        )
        non_primary_local_chargeable_surplus = (
            active_charge_local_surplus
            + active_non_primary_local_surplus
            + idle_non_primary_local_surplus
            + active_non_primary_empty_chargeable
        )
        selected_primary_local_surplus = (
            self.charge_surplus(selected_charge_primary) if selected_charge_primary is not None else 0
        )
        return _LocalChargeSummary(
            active_charge_local_surplus=active_charge_local_surplus,
            active_non_primary_local_surplus=active_non_primary_local_surplus,
            idle_non_primary_local_surplus=idle_non_primary_local_surplus,
            active_pv_charge_first_home=active_pv_charge_first_home,
            active_non_primary_empty_chargeable=active_non_primary_empty_chargeable,
            non_primary_local_chargeable_surplus=non_primary_local_chargeable_surplus,
            selected_primary_local_surplus=selected_primary_local_surplus,
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
        return setpoint < 0 and active_charge_floor_total > 0 and active_charge_floor_total + setpoint > 0

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
    """
    Coordinate Zendure devices and route P1 meter changes into power commands.

    P1 sensor events enter through _p1_changed(), which parses the
    HomeAssistant event, delegates to _route_p1_update(), and returns whether
    routing actually ran. _route_p1_update() writes simulation rows, applies the
    spike filter, fast-delay debounce, and fast-change checks, then owns the
    prepare/execute lifecycle when a route should be handled. Primary selection
    changes also use _route_p1_update(force=True) so they share simulation and
    reset/restore handling while bypassing event suppression.

    _route_p1_update() resets per-cycle state, calls
    _poll_devices_and_prepare_routing_state(), and passes that setpoint to
    _prepare_power_routing(). _prepare_power_routing() reads _routing_policy(),
    checks _selected_primary_routing_enabled(), builds _power_routing_snapshot(),
    shapes the signed setpoint in _shape_primary_aware_setpoint(), applies
    _clamp_setpoint_for_routing_policy(), and turns the result into
    _power_routing_intent(). _route_p1_update() then calls _execute_power_routing() and
    restores debounce timing after the cycle. _execute_power_routing() dispatches
    to _apply_standard_input(), _apply_primary_input(), _apply_standard_home_output(),
    or _apply_primary_home_output().
    """

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
        self.p1meterEvent: Callable[[], None] | None = None
        self.p1_history: deque[int] = deque([25, -25], maxlen=8)
        self.p1_factor = 1
        self.p1_charge_lag_last_update = datetime.min
        self.p1_spike_baseline = 0
        self.p1_spike_started: datetime | None = None
        self.update_count = 0

        self.charge: list[ZendureDevice] = []
        self.charge_time = datetime.max
        self.charge_last = datetime.min
        self.charge_debounce_since: datetime | None = None

        self.discharge: list[ZendureDevice] = []
        self.discharge_bypass = 0
        self.idle: list[ZendureDevice] = []
        self.produced = 0
        self.pwr_low = 0

    def _reset_power_distribution_state(self) -> None:
        """Reset per-cycle distribution state before computing a new routing pass."""
        self.zero_fast = datetime.max
        self.charge.clear()
        self.discharge.clear()
        self.discharge_bypass = 0
        self.idle.clear()
        self.produced = 0
        for fg in self.fuseGroups:
            fg.initPower = True

    def _restore_p1_update_timing(self, time: datetime | None = None) -> None:
        """Restore P1 debounce timers after a routing update."""
        time = datetime.now() if time is None else time
        self.zero_next = time + timedelta(seconds=SmartMode.TIMEZERO)
        self.zero_fast = time + timedelta(seconds=SmartMode.TIMEFAST)

    def _update_spike_filter(self, entity: ZendureSwitch, value: int) -> None:
        """Update the automation-controlled spike filter switch."""
        entity.update_value(value)
        self.p1_spike_started = None

    def _spike_filter_settings(self) -> tuple[bool, int, timedelta]:
        """Return whether spike filtering is active, plus its threshold and duration."""
        spike_filter = getattr(self, "spike_filter", None)
        enabled = bool(getattr(spike_filter, "is_on", False))
        threshold = int(getattr(getattr(self, "spike_filter_threshold", None), "asNumber", 0))
        duration_seconds = float(getattr(getattr(self, "spike_filter_duration", None), "asNumber", 0))
        duration = timedelta(seconds=max(0.0, duration_seconds))
        return enabled and threshold > 0 and duration > timedelta(0), threshold, duration

    def _p1_history_average(self) -> int:
        """Return the current recent P1 baseline."""
        return int(sum(self.p1_history) / len(self.p1_history)) if self.p1_history else 0

    def _is_p1_spike_increase(self, p1: int, time: datetime) -> bool:
        """Return whether a short upward P1 increase should still be suppressed as a spike."""
        enabled, threshold, duration = self._spike_filter_settings()
        if not enabled:
            self.p1_spike_started = None
            return False

        if self.p1_spike_started is not None:
            if p1 - self.p1_spike_baseline >= threshold:
                if time - self.p1_spike_started < duration:
                    _LOGGER.debug(
                        "P1 spike suppressed: p1=%sW baseline=%sW threshold=%sW duration=%s",
                        p1,
                        self.p1_spike_baseline,
                        threshold,
                        duration,
                    )
                    return True
                _LOGGER.debug(
                    "P1 spike released after duration: p1=%sW baseline=%sW threshold=%sW duration=%s",
                    p1,
                    self.p1_spike_baseline,
                    threshold,
                    duration,
                )
                self.p1_spike_started = None
                return False

            _LOGGER.debug(
                "P1 spike ended before duration: p1=%sW baseline=%sW threshold=%sW",
                p1,
                self.p1_spike_baseline,
                threshold,
            )
            self.p1_spike_started = None
            return False

        baseline = self._p1_history_average()
        if p1 - baseline >= threshold:
            self.p1_spike_baseline = baseline
            self.p1_spike_started = time
            _LOGGER.debug(
                "P1 spike candidate started: p1=%sW baseline=%sW threshold=%sW duration=%s",
                p1,
                baseline,
                threshold,
                duration,
            )
            return True

        return False

    def _should_fast_track_charge_lag_p1(self, p1: int, time: datetime) -> bool:
        """Return whether P1 should bypass the normal debounce for charge-lag correction."""
        if (
            abs(p1) <= P1_CHARGE_LAG_FAST_DEVIATION
            or time - self.p1_charge_lag_last_update < SmartMode.P1_MIN_UPDATE
            or self.operation not in P1_CHARGE_LAG_FAST_OPERATIONS
            or not self._selected_primary_routing_enabled()
        ):
            return False
        if any(device.reports_active_pv_charge() for device in self.devices):
            return True
        if any(device.reports_pv() for device in self.charge):
            return True

        return any(device.reports_full_bypass_pv() for device in self.devices) and any(
            device.online and device.state not in {DeviceState.OFFLINE, DeviceState.SOCFULL} and device.charge_limit < 0
            for device in self.devices
        )

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
        self.spike_filter = ZendureSwitch(self, "spike_filter", self._update_spike_filter, value=False)
        self.spike_filter_threshold = ZendureRestoreNumber(
            self,
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
        self.spike_filter_duration = ZendureRestoreNumber(
            self,
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
        self.availableKwh = ZendureSensor(self, "available_kwh", None, "kWh", "energy_storage", None, 1)
        self.totalAvailableKwh = ZendureSensor(self, "total_available_kwh", None, "kWh", "energy_storage", None, 1)
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
            device.on_available_kwh_changed = self.refresh_energy_kwh
        self._refresh_discharge_recovery_margin(None, None)
        self.refresh_energy_kwh()
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
        if not self._operation_supports_selected_primary() or self.config_entry is None:
            return

        p1meter = self.config_entry.data.get(CONF_P1METER, "sensor.power_actual")
        if (state := self.hass.states.get(p1meter)) is None:
            return

        try:
            p1 = int(self.p1_factor * float(state.state))
        except (TypeError, ValueError):
            return

        await self._route_p1_update(p1, datetime.now(), force=True, raise_on_error=True)

    def refresh_primary_device_options(self) -> None:
        """Refresh the selectable primary device list."""
        if not hasattr(self, "primarydevice"):
            return

        options = {PRIMARY_DEVICE_DISABLED: "none"}
        for device in sorted(self.devices, key=lambda dev: dev.name):
            options[device.deviceId] = device.name
        self.primarydevice.setDict(options)

    def _selected_primary_device(self, charging: bool | None = None) -> ZendureDevice | None:
        """Return the selected primary device, optionally filtered by routing direction."""
        device = None
        if hasattr(self, "primarydevice"):
            device_id = self.primarydevice.value
            if device_id not in (None, PRIMARY_DEVICE_DISABLED):
                device = next((candidate for candidate in self.devices if candidate.deviceId == device_id), None)

        if charging is None or device is None:
            return device

        if not device.online or not self._operation_supports_selected_primary():
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
        self.refresh_energy_kwh()

    def refresh_energy_kwh(self) -> None:
        """Refresh all manager energy aggregates derived from device availability."""
        self.availableKwh.update_value(
            sum(device.available_kwh_contribution() for device in self.devices if device.state != DeviceState.OFFLINE)
        )
        self.totalAvailableKwh.update_value(sum(device.available_kwh_contribution() for device in self.devices))

    def _available_discharge_power(
        self,
        device: ZendureDevice,
        *,
        primary_aware: bool = False,
        allow_produced_only: bool = False,
    ) -> int:
        """Return the currently available discharge contribution for a device."""
        if not device.is_discharge_capable():
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
        return min(0, max(device.effective_charge_limit, device.fuseGrp.minpower + other_input))

    def _primary_discharge_limit(self, device: ZendureDevice) -> int:
        """Return the maximum discharge power for the primary within its fusegroup."""
        other_output = sum(max(0, other.homeOutput.asInt) for other in device.fuseGrp.devices if other is not device)
        return max(0, min(device.discharge_limit, device.fuseGrp.maxpower - other_output))

    def _apply_charge_holdoff(self, setpoint: int, time: datetime, *, allow_charge: bool) -> int:
        """Apply the anti-oscillation charge holdoff and return the allowed setpoint."""
        if self.charge_time <= time:
            return setpoint

        if self.charge_time == datetime.max:
            recent_charge = (time - self.charge_last).total_seconds() <= CHARGE_HOLDOFF_RECENT_WINDOW_SECONDS
            delay = CHARGE_HOLDOFF_RECENT_SECONDS if recent_charge else CHARGE_HOLDOFF_IDLE_SECONDS
            self.charge_time = time + timedelta(seconds=delay)
            self.charge_last = self.charge_time
            self.pwr_low = 0

        return setpoint if allow_charge else 0

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
            limit += device.fuseGrp.charge_limit(device, devices)
            optimal += device.charge_optimal
            weight += device.pwr_max * (100 - device.electricLevel.asInt)
        return limit, optimal, weight

    def _apply_first_device_hysteresis(self, pwr: int, device: ZendureDevice, *, discharging: bool) -> int:
        """Apply first-device startup smoothing hysteresis and return the (possibly zeroed) power."""
        if discharging:
            delta = device.discharge_start * 1.5 - pwr
            if delta <= 0:
                self.pwr_low = 0
            else:
                self.pwr_low += int(delta)
            return 0 if self.pwr_low > device.discharge_optimal else pwr
        delta = device.charge_start * 1.5 - pwr
        if delta >= 0:
            self.pwr_low = 0
        else:
            self.pwr_low += int(-delta)
        return 0 if self.pwr_low < device.charge_optimal else pwr

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

    async def _command_home_output(self, device: ZendureDevice, power: int, *, allow_bypass_zero: bool = False) -> int:
        """Command home output, optionally using bypass as the zero-output command."""
        if power == 0 and allow_bypass_zero and getattr(device, "byPass", None) is not None and device.byPass.is_on:
            return 0
        if power == 0 and allow_bypass_zero and device.can_bypass:
            return await device.power_bypass()
        return await device.power_discharge(power)

    async def _stop_home_output_for_input(self, device: ZendureDevice, *, allow_bypass_zero: bool) -> None:
        """Stop home output before input, optionally using bypass as the zero-output command."""
        # pwr_offgrid devices may otherwise import from grid when stopped exactly at zero.
        if device.pwr_offgrid == 0:
            await self._command_home_output(device, 0, allow_bypass_zero=allow_bypass_zero)
        else:
            await device.power_discharge(-10)

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
                pwr = self._apply_first_device_hysteresis(pwr, d, discharging=True)

            targets[d] += pwr
            setpoint -= pwr
            dev_start += 1 if pwr != 0 and d.electricLevel.asInt + 3 < idle_lvlmax else 0

        return dev_start

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
                    await self._command_home_output(d, SmartMode.POWER_START, allow_bypass_zero=True)
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
        self.refresh_energy_kwh()

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

    def _p1_value_from_event(self, event: Event[EventStateChangedData]) -> int | None:
        """Return the P1 watt value from a HomeAssistant state-change event."""
        if not self.hass.is_running or (new_state := event.data["new_state"]) is None:
            return None

        try:
            return int(self.p1_factor * float(new_state.state))
        except (TypeError, ValueError):
            return None

    def _is_fast_p1_change(self, p1: int) -> bool:
        """Return whether the new P1 value is a fast change without mutating history."""
        if len(self.p1_history) <= 1:
            return False

        avg = self._p1_history_average()
        stddev = SmartMode.P1_STDDEV_FACTOR * max(
            SmartMode.P1_STDDEV_MIN, sqrt(sum([pow(i - avg, 2) for i in self.p1_history]) / len(self.p1_history))
        )
        return abs(p1 - avg) > stddev or abs(p1 - self.p1_history[0]) > stddev

    def _record_p1_history(self, p1: int, *, reset: bool = False) -> None:
        """Append a P1 value to recent history, optionally starting a new window."""
        if reset:
            self.p1_history.clear()
        self.p1_history.append(p1)

    async def _p1_changed(self, event: Event[EventStateChangedData]) -> bool:
        """Parse a P1 sensor update and return whether it triggered routing."""
        if (p1 := self._p1_value_from_event(event)) is None:
            return False
        return await self._route_p1_update(p1, datetime.now())

    async def _route_p1_update(
        self,
        p1: int,
        time: datetime,
        *,
        force: bool = False,
        raise_on_error: bool = False,
    ) -> bool:
        """
        Run the high-level P1 routing pipeline and return whether routing ran.

        Normal P1 events pass through suppression and debounce first. Forced
        updates reuse the same simulation and routing lifecycle but bypass those
        filters; this is used when a primary selection change must immediately
        re-evaluate the current P1 value.
        """
        if ZendureManager.simulation:
            self.writeSimulation(time, p1)

        should_route = force
        charge_lag_fast = False

        if not force:
            if self._is_p1_spike_increase(p1, time):
                return False

            charge_lag_fast = self._should_fast_track_charge_lag_p1(p1, time)

            # Check for fast delay
            if time < self.zero_fast and not charge_lag_fast:
                _LOGGER.debug("P1 update suppressed by fast-delay (zero_fast=%s)", self.zero_fast)
                self._record_p1_history(p1)
                return False

            fast_change = self._is_fast_p1_change(p1)
            self._record_p1_history(p1, reset=fast_change)
            # Check minimal time between updates aka debounce.
            should_route = fast_change or charge_lag_fast or time > self.zero_next

        if not should_route:
            return False

        try:
            self._reset_power_distribution_state()
            setpoint = await self._poll_devices_and_prepare_routing_state(p1)
            intent, routing, setpoint = self._prepare_power_routing(p1, time, setpoint)
            _LOGGER.info("P1 ======> p1:%s, setpoint:%sW stored:%sW", p1, setpoint, self.produced)
            await self._execute_power_routing(intent, time, routing)
        except Exception as err:
            if raise_on_error:
                raise
            _LOGGER.error(err)
            _LOGGER.error(traceback.format_exc())
            return False
        finally:
            time = datetime.now()
            if charge_lag_fast:
                self.p1_charge_lag_last_update = time
            self._restore_p1_update_timing(time)
        return True

    def _prepare_power_routing(
        self,
        p1: int,
        time: datetime,
        setpoint: int,
    ) -> tuple[_PowerRoutingIntent, _PowerRoutingSnapshot, int]:
        """Prepare one routing cycle from a polled P1 routing setpoint."""
        policy = self._routing_policy()
        selected_primary_routing = self._selected_primary_routing_enabled()
        pv_charge_first_mode = selected_primary_routing and self.operation in PV_CHARGE_FIRST_OPERATIONS
        selected_primary = self._selected_primary_device() if selected_primary_routing else None
        routing = self._power_routing_snapshot(
            selected_primary,
            primary_aware=selected_primary_routing,
            pv_charge_first=pv_charge_first_mode,
        )
        selected_charge_primary = self._selected_primary_device(charging=True)
        pv_floors = routing.pv_floor_summary()
        local_charge = routing.local_charge_summary(
            selected_charge_primary,
            pv_charge_first_mode=pv_charge_first_mode,
            include_charge_first_home=True,
        )
        setpoint = self._shape_primary_aware_setpoint(
            p1,
            setpoint,
            time,
            routing,
            pv_floors,
            local_charge,
            pv_charge_first_mode=pv_charge_first_mode,
        )
        if self.operation == ManagerMode.MANUAL:
            setpoint = int(self.manualpower.asNumber)

        requested_setpoint = setpoint
        setpoint, produced_only = self._clamp_setpoint_for_routing_policy(setpoint, policy)
        intent = self._power_routing_intent(
            requested_setpoint,
            setpoint,
            policy,
            selected_primary_routing=selected_primary_routing,
            produced_only=produced_only,
        )

        return intent, routing, setpoint

    async def _poll_devices_and_prepare_routing_state(self, p1: int) -> int:
        """Poll devices, classify active flows, and return the adjusted setpoint."""
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
                    setpoint += home
                # Low-SOC states cannot discharge the battery, but can still
                # feed into the home using solar power or off-grid power.
                elif (home := d.homeOutput.asInt) > 0:
                    self.discharge.append(d)
                    self.discharge_bypass -= d.pwr_produced if d.state == DeviceState.SOCFULL else 0
                    setpoint += home

                else:
                    self.idle.append(d)

                power += d.pwr_offgrid + home + d.pwr_produced

        # Update the power entities
        self.power.update_value(power)
        self.refresh_energy_kwh()
        return setpoint

    def _routing_policy(self) -> _RoutingPolicy:
        """Return the mode-level input and home-output clamps for this cycle."""
        return ROUTING_POLICIES.get(self.operation, DEFAULT_ROUTING_POLICY)

    def _operation_supports_selected_primary(self) -> bool:
        """Return whether the active mode has any selected-primary route branch."""
        policy = self._routing_policy()
        return policy.selected_primary_charge or policy.selected_primary_output

    def _selected_primary_routing_enabled(self) -> bool:
        """Return whether selected-primary routing is active for this cycle."""
        return self._operation_supports_selected_primary() and self._selected_primary_device() is not None

    def _power_routing_snapshot(
        self,
        selected_primary: ZendureDevice | None,
        *,
        primary_aware: bool,
        pv_charge_first: bool = False,
    ) -> _PowerRoutingSnapshot:
        """
        Build immutable per-device route facts from the current classification.

        The snapshot is the shared source for produced floors, charge floors,
        local charge surplus, bypass passthrough, and primary-aware discharge
        capacity so shaping and execution do not recompute those facts.
        """
        devices = [*self.charge, *self.discharge, *self.idle]
        if selected_primary is not None and selected_primary not in devices:
            devices.append(selected_primary)

        routing_devices: dict[ZendureDevice, _PowerRoutingDevice] = {}
        for device in devices:
            produced_limit = self._current_produced_output_limit(device, primary_aware=primary_aware)
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

            routing_devices[device] = _PowerRoutingDevice(
                device=device,
                pv_charge_first=pv_charge_first,
                discharging=device in self.discharge,
                produced_limit=produced_limit,
                produced_home=produced_home,
                charge_floor=max(0, device.homeInput.asInt - max(0, device.pwr_offgrid)),
                charge_surplus=device.current_charge_surplus_limit(),
                bypass_passthrough=bypass_passthrough,
                available_discharge=self._available_discharge_power(device, primary_aware=primary_aware),
                available_discharge_with_produced=self._available_discharge_power(
                    device, primary_aware=primary_aware, allow_produced_only=True
                ),
            )

        return _PowerRoutingSnapshot(
            selected_primary=selected_primary,
            primary_aware=primary_aware,
            charge_devices=tuple(self.charge),
            discharge_devices=tuple(self.discharge),
            idle_devices=tuple(self.idle),
            devices=routing_devices,
        )

    def _shape_primary_aware_setpoint(
        self,
        p1: int,
        setpoint: int,
        time: datetime,
        routing: _PowerRoutingSnapshot,
        pv_floors: _PvFloorSummary,
        local_charge: _LocalChargeSummary,
        *,
        pv_charge_first_mode: bool,
    ) -> int:
        """
        Shape the signed setpoint before manager-mode clamps are applied.

        This keeps already-serving PV on home output where required, moves safe
        local surplus into device input, and debounces direction changes that
        would otherwise zero an active produced floor too early.
        """
        selected_primary = routing.selected_primary
        matching_primary_aware = routing.primary_aware and self.operation == ManagerMode.MATCHING
        selected_primary_bypass_passthrough = routing.selected_primary_bypass_passthrough
        self.discharge_bypass += selected_primary_bypass_passthrough
        if p1 > 0 and self.charge and routing.preserves_produced_floor:
            self.discharge_bypass += max(
                0,
                pv_floors.active_serving_pv_floor - selected_primary_bypass_passthrough,
            )

        # discharge_bypass is already-served produced power. Removing it from
        # the setpoint prevents discharging battery for load that PV has covered.
        gross_discharge_setpoint = setpoint
        if self.discharge_bypass > 0:
            net_setpoint = setpoint - self.discharge_bypass
            setpoint = max(0, net_setpoint) if p1 > 0 and not self.charge else net_setpoint
            if (
                matching_primary_aware
                and p1 > 0
                and self.charge
                and pv_floors.active_serving_pv_floor > 0
                and setpoint >= 0
            ):
                setpoint = max(setpoint, gross_discharge_setpoint)
        discharge_candidate_setpoint = setpoint

        extra_surplus = self.produced - self.discharge_bypass
        charge_transition_would_zero = self.charge_time > time
        protects_selected_primary_floor = (
            matching_primary_aware
            and selected_primary is not None
            and pv_floors.active_primary_produced_floor > 0
            and p1 > -pv_floors.active_primary_produced_floor
        )
        primary_keeps_local_surplus = (
            local_charge.selected_primary_local_surplus > 0
            and selected_primary is not None
            and not selected_primary.is_discharge_blocked()
            and (pv_floors.active_primary_produced_floor == 0 or not charge_transition_would_zero)
            and pv_floors.active_non_primary_produced_floor == 0
        )
        if p1 <= 0 and extra_surplus > 0:
            surplus_setpoint = setpoint - extra_surplus
            if (
                setpoint <= 0
                or local_charge.active_charge_local_surplus > 0
                or local_charge.active_non_primary_local_surplus > 0
                or (protects_selected_primary_floor and local_charge.idle_non_primary_local_surplus > 0)
                or (p1 < 0 and local_charge.active_non_primary_empty_chargeable > 0)
                or (primary_keeps_local_surplus and surplus_setpoint < -SmartMode.POWER_START)
            ):
                local_chargeable_surplus = (
                    local_charge.non_primary_local_chargeable_surplus + local_charge.selected_primary_local_surplus
                )
                if protects_selected_primary_floor:
                    requested_charge = max(0, -surplus_setpoint)
                    uncovered_primary_floor = max(
                        0,
                        pv_floors.active_primary_produced_floor - max(0, discharge_candidate_setpoint),
                    )
                    charge_without_primary_floor = max(
                        local_chargeable_surplus,
                        requested_charge - uncovered_primary_floor,
                    )
                    setpoint = (
                        -charge_without_primary_floor
                        if charge_without_primary_floor > 0
                        else max(0, discharge_candidate_setpoint, pv_floors.active_serving_pv_floor)
                    )
                else:
                    setpoint = surplus_setpoint

        if matching_primary_aware and p1 <= 0 and pv_floors.replaceable_non_primary_serving_pv > 0:
            setpoint = min(
                setpoint,
                -(local_charge.non_primary_local_chargeable_surplus + pv_floors.replaceable_non_primary_serving_pv),
            )

        if pv_charge_first_mode and local_charge.active_pv_charge_first_home > 0:
            setpoint = min(setpoint, -local_charge.active_pv_charge_first_home)

        positive_demand_charge_lag = p1 > 0 and routing.positive_demand_charge_lag(setpoint)
        non_primary_preservable_charge = (
            local_charge.non_primary_local_chargeable_surplus + pv_floors.replaceable_non_primary_serving_pv
        )
        charge_uses_only_non_primary_local_surplus = (
            matching_primary_aware
            and p1 <= 0
            and (p1 < 0 or pv_floors.replaceable_non_primary_serving_pv > 0)
            and non_primary_preservable_charge > 0
            and setpoint < 0
            and -setpoint <= non_primary_preservable_charge
        )
        debounce_charge_flip = (
            matching_primary_aware
            and pv_floors.uncovered_chargeable_serving_pv_floor > 0
            and charge_transition_would_zero
            and setpoint < 0
            and not positive_demand_charge_lag
            and not charge_uses_only_non_primary_local_surplus
        )
        if debounce_charge_flip:
            if self.charge_debounce_since is None:
                self.charge_debounce_since = time
            if time - self.charge_debounce_since < timedelta(seconds=SmartMode.TIMEZERO):
                setpoint = max(0, discharge_candidate_setpoint, pv_floors.active_serving_pv_floor)
        else:
            self.charge_debounce_since = None

        return setpoint

    def _clamp_setpoint_for_routing_policy(self, setpoint: int, policy: _RoutingPolicy) -> tuple[int, bool]:
        """Apply manager-mode input/output permissions and identify PV-only output."""
        if setpoint < 0:
            return (setpoint if policy.charge_allowed else 0), False
        if setpoint == 0:
            return 0, False

        match policy.output_clamp:
            case _OutputClamp.FULL:
                return setpoint, False
            case _OutputClamp.PRODUCED_ONLY:
                return (min(self.produced, setpoint) if self.produced > SmartMode.POWER_START else 0), True
            case _OutputClamp.NONE:
                return 0, False
            case _:
                return 0, False

    def _power_routing_intent(
        self,
        requested_setpoint: int,
        setpoint: int,
        policy: _RoutingPolicy,
        *,
        selected_primary_routing: bool,
        produced_only: bool,
    ) -> _PowerRoutingIntent:
        """Build the compact input-or-home-output decision consumed by the executor."""
        route_input = setpoint < 0 or (
            setpoint == 0 and policy.zero_uses_charge_path and requested_setpoint <= 0 and policy.charge_allowed
        )
        return _PowerRoutingIntent(
            input_budget=setpoint if route_input else 0,
            home_output_budget=max(0, setpoint),
            route_input=route_input,
            produced_only_output=produced_only,
            strict_home_output_stop=policy.strict_output_stop,
            selected_primary_input=selected_primary_routing and policy.selected_primary_charge,
            selected_primary_home_output=selected_primary_routing and policy.selected_primary_output,
        )

    async def _execute_power_routing(
        self,
        intent: _PowerRoutingIntent,
        time: datetime,
        routing: _PowerRoutingSnapshot,
    ) -> None:
        """Dispatch the routing intent to the one matching input or home-output executor."""
        if self.operation == ManagerMode.OFF:
            self.operationstate.update_value(ManagerState.OFF.value)
            return

        if intent.route_input:
            if intent.selected_primary_input:
                await self._apply_primary_input(
                    intent.input_budget,
                    time,
                    routing,
                    strict_output_stop=intent.strict_home_output_stop,
                )
            else:
                await self._apply_standard_input(
                    intent.input_budget,
                    time,
                    strict_output_stop=intent.strict_home_output_stop,
                )
            return

        if intent.selected_primary_home_output:
            await self._apply_primary_home_output(
                intent.home_output_budget,
                routing,
                produced_only=intent.produced_only_output,
            )
        else:
            await self._apply_standard_home_output(
                intent.home_output_budget,
                routing,
                produced_only=intent.produced_only_output,
            )

    async def _apply_standard_input(self, setpoint: int, time: datetime, *, strict_output_stop: bool = False) -> None:
        """
        Apply an input budget without selected-primary ordering.

        Existing home output is stopped as required, charge holdoff is applied,
        and the remaining negative setpoint is allocated across active and
        promotable idle charge candidates.
        """
        _LOGGER.info("Input => setpoint %sW", setpoint)

        # stop discharging devices
        for d in self.discharge:
            # avoid stopping bypassing devices
            if not strict_output_stop and d.byPass.is_on:
                continue
            await self._stop_home_output_for_input(d, allow_bypass_zero=False)

        # prevent hysteria
        setpoint = self._apply_charge_holdoff(setpoint, time, allow_charge=False)
        self.operationstate.update_value(ManagerState.CHARGE.value if setpoint < 0 else ManagerState.IDLE.value)

        charge_devices, idle_devices = self._collect_charge_candidates(
            self.charge,
            self.idle,
            promote_idle_devices=setpoint < -SmartMode.POWER_START and not self.charge,
        )
        dev_start = await self._apply_weighted_charge_allocation(setpoint, charge_devices, idle_devices)
        await self._start_idle_charge_devices(idle_devices, dev_start)

    async def _apply_primary_input(
        self,
        setpoint: int,
        time: datetime,
        routing: _PowerRoutingSnapshot,
        *,
        strict_output_stop: bool = False,
    ) -> None:
        """
        Apply an input budget while preserving selected-primary PV behavior.

        Current produced home output can be kept running unless strict stop is
        requested. Selected-primary charge, secondary local surplus, and active
        produced targets are then balanced before fallback charge allocation.
        """
        _LOGGER.info("Input (primary-aware) => setpoint %sW", setpoint)

        selected_primary = routing.selected_primary
        active_discharge_targets = routing.active_produced_targets(self.discharge)
        if strict_output_stop:
            active_discharge_targets = dict.fromkeys(self.discharge, 0)
        requested_setpoint = setpoint
        positive_demand_charge_lag = routing.positive_demand_charge_lag(requested_setpoint)
        adjusts_active_charge = requested_setpoint < 0 and any(
            routing.route(device).charge_floor > 0 for device in self.charge
        )
        allow_home_pv_charge = not positive_demand_charge_lag
        primary = self._selected_primary_device(charging=True)
        pv_floors = routing.pv_floor_summary()
        local_charge = routing.local_charge_summary(
            primary,
            pv_charge_first_mode=self._selected_primary_routing_enabled() and self.operation in PV_CHARGE_FIRST_OPERATIONS,
            include_charge_first_home=allow_home_pv_charge,
        )
        keeps_non_primary_local_charge = (
            requested_setpoint < 0
            and local_charge.non_primary_local_chargeable_surplus + pv_floors.replaceable_non_primary_serving_pv > 0
            and -requested_setpoint
            <= local_charge.non_primary_local_chargeable_surplus + pv_floors.replaceable_non_primary_serving_pv
        )
        # In surplus mode, SOCEMPTY devices should redirect their solar to their
        # own battery instead of continuing to pass it to the home.  Zero their
        # discharge targets so the stop-discharge loop below actually stops them.
        if allow_home_pv_charge:
            active_discharge_targets = {
                d: (0 if d.state in PV_CHARGE_FIRST_STATES and routing.chargeable_produced_home(d) > 0 else t)
                for d, t in active_discharge_targets.items()
            }

        # prevent hysteria
        setpoint = self._apply_charge_holdoff(
            setpoint,
            time,
            allow_charge=adjusts_active_charge or keeps_non_primary_local_charge,
        )
        self.operationstate.update_value(ManagerState.CHARGE.value if setpoint < 0 else ManagerState.IDLE.value)

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
        idle_secondary_surplus_devices = [device for device in idle_devices if routing.charge_surplus(device) > 0]
        full_primary_bypass_handoff_promotions: list[ZendureDevice] = []
        if selected_primary is not None:
            full_primary_bypass_handoff_promotions = [
                device for device in idle_devices if device.state not in {DeviceState.OFFLINE, DeviceState.SOCFULL}
            ]
        full_primary_bypass_handoff = (
            requested_setpoint < -SmartMode.POWER_START
            and not strict_output_stop
            and self.charge_time > time
            and selected_primary is not None
            and selected_primary.online
            and selected_primary in self.discharge
            and selected_primary.state == DeviceState.SOCFULL
            and selected_primary.can_bypass
            and primary is None
            and bool(
                any(device.fuseGrp is not selected_primary.fuseGrp for device in charge_devices)
                or any(device.fuseGrp is not selected_primary.fuseGrp for device in active_secondary_charge_devices)
                or any(device.fuseGrp is not selected_primary.fuseGrp for device in idle_secondary_surplus_devices)
                or full_primary_bypass_handoff_promotions
            )
        )
        primary_local_surplus = routing.charge_surplus(primary) if primary is not None else 0
        move_primary_charge_to_secondary = (
            primary is not None
            and primary in self.charge
            and primary_local_surplus == 0
            and (
                any(routing.charge_surplus(device) > 0 for device in charge_devices)
                or bool(idle_secondary_surplus_devices)
            )
        )
        if move_primary_charge_to_secondary and self.charge_time > time:
            setpoint = requested_setpoint
            self.operationstate.update_value(ManagerState.CHARGE.value if setpoint < 0 else ManagerState.IDLE.value)
        if full_primary_bypass_handoff:
            setpoint = requested_setpoint
            self.operationstate.update_value(ManagerState.CHARGE.value if setpoint < 0 else ManagerState.IDLE.value)
            charge_devices.extend(full_primary_bypass_handoff_promotions)
            idle_devices = [device for device in idle_devices if device not in full_primary_bypass_handoff_promotions]
            idle_secondary_surplus_devices = [
                device
                for device in idle_secondary_surplus_devices
                if device not in full_primary_bypass_handoff_promotions
            ]

        def active_charge_lag_capacity(device: ZendureDevice) -> int:
            return routing.route(device).charge_floor if positive_demand_charge_lag and device in self.charge else 0

        def local_charge_capacity(device: ZendureDevice) -> int:
            capacity = max(routing.charge_surplus(device), active_charge_lag_capacity(device))
            if allow_home_pv_charge:
                capacity += routing.chargeable_produced_home(device)
            return capacity

        surplus_floor_devices = [*active_secondary_charge_devices, *charge_devices, *idle_secondary_surplus_devices]
        charge_targets, setpoint = self._allocate_capped_targets(
            setpoint,
            surplus_floor_devices,
            {device: local_charge_capacity(device) for device in surplus_floor_devices},
            direction=-1,
        )
        selected_primary_output_replacement_target = 0
        if not strict_output_stop and selected_primary is not None and selected_primary in active_discharge_targets:
            replaced_non_primary_home_pv = sum(
                min(
                    routing.chargeable_produced_home(device),
                    max(0, -charge_targets.get(device, 0) - routing.charge_surplus(device)),
                )
                for device in active_secondary_charge_devices
            )
            selected_primary_output_replacement_target = min(
                routing.selected_primary_output_replacement_capacity,
                replaced_non_primary_home_pv,
            )
            active_discharge_targets[selected_primary] += selected_primary_output_replacement_target
        if move_primary_charge_to_secondary and not pure_secondary_charge_devices:
            setpoint = 0

        for d in self.discharge:
            if charge_targets.get(d, 0) != 0:
                continue
            if not strict_output_stop and active_discharge_targets.get(d, 0) > 0:
                continue
            await self._stop_home_output_for_input(d, allow_bypass_zero=not strict_output_stop)

        if move_primary_charge_to_secondary and primary is not None:
            await primary.power_charge(0)
        elif primary is not None and setpoint < 0:
            primary_target = min(0, max(setpoint, self._primary_charge_limit(primary)))
            if primary_target != 0:
                setpoint -= await primary.power_charge(primary_target)
            elif (
                not strict_output_stop
                and active_discharge_targets.get(primary, 0) > 0
                and not positive_demand_charge_lag
            ):
                await self._command_home_output(primary, active_discharge_targets[primary], allow_bypass_zero=True)
        elif positive_demand_charge_lag and primary is not None and primary in self.charge:
            await primary.power_charge(0)
        elif (
            not strict_output_stop
            and selected_primary is not None
            and active_discharge_targets.get(selected_primary, 0) > 0
            and not positive_demand_charge_lag
        ):
            await self._command_home_output(
                selected_primary,
                active_discharge_targets[selected_primary],
                allow_bypass_zero=True,
            )

        dev_start = await self._apply_weighted_charge_allocation(
            setpoint,
            charge_devices,
            idle_devices,
            charge_targets=charge_targets,
            fallback_devices=set(pure_secondary_charge_devices) if move_primary_charge_to_secondary else None,
            command_zero_targets=False,
            subtract_actual_charge=False,
        )

        for d in idle_secondary_surplus_devices:
            target = charge_targets.get(d, 0)
            if target != 0:
                await d.power_charge(target)

        for d in active_secondary_charge_devices:
            target = charge_targets.get(d, 0)
            if target != 0:
                await d.power_charge(target)
            elif not strict_output_stop and active_discharge_targets.get(d, 0) > 0:
                await self._command_home_output(d, active_discharge_targets[d], allow_bypass_zero=True)

        if not strict_output_stop:
            for d, target in active_discharge_targets.items():
                if d is selected_primary or d in active_secondary_charge_devices:
                    continue
                if target > 0:
                    await self._command_home_output(d, target, allow_bypass_zero=True)

        await self._start_idle_charge_devices(idle_devices, dev_start)

    async def _apply_weighted_charge_allocation(
        self,
        setpoint: int,
        charge_devices: list[ZendureDevice],
        idle_devices: list[ZendureDevice],
        *,
        charge_targets: dict[ZendureDevice, int] | None = None,
        fallback_devices: set[ZendureDevice] | None = None,
        command_zero_targets: bool = True,
        subtract_actual_charge: bool = True,
    ) -> int:
        """Apply the fallback weighted charge allocation and return the idle-start budget."""
        _idle_lvlmax, idle_lvlmin = self._idle_levels(idle_devices)
        charge_limit, charge_optimal, charge_weight = self._charge_metrics(charge_devices)

        dev_start = min(0, setpoint - charge_optimal * 2) if setpoint < -SmartMode.POWER_START else 0
        limit = charge_limit
        setpoint = max(limit, setpoint)
        for i, d in enumerate(sorted(charge_devices, key=lambda d: d.electricLevel.asInt, reverse=True)):
            pwr = (
                int(setpoint * (d.pwr_max * (100 - d.electricLevel.asInt)) / charge_weight) if charge_weight != 0 else 0
            )
            charge_weight -= d.pwr_max * (100 - d.electricLevel.asInt)

            limit -= d.pwr_max
            pwr = max(pwr, setpoint, d.pwr_max)
            if limit > setpoint - pwr:
                pwr = max(setpoint - limit, setpoint, d.pwr_max)

            if len(charge_devices) > 1 and i == 0:
                pwr = self._apply_first_device_hysteresis(pwr, d, discharging=False)

            target = charge_targets.get(d, 0) if charge_targets is not None else 0
            if fallback_devices is None or d in fallback_devices:
                target += pwr
            else:
                pwr = 0

            if subtract_actual_charge:
                setpoint -= await d.power_charge(target)
            else:
                setpoint -= pwr
                if command_zero_targets or target != 0:
                    await d.power_charge(target)
            dev_start += -1 if pwr != 0 and d.electricLevel.asInt > idle_lvlmin + 3 else 0

        return dev_start

    async def _start_idle_charge_devices(self, idle_devices: list[ZendureDevice], dev_start: int) -> None:
        """Start idle devices when fallback charge allocation still needs capacity."""
        if dev_start >= 0 or not idle_devices:
            return

        idle_devices.sort(key=lambda d: d.electricLevel.asInt, reverse=False)
        for d in idle_devices:
            # Off-grid devices need at least off-grid power to be recognized as charging.
            await d.power_charge(
                -SmartMode.POWER_START - max(0, d.pwr_offgrid)
                if d.state != DeviceState.SOCFULL
                else -max(0, d.pwr_offgrid)
            )
            if (dev_start := dev_start - d.charge_optimal * 2) >= 0:
                break
        self.pwr_low: int = 0

    async def _apply_standard_home_output(
        self, setpoint: int, routing: _PowerRoutingSnapshot, *, produced_only: bool = False
    ) -> None:
        """
        Apply a home-output budget without selected-primary ordering.

        Active charging is stopped, produced output is assigned first, and any
        remaining demand may be filled from battery-backed discharge unless the
        route was clamped to produced-only output.
        """
        _LOGGER.info("Home output => setpoint %sW", setpoint)

        self._reset_home_output_charge_state()
        await self._stop_charging_for_home_output()

        discharge_devices = routing.discharge_candidates(list(self.discharge), list(self.idle), promote_idle_devices=False)
        idle_devices = [device for device in self.idle if device not in discharge_devices]
        idle_lvlmax, _idle_lvlmin = self._idle_levels(idle_devices)
        self.operationstate.update_value(
            ManagerState.DISCHARGE.value if setpoint > 0 and discharge_devices else ManagerState.IDLE.value
        )
        targets, setpoint = self._allocate_produced_floor(setpoint, discharge_devices)

        dev_start = (
            0 if produced_only else self._add_battery_remainder(targets, setpoint, discharge_devices, idle_lvlmax)
        )

        await self._command_home_output_targets(discharge_devices, targets)

        if produced_only:
            return

        await self._start_idle_discharge_devices(idle_devices, dev_start)

    async def _apply_primary_home_output(
        self, setpoint: int, routing: _PowerRoutingSnapshot, *, produced_only: bool = False
    ) -> None:
        """
        Apply a home-output budget with selected-primary ordering.

        The selected primary keeps or increases PV-backed home output first,
        secondary produced floors are preserved where possible, and battery
        remainder is added only when the route is not produced-only.
        """
        _LOGGER.info("Home output (primary-aware) => setpoint %sW", setpoint)
        requested_setpoint = setpoint

        self._reset_home_output_charge_state()

        selected_primary = routing.selected_primary
        charge_produced_devices = [
            device for device in self.charge if device.is_discharge_blocked() and routing.produced_limit(device) > 0
        ]
        await self._stop_charging_for_home_output(
            skip_devices=set(charge_produced_devices),
            allow_bypass_zero=True,
        )

        primary_produced_cap = routing.produced_limit(selected_primary) if selected_primary is not None else 0
        primary_bypass_floor = 0
        if (
            selected_primary is not None
            and selected_primary in self.discharge
            and selected_primary.state == DeviceState.SOCFULL
        ):
            primary_bypass_floor = min(
                routing.route(selected_primary).bypass_passthrough,
                max(0, selected_primary.solarInput.asInt),
            )
        if primary_bypass_floor > 0:
            primary_produced_cap = 0
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
            await self._command_home_output(selected_primary, 0, allow_bypass_zero=True)

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
            [device for device in remaining_active if device.state != DeviceState.RESERVE_RECOVERY],
            {
                device: min(max(0, device.homeOutput.asInt), routing.produced_limit(device))
                for device in remaining_active
                if device.state != DeviceState.RESERVE_RECOVERY
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
            primary_floor = primary_bypass_floor if primary_target == 0 else primary_target
            primary_battery_cap = max(0, primary_cap - primary_floor)
            additional_primary = min(setpoint, primary_battery_cap)
            if additional_primary > 0:
                primary_target = primary_floor + additional_primary
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
            await self._command_home_output(primary, primary_target, allow_bypass_zero=True)

        await self._command_home_output_targets(command_devices, targets, allow_bypass_zero=True)

        if produced_only:
            return

        await self._start_idle_discharge_devices(idle_battery_devices, dev_start, primary_aware=True)

    def _reset_home_output_charge_state(self) -> None:
        """Reset charge hysteresis state before switching to home output."""
        if self.charge_time != datetime.max:
            self.charge_time = datetime.max
            self.pwr_low = 0

    async def _stop_charging_for_home_output(
        self,
        *,
        skip_devices: set[ZendureDevice] | None = None,
        allow_bypass_zero: bool = False,
    ) -> None:
        """Stop active charging devices before assigning home output."""
        skip_devices = set() if skip_devices is None else skip_devices
        for device in self.charge:
            if device in skip_devices:
                continue
            # SF 2400 may show more gridInputPower than offGridPower and will be
            # recognized as charging, so set power to 10 instead of 0.
            if max(0, device.pwr_offgrid) == 0:
                await self._command_home_output(device, 0, allow_bypass_zero=allow_bypass_zero)
            else:
                await device.power_discharge(10)

    async def _command_home_output_targets(
        self,
        devices: list[ZendureDevice],
        targets: dict[ZendureDevice, int],
        *,
        allow_bypass_zero: bool = False,
    ) -> None:
        """Command home output targets for the provided devices."""
        for device in devices:
            await self._command_home_output(device, targets[device], allow_bypass_zero=allow_bypass_zero)
