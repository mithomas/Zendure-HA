"""Manager-level reserve-threshold and discharge-candidate tests."""

from __future__ import annotations

import pytest

from .common import StubDevice, attach_devices, make_device, make_manager


def test_refresh_available_kwh_updates_when_device_thresholds_change(hass):
    """Per-device SoC and reserve updates should immediately refresh the manager aggregate."""
    manager = make_manager(hass)
    first = make_device(hass, device_id="device-1", level=20, min_soc=10, reserve=10, kwh=2.0)
    second = make_device(hass, device_id="device-2", level=30, min_soc=10, reserve=10, kwh=2.0)
    attach_devices(manager, first, second)

    assert manager.availableKwh.asNumber == pytest.approx(0.6)

    second.entityUpdate("socReserve", 25)
    assert manager.availableKwh.asNumber == pytest.approx(0.3)

    second.entityUpdate("electricLevel", 25)
    assert manager.availableKwh.asNumber == pytest.approx(0.2)


def test_refresh_available_kwh_updates_when_capacity_changes(hass):
    """Capacity-driven availability changes should propagate through the callback path."""
    manager = make_manager(hass)
    device = make_device(hass, level=30, min_soc=10, reserve=10, kwh=2.0)
    attach_devices(manager, device)

    assert manager.availableKwh.asNumber == pytest.approx(0.4)

    device.kWh = 4.0
    device.totalKwh.update_value(4.0)
    device.refresh_discharge_state()

    assert manager.availableKwh.asNumber == pytest.approx(0.8)


def test_collect_discharge_candidates_includes_idle_devices_with_produced_power(hass):
    """Idle devices that can only contribute produced power should join discharge candidates."""
    manager = make_manager(hass)
    active = StubDevice(name="active", deviceId="active", level=40, home_output=100)
    idle_produced = StubDevice(name="idle solar", deviceId="idle-solar", level=20, pwr_produced=-120)
    idle_plain = StubDevice(name="idle", deviceId="idle", level=60)

    candidates = manager._collect_discharge_candidates([active], [idle_plain, idle_produced])

    assert candidates == [idle_produced, active]
