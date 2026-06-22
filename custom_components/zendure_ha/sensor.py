"""Interfaces with the Zendure Integration api sensors."""

import logging
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util
from homeassistant.util.dt import parse_datetime

from .entity import EntityDevice, EntityZendure

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Zendure sensor."""
    ZendureSensor.add = async_add_entities


class ZendureSensor(  # pyright: ignore[reportIncompatibleVariableOverride]
    EntityZendure,
    SensorEntity,
):
    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        template: Template | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        stateclass: Any | None = None,
        precision: int | None = None,
        factor: int = 1,
        state: Any = None,
        icon: str | None = None,
        translation_key: str | None = None,
    ) -> None:
        """Initialize a Zendure entity."""
        super().__init__(device, uniqueid, "sensor", translation_key)
        self.entity_description = SensorEntityDescription(
            key=uniqueid,
            name=uniqueid,
            native_unit_of_measurement=uom,
            device_class=deviceclass,
            state_class=stateclass,
            icon=icon,
        )
        self._value_template: Template | None = template
        if precision is not None:
            self._attr_suggested_display_precision = precision
        if state is not None:
            self._attr_native_value = state
        self.factor = factor

    def _filter_value(self, _new_value: Any) -> bool:
        """Filter value before applying it. Return True to accept, False to drop."""
        return True

    def update_value(self, value: Any) -> bool:
        try:
            new_value = (
                self._value_template.async_render_with_possible_json_value(value, None)
                if self._value_template is not None
                else value
            )
            if self.factor != 1:
                try:
                    new_value = float(new_value) / self.factor
                except ValueError:
                    new_value = 0

            if not self._filter_value(new_value):
                return False

            if new_value != self._attr_native_value:
                self._attr_native_value = new_value
                if self.hass:
                    self.write_ha_state()
                return True

        except Exception as err:
            self._attr_native_value = value
            _LOGGER.error("Error %s setting state: %s => %s", err, self._attr_unique_id, value)
            _LOGGER.error(traceback.format_exc())
        return False

    @property
    def asNumber(self) -> int | float:
        """Return the current value of the sensor."""
        if self._attr_native_value is None:
            return 0
        return self._attr_native_value if isinstance(self._attr_native_value, (int, float)) else 0

    @property
    def asInt(self) -> int:
        """Return the current value of the sensor."""
        if self._attr_native_value is None:
            return 0
        return int(self._attr_native_value / self.factor) if isinstance(self._attr_native_value, (int, float)) else 0


class ZendureTemperatureSensor(ZendureSensor):
    """Sensor specifically for temperature readings with filtering."""

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        template: Template | None = None,
        precision: int | None = None,
        state: Any = None,
        icon: str | None = None,
        translation_key: str | None = None,
    ) -> None:
        """Initialize the temperature sensor."""
        super().__init__(
            device=device,
            uniqueid=uniqueid,
            template=template,
            uom="°C",
            deviceclass="temperature",
            stateclass="measurement",
            precision=precision,
            factor=1,
            state=state,
            icon=icon,
            translation_key=translation_key,
        )
        self._last_drop_time: datetime | None = None

    def _filter_value(self, new_value: Any) -> bool:
        """Filter physically impossible or rapidly dropping temperatures."""
        try:
            val = float(new_value)
        except (ValueError, TypeError):
            return True

        # Ignore physically impossible low temperatures
        if val < -30.0:  # noqa: PLR2004
            return False

        try:
            prev_val = float(str(self._attr_native_value))
        except (ValueError, TypeError):
            prev_val = None

        if prev_val is not None and val < prev_val - 15:
            now = dt_util.utcnow()
            if self._last_drop_time is None:
                self._last_drop_time = now
                return False

            if (now - self._last_drop_time).total_seconds() <= 120:  # noqa: PLR2004
                return False

        self._last_drop_time = None
        return True


class ZendureRestoreSensor(  # pyright: ignore[reportIncompatibleVariableOverride]
    ZendureSensor,
    RestoreEntity,
):
    """Representation of a Zendure sensor entity with restore."""

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        template: Template | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        stateclass: Any | None = None,
        precision: int | None = None,
    ) -> None:
        """Initialize a select entity."""
        super().__init__(device, uniqueid, template, uom, deviceclass, stateclass, precision)
        self.last_value = 0
        self.lastValueUpdate = dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        init_value = None if self.device_class in ["date", "timestamp"] else 0.0

        self._attr_native_value = init_value
        state = await self.async_get_last_state()
        try:
            self._attr_native_value = (
                init_value
                if state is None
                else parse_datetime(state.state)
                if self.device_class in ["date", "timestamp"]
                else float(state.state)
            )
            if state is not None and (lr := state.attributes.get("last_reset")):
                self._attr_last_reset = parse_datetime(lr)
            _LOGGER.debug("Restored state for %s: %s", self.entity_id, self._attr_native_value)
        except ValueError:
            self._attr_native_value = init_value

        # Fall back to recorder statistics for aggregation sensors with missing/zero restore
        if self._attr_native_value in (0.0, 0, None) and self.state_class in ("total", "total_increasing"):
            recovered = await self._recover_from_statistics()
            if recovered is not None:
                self._attr_native_value = recovered
                _LOGGER.info("Recovered %s from statistics: %s", self.entity_id, recovered)

    async def _recover_from_statistics(self) -> float | None:
        """Try to recover value from recorder statistics."""
        try:
            from homeassistant.components.recorder.statistics import (
                get_last_short_term_statistics,
                get_last_statistics,
            )

            columns: set[Literal["last_reset", "max", "mean", "min", "state", "sum"]] = {"sum", "state"}
            for getter in (get_last_short_term_statistics, get_last_statistics):
                stats = await self.hass.async_add_executor_job(getter, self.hass, 1, self.entity_id, True, columns)
                if stats and self.entity_id in stats:
                    row = stats[self.entity_id][0]
                    value = row.get("sum") or row.get("state")
                    if value is not None and value != 0:
                        return float(value)
        except Exception as e:
            _LOGGER.debug("Could not recover from statistics for %s: %s", self.entity_id, e)
        return None

    def aggregate(self, time: datetime, value: Any) -> None:
        # Get the kWh value from the last value and the time since the last update
        value = float(value) if isinstance(value, (int, float)) else 0.0
        if (
            self.last_reset is None or self.last_reset.date() != time.date()
        ) and self.state_class != "total_increasing":
            self._attr_native_value = 0.0
            self._attr_last_reset = time
        else:
            try:
                kWh = self.last_value * (time.timestamp() - self.lastValueUpdate.timestamp()) / 3600000
                self._attr_native_value = kWh + (
                    float(self._attr_native_value) if isinstance(self._attr_native_value, (int, float)) else 0.0
                )
            except Exception as e:
                if not isinstance(self.state, (int, float)):
                    self._attr_native_value = 0.0

                _LOGGER.error("Unable to update aggregation %s!", e)

        self.last_value = value
        self.lastValueUpdate = time
        self.write_ha_state()


class ZendureCalcSensor(ZendureSensor):
    """Representation of a Zendure Calculated Sensor."""

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        calculate: Callable[[Any], Any] | None = None,
        uom: str | None = None,
        deviceclass: Any | None = None,
        stateclass: Any | None = None,
        precision: int | None = None,
    ) -> None:
        """Initialize a Zendure entity."""
        super().__init__(device, uniqueid, None, uom, deviceclass, stateclass, precision)
        self.calculate = calculate

    def update_value(self, value: Any) -> bool:
        try:
            new_value = (
                self._value_template.async_render_with_possible_json_value(value, None)
                if self._value_template is not None
                else value
            )

            if self.hass and new_value != self._attr_native_value and self.calculate is not None:
                self._attr_native_value = self.calculate(new_value)
                self.write_ha_state()
                return True

        except Exception as err:
            self._attr_native_value = value
            _LOGGER.error("Error %s setting state: %s => %s", err, self._attr_unique_id, value)
            _LOGGER.error(traceback.format_exc())
        return False

    def calculate_version(self, value: Any) -> Any:
        """Calculate the version from the value."""
        version = int(value)
        version = (
            f"v{(version & 0xF000) >> 12}.{(version & 0x0F00) >> 8}.{version & 0x00FF}"
            if version > 10  # noqa: PLR2004
            else "not provided"
            if version <= 0
            else str(version)
        )
        if self._attr_native_value == version:
            return version
        if (
            self._attr_translation_key in {"soft_version", "master_soft_version", "master_firmware_version"}
            and self.device_info is not None
            and self.device_info.get("sw_version") != version
        ):
            self.device.updateVersion(version)

        return version
