"""Interfaces with the Zendure Integration."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import EntityDevice, EntityZendure

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Zendure select."""
    ZendureSelect.add = async_add_entities


class ZendureSelect(  # pyright: ignore[reportIncompatibleVariableOverride]
    EntityZendure,
    SelectEntity,
):
    """Representation of a Zendure select entity."""

    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        options: dict[Any, str],
        onchanged: Callable | None,
        current: Any | None = None,
    ) -> None:
        """Initialize a select entity."""
        super().__init__(device, uniqueid, "select")
        self.entity_description = SelectEntityDescription(key=uniqueid, name=uniqueid)
        self._options = options
        self._attr_options = list(options.values())
        self._selected_key = current if current in options else None
        if current and current in options:
            self._attr_current_option = options[current]
        else:
            self._selected_key = next(iter(options), None)
            self._attr_current_option = self._attr_options[0]
        self.onchanged = onchanged

    def setDict(self, options: dict[Any, str]) -> None:
        """Set the options for the select entity."""
        current_value = self.value
        self._options = options
        self._attr_options = list(options.values())
        if current_value in options:
            self._selected_key = current_value
            self._attr_current_option = options[current_value]
        elif self._attr_current_option not in self._attr_options:
            self._selected_key = next(iter(options), None)
            self._attr_current_option = self._attr_options[0]
        self.write_ha_state()

    def setList(self, options: list[str]) -> None:
        """Set the options for the select entity."""
        self._options = None
        self._selected_key = None
        self._attr_options = options
        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = self._attr_options[0]
        self.write_ha_state()

    def update_value(self, value: Any) -> bool:
        try:
            if self._options is None or value not in self._options:
                return False

            if self._options is not None:
                new_value = self._options[value]
                if value != self._selected_key or new_value != self._attr_current_option:
                    self._selected_key = value
                    self._attr_current_option = new_value
                    self.write_ha_state()

        except Exception as err:
            _LOGGER.error("Error %s setting state: %s => %s", err, self._attr_unique_id, value)
        return True

    async def async_select_option(self, option: str) -> None:
        """Update the current selected option."""
        self._attr_current_option = option
        if self._options is not None and not (
            self._selected_key in self._options and self._options[self._selected_key] == option
        ):
            self._selected_key = next((key for key, value in self._options.items() if value == option), None)
        value = self.value
        if self.onchanged:
            if asyncio.iscoroutinefunction(self.onchanged):
                await self.onchanged(self, value)
            else:
                self.onchanged(self, value)
        self.async_write_ha_state()

    @property
    def value(self) -> Any:
        if self._options is not None:
            if self._selected_key in self._options:
                return self._selected_key
            for key, value in self._options.items():
                if value == self._attr_current_option:
                    self._selected_key = key
                    return key
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Store the stable select key for restore-safe option updates."""
        value = self._selected_key if self._options is not None else None
        return None if value is None else {"selected_key": value}


class ZendureRestoreSelect(  # pyright: ignore[reportIncompatibleVariableOverride]
    ZendureSelect,
    RestoreEntity,
):
    """Representation of a Zendure select entity with restore."""

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        options: dict[Any, str],
        onchanged: Callable | None,
        current: Any | None = None,
    ) -> None:
        """Initialize a select entity."""
        super().__init__(device, uniqueid, options, onchanged, current)

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if state := await self.async_get_last_state():
            selected_key = state.attributes.get("selected_key")
            if selected_key is not None:
                self._selected_key = selected_key
                self._attr_current_option = (
                    self._options.get(selected_key, state.state) if self._options else state.state
                )
            else:
                self._selected_key = None
                self._attr_current_option = state.state
        else:
            self._selected_key = next(iter(self._options), None) if self._options is not None else None
            self._attr_current_option = self._attr_options[0]

        # do the onchanged callback
        if self.onchanged:
            if asyncio.iscoroutinefunction(self.onchanged):
                await self.onchanged(self, self.current_option)
            else:
                self.onchanged(self, self.current_option)
