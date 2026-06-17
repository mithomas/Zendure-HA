"""Interfaces with the Zendure Integration number."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
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
    """Set up the Zendure number."""
    ZendureNumber.add = async_add_entities


class ZendureNumber(  # pyright: ignore[reportIncompatibleVariableOverride]
    EntityZendure,
    NumberEntity,
):
    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        onwrite: Callable | None,
        template: Template | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        maximum: int = 2000,
        minimum: int = 0,
        mode: NumberMode = NumberMode.AUTO,
        factor: int = 1,
        doupdate: bool = False,
    ) -> None:
        """Initialize a number entity."""
        super().__init__(device, uniqueid, "number")
        self.entity_description = NumberEntityDescription(
            key=uniqueid,
            name=uniqueid,
            native_unit_of_measurement=uom,
            device_class=deviceclass,
        )

        self._value_template: Template | None = template
        self._onwrite = onwrite
        self._attr_native_max_value = maximum
        self._attr_native_min_value = minimum
        self._attr_mode = mode
        self._attr_native_value = None
        self.factor = factor
        self.doupdate = doupdate

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None:
            return None

        value_str = str(value).strip().lower()
        if value_str in ("", STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def update_value(self, value: Any) -> bool:
        rendered_value = (
            self._value_template.async_render_with_possible_json_value(value, None)
            if self._value_template is not None
            else value
        )

        parsed_value = self._as_float(rendered_value)
        if parsed_value is None:
            _LOGGER.warning("Invalid number state for %s: %s", self._attr_unique_id, rendered_value)
            return False

        try:
            new_value = int(parsed_value) / self.factor

            current_state = self._as_float(self.state) if self.hass else None
            if self._attr_native_value == new_value and current_state == new_value:
                return False

            self._attr_native_value = new_value
            self.write_ha_state()
        except Exception as err:
            _LOGGER.error("Error %s setting state: %s => %s", err, self._attr_unique_id, value)
            return False

        return True

    async def async_set_native_value(self, value: float) -> None:
        """Set the value."""
        if self.doupdate:
            self._attr_native_value = value
            self.write_ha_state()

        if self._onwrite is not None:
            if asyncio.iscoroutinefunction(self._onwrite):
                await self._onwrite(self, int(self.factor * value))
            else:
                self._onwrite(self, int(self.factor * value))

    def update_range(self, minimum: int, maximum: int) -> None:
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self.write_ha_state()

    @property
    def asNumber(self) -> int | float:
        """Return the current value of the sensor."""
        return self._attr_native_value if isinstance(self._attr_native_value, (int, float)) else 0

    @property
    def asInt(self) -> int:
        """Return the current value as int."""
        return int(self._attr_native_value) if isinstance(self._attr_native_value, (int, float)) else 0


class ZendureRestoreNumber(  # pyright: ignore[reportIncompatibleVariableOverride]
    ZendureNumber,
    RestoreNumber,
):
    """Representation of a Zendure number entity with restore."""

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        onwrite: Callable | None,
        template: Template | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        maximum: int = 2000,
        minimum: int = 0,
        mode: NumberMode = NumberMode.AUTO,
        doupdate: bool = False,
        initial_value: float = 0,
    ) -> None:
        """Initialize a number entity."""
        super().__init__(device, uniqueid, onwrite, template, uom, deviceclass, maximum, minimum, mode, 1, doupdate)
        self._attr_native_value = initial_value

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if number_data := await self.async_get_last_number_data():
            if number_data.native_min_value is not None:
                self._attr_native_min_value = number_data.native_min_value
            if number_data.native_max_value is not None:
                self._attr_native_max_value = number_data.native_max_value
            if number_data.native_value is not None:
                self._attr_native_value = number_data.native_value

        if self._onwrite is not None:
            if asyncio.iscoroutinefunction(self._onwrite):
                await self._onwrite(self, self._attr_native_value)
            else:
                self._onwrite(self, self._attr_native_value)
