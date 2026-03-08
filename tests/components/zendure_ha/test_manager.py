"""Manager-level reserve-threshold and routing tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import ANY, AsyncMock, Mock, call, patch

import pytest

from custom_components.zendure_ha.const import AcMode, DeviceState, ManagerMode
from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro

from .common import (
    attach_devices,
    make_device,
    make_manager,
)

LOW_SOC_DEVICE_CASES = (
    pytest.param(5, DeviceState.SOCEMPTY, id="socempty"),
    pytest.param(10, DeviceState.SOCRESERVE, id="socreserve"),
)


class TestAvailableKwh:
    def test_refresh_available_kwh_updates_when_device_thresholds_change(self, hass):
        """Two devices at 20% and 30% should update aggregate available kWh when one reserve and SoC change."""
        manager = make_manager(hass)
        first = make_device(hass, device_id="device-1", level=20, min_soc=10, reserve=10, kwh=2.0)
        second = make_device(hass, device_id="device-2", level=30, min_soc=10, reserve=10, kwh=2.0)
        attach_devices(manager, first, second)

        assert manager.availableKwh.asNumber == pytest.approx(0.6)

        second.entityUpdate("socReserve", 25)
        assert manager.availableKwh.asNumber == pytest.approx(0.3)

        second.entityUpdate("electricLevel", 25)
        assert manager.availableKwh.asNumber == pytest.approx(0.2)

    def test_refresh_available_kwh_updates_when_capacity_changes(self, hass):
        """A single 30% device should double aggregate available kWh when total capacity doubles from 2 to 4 kWh."""
        manager = make_manager(hass)
        device = make_device(hass, level=30, min_soc=10, reserve=10, kwh=2.0)
        attach_devices(manager, device)

        assert manager.availableKwh.asNumber == pytest.approx(0.4)

        device.kWh = 4.0
        device.totalKwh.update_value(4.0)
        device.refresh_discharge_state()

        assert manager.availableKwh.asNumber == pytest.approx(0.8)

class TestSmartMatchingPrimaryAware:
    async def test_includes_idle_devices_that_already_have_produced_power(self, hass):
        """Idle devices that already contribute produced power should still join discharge candidate selection."""
        active = make_device(
            hass,
            device_id="active",
            device_name="active",
            level=40,
            home_output=100,
            battery_output=100,
        )
        idle_produced = make_device(
            hass,
            device_id="idle-solar",
            device_name="idle solar",
            level=20,
            battery_input=120,
        )
        idle_plain = make_device(
            hass,
            device_id="idle",
            device_name="idle",
            level=60,
        )
        manager = make_manager(
            hass,
            devices=(active, idle_produced, idle_plain),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
        )
        active.power_get = AsyncMock(return_value=True)
        idle_produced.power_get = AsyncMock(return_value=True)
        idle_plain.power_get = AsyncMock(return_value=True)
        active.power_discharge = AsyncMock(side_effect=lambda power: power)
        idle_produced.power_discharge = AsyncMock(side_effect=lambda power: power)
        idle_plain.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        idle_produced.power_discharge.assert_awaited_once_with(100)
        active.power_discharge.assert_awaited_once_with(0)
        idle_plain.power_discharge.assert_awaited_once_with(0)

    async def test_stops_a_blocked_primary_before_discharge_allocation(self, hass):
        """A selected primary blocked at its floor should be stopped before the manager allocates discharge elsewhere."""
        primary = make_device(
            hass,
            device_id="primary",
            device_name="primary",
            level=40,
            home_output=100,
            battery_output=100,
        )
        other = make_device(
            hass,
            device_id="other",
            device_name="other",
            level=50,
            home_output=100,
            battery_output=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, other),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        other.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        other.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.is_discharge_blocked = Mock(return_value=True)

        await manager.powerChanged(0, False, datetime.now())

        assert primary.power_discharge.await_args_list[0] == call(0)
        other.power_discharge.assert_awaited_once_with(200)

    async def test_prioritizes_the_selected_primary_before_other_discharge_devices(self, hass):
        """When several devices can discharge, the selected primary should receive the first allocation."""
        primary = make_device(
            hass,
            device_id="primary",
            device_name="primary",
            level=60,
            home_output=100,
            battery_output=100,
        )
        other = make_device(
            hass,
            device_id="other",
            device_name="other",
            level=40,
            home_output=100,
            battery_output=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, other),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        other.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        other.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        primary.power_discharge.assert_awaited_once_with(200)
        other.power_discharge.assert_awaited_once_with(0)

    async def test_spills_discharge_remainder_to_the_secondary_when_the_primary_hits_its_limit(self, hass):
        """
        When demand exceeds the selected primary's discharge limit, the secondary should immediately take the battery-backed remainder.

        The encoded example has the primary already at its limit and another chunk of demand
        arriving on top, so the secondary must absorb the remainder in the same cycle.
        """
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-1200w",
            device_name="sf800 pro primary 1200w",
            product_model="SolarFlow 800 Pro",
            level=90,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=800,
            battery_output=800,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-1200w",
            device_name="sf800 pro secondary 1200w",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(400, False, datetime.now())

        primary.power_discharge.assert_awaited_once_with(800)
        secondary.power_discharge.assert_awaited_once_with(400)

    async def test_passes_current_solar_through_to_the_home_before_other_routing(self, hass):
        """Current solar should be passed through to home demand before the manager does other battery routing."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-pass-through",
            device_name="sf800 pro pass through",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=device.deviceId,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(200, False, datetime.now())

        assert device.state is DeviceState.SOCFULL
        device.power_discharge.assert_awaited_once_with(200)

    async def test_spends_primary_solar_before_secondary_solar_before_primary_battery(self, hass):
        """
        The selected primary's produced solar should be spent before secondary produced solar.

        With 400W of remaining home demand, the manager should first spend the primary's 350W of PV
        and only then use 50W from the secondary's available PV.
        """
        system_1 = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-solar-pass-through-701",
            device_name="sf800 pro secondary solar pass through",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=70,
        )
        system_2_primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-solar-plus-discharge-702",
            device_name="sf800 pro primary solar plus discharge",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=350,
        )
        system_1.name = "secondary solar 701"
        system_2_primary.name = "primary solar 702"
        manager = make_manager(
            hass,
            devices=(system_1, system_2_primary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=system_2_primary.deviceId,
        )
        system_1.power_get = AsyncMock(return_value=True)
        system_2_primary.power_get = AsyncMock(return_value=True)
        system_1.power_discharge = AsyncMock(side_effect=lambda power: power)
        system_2_primary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(400, False, datetime.now())

        system_1.power_discharge.assert_awaited_once_with(50)
        system_2_primary.power_discharge.assert_awaited_once_with(350)

    async def test_spends_secondary_solar_before_spilling_true_battery_remainder(self, hass):
        """
        Both produced solar sources should be consumed before any true battery spill is used.

        The selected primary still carries as much of the remainder as it can before a
        separate secondary battery receives the final spill remainder.
        """
        secondary_solar = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-produced-703",
            device_name="sf800 pro secondary produced spill",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=70,
        )
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-produced-704",
            device_name="sf800 pro primary produced spill",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=350,
        )
        spill_secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-spill-705",
            device_name="sf800 pro secondary spill target",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_output=200,
        )
        secondary_solar.name = "secondary produced 703"
        primary.name = "primary produced 704"
        spill_secondary.name = "spill secondary 705"
        manager = make_manager(
            hass,
            devices=(secondary_solar, primary, spill_secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        secondary_solar.power_get = AsyncMock(return_value=True)
        primary.power_get = AsyncMock(return_value=True)
        spill_secondary.power_get = AsyncMock(return_value=True)
        secondary_solar.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        spill_secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(700, False, datetime.now())

        secondary_solar.power_discharge.assert_awaited_once_with(70)
        primary.power_discharge.assert_awaited_once_with(800)
        spill_secondary.power_discharge.assert_awaited_once_with(30)

    async def test_two_system_demand_prefers_primary_battery_before_secondary_battery_when_no_solar_is_available(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-no-solar-both-batteries",
            device_name="sf800 pro primary no solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-no-solar-both-batteries",
            device_name="sf800 pro secondary no solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    async def test_two_system_demand_uses_primary_solar_before_any_secondary_contribution_when_only_primary_has_solar(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-primary-solar-both-batteries",
            device_name="sf800 pro primary primary solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-primary-solar-both-batteries",
            device_name="sf800 pro secondary primary solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    async def test_two_system_demand_uses_idle_secondary_solar_before_secondary_battery(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-secondary-solar-both-batteries",
            device_name="sf800 pro primary secondary solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-secondary-solar-both-batteries",
            device_name="sf800 pro secondary secondary solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(100)
        assert secondary.power_discharge.await_args_list[-1] == call(200)

    async def test_two_system_demand_uses_primary_solar_then_secondary_solar_when_both_batteries_are_available(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-both-solar-both-batteries",
            device_name="sf800 pro primary both solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-both-solar-both-batteries",
            device_name="sf800 pro secondary both solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_primary_battery_only_when_secondary_battery_is_socempty_and_no_solar(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-no-solar-primary-battery-only",
            device_name="sf800 pro primary no solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-no-solar-primary-battery-only",
            device_name="sf800 pro secondary no solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_rolls_primary_solar_into_primary_battery_when_secondary_battery_is_socempty(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-primary-solar-primary-battery-only",
            device_name="sf800 pro primary primary solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-primary-solar-primary-battery-only",
            device_name="sf800 pro secondary primary solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_secondary_solar_but_not_secondary_battery_when_only_primary_battery_is_available(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-secondary-solar-primary-battery-only",
            device_name="sf800 pro primary secondary solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-secondary-solar-primary-battery-only",
            device_name="sf800 pro secondary secondary solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(100)
        assert secondary.power_discharge.await_args_list[-1] == call(200)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_keeps_secondary_battery_unavailable_even_when_both_systems_have_solar(
        self, hass, low_soc_level, low_soc_state
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-both-solar-primary-battery-only",
            device_name="sf800 pro primary both solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-both-solar-primary-battery-only",
            device_name="sf800 pro secondary both solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_secondary_battery_only_when_primary_battery_is_socempty_and_no_solar(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-no-solar-secondary-battery-only",
            device_name="sf800 pro primary no solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-no-solar-secondary-battery-only",
            device_name="sf800 pro secondary no solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_primary_solar_before_secondary_battery_when_primary_battery_is_socempty(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-primary-solar-secondary-battery-only",
            device_name="sf800 pro primary primary solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-primary-solar-secondary-battery-only",
            device_name="sf800 pro secondary primary solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(500, False, datetime.now())

        assert primary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_only_idle_secondary_solar_when_primary_battery_is_socempty(
        self, hass, low_soc_level, low_soc_state
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-secondary-solar-secondary-battery-only",
            device_name="sf800 pro primary secondary solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-secondary-solar-secondary-battery-only",
            device_name="sf800 pro secondary secondary solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(200)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_primary_solar_then_secondary_solar_before_secondary_battery_when_primary_is_socempty(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-both-solar-secondary-battery-only",
            device_name="sf800 pro primary both solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-both-solar-secondary-battery-only",
            device_name="sf800 pro secondary both solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_has_no_available_source_when_both_batteries_are_socempty_and_no_solar(
        self, hass, low_soc_level, low_soc_state
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-no-solar-no-batteries",
            device_name="sf800 pro primary no solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-no-solar-no-batteries",
            device_name="sf800 pro secondary no solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_only_primary_solar_when_both_batteries_are_socempty(
        self, hass, low_soc_level, low_soc_state
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-primary-solar-no-batteries",
            device_name="sf800 pro primary primary solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-primary-solar-no-batteries",
            device_name="sf800 pro secondary primary solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_only_secondary_solar_when_both_batteries_are_socempty(
        self, hass, low_soc_level, low_soc_state
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-secondary-solar-no-batteries",
            device_name="sf800 pro primary secondary solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-secondary-solar-no-batteries",
            device_name="sf800 pro secondary secondary solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(200)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_both_solar_sources_when_both_batteries_are_socempty(
        self, hass, low_soc_level, low_soc_state
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-both-solar-no-batteries",
            device_name="sf800 pro primary both solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-both-solar-no-batteries",
            device_name="sf800 pro secondary both solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_solar_charges_empty_secondary_when_on_discharge_path_and_all_solar_goes_to_home(
        self, hass, low_soc_level, low_soc_state
    ):
        """
        Secondary at low SoC that has all its solar passing to home should be redirected to charge its battery.

        This covers the case where the secondary was previously on a discharge path
        (homeOutput > 0) and its solar exactly matches homeOutput (charge_surplus == 0).
        The manager must enter charge mode and redirect that solar into the secondary battery.
        """
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-empty-secondary-solar-charges",
            device_name="sf800 pro primary empty secondary solar charges",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-empty-secondary-solar-charges",
            device_name="sf800 pro secondary empty secondary solar charges",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=300,  # all solar passing to home from previous discharge path
        )
        # secondary: pwr_produced = min(0, 0+0-0-300) = -300 (300 W solar → home)
        # charge_surplus = max(0, 300 - 300) = 0  (no leftover solar beyond homeOutput)
        # chargeable_produced_home = 300  (solar can be redirected to battery)
        assert secondary.state is low_soc_state

        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda p: p)
        secondary.power_charge = AsyncMock(side_effect=lambda p: p)
        secondary.power_discharge = AsyncMock(side_effect=lambda p: p)

        # p1 = -200: secondary is exporting 300 W to home, home only needs 100 W
        await manager.powerChanged(-200, False, datetime.now())

        # Cycle 1: manager enters charge mode and stops the secondary pass-through.
        # The charge holdoff fires on first entry (no device in charge yet), so charging
        # starts only in cycle 2; cycle 1 just stops the solar→home pass-through.
        secondary.power_discharge.assert_awaited_once_with(0)
        secondary.power_charge.assert_not_awaited()

    async def test_two_system_active_demand_prefers_primary_battery_before_secondary_battery_when_no_solar_is_available(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-no-solar-both-batteries",
            device_name="sf800 pro active primary no solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-no-solar-both-batteries",
            device_name="sf800 pro active secondary no solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(100, False, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(300)
        assert secondary.power_discharge.await_args_list[-1] == call(0)

    async def test_two_system_active_demand_uses_primary_solar_before_any_secondary_contribution_when_only_primary_has_solar(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-primary-solar-both-batteries",
            device_name="sf800 pro active primary primary solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-primary-solar-both-batteries",
            device_name="sf800 pro active secondary primary solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(100, False, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(300)
        assert secondary.power_discharge.await_args_list[-1] == call(0)

    async def test_two_system_active_demand_uses_secondary_solar_before_secondary_battery(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-secondary-solar-both-batteries",
            device_name="sf800 pro active primary secondary solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-secondary-solar-both-batteries",
            device_name="sf800 pro active secondary secondary solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(100, False, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(100)
        assert secondary.power_discharge.await_args_list[-1] == call(200)

    async def test_two_system_active_demand_uses_primary_solar_then_secondary_solar_when_both_batteries_are_available(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-both-solar-both-batteries",
            device_name="sf800 pro active primary both solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-both-solar-both-batteries",
            device_name="sf800 pro active secondary both solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(100, False, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_primary_battery_only_when_secondary_battery_is_socempty_and_no_solar(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-no-solar-primary-battery-only",
            device_name="sf800 pro active primary no solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-no-solar-primary-battery-only",
            device_name="sf800 pro active secondary no solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(200, False, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_rolls_primary_solar_into_primary_battery_when_secondary_battery_is_socempty(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-primary-solar-primary-battery-only",
            device_name="sf800 pro active primary primary solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-primary-solar-primary-battery-only",
            device_name="sf800 pro active secondary primary solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(200, False, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_secondary_solar_but_not_secondary_battery_when_only_primary_battery_is_available(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-secondary-solar-primary-battery-only",
            device_name="sf800 pro active primary secondary solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-secondary-solar-primary-battery-only",
            device_name="sf800 pro active secondary secondary solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=0,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(100, False, datetime.now())

        assert secondary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_keeps_secondary_battery_unavailable_even_when_both_systems_have_solar(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-both-solar-primary-battery-only",
            device_name="sf800 pro active primary both solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-both-solar-primary-battery-only",
            device_name="sf800 pro active secondary both solar primary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=0,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(100, False, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_secondary_battery_only_when_primary_battery_is_socempty_and_no_solar(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-no-solar-secondary-battery-only",
            device_name="sf800 pro active primary no solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-no-solar-secondary-battery-only",
            device_name="sf800 pro active secondary no solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(200, False, datetime.now())

        assert primary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_primary_solar_before_secondary_battery_when_primary_battery_is_socempty(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-primary-solar-secondary-battery-only",
            device_name="sf800 pro active primary primary solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=0,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-primary-solar-secondary-battery-only",
            device_name="sf800 pro active secondary primary solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(100, False, datetime.now())

        assert primary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        assert secondary.power_discharge.await_args_list[-1] == call(0)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_only_active_secondary_solar_when_primary_battery_is_socempty(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-secondary-solar-secondary-battery-only",
            device_name="sf800 pro active primary secondary solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-secondary-solar-secondary-battery-only",
            device_name="sf800 pro active secondary secondary solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(200, False, datetime.now())

        assert primary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_primary_solar_then_secondary_solar_before_secondary_battery_when_primary_is_socempty(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-both-solar-secondary-battery-only",
            device_name="sf800 pro active primary both solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=0,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-both-solar-secondary-battery-only",
            device_name="sf800 pro active secondary both solar secondary battery only",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        assert secondary.power_discharge.await_args_list[-1] == call(200)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_has_no_available_source_when_both_batteries_are_socempty_and_no_solar(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-no-solar-no-batteries",
            device_name="sf800 pro active primary no solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-no-solar-no-batteries",
            device_name="sf800 pro active secondary no solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(300, False, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_only_primary_solar_when_both_batteries_are_socempty(
        self, hass, low_soc_level, low_soc_state
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-primary-solar-no-batteries",
            device_name="sf800 pro active primary primary solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=0,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-primary-solar-no-batteries",
            device_name="sf800 pro active secondary primary solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(200, False, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_only_secondary_solar_when_both_batteries_are_socempty(
        self, hass, low_soc_level, low_soc_state
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-secondary-solar-no-batteries",
            device_name="sf800 pro active primary secondary solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-secondary-solar-no-batteries",
            device_name="sf800 pro active secondary secondary solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=0,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(200, False, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_both_solar_sources_when_both_batteries_are_socempty(
        self, hass, low_soc_level, low_soc_state
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-both-solar-no-batteries",
            device_name="sf800 pro active primary both solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=0,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-both-solar-no-batteries",
            device_name="sf800 pro active secondary both solar no batteries",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=0,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(100, False, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        assert secondary.power_discharge.await_args_list[-1] == call(0)

    async def test_keeps_threshold_sized_discharge_remainder_off_the_secondary(self, hass):
        """A remainder at the power-start threshold should not fully promote the secondary into discharge allocation."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-50w-remainder",
            device_name="sf800 pro primary 50w remainder",
            product_model="SolarFlow 800 Pro",
            level=90,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=800,
            battery_output=800,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-50w-remainder",
            device_name="sf800 pro secondary 50w remainder",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(50, False, datetime.now())

        primary.power_discharge.assert_awaited_once_with(800)
        secondary.power_discharge.assert_not_awaited()

    async def test_charges_only_the_true_surplus_after_home_pass_through(self, hass):
        """Charging should use only the leftover surplus after current solar has already been passed through to the home."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-50w-surplus",
            device_name="sf800 pro primary 50w surplus",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_input=60,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-50w-surplus",
            device_name="sf800 pro secondary 50w surplus",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)

        await manager.powerChanged(0, False, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-60)

    async def test_routes_surplus_from_a_full_primary_to_the_secondary_in_bypass_mode(self, hass):
        """
        Surplus solar from a full selected primary should put it into bypass and route the remainder into charging the secondary.

        The encoded telemetry derives local production and home demand that leave a net surplus,
        so the primary should switch into AC input passthrough while the secondary absorbs the PV remainder.
        """
        first = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-first-solar-surplus",
            device_name="sf800 pro first solar surplus",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_input=300,
        )
        second = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-second-solar-surplus",
            device_name="sf800 pro second solar surplus",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        manager = make_manager(
            hass,
            devices=(first, second),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=first.deviceId,
            charge_time=datetime.min,
        )
        first.power_get = AsyncMock(return_value=True)
        second.power_get = AsyncMock(return_value=True)
        first.power_bypass = AsyncMock(return_value=0)
        first.power_charge = AsyncMock(side_effect=lambda power: power)
        second.power_charge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        assert first.state is DeviceState.SOCFULL
        assert second.state is DeviceState.INACTIVE
        first.power_bypass.assert_awaited_once()
        first.power_charge.assert_not_awaited()
        second.power_charge.assert_awaited_once_with(-300)

    async def test_charges_the_secondary_only_with_true_net_surplus_when_both_devices_have_solar(self, hass):
        """
        When both devices have solar, charging should use only the true net surplus after local demand is covered.

        The encoded example includes production on both devices and local demand on one of them,
        so the secondary should receive only the surplus left after pass-through is accounted for.
        """
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-both-solar",
            device_name="sf800 pro primary both solar",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_input=300,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-both-solar",
            device_name="sf800 pro secondary both solar",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)

        await manager.powerChanged(0, False, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-400)

    async def test_keeps_secondary_local_surplus_out_of_primary_charge_priority(self, hass):
        """
        When both devices have PV surplus, the selected primary must not absorb the secondary's own local surplus.

        With 450W on the primary and 100W on the secondary, 200W should be served from the primary,
        leaving 250W to charge there and 100W to charge on the secondary.
        """
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-split-surplus",
            device_name="sf800 pro primary split surplus",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_input=250,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-split-surplus",
            device_name="sf800 pro secondary split surplus",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(-250)
        secondary.power_charge.assert_awaited_once_with(-100)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_keeps_small_secondary_local_surplus_out_of_primary_charge_priority(self, hass):
        """
        When the primary fully consumes its own PV for the home, only the secondary's leftover PV may charge.

        With 150W served from the primary and 50W served from the secondary, only the secondary's remaining
        50W surplus should be charged. The selected primary must not absorb that secondary-local surplus.
        """
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-small-split-surplus",
            device_name="sf800 pro primary small split surplus",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=150,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-small-split-surplus",
            device_name="sf800 pro secondary small split surplus",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=50,
            battery_input=50,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-50)
        primary.power_discharge.assert_awaited_once_with(150)
        secondary.power_discharge.assert_not_awaited()

    async def test_does_not_start_charging_a_secondary_when_the_primary_is_already_loading(self, hass):
        """An already-loading selected primary should not create phantom leftover surplus for a secondary in the same fuse group."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-already-loading",
            device_name="sf800 pro primary already loading",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=300,
            output_limit=0,
            home_input=300,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-added-to-fuse-group",
            device_name="sf800 pro secondary added to fuse group",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        primary.fuseGrp.devices = [primary, secondary]
        secondary.fuseGrp = primary.fuseGrp
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary_power_charge = primary.power_charge
        primary.power_charge = AsyncMock(side_effect=primary_power_charge)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(-300)
        secondary.power_charge.assert_not_awaited()

    async def test_keeps_secondary_local_pv_on_the_secondary_when_both_devices_are_already_charging(self, hass):
        """Secondary local PV should stay on the secondary when both devices are already on the charge path."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-already-charging",
            device_name="sf800 pro primary already charging",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-already-charging-local-pv",
            device_name="sf800 pro secondary already charging local pv",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_awaited_once_with(-300)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_keeps_secondary_local_pv_on_the_secondary_while_charge_hysteresis_is_active(self, hass):
        """Charge hysteresis must not stop system 2 from keeping its own PV on the charge path."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-hysteresis-charging",
            device_name="sf800 pro primary hysteresis charging",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-hysteresis-local-pv",
            device_name="sf800 pro secondary hysteresis local pv",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_awaited_once_with(-300)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize("full_bypass_device_is_primary", [True, False])
    async def test_negative_p1_with_full_bypass_pv_increases_the_charging_device(
        self, hass, full_bypass_device_is_primary
    ):
        """A full bypassing peer should keep its PV on bypass while export is absorbed by the charging device."""
        full_bypass = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-full-450w-pv-{full_bypass_device_is_primary}",
            device_name=f"sf800 pro full 450w pv {full_bypass_device_is_primary}",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=450,
        )
        full_bypass.solarInput.update_value(450)
        full_bypass.byPass.update_value(1)
        charging = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-charging-100w-pv-{full_bypass_device_is_primary}",
            device_name=f"sf800 pro charging 100w pv {full_bypass_device_is_primary}",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=100,
        )
        charging.solarInput.update_value(100)
        manager = make_manager(
            hass,
            devices=(full_bypass, charging),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=full_bypass.deviceId if full_bypass_device_is_primary else charging.deviceId,
        )
        full_bypass.power_get = AsyncMock(return_value=True)
        charging.power_get = AsyncMock(return_value=True)
        full_bypass.power_charge = AsyncMock(side_effect=lambda power: power)
        charging.power_charge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_discharge = AsyncMock(side_effect=lambda power: power)
        charging.power_discharge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_bypass = AsyncMock(return_value=0)

        await manager.powerChanged(-200, False, datetime.now())

        assert full_bypass.state is DeviceState.SOCFULL
        charging.power_charge.assert_awaited_once_with(-300)
        full_bypass.power_charge.assert_not_awaited()
        charging.power_discharge.assert_not_awaited()

    async def test_keeps_idle_secondary_local_pv_out_of_the_primary_on_a_small_negative_charge_cycle(self, hass):
        """A small negative charge target must still let an idle secondary keep its own PV locally."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-small-negative-charge",
            device_name="sf800 pro primary small negative charge",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-idle-local-pv",
            device_name="sf800 pro secondary idle local pv",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(-20, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_awaited_once_with(-100)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize("charging_device_is_primary", [True, False])
    async def test_positive_p1_with_full_bypass_pv_reduces_the_charging_device(
        self, hass, charging_device_is_primary
    ):
        """A full bypassing peer should cover its PV share while the charging device is reduced for grid import."""
        charging = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-charging-350w-pv-{charging_device_is_primary}",
            device_name=f"sf800 pro charging 350w pv {charging_device_is_primary}",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=200,
            output_limit=0,
            home_input=200,
            battery_input=200,
        )
        charging.solarInput.update_value(350)
        full_bypass = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-full-100w-pv-{charging_device_is_primary}",
            device_name=f"sf800 pro full 100w pv {charging_device_is_primary}",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=100,
        )
        full_bypass.solarInput.update_value(100)
        full_bypass.byPass.update_value(1)
        manager = make_manager(
            hass,
            devices=(charging, full_bypass),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=charging.deviceId if charging_device_is_primary else full_bypass.deviceId,
        )
        charging.power_get = AsyncMock(return_value=True)
        full_bypass.power_get = AsyncMock(return_value=True)
        charging.power_charge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_charge = AsyncMock(side_effect=lambda power: power)
        charging.power_discharge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_discharge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_bypass = AsyncMock(return_value=0)

        await manager.powerChanged(70, False, datetime.now())

        assert full_bypass.state is DeviceState.SOCFULL
        charging.power_charge.assert_awaited_once_with(-130)
        full_bypass.power_charge.assert_not_awaited()
        charging.power_discharge.assert_not_awaited()
        full_bypass.power_discharge.assert_not_awaited()
        full_bypass.power_bypass.assert_not_awaited()

    async def test_positive_p1_with_healthy_secondary_pv_still_reduces_primary_charge(self, hass):
        """A healthy secondary passing PV to the home should not make charge holdoff keep a stale primary target."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-charging-with-healthy-secondary-pv",
            device_name="sf800 pro primary charging with healthy secondary pv",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=200,
            output_limit=0,
            home_input=200,
            battery_input=550,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-healthy-secondary-pv-for-grid-import",
            device_name="sf800 pro healthy secondary pv for grid import",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await manager.powerChanged(50, False, datetime.now())

        assert secondary.state is DeviceState.INACTIVE
        primary.power_charge.assert_awaited_once_with(-150)
        secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_awaited_once_with(100)
        secondary.power_bypass.assert_not_awaited()

    async def test_positive_p1_with_battery_backed_secondary_discharge_stops_secondary_and_reduces_primary_charge(
        self, hass
    ):
        """A battery-backed secondary discharge should stop before the stale primary charge target is reduced."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-charging-with-secondary-battery-output",
            device_name="sf800 pro primary charging with secondary battery output",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=200,
            output_limit=0,
            home_input=200,
            battery_input=550,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-battery-output-for-grid-import",
            device_name="sf800 pro secondary battery output for grid import",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=100,
            battery_output=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(50, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(-50)
        secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_awaited_once_with(0)

    async def test_positive_p1_with_selected_primary_pv_still_reduces_secondary_charge(self, hass):
        """A selected primary passing PV to the home should not make charge holdoff stop a charging secondary."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-pv-while-secondary-charges",
            device_name="sf800 pro primary pv while secondary charges",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-charging-with-primary-pv",
            device_name="sf800 pro secondary charging with primary pv",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=200,
            output_limit=0,
            home_input=200,
            battery_input=550,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(50, False, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-150)
        primary.power_discharge.assert_awaited_once_with(100)
        secondary.power_discharge.assert_not_awaited()

    async def test_keeps_mixed_secondary_home_and_charge_pv_on_the_secondary_when_primary_pv_can_cover_demand(
        self, hass
    ):
        """A secondary that is both serving the home and charging must keep that PV locally when primary PV can cover demand."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-covering-demand-while-charging",
            device_name="sf800 pro primary covering demand while charging",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-mixed-home-and-charge",
            device_name="sf800 pro secondary mixed home and charge",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            home_input=50,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_awaited_once_with(-50)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_moves_secondary_home_pv_to_secondary_charge_when_primary_charge_can_cover_demand(self, hass):
        """A secondary PV floor serving the whole home should charge locally while primary charge is reduced."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-reduces-charge-for-secondary-pv",
            device_name="sf800 pro primary reduces charge for secondary pv",
            product_model="SolarFlow 800 Pro",
            level=30,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=500,
            output_limit=0,
            home_input=500,
            battery_input=500,
        )
        primary.solarInput.update_value(600)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-home-pv-becomes-charge",
            device_name="sf800 pro secondary home pv becomes charge",
            product_model="SolarFlow 800 Pro",
            level=90,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=60,
        )
        secondary.solarInput.update_value(60)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(0, False, datetime.now())

        secondary.power_charge.assert_awaited_once_with(-60)
        primary.power_charge.assert_awaited_once_with(-440)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_positive_p1_charge_lag_stays_on_the_primary_aware_charge_path(self, hass):
        """A positive-demand lag cycle should stay on the charge path when both systems are still reporting charging telemetry."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-charge-lag-primary-dispatch",
            device_name="sf800 pro positive p1 charge lag primary dispatch",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=150,
            output_limit=0,
            home_input=150,
            battery_input=300,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-charge-lag-secondary-dispatch",
            device_name="sf800 pro positive p1 charge lag secondary dispatch",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=140,
            output_limit=0,
            home_input=140,
            battery_input=280,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        manager.power_charge_primary_aware = AsyncMock()
        manager.power_discharge_primary_aware = AsyncMock()

        await manager.powerChanged(120, False, datetime.now())

        manager.power_charge_primary_aware.assert_awaited_once_with(-170, ANY)
        manager.power_discharge_primary_aware.assert_not_awaited()

    async def test_reduces_primary_charge_before_secondary_when_positive_p1_appears_during_charge_lag(self, hass):
        """Positive demand should be satisfied by reducing primary charge first while leaving the secondary's remaining PV local."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-charge-lag-primary",
            device_name="sf800 pro positive p1 charge lag primary",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=150,
            output_limit=0,
            home_input=150,
            battery_input=300,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-charge-lag-secondary",
            device_name="sf800 pro positive p1 charge lag secondary",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=140,
            output_limit=0,
            home_input=140,
            battery_input=280,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(120, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(-30)
        secondary.power_charge.assert_awaited_once_with(-140)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_reduces_secondary_charge_only_after_the_primary_reaches_zero_when_positive_p1_exceeds_primary_pv(
        self, hass
    ):
        """If positive demand exceeds primary PV, the primary should be reduced to zero before the secondary is reduced."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-primary-zero-first",
            device_name="sf800 pro positive p1 primary zero first",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=150,
            output_limit=0,
            home_input=150,
            battery_input=300,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-secondary-after-primary-zero",
            device_name="sf800 pro positive p1 secondary after primary zero",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=140,
            output_limit=0,
            home_input=140,
            battery_input=280,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(220, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_awaited_once_with(-70)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_positive_p1_charge_lag_ignores_charge_hysteresis_for_local_pv(self, hass):
        """The charge holdoff window must not prevent local PV from being rerouted to home demand in the same cycle."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-hysteresis-primary",
            device_name="sf800 pro positive p1 hysteresis primary",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=150,
            output_limit=0,
            home_input=150,
            battery_input=300,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-hysteresis-secondary",
            device_name="sf800 pro positive p1 hysteresis secondary",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=140,
            output_limit=0,
            home_input=140,
            battery_input=280,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(120, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(-30)
        secondary.power_charge.assert_awaited_once_with(-140)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_positive_p1_charge_lag_keeps_secondary_charging_after_primary_pv_is_exhausted(self, hass):
        """When demand exceeds primary PV during a charge-lag cycle, only the secondary remainder should be reduced."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-primary-exhausted",
            device_name="sf800 pro positive p1 primary exhausted",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-secondary-remainder",
            device_name="sf800 pro positive p1 secondary remainder",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=80,
            output_limit=0,
            home_input=80,
            battery_input=160,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(120, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_awaited_once_with(-60)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_positive_p1_charge_lag_keeps_mixed_secondary_home_and_charge_pv_primary_first(self, hass):
        """Positive demand should still reduce charging primary-first when system 2 reports mixed home and charge telemetry."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-mixed-primary",
            device_name="sf800 pro positive p1 mixed primary",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=150,
            output_limit=0,
            home_input=150,
            battery_input=300,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-positive-p1-mixed-secondary",
            device_name="sf800 pro positive p1 mixed secondary",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            home_input=50,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(120, False, datetime.now())

        primary.power_charge.assert_awaited_once_with(-30)
        secondary.power_charge.assert_awaited_once_with(-50)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_primary_device_change_recomputes_charge_lag_routing_immediately(self, hass):
        """Changing the selected primary should immediately affect the next routing command, even if telemetry still reflects the prior charge cycle."""
        first = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-change-first",
            device_name="sf800 pro primary change first",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=200,
        )
        second = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-change-second",
            device_name="sf800 pro primary change second",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=80,
            output_limit=0,
            home_input=80,
            battery_input=160,
        )
        manager = make_manager(
            hass,
            devices=(first, second),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=first.deviceId,
        )
        first.power_get = AsyncMock(return_value=True)
        second.power_get = AsyncMock(return_value=True)
        first.power_charge = AsyncMock(side_effect=lambda power: power)
        second.power_charge = AsyncMock(side_effect=lambda power: power)
        first.power_discharge = AsyncMock(side_effect=lambda power: power)
        second.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(120, False, datetime.now())

        first.power_charge.reset_mock()
        second.power_charge.reset_mock()
        first.power_discharge.reset_mock()
        second.power_discharge.reset_mock()

        hass.states.async_set("sensor.power_actual", "120", {"unit_of_measurement": "W"})
        await manager.update_primary_device(manager.primarydevice, second.deviceId)

        assert manager.resolve_primary_device() is second
        first.power_charge.assert_awaited_once_with(-60)
        second.power_charge.assert_awaited_once_with(0)
        first.power_discharge.assert_not_awaited()
        second.power_discharge.assert_not_awaited()

    async def test_charges_a_99_percent_primary_in_output_mode_before_the_secondary(self, hass):
        """A selected primary at 99% should still receive charge priority even if it was previously in AC output mode."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-99-output-mode",
            device_name="sf800 pro primary 99 output mode",
            product_model="SolarFlow 800 Pro",
            level=99,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
            home_output=300,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-same-fuse-group",
            device_name="sf800 pro secondary same fuse group",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        primary.fuseGrp.devices = [primary, secondary]
        secondary.fuseGrp = primary.fuseGrp
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(return_value=0)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(return_value=0)

        await manager.powerChanged(-300, False, datetime.now())

        assert primary.state is DeviceState.INACTIVE
        primary.power_discharge.assert_not_awaited()
        primary.power_charge.assert_awaited_once_with(-300)
        secondary.power_charge.assert_not_awaited()

    async def test_falls_back_to_the_secondary_for_discharge_when_the_primary_is_offline(self, hass):
        """If the selected primary is offline, the secondary should take over the discharge target."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-offline-primary-discharge",
            device_name="sf800 pro offline primary discharge",
            product_model="SolarFlow 800 Pro",
            level=90,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-offline-secondary-discharge",
            device_name="sf800 pro offline secondary discharge",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=255,
            battery_output=255,
        )
        primary.connectionStatus.update_value(0)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=False)
        secondary.power_get = AsyncMock(return_value=True)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(81, False, datetime.now())

        secondary.power_discharge.assert_awaited_once_with(336)

    async def test_falls_back_to_the_secondary_for_charge_when_the_primary_is_offline(self, hass):
        """If the selected primary is offline, the secondary should take over the charge target."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-offline-primary-charge",
            device_name="sf800 pro offline primary charge",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-offline-secondary-charge",
            device_name="sf800 pro offline secondary charge",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        primary.connectionStatus.update_value(0)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=False)
        secondary.power_get = AsyncMock(return_value=True)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await manager.powerChanged(-300, False, datetime.now())

        secondary.power_charge.assert_awaited_once_with(-300)


class TestZeroFastRecovery:
    """Verify that zero_fast is always restored after power distribution calls."""

    async def test_p1_changed_resets_zero_fast_after_successful_power_changed(self, hass):
        """zero_fast must not stay at datetime.max after a normal _p1_changed cycle."""
        device = make_device(hass, level=50, home_output=100, battery_output=100)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_discharge = AsyncMock(side_effect=lambda power: power)

        # Ensure the debounce window is open
        manager.zero_next = datetime.min
        manager.zero_fast = datetime.min

        state = Mock()
        state.state = "100"
        event_data = {"new_state": state, "old_state": None, "entity_id": "sensor.power_actual"}
        event = Mock()
        event.data = event_data

        await manager._p1_changed(event)

        assert manager.zero_fast != datetime.max
        assert manager.zero_fast < datetime.now() + timedelta(seconds=10)

    async def test_p1_changed_resets_zero_fast_after_power_changed_raises(self, hass):
        """zero_fast must be restored even when powerChanged raises an exception."""
        device = make_device(hass, level=50)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
        )
        manager.zero_next = datetime.min
        manager.zero_fast = datetime.min

        state = Mock()
        state.state = "100"
        event = Mock()
        event.data = {"new_state": state, "old_state": None, "entity_id": "sensor.power_actual"}

        with patch.object(manager, "powerChanged", side_effect=RuntimeError("boom")):
            await manager._p1_changed(event)

        assert manager.zero_fast != datetime.max
        assert manager.zero_fast < datetime.now() + timedelta(seconds=10)

    async def test_p1_changed_resets_zero_fast_after_cancelled_error(self, hass):
        """zero_fast must be restored even when powerChanged is cancelled."""
        device = make_device(hass, level=50)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
        )
        manager.zero_next = datetime.min
        manager.zero_fast = datetime.min

        state = Mock()
        state.state = "100"
        event = Mock()
        event.data = {"new_state": state, "old_state": None, "entity_id": "sensor.power_actual"}

        with (
            patch.object(manager, "powerChanged", side_effect=asyncio.CancelledError),
            pytest.raises(asyncio.CancelledError),
        ):
            await manager._p1_changed(event)

        assert manager.zero_fast != datetime.max
        assert manager.zero_fast < datetime.now() + timedelta(seconds=10)

    async def test_update_primary_device_resets_zero_fast(self, hass):
        """zero_fast must not remain at datetime.max after update_primary_device runs."""
        device = make_device(
            hass,
            device_id="primary",
            device_name="primary",
            level=50,
            home_output=100,
            battery_output=100,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=device.deviceId,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_discharge = AsyncMock(side_effect=lambda power: power)

        # Simulate HA returning a P1 state
        p1_state = Mock()
        p1_state.state = "200"
        p1_state.attributes = {"unit_of_measurement": "W"}
        manager.hass.states = Mock()
        manager.hass.states.get = Mock(return_value=p1_state)

        manager.zero_fast = datetime.min

        await manager.update_primary_device(manager.primarydevice, device.deviceId)

        assert manager.zero_fast != datetime.max
        assert manager.zero_fast < datetime.now() + timedelta(seconds=10)

    async def test_update_primary_device_resets_zero_fast_on_error(self, hass):
        """zero_fast must be restored even when powerChanged raises during update_primary_device."""
        device = make_device(
            hass,
            device_id="primary",
            device_name="primary",
            level=50,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING_PRIMARY_AWARE,
            primary_device_id=device.deviceId,
        )

        p1_state = Mock()
        p1_state.state = "200"
        p1_state.attributes = {"unit_of_measurement": "W"}
        manager.hass.states = Mock()
        manager.hass.states.get = Mock(return_value=p1_state)

        manager.zero_fast = datetime.min

        with (
            patch.object(manager, "powerChanged", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            await manager.update_primary_device(manager.primarydevice, device.deviceId)

        assert manager.zero_fast != datetime.max
        assert manager.zero_fast < datetime.now() + timedelta(seconds=10)
