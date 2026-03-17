"""Home Assistant setup smoke tests."""

from unittest.mock import AsyncMock, Mock, patch

from custom_components.zendure_ha import (
    PLATFORMS,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.zendure_ha.const import DOMAIN

from .common import make_config_entry


async def test_async_setup_entry_smoke(hass):
    """Set up and unload the config entry with Home Assistant entrypoints patched around HA dependencies."""
    entry = make_config_entry()
    mqtt_cloud = Mock()
    mqtt_cloud.is_connected.return_value = False
    mqtt_local = Mock()
    mqtt_local.is_connected.return_value = False

    with (
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock()
        ) as mock_forward,
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ) as mock_unload,
        patch(
            "custom_components.zendure_ha.ZendureManager.loadDevices", AsyncMock()
        ) as mock_load,
        patch(
            "custom_components.zendure_ha.ZendureManager.async_config_entry_first_refresh",
            AsyncMock(),
        ) as mock_first_refresh,
        patch("custom_components.zendure_ha.Api.mqttCloud", mqtt_cloud),
        patch("custom_components.zendure_ha.Api.mqttLocal", mqtt_local),
    ):
        assert await async_setup_entry(hass, entry)

        assert entry.domain == DOMAIN
        assert entry.runtime_data is not None
        mock_forward.assert_awaited_once_with(entry, PLATFORMS)
        mock_load.assert_awaited_once()
        mock_first_refresh.assert_awaited_once()

        assert await async_unload_entry(hass, entry)
        mock_unload.assert_awaited_once_with(entry, PLATFORMS)
