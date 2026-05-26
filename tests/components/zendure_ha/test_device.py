"""Device behavior tests."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from custom_components.zendure_ha.binary_sensor import ZendureBinarySensor
from custom_components.zendure_ha.const import AcMode, ConnectionMode, DeviceState, SmartMode, SocLimitState
from custom_components.zendure_ha.device import CONST_HEADER, CONST_HEADER_CLOSE
from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro
from custom_components.zendure_ha.sensor import ZendureSensor

from .common import make_device

TARGET_SOC_AFTER_UPDATE = 90
MIN_SOC_AFTER_UPDATE = 8
STALE_EMPTY_LEVEL = 50
STALE_EMPTY_RESERVE_LEVEL = 8
STALE_EMPTY_RESERVE_SOC = 10
LOWERED_MIN_SOC = 5
LOWERED_MIN_SOC_RAW = LOWERED_MIN_SOC * 10
RAISED_MIN_SOC = 50
RAISED_MIN_SOC_RAW = RAISED_MIN_SOC * 10


def test_bypass_entity_is_restored_as_a_binary_sensor(hass):
    """Bypass should expose the binary-sensor API expected by manager routing."""
    device = make_device(hass)

    assert isinstance(device.byPass, ZendureBinarySensor)
    assert device.byPass.is_on is False

    device.byPass.update_value(1)

    assert device.byPass.is_on is True


def test_discharge_recovery_active_is_not_exposed_as_an_entity(hass):
    """Recovery state should remain device-owned and surface through SoC Limit."""
    device = make_device(hass)

    assert "dischargeRecoveryActive" not in device.entities
    assert device.discharge_recovery_active is False


@pytest.mark.parametrize(
    ("min_soc", "reserve", "expected"),
    [
        (5, 0, 5),
        (5, 10, 10),
        (20, 10, 20),
    ],
)
def test_discharge_floor_soc_uses_max_of_min_soc_and_reserve(hass, min_soc, reserve, expected):
    """Reserve-aware floor calculations should always use the higher threshold."""
    device = make_device(hass, min_soc=min_soc, reserve=reserve)

    assert device.discharge_floor_soc() == expected


@pytest.mark.parametrize(
    ("level", "min_soc", "reserve", "kwh", "expected_available"),
    [
        (30, 10, 5, 2.0, 0.4),
        (30, 5, 10, 2.0, 0.4),
        (8, 10, 5, 2.0, -0.04),
    ],
)
def test_available_kwh_uses_reserve_aware_floor(hass, level, min_soc, reserve, kwh, expected_available):
    """Available energy should use the higher of min SoC and reserve as its baseline."""
    device = make_device(
        hass,
        level=level,
        min_soc=min_soc,
        reserve=reserve,
        kwh=kwh,
    )

    device.refresh_discharge_state()

    assert device.availableKwh.asNumber == pytest.approx(expected_available)
    assert device.actualKwh == pytest.approx(expected_available)


@pytest.mark.parametrize(
    ("state", "actual_kwh", "expected"),
    [
        (DeviceState.INACTIVE, 0.4, 0.4),
        (DeviceState.INACTIVE, -0.1, 0),
        (DeviceState.SOCEMPTY, 0.4, 0),
        (DeviceState.SOCRESERVE, 0.4, 0),
        (DeviceState.RESERVE_RECOVERY, 0.4, 0),
        (DeviceState.OFFLINE, 0.4, 0.4),
    ],
)
def test_available_kwh_contribution_is_device_owned(hass, state, actual_kwh, expected):
    """Manager aggregate contribution should be derived from device state."""
    device = make_device(hass)
    device.state = state
    device.actualKwh = actual_kwh

    assert device.available_kwh_contribution() == pytest.approx(expected)


@pytest.mark.parametrize(
    ("state", "discharge_limit", "expected"),
    [
        (DeviceState.OFFLINE, 800, False),
        (DeviceState.SOCEMPTY, 800, False),
        (DeviceState.SOCRESERVE, 800, False),
        (DeviceState.INACTIVE, 0, False),
        (DeviceState.INACTIVE, 800, True),
        (DeviceState.RESERVE_RECOVERY, 800, True),
    ],
)
def test_discharge_capability_is_device_owned(hass, state, discharge_limit, expected):
    """General discharge capability should be derived from device state."""
    device = make_device(hass)
    device.state = state
    device.discharge_limit = discharge_limit

    assert device.is_discharge_capable() is expected


@pytest.mark.parametrize(
    ("solar_input", "produced_power", "expected"),
    [
        (120, 0, True),
        (0, -80, True),
        (0, 0, False),
    ],
)
def test_reports_pv_is_device_owned(hass, solar_input, produced_power, expected):
    """PV evidence should be derived from device telemetry."""
    device = make_device(hass)
    device.solarInput.update_value(solar_input)
    device.pwr_produced = produced_power

    assert device.reports_pv() is expected


@pytest.mark.parametrize(
    (
        "state",
        "connection_status",
        "charge_limit",
        "solar_input",
        "produced_power",
        "home_input",
        "battery_input",
        "battery_output",
        "expected",
    ),
    [
        (DeviceState.INACTIVE, SmartMode.CONNECTED, -1000, 120, 0, 80, 0, 0, True),
        (DeviceState.INACTIVE, SmartMode.CONNECTED, -1000, 0, -120, 0, 80, 10, True),
        (DeviceState.OFFLINE, SmartMode.CONNECTED, -1000, 120, 0, 80, 0, 0, False),
        (DeviceState.SOCFULL, SmartMode.CONNECTED, -1000, 120, 0, 80, 0, 0, False),
        (DeviceState.INACTIVE, 0, -1000, 120, 0, 80, 0, 0, False),
        (DeviceState.INACTIVE, SmartMode.CONNECTED, 0, 120, 0, 80, 0, 0, False),
        (DeviceState.INACTIVE, SmartMode.CONNECTED, -1000, 0, 0, 80, 0, 0, False),
    ],
)
def test_reports_active_pv_charge_is_device_owned(
    hass,
    state,
    connection_status,
    charge_limit,
    solar_input,
    produced_power,
    home_input,
    battery_input,
    battery_output,
    expected,
):
    """Active PV charge evidence should be derived from device telemetry."""
    device = make_device(hass, home_input=home_input, battery_input=battery_input, battery_output=battery_output)
    device.state = state
    device.connectionStatus.update_value(connection_status)
    device.charge_limit = charge_limit
    device.solarInput.update_value(solar_input)
    device.pwr_produced = produced_power

    assert device.reports_active_pv_charge() is expected


@pytest.mark.parametrize(
    ("state", "bypass_on", "has_bypass", "home_output", "solar_input", "expected"),
    [
        (DeviceState.SOCFULL, True, True, 120, 120, True),
        (DeviceState.SOCFULL, False, True, 120, 120, False),
        (DeviceState.SOCFULL, True, False, 120, 120, False),
        (DeviceState.INACTIVE, True, True, 120, 120, False),
        (DeviceState.SOCFULL, True, True, 0, 120, False),
        (DeviceState.SOCFULL, True, True, 120, 0, False),
    ],
)
def test_reports_full_bypass_pv_is_device_owned(hass, state, bypass_on, has_bypass, home_output, solar_input, expected):
    """Full bypass PV evidence should be derived from device telemetry."""
    device = make_device(hass, level=100, soc_set=100, home_output=home_output)
    device.state = state
    device.solarInput.update_value(solar_input)
    if has_bypass:
        device.byPass.update_value(bypass_on)
    else:
        delattr(device, "byPass")

    assert device.reports_full_bypass_pv() is expected


@pytest.mark.parametrize(
    ("level", "soc_set", "max_expected_charge"),
    [
        (94, 100, 200),
        (96, 100, 150),
        (98, 100, 100),
        (74, 80, 200),
        (76, 80, 150),
        (78, 80, 100),
    ],
)
def test_current_charge_surplus_limit_respects_taper(hass, level, soc_set, max_expected_charge):
    """Current charge surplus should be capped at the device taper limit."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro-surplus",
        product_model="SolarFlow 800 Pro",
        level=level,
        soc_set=soc_set,
    )
    device.pwr_produced = -500

    assert device.state is DeviceState.SOCNEARLYFULL
    result = device.current_charge_surplus_limit()
    assert result <= max_expected_charge
    assert result > 0


def test_current_charge_surplus_limit_is_zero_for_socfull(hass):
    """Current charge surplus should be 0 once the device has reached target SoC."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro-full",
        product_model="SolarFlow 800 Pro",
        level=100,
        soc_set=100,
    )
    device.pwr_produced = -500

    assert device.state is DeviceState.SOCFULL
    assert device.current_charge_surplus_limit() == 0


def test_current_charge_surplus_limit_is_full_below_taper_range(hass):
    """Current charge surplus should use the full produced surplus below taper range."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro-below-taper",
        product_model="SolarFlow 800 Pro",
        level=90,
        soc_set=100,
    )
    device.pwr_produced = -800

    assert device.state is DeviceState.INACTIVE
    result = device.current_charge_surplus_limit()
    assert result == abs(device.pwr_produced)


@pytest.mark.parametrize(
    ("level", "min_soc", "reserve", "soc_limit", "expected_state"),
    [
        (4, 5, 10, 0, DeviceState.SOCEMPTY),
        (5, 5, 10, 0, DeviceState.SOCEMPTY),
        (6, 5, 10, 0, DeviceState.SOCRESERVE),
        (10, 5, 10, 0, DeviceState.SOCRESERVE),
        (11, 5, 10, 0, DeviceState.INACTIVE),
        (8, 10, 5, 0, DeviceState.SOCEMPTY),
        (8, 5, 10, SocLimitState.EMPTY, DeviceState.SOCEMPTY),
    ],
)
def test_device_state_distinguishes_empty_from_reserve_floor(
    hass,
    level,
    min_soc,
    reserve,
    soc_limit,
    expected_state,
):
    """Device state should expose reserve-band SoC separately from empty SoC."""
    device = make_device(hass, level=20, min_soc=min_soc, reserve=reserve, soc_set=100)
    device.entityUpdate("socLimit", soc_limit)
    device.electricLevel.update_value(level)

    device.refresh_discharge_state()

    assert device.state is expected_state


@pytest.mark.parametrize(
    (
        "level",
        "min_soc",
        "reserve",
        "soc_set",
        "raw_soc_limit",
        "recovery_active",
        "margin",
        "online",
        "expected_state",
        "expected_soc_limit",
    ),
    [
        (50, 10, 10, 80, 0, False, 0, True, DeviceState.INACTIVE, 0),
        (80, 10, 10, 80, 0, False, 0, True, DeviceState.SOCFULL, SocLimitState.FULL),
        (10, 10, 10, 80, 0, False, 0, True, DeviceState.SOCEMPTY, SocLimitState.EMPTY),
        (10, 5, 10, 80, 0, False, 0, True, DeviceState.SOCRESERVE, SocLimitState.RESERVE),
        (
            12,
            10,
            10,
            80,
            0,
            True,
            5,
            True,
            DeviceState.RESERVE_RECOVERY,
            SocLimitState.RESERVE_RECOVERY,
        ),
        (50, 10, 10, 80, 0, False, 0, False, DeviceState.OFFLINE, SocLimitState.NORMAL),
    ],
)
def test_soc_limit_sensor_exposes_effective_device_state(
    hass,
    level,
    min_soc,
    reserve,
    soc_set,
    raw_soc_limit,
    recovery_active,
    margin,
    online,
    expected_state,
    expected_soc_limit,
):
    """SoC Limit should expose the effective derived device state."""
    device = make_device(hass, level=level, min_soc=min_soc, reserve=reserve, soc_set=soc_set)
    device.discharge_recovery_margin_soc = margin
    device.discharge_recovery_active = recovery_active
    device.entityUpdate("socLimit", raw_soc_limit)
    if not online:
        device.connectionStatus.update_value(0)

    device.refresh_discharge_state()

    assert device.state is expected_state
    assert device.socLimit.asInt == expected_soc_limit
    assert cast("ZendureSensor", device.entities["state"]).asInt == expected_state.value


def test_soc_limit_sensor_keeps_last_value_while_device_offline(hass):
    """SoC Limit should not expose offline while the device state does."""
    device = make_device(hass, level=50, min_soc=10, reserve=10, soc_set=80)
    device.entityUpdate("socLimit", SocLimitState.FULL)

    assert device.state is DeviceState.SOCFULL
    assert device.socLimit.asInt == SocLimitState.FULL

    device.connectionStatus.update_value(0)
    device.entityUpdate("socLimit", SocLimitState.EMPTY)

    assert device.state is DeviceState.OFFLINE
    assert cast("ZendureSensor", device.entities["state"]).asInt == DeviceState.OFFLINE.value
    assert device.socLimit.asInt == SocLimitState.FULL

    device.connectionStatus.update_value(SmartMode.CONNECTED)
    device.refresh_discharge_state()

    assert device.state is DeviceState.SOCEMPTY
    assert device.socLimit.asInt == SocLimitState.EMPTY


def test_soc_limit_sensor_drops_threshold_full_when_soc_falls_below_target(hass):
    """A threshold-derived full state should not stick after SoC drops below the target."""
    device = make_device(hass, level=80, min_soc=10, reserve=10, soc_set=80)

    assert device.state is DeviceState.SOCFULL
    assert device.socLimit.asInt == SocLimitState.FULL

    device.electricLevel.update_value(79)
    device.refresh_discharge_state()

    assert device.state is DeviceState.INACTIVE
    assert device.socLimit.asInt == 0


def test_raw_soc_limit_full_override_still_forces_effective_full(hass):
    """The device-reported full limit should still force the effective full state."""
    device = make_device(hass, level=50, min_soc=10, reserve=10, soc_set=80)

    device.entityUpdate("socLimit", SocLimitState.FULL)
    device.electricLevel.update_value(40)
    device.refresh_discharge_state()

    assert device.state is DeviceState.SOCFULL
    assert device.socLimit.asInt == SocLimitState.FULL


def test_soc_set_update_refreshes_effective_soc_limit_state(hass):
    """Changing the target SoC should immediately refresh the effective SoC Limit sensor."""
    device = make_device(hass, level=80, min_soc=10, reserve=10, soc_set=80)

    assert device.state is DeviceState.SOCFULL
    assert device.socLimit.asInt == SocLimitState.FULL

    device.entityUpdate("socSet", TARGET_SOC_AFTER_UPDATE * 10)

    assert device.socSet.asNumber == TARGET_SOC_AFTER_UPDATE
    assert device.state is DeviceState.INACTIVE
    assert device.socLimit.asInt == 0


async def test_min_soc_update_refreshes_effective_soc_limit_state(hass):
    """Changing minimum SoC should immediately refresh the effective SoC Limit sensor."""
    device = make_device(hass, level=8, min_soc=5, reserve=10, soc_set=80)

    assert device.state is DeviceState.SOCRESERVE
    assert device.socLimit.asInt == SocLimitState.RESERVE

    await device.minSoc.async_set_native_value(MIN_SOC_AFTER_UPDATE)

    assert device.minSoc.asNumber == MIN_SOC_AFTER_UPDATE
    assert device.state is DeviceState.SOCEMPTY
    assert device.socLimit.asInt == SocLimitState.EMPTY


async def test_min_soc_write_clears_stale_empty_soc_limit_when_level_is_above_new_floor(hass):
    """Lowering minimum SoC should clear a cached empty limit when the current SoC is valid again."""
    device = make_device(hass, level=STALE_EMPTY_LEVEL, min_soc=RAISED_MIN_SOC, reserve=LOWERED_MIN_SOC, soc_set=80)
    device.entityUpdate("socLimit", SocLimitState.EMPTY)

    assert device.state is DeviceState.SOCEMPTY
    assert device.socLimit.asInt == SocLimitState.EMPTY
    assert device.availableKwh.asNumber == 0

    await device.minSoc.async_set_native_value(LOWERED_MIN_SOC)

    assert device.minSoc.asNumber == LOWERED_MIN_SOC
    assert device.state is DeviceState.INACTIVE
    assert device.socLimit.asInt == SocLimitState.NORMAL
    assert device.availableKwh.asNumber > 0

    device.refresh_discharge_state()

    assert device.state is DeviceState.INACTIVE
    assert device.socLimit.asInt == SocLimitState.NORMAL


def test_min_soc_report_clears_stale_empty_soc_limit_when_level_is_above_new_floor(hass):
    """A reported minimum SoC change should clear stale empty state using the same rule."""
    device = make_device(hass, level=STALE_EMPTY_LEVEL, min_soc=RAISED_MIN_SOC, reserve=LOWERED_MIN_SOC, soc_set=80)
    device.entityUpdate("socLimit", SocLimitState.EMPTY)

    assert device.state is DeviceState.SOCEMPTY
    assert device.socLimit.asInt == SocLimitState.EMPTY
    assert device.availableKwh.asNumber == 0

    device.entityUpdate("minSoc", LOWERED_MIN_SOC_RAW)

    assert device.minSoc.asNumber == LOWERED_MIN_SOC
    assert device.state is DeviceState.INACTIVE
    assert device.socLimit.asInt == SocLimitState.NORMAL
    assert device.availableKwh.asNumber > 0

    device.refresh_discharge_state()

    assert device.state is DeviceState.INACTIVE
    assert device.socLimit.asInt == SocLimitState.NORMAL


async def test_min_soc_write_recalculates_reserve_after_clearing_stale_empty_soc_limit(hass):
    """Clearing stale empty should still let the normal reserve calculation win."""
    device = make_device(
        hass,
        level=STALE_EMPTY_RESERVE_LEVEL,
        min_soc=STALE_EMPTY_RESERVE_SOC,
        reserve=STALE_EMPTY_RESERVE_SOC,
        soc_set=80,
    )
    device.entityUpdate("socLimit", SocLimitState.EMPTY)

    assert device.state is DeviceState.SOCEMPTY
    assert device.socLimit.asInt == SocLimitState.EMPTY
    assert device.available_kwh_contribution() == 0

    await device.minSoc.async_set_native_value(LOWERED_MIN_SOC)

    assert device.minSoc.asNumber == LOWERED_MIN_SOC
    assert device.state is DeviceState.SOCRESERVE
    assert device.socLimit.asInt == SocLimitState.RESERVE
    assert device.available_kwh_contribution() == 0


def test_min_soc_report_recalculates_reserve_after_clearing_stale_empty_soc_limit(hass):
    """Reported min SoC changes should also let normal reserve calculation win."""
    device = make_device(
        hass,
        level=STALE_EMPTY_RESERVE_LEVEL,
        min_soc=STALE_EMPTY_RESERVE_SOC,
        reserve=STALE_EMPTY_RESERVE_SOC,
        soc_set=80,
    )
    device.entityUpdate("socLimit", SocLimitState.EMPTY)

    assert device.state is DeviceState.SOCEMPTY
    assert device.socLimit.asInt == SocLimitState.EMPTY
    assert device.available_kwh_contribution() == 0

    device.entityUpdate("minSoc", LOWERED_MIN_SOC_RAW)

    assert device.minSoc.asNumber == LOWERED_MIN_SOC
    assert device.state is DeviceState.SOCRESERVE
    assert device.socLimit.asInt == SocLimitState.RESERVE
    assert device.available_kwh_contribution() == 0


async def test_min_soc_write_marks_empty_when_level_reaches_new_floor(hass):
    """Raising minimum SoC to the current level should immediately mark the device empty."""
    device = make_device(hass, level=STALE_EMPTY_LEVEL, min_soc=LOWERED_MIN_SOC, reserve=LOWERED_MIN_SOC, soc_set=80)

    assert device.state is DeviceState.INACTIVE
    assert device.socLimit.asInt == SocLimitState.NORMAL
    assert device.availableKwh.asNumber > 0

    await device.minSoc.async_set_native_value(RAISED_MIN_SOC)

    assert device.minSoc.asNumber == RAISED_MIN_SOC
    assert device.state is DeviceState.SOCEMPTY
    assert device.socLimit.asInt == SocLimitState.EMPTY
    assert device.availableKwh.asNumber == 0


def test_min_soc_report_marks_empty_when_level_reaches_new_floor(hass):
    """A reported minimum SoC increase should refresh the effective empty state."""
    device = make_device(hass, level=STALE_EMPTY_LEVEL, min_soc=LOWERED_MIN_SOC, reserve=LOWERED_MIN_SOC, soc_set=80)

    assert device.state is DeviceState.INACTIVE
    assert device.socLimit.asInt == SocLimitState.NORMAL
    assert device.availableKwh.asNumber > 0

    device.entityUpdate("minSoc", RAISED_MIN_SOC_RAW)

    assert device.minSoc.asNumber == RAISED_MIN_SOC
    assert device.state is DeviceState.SOCEMPTY
    assert device.socLimit.asInt == SocLimitState.EMPTY
    assert device.availableKwh.asNumber == 0


@pytest.mark.parametrize(
    ("floor", "margin", "recovery_active", "expected"),
    [
        (10, 5, False, 10),
        (10, 5, True, 15),
        (95, 10, True, 100),
    ],
)
def test_available_discharge_baseline_soc_uses_margin_only_while_recovering(
    hass,
    floor,
    margin,
    recovery_active,
    expected,
):
    """The recovery margin only affects the discharge baseline while active."""
    device = make_device(hass, min_soc=floor, reserve=floor)
    device.discharge_recovery_margin_soc = margin
    device.discharge_recovery_active = recovery_active

    assert device.available_discharge_baseline_soc() == expected


@pytest.mark.parametrize(
    (
        "level",
        "min_soc",
        "reserve",
        "margin",
        "recovery_active",
        "kwh",
        "expected_available",
    ),
    [
        (30, 5, 10, 5, False, 2.0, 0.4),
        (30, 5, 10, 5, True, 2.0, 0.3),
        (15, 5, 10, 5, True, 2.0, 0.0),
        (12, 5, 10, 5, True, 2.0, -0.06),
        (15, 10, 10, 0, True, 2.0, 0.1),
        (97, 95, 95, 10, True, 2.0, -0.06),
        (100, 95, 95, 10, True, 2.0, 0.0),
    ],
)
def test_available_kwh_uses_recovery_baseline_when_active(
    hass,
    level,
    min_soc,
    reserve,
    margin,
    recovery_active,
    kwh,
    expected_available,
):
    """Available energy should use the recovery baseline once recovery is active."""
    device = make_device(
        hass,
        level=level,
        min_soc=min_soc,
        reserve=reserve,
        kwh=kwh,
    )
    device.discharge_recovery_margin_soc = margin
    device.discharge_recovery_active = recovery_active

    device.refresh_discharge_state()

    assert device.availableKwh.asNumber == pytest.approx(expected_available)
    assert device.actualKwh == pytest.approx(expected_available)


@pytest.mark.parametrize(
    (
        "level",
        "min_soc",
        "reserve",
        "activate_margin_window",
        "starting_recovery",
        "expected_blocked",
        "expected_recovery",
        "expected_state",
        "expected_available",
    ),
    [
        (10, 10, 10, False, False, True, True, DeviceState.SOCEMPTY, -0.1),
        (10, 5, 10, False, False, True, True, DeviceState.SOCRESERVE, -0.1),
        (12, 10, 10, True, False, True, True, DeviceState.RESERVE_RECOVERY, -0.06),
        (14, 10, 10, False, True, True, True, DeviceState.RESERVE_RECOVERY, -0.02),
        (15, 10, 10, False, True, False, False, DeviceState.INACTIVE, 0.1),
    ],
)
def test_recovery_window_keeps_blocking_state_and_available_energy_consistent(
    hass,
    level,
    min_soc,
    reserve,
    activate_margin_window,
    starting_recovery,
    expected_blocked,
    expected_recovery,
    expected_state,
    expected_available,
):
    """Recovery transitions should keep the derived sensors aligned with the state."""
    device = make_device(hass, level=20, min_soc=min_soc, reserve=reserve, kwh=2.0)
    device.discharge_recovery_margin_soc = 5
    device.discharge_recovery_active = starting_recovery
    device.electricLevel.update_value(level)

    blocked = device.is_discharge_blocked(activate_margin_window=activate_margin_window)

    assert blocked is expected_blocked
    assert device.discharge_recovery_active is expected_recovery
    assert device.state is expected_state
    assert device.availableKwh.asNumber == pytest.approx(expected_available)
    assert device.actualKwh == pytest.approx(expected_available)


@pytest.mark.parametrize(
    ("recovery_active", "expected_hours"),
    [
        (False, 2.0),
        (True, 1.0),
    ],
)
def test_remaining_time_uses_recovery_aware_discharge_baseline(hass, recovery_active, expected_hours):
    """Discharge remaining time should shrink while the recovery margin is active."""
    device = make_device(hass, level=20, min_soc=10, reserve=10, kwh=2.0, battery_input=0, battery_output=100)
    device.discharge_recovery_margin_soc = 5
    device.discharge_recovery_active = recovery_active

    assert device.calcRemainingTime() == pytest.approx(expected_hours)


async def test_sf800_pro_power_charge_sets_ac_input_mode_and_charge_limits(hass):
    """Charging an SF800 Pro should switch it into AC input mode with the requested limit."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro",
        device_name="sf800 pro",
        product_model="SolarFlow 800 Pro",
        ac_mode=AcMode.OUTPUT,
        input_limit=0,
        output_limit=0,
    )
    with patch.object(device, "doCommand", AsyncMock()) as mock_do_command:
        await device.power_charge(-300)

    mock_do_command.assert_awaited_once_with(
        {"properties": {"smartMode": 1, "acMode": 1, "outputLimit": 0, "inputLimit": 300}}
    )


async def test_sf800_pro_power_bypass_sets_ac_input_mode(hass):
    """Bypass on an SF800 Pro should switch it into AC input mode without requesting active output power."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro-bypass",
        device_name="sf800 pro bypass",
        product_model="SolarFlow 800 Pro",
        ac_mode=AcMode.OUTPUT,
        input_limit=0,
        output_limit=0,
    )
    with patch.object(device, "doCommand", AsyncMock()) as mock_do_command:
        await device.power_bypass()

    mock_do_command.assert_awaited_once_with(
        {"properties": {"smartMode": 0, "acMode": 1, "outputLimit": 0, "inputLimit": 0}}
    )


async def test_http_report_updates_last_http_report_only(hass):
    """Applied HTTP reports should advance only the HTTP freshness sensor."""
    device = make_device(hass)

    assert getattr(device.lastHttpReport, "_attr_native_value", None) is None
    assert getattr(device.lastMqttReport, "_attr_native_value", None) is None

    with patch.object(device, "register_pending_entities"):
        await device.mqttProperties({"properties": {"hyperTmp": 2801}}, "http")

    assert device.lastHttpReport._attr_native_value is not None
    assert getattr(device.lastMqttReport, "_attr_native_value", None) is None
    assert float(cast("ZendureSensor", device.entities["hyperTmp"])._attr_native_value) == pytest.approx(7.0)


async def test_empty_http_report_does_not_update_report_timestamps(hass):
    """Empty or non-report HTTP payloads should not advance freshness sensors."""
    device = make_device(hass)

    await device.mqttProperties({}, "http")
    await device.mqttProperties({"foo": "bar"}, "http")

    assert getattr(device.lastHttpReport, "_attr_native_value", None) is None
    assert getattr(device.lastMqttReport, "_attr_native_value", None) is None


async def test_mqtt_report_updates_last_mqtt_report_only(hass):
    """Applied MQTT reports should advance only the MQTT freshness sensor."""
    device = make_device(hass)

    with patch.object(device, "register_pending_entities"):
        await device.mqttProperties({"properties": {"hyperTmp": 2801}}, "mqtt")

    assert getattr(device.lastHttpReport, "_attr_native_value", None) is None
    assert device.lastMqttReport._attr_native_value is not None
    assert float(cast("ZendureSensor", device.entities["hyperTmp"])._attr_native_value) == pytest.approx(7.0)


async def test_http_pack_data_updates_last_http_report(hass):
    """Battery-only HTTP reports should still advance HTTP freshness."""
    device = make_device(hass)

    with patch.object(device, "register_pending_entities"):
        await device.mqttProperties({"packData": [{"sn": "C123456789", "state": 1}]}, "http")

    assert device.lastHttpReport._attr_native_value is not None
    assert getattr(device.lastMqttReport, "_attr_native_value", None) is None
    assert "C123456789" in device.batteries


def test_local_mqtt_availability_message_does_not_update_last_mqtt_report(hass):
    """Ignored local availability topics should not move MQTT freshness."""
    device = cast(
        "SolarFlow800Pro",
        make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-local",
            product_model="SolarFlow 800 Pro",
        ),
    )
    device.connection.update_value(ConnectionMode.ZENSDK_WITH_LOCAL_MQTT)

    device.handleLocalMqttMessage(Mock(), "sensor", "hyperTmp/availability", "online")

    assert getattr(device.lastMqttReport, "_attr_native_value", None) is None


def test_local_mqtt_device_message_updates_last_mqtt_report(hass):
    """Applied hybrid device MQTT updates should advance MQTT freshness."""
    device = cast(
        "SolarFlow800Pro",
        make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-mqtt",
            product_model="SolarFlow 800 Pro",
        ),
    )
    device.connection.update_value(ConnectionMode.ZENSDK_WITH_LOCAL_MQTT)

    with patch.object(device, "register_pending_entities"):
        device.handleLocalMqttMessage(Mock(), "sensor", "hyperTmp", "7")

    assert device.lastMqttReport._attr_native_value is not None
    assert float(cast("ZendureSensor", device.entities["hyperTmp"])._attr_native_value) == pytest.approx(7.0)


def test_local_mqtt_battery_message_updates_last_mqtt_report(hass):
    """Applied hybrid battery MQTT updates should advance MQTT freshness."""
    device = cast(
        "SolarFlow800Pro",
        make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-battery",
            product_model="SolarFlow 800 Pro",
        ),
    )
    device.connection.update_value(ConnectionMode.ZENSDK_WITH_LOCAL_MQTT)

    with patch("custom_components.zendure_ha.entity.EntityDevice.register_pending_entities"):
        device.handleLocalMqttBatteryMessage("C123456789", "state", "charging")

    assert device.lastMqttReport._attr_native_value is not None


class TestZenSdkDataRefresh:
    """Verify ZenSDK devices poll on every coordinator cycle."""

    async def test_data_refresh_polls_on_non_zero_update_count(self, hass):
        """dataRefresh must call httpGet on cycles after the first when in ZenSDK mode."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-poll",
            product_model="SolarFlow 800 Pro",
        )
        device.connection.update_value(ConnectionMode.ZENSDK)

        with patch.object(device, "httpGet", new_callable=AsyncMock, return_value={}) as mock_get:
            await device.dataRefresh(5)
            mock_get.assert_awaited_once()

    async def test_data_refresh_skips_cloud_mode(self, hass):
        """dataRefresh must not poll via HTTP when the device is in CLOUD mode."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-cloud",
            product_model="SolarFlow 800 Pro",
        )
        device.connection.update_value(ConnectionMode.CLOUD)

        with patch.object(device, "httpGet", new_callable=AsyncMock, return_value={}) as mock_get:
            await device.dataRefresh(5)
            mock_get.assert_not_awaited()


class TestHttpConnectionClose:
    """Verify HTTP requests use Connection: close to prevent stale pooled sockets."""

    @staticmethod
    def _request_context(response: MagicMock) -> MagicMock:
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=None)
        return context

    def test_const_header_no_connection_close(self):
        """CONST_HEADER must not contain Connection: close (keep-alive by default)."""
        assert "Connection" not in CONST_HEADER

    def test_const_header_close_has_connection_close(self):
        """CONST_HEADER_CLOSE must contain Connection: close."""
        assert CONST_HEADER_CLOSE["Connection"] == "close"

    async def test_http_get_sends_connection_close_when_healthy(self, hass):
        """httpGet must pass Connection: close even when there are no prior failures."""
        device = cast(
            "SolarFlow800Pro",
            make_device(
                hass,
                device_cls=SolarFlow800Pro,
                device_id="sf800-hdr",
                product_model="SolarFlow 800 Pro",
            ),
        )
        device.ipAddress = "192.168.1.99"

        mock_response = MagicMock()
        mock_response.raise_for_status = Mock()
        mock_response.json = AsyncMock(return_value={"properties": {"solarInputPower": 100}})
        device.session.get = Mock(return_value=self._request_context(mock_response))

        await device.httpGet("properties/report")

        call_kwargs = device.session.get.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Connection"] == "close"

    async def test_http_get_sends_connection_close_after_failure(self, hass):
        """httpGet must pass Connection: close after a prior failure."""
        device = cast(
            "SolarFlow800Pro",
            make_device(
                hass,
                device_cls=SolarFlow800Pro,
                device_id="sf800-hdr2",
                product_model="SolarFlow 800 Pro",
            ),
        )
        device.ipAddress = "192.168.1.99"
        device._http_failures = 1

        mock_response = MagicMock()
        mock_response.raise_for_status = Mock()
        mock_response.json = AsyncMock(return_value={"properties": {"solarInputPower": 100}})
        device.session.get = Mock(return_value=self._request_context(mock_response))

        await device.httpGet("properties/report")

        call_kwargs = device.session.get.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Connection"] == "close"

    async def test_http_post_sends_connection_close_after_failure(self, hass):
        """httpPost must pass Connection: close after a prior failure."""
        device = cast(
            "SolarFlow800Pro",
            make_device(
                hass,
                device_cls=SolarFlow800Pro,
                device_id="sf800-hdr3",
                product_model="SolarFlow 800 Pro",
            ),
        )
        device.ipAddress = "192.168.1.99"
        device._http_failures = 2

        mock_response = MagicMock()
        mock_response.raise_for_status = Mock()
        device.session.post = Mock(return_value=self._request_context(mock_response))

        await device.httpPost("properties/write", {"properties": {"outputLimit": 100}})

        call_kwargs = device.session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Connection"] == "close"


@pytest.mark.parametrize(
    ("level", "soc_set", "expected_taper"),
    [
        (93, 100, None),
        (94, 100, 200),
        (95, 100, 200),
        (96, 100, 150),
        (97, 100, 150),
        (98, 100, 100),
        (99, 100, 100),
        (100, 100, None),
        (73, 80, None),
        (74, 80, 200),
        (75, 80, 200),
        (76, 80, 150),
        (77, 80, 150),
        (78, 80, 100),
        (79, 80, 100),
        (80, 80, None),
    ],
)
def test_sf800_pro_taper_charge_limit_by_soc_set_offset(hass, level, soc_set, expected_taper):
    """SF800 Pro should return the correct taper limit by distance below socSet."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro-taper",
        product_model="SolarFlow 800 Pro",
        level=level,
        soc_set=soc_set,
    )

    assert device.taper_charge_limit == expected_taper


@pytest.mark.parametrize(
    ("level", "soc_set", "expected_effective"),
    [
        (90, 100, -1000),
        (93, 100, -1000),
        (94, 100, -200),
        (96, 100, -150),
        (98, 100, -100),
        (73, 80, -1000),
        (74, 80, -200),
        (76, 80, -150),
        (78, 80, -100),
    ],
)
def test_sf800_pro_effective_charge_limit_reflects_relative_taper(hass, level, soc_set, expected_effective):
    """effective_charge_limit should match the taper cap in watts (negative) or the device limit."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro-effective",
        product_model="SolarFlow 800 Pro",
        level=level,
        soc_set=soc_set,
    )

    assert device.effective_charge_limit == expected_effective


@pytest.mark.parametrize(
    ("level", "soc_set", "expected_state"),
    [
        (90, 100, DeviceState.INACTIVE),
        (94, 100, DeviceState.SOCNEARLYFULL),
        (95, 100, DeviceState.SOCNEARLYFULL),
        (98, 100, DeviceState.SOCNEARLYFULL),
        (100, 100, DeviceState.SOCFULL),
        (73, 80, DeviceState.INACTIVE),
        (74, 80, DeviceState.SOCNEARLYFULL),
        (78, 80, DeviceState.SOCNEARLYFULL),
        (79, 80, DeviceState.SOCNEARLYFULL),
        (80, 80, DeviceState.SOCFULL),
        (81, 80, DeviceState.SOCFULL),
    ],
)
def test_sf800_pro_state_transitions_around_taper_thresholds(hass, level, soc_set, expected_state):
    """SF800 Pro should enter SOCNEARLYFULL near socSet but SOCFULL at/above socSet."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro-state",
        product_model="SolarFlow 800 Pro",
        level=level,
        soc_set=soc_set,
    )

    device.refresh_discharge_state()

    assert device.state is expected_state


def test_base_device_has_no_taper_by_default(hass):
    """Base ZendureDevice should return None for taper_charge_limit (no taper)."""
    device = make_device(hass, level=95)

    assert device.taper_charge_limit is None
    assert device.effective_charge_limit == device.charge_limit


def test_sf800_pro_socnearlyfull_state_uses_nearly_full_soc_limit_sensor(hass):
    """SOCNEARLYFULL should expose a distinct SoC Limit sensor value."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro-sensor",
        product_model="SolarFlow 800 Pro",
        level=95,
        soc_set=100,
    )

    device.refresh_discharge_state()

    assert device.state is DeviceState.SOCNEARLYFULL
    assert device.socLimit.asInt == SocLimitState.NEARLY_FULL
    state_sensor = cast("ZendureSensor", device.entities["state"])
    assert state_sensor.asInt == DeviceState.SOCNEARLYFULL.value
    assert state_sensor.translation_key == "device_state"


def test_raw_device_state_update_does_not_overwrite_derived_state_sensor(hass):
    """Device-level raw state telemetry should not replace the derived device state sensor."""
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="sf800-pro-derived-state",
        product_model="SolarFlow 800 Pro",
        level=95,
        soc_set=100,
    )
    state_sensor = cast("ZendureSensor", device.entities["state"])

    assert device.state is DeviceState.SOCNEARLYFULL
    assert state_sensor.asInt == DeviceState.SOCNEARLYFULL.value

    changed = device.entityUpdate("state", 1)

    assert changed is False
    assert device.state is DeviceState.SOCNEARLYFULL
    assert state_sensor.asInt == DeviceState.SOCNEARLYFULL.value
