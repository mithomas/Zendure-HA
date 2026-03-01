"""Device behavior tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch

from custom_components.zendure_ha.binary_sensor import ZendureBinarySensor
from custom_components.zendure_ha.const import AcMode, ConnectionMode
from custom_components.zendure_ha.device import CONST_HEADER, CONST_HEADER_CLOSE
from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro

from .common import make_device


def test_bypass_entity_is_restored_as_a_binary_sensor(hass):
    """Bypass should expose the binary-sensor API expected by manager routing."""
    device = make_device(hass)

    assert isinstance(device.byPass, ZendureBinarySensor)
    assert device.byPass.is_on is False

    device.byPass.update_value(1)

    assert device.byPass.is_on is True


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
