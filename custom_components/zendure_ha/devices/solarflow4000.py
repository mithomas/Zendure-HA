"""Module for the Solarflow4000 device integration in Home Assistant."""

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.device import ZendureZenSdk
from custom_components.zendure_ha.sensor import ZendureRestoreSensor, ZendureSensor

_LOGGER = logging.getLogger(__name__)


class SolarFlow4000ACPlus(ZendureZenSdk):
    """Zendure SolarFlow 4000 AC+ device."""

    def __init__(self, hass: HomeAssistant, deviceId: str, prodName: str, definition: Any) -> None:
        """Initialise SolarFlow4000ACPlus."""
        super().__init__(hass, deviceId, prodName, definition["productModel"], definition)
        self.setLimits(-3200, 2400)
        self.maxSolar = -2400
        self.offGrid = ZendureSensor(self, "gridOffPower", None, "W", "power", "measurement")
        self.aggrOffGrid = ZendureRestoreSensor(self, "aggrGridOffPower", None, "kWh", "energy", "total", 2)

    @property
    def pwr_offgrid(self) -> int:
        """Get the offgrid power."""
        return self.offGrid.asInt


SolarFlow4000AC_Plus = SolarFlow4000ACPlus
