"""Interfaces with the Zendure Integration binairy sensors."""

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.template import Template

from .entity import EntityDevice, EntityZendure

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Zendure binary_sensor."""
    ZendureBinarySensor.add = async_add_entities


class ZendureBinarySensor(  # pyright: ignore[reportIncompatibleVariableOverride]
    EntityZendure,
    BinarySensorEntity,
):
    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        template: Template | None = None,
        deviceclass: Any | None = None,
        initial_state: bool = False,
    ) -> None:
        """Initialize a binary sensor entity."""
        super().__init__(device, uniqueid, "binary_sensor")
        self.entity_description = BinarySensorEntityDescription(key=uniqueid, name=uniqueid, device_class=deviceclass)
        self._attr_is_on = initial_state
        self._value_template: Template | None = template

    _FALSY = frozenset({"0", "no", "false", "off", "none", ""})  # conversion required for local MQTT

    def update_value(self, value: Any) -> bool:
        try:
            if self._value_template is not None:
                is_on = int(self._value_template.async_render_with_possible_json_value(value, None)) != 0
            elif isinstance(value, str):
                s = value.lower()
                is_on = s not in self._FALSY and not s.startswith("not_")
            else:
                is_on = int(value) != 0

            if self._attr_is_on == is_on:
                return False

            self._attr_is_on = is_on
            self.write_ha_state()
        except Exception as err:
            _LOGGER.error("Error %s setting state: %s => %s", err, self._attr_unique_id, value)

        return True
