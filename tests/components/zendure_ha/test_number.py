"""Tests for ZendureNumber clamping and range handling."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberMode

from custom_components.zendure_ha.number import ZendureNumber

from .common import make_device


def _make_number(device: Any, onwrite: Any, maximum: int = 2400, minimum: int = 0) -> ZendureNumber:
    """Create a ZendureNumber test instance."""
    return ZendureNumber(
        device,
        "testLimit",
        onwrite,
        None,
        "W",
        "power",
        maximum,
        minimum,
        NumberMode.SLIDER,
    )


async def test_set_value_within_range_passes_through_unclamped(hass: Any) -> None:
    """Set value within device limits passes through without modification."""
    device = make_device(hass)
    calls: list[int] = []
    number = _make_number(device, lambda _entity, value: calls.append(value))

    await number.async_set_native_value(1500)

    assert calls == [1500]


async def test_set_value_above_narrowed_device_max_is_clamped(hass: Any) -> None:
    """Set value above narrowed runtime limit is clamped to device max."""
    device = make_device(hass)
    calls: list[int] = []
    number = _make_number(device, lambda _entity, value: calls.append(value))
    number.update_range(0, 800)  # Device narrowed its usable range at runtime

    await number.async_set_native_value(2400)

    assert calls == [800]
    # Advertised max must stay wide to prevent HA core rejecting higher setpoints
    assert number._attr_native_max_value == 2400


async def test_set_value_below_device_min_is_clamped(hass: Any) -> None:
    """Set value below device minimum is clamped to device min."""
    device = make_device(hass)
    calls: list[int] = []
    number = _make_number(device, lambda _entity, value: calls.append(value), minimum=100)

    await number.async_set_native_value(0)

    assert calls == [100]


async def test_update_range_never_narrows_advertised_max(hass: Any) -> None:
    """Update range updates device max but never narrows native max value."""
    device = make_device(hass)
    number = _make_number(device, lambda _entity, _value: None)

    number.update_range(0, 800)

    assert number._attr_native_max_value == 2400
    assert number.device_max == 800


async def test_async_onwrite_is_awaited(hass: Any) -> None:
    """Async onwrite handler is properly awaited on value change."""
    device = make_device(hass)
    calls: list[int] = []

    async def onwrite(_entity: Any, value: int) -> None:
        calls.append(value)

    number = _make_number(device, onwrite)

    await number.async_set_native_value(500)

    assert calls == [500]
