"""Tests for the Zendure Select platform."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.select import ZendureRestoreSelect

pytestmark = pytest.mark.asyncio


async def test_zendure_restore_select_preserves_restored_state_before_options_are_set(hass: HomeAssistant):
    """
    Test ZendureRestoreSelect preserves restored state before options are set.

    Test that ZendureRestoreSelect correctly preserves its restored state during
    async_added_to_hass even if its _options dictionary doesn't contain the
    restored key yet.
    """
    device = MagicMock()
    device.hass = hass
    device.entity_prefix = "test_device"

    # Start with an empty or default options dictionary, like during startup.
    options = {"__disabled__": "none"}

    # Create the select entity.
    select = ZendureRestoreSelect(
        device=device,
        uniqueid="test_select",
        options=options,
        onchanged=None,
        current="__disabled__",
    )

    # Mock the restore state.
    mock_state = MagicMock()
    mock_state.state = "Test Device Name"
    mock_state.attributes = {"selected_key": "device_12345"}

    with patch("custom_components.zendure_ha.select.RestoreEntity.async_get_last_state", return_value=mock_state):
        await select.async_added_to_hass()

    # The entity should have trusted the restored key and state, even though
    # 'device_12345' is not yet in the options dictionary.
    assert select._selected_key == "device_12345"
    assert select._attr_current_option == "Test Device Name"

    # Later, when the API loads, the manager will call setDict with the real options.
    new_options = {
        "__disabled__": "none",
        "device_12345": "Test Device Name",
        "device_67890": "Other Device Name",
    }
    select.setDict(new_options)

    # The selection should remain stable.
    assert select._selected_key == "device_12345"
    assert select._attr_current_option == "Test Device Name"
