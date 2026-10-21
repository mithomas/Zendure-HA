"""Tests for the Zendure config flow and options flow."""

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector

from custom_components.zendure_ha.config_flow import (
    ZendureConfigFlow,
    ZendureOptionsFlowHandler,
)
from custom_components.zendure_ha.const import (
    CONF_P1METER,
)

from .common import config_entry_data, make_config_entry


async def test_config_flow_p1_selector(hass: HomeAssistant) -> None:
    """Test that the initial config flow presents a power sensor selector for the P1 meter."""
    flow = ZendureConfigFlow()
    flow.hass = hass

    result = await flow.async_step_user()
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "user"

    data_schema = result.get("data_schema")
    assert isinstance(data_schema, vol.Schema)

    p1_selector = None
    for key, val in data_schema.schema.items():
        if getattr(key, "schema", None) == CONF_P1METER or key == CONF_P1METER:
            p1_selector = val
            break

    assert isinstance(p1_selector, selector.EntitySelector)
    assert p1_selector.config.get("domain") == ["sensor"]
    assert p1_selector.config.get("device_class") == ["power"]


async def test_options_flow_p1_selector(hass: HomeAssistant) -> None:
    """Test that the options flow presents a power sensor selector for the P1 meter."""
    entry = make_config_entry(config_entry_data() | {CONF_P1METER: "sensor.custom_p1_meter"})
    entry.add_to_hass(hass)

    handler = ZendureOptionsFlowHandler()
    handler.hass = hass
    handler.handler = entry.entry_id

    result = await handler.async_step_init()
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "init"

    data_schema = result.get("data_schema")
    assert isinstance(data_schema, vol.Schema)

    p1_selector = None
    for key, val in data_schema.schema.items():
        if getattr(key, "schema", None) == CONF_P1METER or key == CONF_P1METER:
            p1_selector = val
            break

    assert isinstance(p1_selector, selector.EntitySelector)
    assert p1_selector.config.get("domain") == ["sensor"]
    assert p1_selector.config.get("device_class") == ["power"]
