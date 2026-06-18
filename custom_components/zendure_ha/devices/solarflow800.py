"""Module for SolarFlow800 integration."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureZenSdk, ZendureZenSDKWithLocalMQTT
from custom_components.zendure_ha.sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)

SF800_PRO_TAPER_LIMITS = (
    (1, 150),
    (2, 200),
    (4, 250),
)


class SolarFlow800(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition)
        self.setLimits(-1000, 800)
        self.maxSolar = -1200


class SolarFlow800Plus(ZendureZenSdk):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800Plus."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition)
        self.setLimits(-1000, 800)
        self.maxSolar = -1500


class SolarFlow800Pro(ZendureZenSDKWithLocalMQTT):
    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow800Pro."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition)
        self.setLimits(-1000, 800)
        self.maxSolar = -1200
        self.offGrid = ZendureSensor(self, "gridOffPower", None, "W", "power", "measurement")
        self.aggrOffGrid = ZendureRestoreSensor(self, "aggrGridOffPower", None, "kWh", "energy", "total", 2)

    @property
    def pwr_offgrid(self) -> int:
        """Get the offgrid power."""
        return self.offGrid.asInt

    @property
    def supports_bypass(self) -> bool:
        """Return whether the device supports explicit bypass mode."""
        return True

    @property
    def taper_charge_limit(self) -> int | None:
        """Return the charge rate cap in watts when near-full, or None below the taper range."""
        distance_to_target = self.socSet.asNumber - self.electricLevel.asNumber
        if distance_to_target <= 0:
            return None
        for distance, limit in SF800_PRO_TAPER_LIMITS:
            if distance_to_target <= distance:
                return limit
        return None

    async def power_bypass(self) -> int:
        """Put the SF800 Pro into explicit bypass mode."""
        await self.doCommand({"properties": {"smartMode": 0, "acMode": 1, "outputLimit": 0, "inputLimit": 0}})
        return 0
