"""Device behavior tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock


from custom_components.zendure_ha.device import CONST_HEADER, CONST_HEADER_CLOSE
from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro

from .common import make_device


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
