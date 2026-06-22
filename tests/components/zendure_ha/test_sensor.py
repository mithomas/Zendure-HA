"""Tests for the Zendure sensor."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from custom_components.zendure_ha.sensor import ZendureSensor, ZendureTemperatureSensor


def test_temperature_sensor_filtering() -> None:
    """Test filtering of erroneous temperature readings."""
    device = Mock()
    device.entity_prefix = "test_dev"
    device.entities = {}
    device.checkEntity = {}
    sensor = ZendureTemperatureSensor(device, "hyperTmp")

    # Initial state (None) allows any valid reading above -30
    assert sensor.update_value(10.0) is True
    assert sensor._attr_native_value == 10.0

    # Completely bogus reading (below -30) is ignored immediately
    assert sensor.update_value(-40.0) is False
    assert sensor._attr_native_value == 10.0

    # Valid reading passes through
    assert sensor.update_value(11.0) is True
    assert sensor._attr_native_value == 11.0

    # Drop of >15 ignored temporarily
    with patch("homeassistant.util.dt.utcnow") as mock_utcnow:
        base_time = datetime.now(UTC)
        mock_utcnow.return_value = base_time
        assert sensor.update_value(-15.0) is False
        assert sensor._attr_native_value == 11.0

        # 1 minute later, still ignored
        mock_utcnow.return_value = base_time + timedelta(minutes=1)
        assert sensor.update_value(-15.0) is False
        assert sensor._attr_native_value == 11.0

        # Normal reading arriving during the window is accepted and resets the timer
        assert sensor.update_value(10.0) is True
        assert sensor._attr_native_value == 10.0

        # Subsequent drop needs to wait 2 minutes again
        mock_utcnow.return_value = base_time + timedelta(minutes=1, seconds=30)
        assert sensor.update_value(-15.0) is False
        assert sensor._attr_native_value == 10.0

        # 2 minutes later, accepted
        mock_utcnow.return_value = base_time + timedelta(minutes=3, seconds=31)
        assert sensor.update_value(-15.0) is True
        assert sensor._attr_native_value == -15.0

    # Increase is always accepted
    assert sensor.update_value(10.0) is True
    assert sensor._attr_native_value == 10.0

    # Valid drop is accepted immediately
    assert sensor.update_value(-5.0) is True
    assert sensor._attr_native_value == -5.0

    # String values are correctly parsed and filtered
    assert sensor.update_value("-273.1") is False
    assert sensor._attr_native_value == -5.0

    # String value drop > 15 is ignored temporarily
    with patch("homeassistant.util.dt.utcnow") as mock_utcnow:
        mock_utcnow.return_value = base_time
        sensor._last_drop_time = None
        assert sensor.update_value("-25.0") is False
        assert sensor._attr_native_value == -5.0

        # String drop is accepted after 2 minutes
        mock_utcnow.return_value = base_time + timedelta(minutes=2, seconds=1)
        assert sensor.update_value("-25.0") is True
        assert sensor._attr_native_value == "-25.0"

    # Non-numeric string is passed through directly without raising an exception in the filter
    assert sensor.update_value("unavailable") is True
    assert sensor._attr_native_value == "unavailable"


def test_non_temperature_sensor_no_filtering() -> None:
    """Test that non-temperature sensors are not filtered."""
    device = Mock()
    device.entity_prefix = "test_dev"
    device.entities = {}
    device.checkEntity = {}
    sensor = ZendureSensor(device, "power", uom="W")

    assert sensor.update_value(100.0) is True
    assert sensor._attr_native_value == 100.0

    # Drops > 20 are accepted immediately
    assert sensor.update_value(50.0) is True
    assert sensor._attr_native_value == 50.0

    # Negative values are accepted immediately
    assert sensor.update_value(-40.0) is True
    assert sensor._attr_native_value == -40.0
