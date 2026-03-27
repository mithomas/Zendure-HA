"""Device behavior tests."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from custom_components.zendure_ha.binary_sensor import ZendureBinarySensor
from custom_components.zendure_ha.const import AcMode, ConnectionMode, DeviceState, SmartMode
from custom_components.zendure_ha.device import CONST_HEADER, CONST_HEADER_CLOSE
from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro
from custom_components.zendure_ha.sensor import ZendureSensor

from .common import make_device


def test_bypass_entity_is_restored_as_a_binary_sensor(hass):
    """Bypass should expose the binary-sensor API expected by manager routing."""
    device = make_device(hass)

    assert isinstance(device.byPass, ZendureBinarySensor)
    assert device.byPass.is_on is False

    device.byPass.update_value(1)

    assert device.byPass.is_on is True


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
    ("level", "min_soc", "reserve", "soc_limit", "expected_state"),
    [
        (4, 5, 10, 0, DeviceState.SOCEMPTY),
        (5, 5, 10, 0, DeviceState.SOCEMPTY),
        (6, 5, 10, 0, DeviceState.SOCRESERVE),
        (10, 5, 10, 0, DeviceState.SOCRESERVE),
        (11, 5, 10, 0, DeviceState.INACTIVE),
        (8, 10, 5, 0, DeviceState.SOCEMPTY),
        (8, 5, 10, SmartMode.SOCEMPTY, DeviceState.SOCEMPTY),
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
    device.socLimit.update_value(soc_limit)
    device.electricLevel.update_value(level)

    device.refresh_discharge_state()

    assert device.state is expected_state


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
    device.discharge_recovery_active._attr_is_on = recovery_active

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
    device.discharge_recovery_active._attr_is_on = recovery_active

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
    device.discharge_recovery_active._attr_is_on = starting_recovery
    device.electricLevel.update_value(level)

    blocked = device.is_discharge_blocked(activate_margin_window=activate_margin_window)

    assert blocked is expected_blocked
    assert device.discharge_recovery_active._attr_is_on is expected_recovery
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
    device.discharge_recovery_active._attr_is_on = recovery_active

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
    """Verify HTTP retries use Connection: close to prevent stale pooled sockets."""

    def test_const_header_no_connection_close(self):
        """CONST_HEADER must not contain Connection: close (keep-alive by default)."""
        assert "Connection" not in CONST_HEADER

    def test_const_header_close_has_connection_close(self):
        """CONST_HEADER_CLOSE must contain Connection: close."""
        assert CONST_HEADER_CLOSE["Connection"] == "close"

    async def test_http_get_uses_keepalive_when_healthy(self, hass):
        """httpGet must use normal headers when there are no prior failures."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-hdr",
            product_model="SolarFlow 800 Pro",
        )
        device.ipAddress = "192.168.1.99"

        mock_response = MagicMock()
        mock_response.raise_for_status = Mock()
        mock_response.text = AsyncMock(return_value='{"properties": {"solarInputPower": 100}}')
        device.session.get = AsyncMock(return_value=mock_response)

        await device.httpGet("properties/report")

        call_kwargs = device.session.get.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert "Connection" not in headers

    async def test_http_get_sends_connection_close_after_failure(self, hass):
        """httpGet must pass Connection: close after a prior failure."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-hdr2",
            product_model="SolarFlow 800 Pro",
        )
        device.ipAddress = "192.168.1.99"
        device._http_failures = 1

        mock_response = MagicMock()
        mock_response.raise_for_status = Mock()
        mock_response.text = AsyncMock(return_value='{"properties": {"solarInputPower": 100}}')
        device.session.get = AsyncMock(return_value=mock_response)

        await device.httpGet("properties/report")

        call_kwargs = device.session.get.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Connection"] == "close"

    async def test_http_post_sends_connection_close_after_failure(self, hass):
        """httpPost must pass Connection: close after a prior failure."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-hdr3",
            product_model="SolarFlow 800 Pro",
        )
        device.ipAddress = "192.168.1.99"
        device._http_failures = 2

        mock_response = MagicMock()
        mock_response.raise_for_status = Mock()
        device.session.post = AsyncMock(return_value=mock_response)

        await device.httpPost("properties/write", {"properties": {"outputLimit": 100}})

        call_kwargs = device.session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Connection"] == "close"
