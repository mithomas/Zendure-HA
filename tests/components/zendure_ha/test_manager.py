"""Manager-level reserve-threshold and routing tests."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from custom_components.zendure_ha.const import AcMode, DeviceState, ManagerMode, SmartMode
from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro
from custom_components.zendure_ha.fusegroup import FuseGroup
from custom_components.zendure_ha.manager import ZendureManager, _PowerRoutingIntent

from .common import (
    attach_devices,
    make_device,
    make_manager,
    make_p1_event,
)

LOW_SOC_DEVICE_CASES = (
    pytest.param(5, DeviceState.SOCEMPTY, id="socempty"),
    pytest.param(10, DeviceState.SOCRESERVE, id="socreserve"),
)

PV_HOME_PRIORITY_DEVICE_CASES = (pytest.param(10, DeviceState.SOCRESERVE, id="socreserve"),)


def _power_routing_intent_from_execute(execute: AsyncMock) -> _PowerRoutingIntent:
    execute_args = execute.await_args
    assert execute_args is not None
    return execute_args.args[0]


def _manager_power_routing_intent(manager: ZendureManager) -> _PowerRoutingIntent:
    return _power_routing_intent_from_execute(cast("AsyncMock", manager._execute_power_routing))


def _prepare_mock(manager: ZendureManager) -> Mock:
    return cast("Mock", manager._prepare_power_routing)


def _execute_mock(manager: ZendureManager) -> AsyncMock:
    return cast("AsyncMock", manager._execute_power_routing)


async def _run_prepared_power_routing(manager: ZendureManager, p1: int, time: datetime) -> bool:
    try:
        manager._reset_power_distribution_state()
        setpoint = await manager._poll_devices_and_prepare_routing_state(p1)
        intent, routing, _setpoint = manager._prepare_power_routing(p1, time, setpoint)
        await manager._execute_power_routing(intent, time, routing)
    finally:
        manager._restore_p1_update_timing(datetime.now())
    return True


def _mock_prepared_power_routing(manager: ZendureManager, *, setpoint: int = 0) -> tuple[Mock, Mock, int]:
    prepared = (Mock(), Mock(), setpoint)
    manager._poll_devices_and_prepare_routing_state = AsyncMock(return_value=setpoint)
    manager._prepare_power_routing = Mock(return_value=prepared)
    manager._execute_power_routing = AsyncMock()
    return prepared


class TestAvailableKwh:
    def test_refresh_available_kwh_updates_when_device_thresholds_change(self, hass):
        """Two devices at 20% and 30% should update aggregate available kWh when one reserve and SoC change."""
        manager = make_manager(hass)
        first = make_device(hass, device_id="device-1", level=20, min_soc=10, reserve=10, kwh=2.0)
        second = make_device(hass, device_id="device-2", level=30, min_soc=10, reserve=10, kwh=2.0)
        attach_devices(manager, first, second)

        assert manager.availableKwh.asNumber == pytest.approx(0.6)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.6)

        second.entityUpdate("socReserve", 25)
        assert manager.availableKwh.asNumber == pytest.approx(0.3)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.3)

        second.entityUpdate("electricLevel", 25)
        assert manager.availableKwh.asNumber == pytest.approx(0.2)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.2)

    def test_refresh_available_kwh_updates_when_capacity_changes(self, hass):
        """A single 30% device should double aggregate available kWh when total capacity doubles from 2 to 4 kWh."""
        manager = make_manager(hass)
        device = make_device(hass, level=30, min_soc=10, reserve=10, kwh=2.0)
        attach_devices(manager, device)

        assert manager.availableKwh.asNumber == pytest.approx(0.4)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.4)

        device.kWh = 4.0
        device.totalKwh.update_value(4.0)
        device.refresh_discharge_state()

        assert manager.availableKwh.asNumber == pytest.approx(0.8)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.8)

    def test_refresh_available_kwh_updates_when_discharge_recovery_margin_changes(self, hass):
        """A 12% device with a 10% floor should move in and out of recovery as the margin changes."""
        manager = make_manager(hass)
        device = make_device(hass, level=12, min_soc=10, reserve=10, kwh=2.0)
        attach_devices(manager, device)

        assert manager.availableKwh.asNumber == pytest.approx(0.04)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.04)

        manager.discharge_recovery_margin._attr_native_value = 5
        manager._refresh_discharge_recovery_margin(None, None)
        assert device.actualKwh == pytest.approx(-0.06)
        assert device.state is DeviceState.RESERVE_RECOVERY
        assert manager.availableKwh.asNumber == pytest.approx(0)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0)

        manager.discharge_recovery_margin._attr_native_value = 0
        manager._refresh_discharge_recovery_margin(None, None)
        assert manager.availableKwh.asNumber == pytest.approx(0.04)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.04)

    def test_refresh_available_kwh_clamps_negative_device_contributions(self, hass):
        """Manager aggregates should count any negative per-device available energy as zero."""
        manager = make_manager(hass)
        charged = make_device(hass, device_id="charged-device", level=20, min_soc=10, reserve=10, kwh=2.0)
        empty = make_device(hass, device_id="empty-device", level=5, min_soc=10, reserve=10, kwh=2.0)
        high_reserve = make_device(hass, device_id="high-reserve-device", level=20, min_soc=10, reserve=30, kwh=2.0)
        attach_devices(manager, charged, empty, high_reserve)

        assert charged.actualKwh == pytest.approx(0.2)
        assert empty.actualKwh < 0
        assert high_reserve.actualKwh < 0
        assert manager.availableKwh.asNumber == pytest.approx(0.2)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.2)

    def test_total_available_kwh_clamps_negative_offline_device_contributions(self, hass):
        """Stable aggregate should keep positive offline energy but not subtract negative offline energy."""
        manager = make_manager(hass)
        positive = make_device(hass, device_id="positive-offline-device", level=20, min_soc=10, reserve=10, kwh=2.0)
        negative = make_device(hass, device_id="negative-offline-device", level=5, min_soc=10, reserve=10, kwh=2.0)
        attach_devices(manager, positive, negative)

        positive.lastseen = datetime.min
        positive.setStatus()
        positive.update_device_state()
        negative.lastseen = datetime.min
        negative.setStatus()
        negative.update_device_state()
        manager.refresh_energy_kwh()

        assert positive.actualKwh == pytest.approx(0.2)
        assert negative.actualKwh < 0
        assert manager.availableKwh.asNumber == pytest.approx(0)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.2)

    def test_total_available_kwh_keeps_offline_devices_included(self, hass):
        """Stable aggregate should keep the last energy of devices that temporarily go offline."""
        manager = make_manager(hass)
        first = make_device(hass, device_id="device-1", level=20, min_soc=10, reserve=10, kwh=2.0)
        second = make_device(hass, device_id="device-2", level=30, min_soc=10, reserve=10, kwh=2.0)
        attach_devices(manager, first, second)

        second.lastseen = datetime.min
        second.setStatus()
        second.update_device_state()
        manager.refresh_energy_kwh()

        assert manager.availableKwh.asNumber == pytest.approx(0.2)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.6)

    def test_total_available_kwh_ignores_unused_fusegroup_setting(self, hass):
        """Stable aggregate should include devices even when their fusegroup select is set to unused."""
        manager = make_manager(hass)
        first = make_device(hass, device_id="device-1", level=20, min_soc=10, reserve=10, kwh=2.0)
        second = make_device(hass, device_id="device-2", level=30, min_soc=10, reserve=10, kwh=2.0)
        second.fuseGroup.update_value(0)
        attach_devices(manager, first, second)

        assert manager.availableKwh.asNumber == pytest.approx(0.6)
        assert manager.totalAvailableKwh.asNumber == pytest.approx(0.6)


class TestUpdateOperation:
    @pytest.mark.parametrize("offline_only", [False, True], ids=["no-devices", "offline-devices"])
    async def test_does_not_create_a_notification_when_no_devices_are_available(self, hass, caplog, offline_only):
        """Starting manager operation without available devices should only log a warning."""
        devices = ()
        device = None
        power_off_mock = None
        if offline_only:
            device = make_device(hass, device_id="offline-device", device_name="offline device", level=50)
            device.connectionStatus.update_value(0)
            power_off_mock = AsyncMock()
            device.power_off = power_off_mock
            devices = (device,)

        manager = make_manager(hass, devices=devices)
        manager.p1meterEvent = Mock()
        operation_entity = Mock(value=ManagerMode.MATCHING.value)

        with (
            patch("homeassistant.components.persistent_notification.async_create") as mock_notify,
            caplog.at_level(logging.WARNING),
        ):
            await manager.update_operation(operation_entity, ManagerMode.MATCHING.value)

        mock_notify.assert_not_called()
        assert "No devices online, not possible to start the operation" in caplog.text
        assert manager.operation == ManagerMode.MATCHING
        if power_off_mock is not None:
            power_off_mock.assert_not_awaited()


class TestPrimaryAwareModeFolding:
    """Verify primary-aware routing is selected by primary device, not by mode."""

    @staticmethod
    def _manager_with_dispatch_mocks(
        hass,
        *,
        operation: ManagerMode,
        primary: bool = False,
        manual_power: int = 0,
    ) -> tuple[ZendureManager, dict[str, AsyncMock]]:
        device = make_device(hass, device_id="folded-primary", device_name="folded primary", level=50)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=operation,
            manual_power=manual_power,
            primary_device_id=device.deviceId if primary else None,
        )
        device.power_get = AsyncMock(return_value=True)
        mocks = {"execute": AsyncMock()}
        manager._execute_power_routing = mocks["execute"]
        return manager, mocks

    @staticmethod
    def _power_routing_intent(mocks: dict[str, AsyncMock]) -> _PowerRoutingIntent:
        mocks["execute"].assert_awaited_once()
        return _power_routing_intent_from_execute(mocks["execute"])

    async def test_matching_uses_normal_paths_without_primary(self, hass):
        manager, mocks = self._manager_with_dispatch_mocks(hass, operation=ManagerMode.MATCHING)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        intent = self._power_routing_intent(mocks)
        assert intent.home_output_budget == 200  # noqa: PLR2004
        assert not intent.route_input
        assert not intent.selected_primary_home_output

        mocks["execute"].reset_mock()
        await _run_prepared_power_routing(manager, -200, datetime.now())

        intent = self._power_routing_intent(mocks)
        assert intent.input_budget == -200  # noqa: PLR2004
        assert intent.route_input
        assert not intent.selected_primary_input

    async def test_matching_uses_primary_aware_paths_with_primary(self, hass):
        manager, mocks = self._manager_with_dispatch_mocks(hass, operation=ManagerMode.MATCHING, primary=True)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        intent = self._power_routing_intent(mocks)
        assert intent.home_output_budget == 200  # noqa: PLR2004
        assert not intent.route_input
        assert intent.selected_primary_home_output

        mocks["execute"].reset_mock()
        await _run_prepared_power_routing(manager, -200, datetime.now())

        intent = self._power_routing_intent(mocks)
        assert intent.input_budget == -200  # noqa: PLR2004
        assert intent.route_input
        assert intent.selected_primary_input

    async def test_matching_discharge_uses_primary_aware_path_only_with_primary(self, hass):
        normal, normal_mocks = self._manager_with_dispatch_mocks(hass, operation=ManagerMode.MATCHING_DISCHARGE)
        primary, primary_mocks = self._manager_with_dispatch_mocks(
            hass, operation=ManagerMode.MATCHING_DISCHARGE, primary=True
        )

        await _run_prepared_power_routing(normal, 200, datetime.now())
        await _run_prepared_power_routing(primary, 200, datetime.now())

        normal_intent = self._power_routing_intent(normal_mocks)
        primary_intent = self._power_routing_intent(primary_mocks)
        assert normal_intent.home_output_budget == 200  # noqa: PLR2004
        assert not normal_intent.selected_primary_home_output
        assert primary_intent.home_output_budget == 200  # noqa: PLR2004
        assert primary_intent.selected_primary_home_output

    async def test_matching_charge_uses_primary_aware_path_only_with_primary(self, hass):
        normal, normal_mocks = self._manager_with_dispatch_mocks(hass, operation=ManagerMode.MATCHING_CHARGE)
        primary, primary_mocks = self._manager_with_dispatch_mocks(
            hass, operation=ManagerMode.MATCHING_CHARGE, primary=True
        )

        await _run_prepared_power_routing(normal, -200, datetime.now())
        await _run_prepared_power_routing(primary, -200, datetime.now())

        normal_intent = self._power_routing_intent(normal_mocks)
        primary_intent = self._power_routing_intent(primary_mocks)
        assert normal_intent.input_budget == -200  # noqa: PLR2004
        assert not normal_intent.selected_primary_input
        assert primary_intent.input_budget == -200  # noqa: PLR2004
        assert primary_intent.selected_primary_input

    @pytest.mark.parametrize(
        ("manual_power", "route_input", "expected"),
        [
            pytest.param(200, False, 200, id="output"),
            pytest.param(-200, True, -200, id="input"),
        ],
    )
    async def test_manual_uses_primary_aware_path_only_with_primary(self, hass, manual_power, route_input, expected):
        normal, normal_mocks = self._manager_with_dispatch_mocks(
            hass, operation=ManagerMode.MANUAL, manual_power=manual_power
        )
        primary, primary_mocks = self._manager_with_dispatch_mocks(
            hass, operation=ManagerMode.MANUAL, primary=True, manual_power=manual_power
        )

        await _run_prepared_power_routing(normal, 0, datetime.now())
        await _run_prepared_power_routing(primary, 0, datetime.now())

        normal_intent = self._power_routing_intent(normal_mocks)
        primary_intent = self._power_routing_intent(primary_mocks)
        assert normal_intent.route_input is route_input
        assert primary_intent.route_input is route_input
        assert (normal_intent.input_budget if route_input else normal_intent.home_output_budget) == expected
        assert (primary_intent.input_budget if route_input else primary_intent.home_output_budget) == expected
        assert not normal_intent.selected_primary_input
        assert not normal_intent.selected_primary_home_output
        assert primary_intent.selected_primary_input
        assert primary_intent.selected_primary_home_output

    async def test_store_solar_uses_strict_primary_charge_path_with_primary(self, hass):
        manager, mocks = self._manager_with_dispatch_mocks(hass, operation=ManagerMode.STORE_SOLAR, primary=True)

        await _run_prepared_power_routing(manager, -200, datetime.now())

        intent = self._power_routing_intent(mocks)
        assert intent.input_budget == -200  # noqa: PLR2004
        assert intent.route_input
        assert intent.selected_primary_input
        assert intent.strict_home_output_stop

    async def test_store_solar_uses_strict_normal_charge_path_without_primary(self, hass):
        manager, mocks = self._manager_with_dispatch_mocks(hass, operation=ManagerMode.STORE_SOLAR)

        await _run_prepared_power_routing(manager, -200, datetime.now())

        intent = self._power_routing_intent(mocks)
        assert intent.input_budget == -200  # noqa: PLR2004
        assert intent.route_input
        assert not intent.selected_primary_input
        assert intent.strict_home_output_stop

    async def test_store_solar_clamps_positive_output_to_zero_with_primary(self, hass):
        manager, mocks = self._manager_with_dispatch_mocks(hass, operation=ManagerMode.STORE_SOLAR, primary=True)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        intent = self._power_routing_intent(mocks)
        assert intent.home_output_budget == 0
        assert not intent.route_input
        assert not intent.selected_primary_home_output
        assert intent.strict_home_output_stop


class TestStoreSolarRouting:
    """Verify store-solar treats home output as clamped to zero."""

    async def test_positive_pv_output_is_stopped_instead_of_forwarded_to_home(self, hass):
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="store-solar-pv-output",
            device_name="store solar pv output",
            product_model="SolarFlow 800 Pro",
            level=80,
            home_output=300,
        )
        device.solarInput.update_value(300)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.STORE_SOLAR,
            primary_device_id=device.deviceId,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_charge = AsyncMock(side_effect=lambda power: power)
        device.power_discharge = AsyncMock(side_effect=lambda power: power)
        device.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        device.power_discharge.assert_awaited_once_with(0)
        device.power_charge.assert_not_awaited()
        device.power_bypass.assert_not_awaited()

    async def test_strict_charge_keeps_full_bypassing_device_in_pass_through(self, hass):
        """A full bypassing device has no battery capacity left; its PV pass-through should be preserved."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="store-solar-bypass-full",
            device_name="store solar bypass full",
            product_model="SolarFlow 800 Pro",
            level=100,
            home_output=300,
            battery_output=300,
        )
        device.byPass.update_value(1)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.STORE_SOLAR,
            primary_device_id=device.deviceId,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_charge = AsyncMock(side_effect=lambda power: power)
        device.power_discharge = AsyncMock(side_effect=lambda power: power)
        device.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, -500, datetime.now())

        # Full device stays in bypass — no stop command, no charge command.
        device.power_discharge.assert_not_awaited()
        device.power_charge.assert_not_awaited()

    async def test_strict_charge_stops_non_full_bypassing_device(self, hass):
        """A non-full bypassing device can still accept charge; strict stop must halt its output."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="store-solar-bypass-non-full",
            device_name="store solar bypass non full",
            product_model="SolarFlow 800 Pro",
            level=70,  # below soc_set=80 default, so not SOCFULL
            home_output=300,
            battery_output=300,
        )
        device.byPass.update_value(1)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.STORE_SOLAR,
            primary_device_id=device.deviceId,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_charge = AsyncMock(side_effect=lambda power: power)
        device.power_discharge = AsyncMock(side_effect=lambda power: power)
        device.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, -500, datetime.now())

        device.power_discharge.assert_awaited_once_with(0)
        device.power_charge.assert_not_awaited()

    async def test_selected_primary_charges_before_idle_secondary(self, hass):
        primary = make_device(
            hass,
            device_id="store-solar-primary-charge",
            device_name="store solar primary charge",
            level=70,
        )
        secondary = make_device(
            hass,
            device_id="store-solar-secondary-charge",
            device_name="store solar secondary charge",
            level=40,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.STORE_SOLAR,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -300, datetime.now())

        primary.power_charge.assert_awaited_once_with(-300)
        secondary.power_charge.assert_not_awaited()


class TestSmartMatchingPrimaryAware:

    async def test_taper_export_stuck(self, hass, freezer):
        """Test that primary taper floor doesn't cause infinite holdoff loop."""
        from custom_components.zendure_ha.const import ManagerMode, DeviceState, AcMode
        from tests.components.zendure_ha.common import make_manager, make_device, make_p1_event
        from unittest.mock import PropertyMock, patch, AsyncMock
        from datetime import datetime, timedelta

        primary = make_device(
            hass,
            device_id="A1",
            device_name="Primary",
            level=99,
            ac_mode=AcMode.OUTPUT,
            home_output=343,
            battery_input=600,
            battery_output=0,
        )
        primary.power_charge = AsyncMock(return_value=0)
        primary.power_discharge = AsyncMock(return_value=343)
        
        # Mock SolarFlow 800 taper behavior using patch so it doesn't break other tests
        with patch("custom_components.zendure_ha.devices.solarflow800.SolarFlow800.taper_charge_limit", new_callable=PropertyMock) as mock_taper:
            mock_taper.return_value = 600
            primary.pwr_max = 1200
            primary.charge_limit = -1200
            primary.discharge_limit = 1200
            primary.update_device_state(None, DeviceState.RESERVE_RECOVERY.value)
            
            # We must explicitly set pwr_produced or solarInput so that local_production is calculated.
            primary.pwr_produced = -950
            primary.solarInput.update_value(950)
        
            secondary = make_device(
                hass,
                device_id="A2",
                device_name="Secondary",
                level=50,
                ac_mode=AcMode.INPUT,
                home_input=0,
                battery_input=80,
                battery_output=0,
            )
            secondary.power_charge = AsyncMock(return_value=-600)
            secondary.power_discharge = AsyncMock(return_value=0)
            
            secondary.pwr_max = 1200
            secondary.charge_limit = -1200
            secondary.discharge_limit = 1200
            secondary.update_device_state(None, DeviceState.INACTIVE.value)
            secondary.pwr_produced = 0
            secondary.solarInput.update_value(0)
        
            manager = make_manager(
                hass,
                devices=[primary, secondary],
                operation=ManagerMode.MATCHING,
                primary_device_id="A1",
            )
        
            # Initial routing cycle with small demand, so it outputs 343W from primary.
            await manager._p1_changed(make_p1_event(170))
            
            # Now simulate export: p1 meter reads -170W because primary is pushing 343W.
            freezer.tick(timedelta(seconds=15))
            # We need to simulate that primary is now outputting 343.
            primary.homeOutput.update_value(343)
            await manager._p1_changed(make_p1_event(-170))
            
            # Secondary is expected to be in input mode with homeInput = 0.
            # The manager should have held off charging and set charge_time.
            assert manager.charge_time > datetime.now()
            
            # Reset mock to see the next calls
            secondary.power_charge.reset_mock()

            # Loop for 5 minutes, as if the grid keeps exporting -170W.
            # The primary keeps outputting 343W.
            for i in range(10):
                freezer.tick(timedelta(seconds=30))
                await manager._p1_changed(make_p1_event(-170))
                
            # Secondary should have started AC charging to absorb the export
            # We assert it was called with a negative power (charging)
            assert secondary.power_charge.await_count > 0
            last_charge_call = secondary.power_charge.await_args_list[-1].args[0]
            print(f"last_charge_call = {last_charge_call}")
            assert last_charge_call < 0
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
            operation=ManagerMode.MATCHING,
            primary_device_id=active.deviceId,
        )
        active.power_get = AsyncMock(return_value=True)
        idle_produced.power_get = AsyncMock(return_value=True)
        idle_plain.power_get = AsyncMock(return_value=True)
        active.power_discharge = AsyncMock(side_effect=lambda power: power)
        idle_produced.power_discharge = AsyncMock(side_effect=lambda power: power)
        idle_plain.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        idle_produced.power_discharge.assert_awaited_once_with(100)
        active.power_discharge.assert_not_awaited()
        idle_plain.power_discharge.assert_not_awaited()

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        other.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        other.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.is_discharge_blocked = Mock(return_value=True)

        await _run_prepared_power_routing(manager, 0, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        other.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        other.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_discharge.assert_awaited_once_with(200)
        other.power_discharge.assert_awaited_once_with(0)

    async def test_uses_bypass_when_a_full_sf800_pro_is_reduced_to_zero(self, hass):
        """A full SF800 Pro should switch to bypass instead of receiving a zero-watt discharge command."""
        manager = make_manager(hass)
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro",
            device_name="sf800 pro",
            product_model="SolarFlow 800 Pro",
            level=100,
            home_input=100,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_bypass = AsyncMock(return_value=0)
        device.power_discharge = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        device.power_bypass.assert_awaited_once()
        device.power_discharge.assert_not_called()

    async def test_does_not_send_another_bypass_command_when_a_full_sf800_pro_is_already_in_bypass(self, hass):
        """A full SF800 Pro already reporting pass on should not receive another bypass command for a zero target."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-already-bypassing",
            device_name="sf800 pro already bypassing",
            product_model="SolarFlow 800 Pro",
            level=100,
            home_input=100,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
        )
        device.byPass.update_value(1)
        device.power_get = AsyncMock(return_value=True)
        device.power_bypass = AsyncMock(return_value=0)
        device.power_discharge = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        device.power_bypass.assert_not_awaited()
        device.power_discharge.assert_not_called()

    async def test_power_charge_skips_devices_that_are_already_in_bypass(self, hass):
        """Charging should not stop a device that is already reporting bypass on."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-charge-bypass",
            device_name="sf800 pro charge bypass",
            product_model="SolarFlow 800 Pro",
            level=100,
            home_input=100,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            charge_time=datetime.min,
        )
        device.byPass.update_value(1)
        device.homeOutput.update_value(100)
        device.power_get = AsyncMock(return_value=True)
        device.power_discharge = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, -200, datetime.now())

        device.power_discharge.assert_not_awaited()

    async def test_uses_bypass_when_a_full_non_primary_sf800_pro_is_reduced_to_zero(self, hass):
        """A full non-primary SF800 Pro should also switch to bypass when the selected primary covers the full discharge request."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-bypass-target-covered",
            device_name="sf800 pro primary bypass target covered",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_output=200,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-full-zero-target",
            device_name="sf800 pro secondary full zero target",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=10,
            battery_output=10,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(return_value=0)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert secondary.state is DeviceState.SOCFULL
        primary.power_discharge.assert_awaited_once_with(210)
        secondary.power_bypass.assert_awaited_once()
        secondary.power_discharge.assert_not_awaited()

    async def test_prefers_the_selected_primary_for_battery_remainder_when_both_devices_already_feed_home(self, hass):
        """A primary already passing through solar should still take the battery-backed remainder."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-bypass-remainder",
            device_name="sf800 pro primary bypass remainder",
            product_model="SolarFlow 800 Pro",
            level=99,
            min_soc=5,
            reserve=10,
            soc_set=99,
            home_output=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-battery-remainder",
            device_name="sf800 pro secondary battery remainder",
            product_model="SolarFlow 800 Pro",
            level=99,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=70,
        )
        FuseGroup("group3600", 3600, primary.charge_limit or -1000, [primary, secondary])
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.byPass.update_value(1)
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert primary.state is DeviceState.SOCFULL
        assert secondary.state is DeviceState.SOCNEARLYFULL
        primary.power_discharge.assert_awaited_once_with(170)
        secondary.power_discharge.assert_awaited_once_with(30)
        primary.power_bypass.assert_not_awaited()
        secondary.power_bypass.assert_not_awaited()

    async def test_prefers_the_selected_primary_for_battery_remainder_when_it_is_99_percent_and_already_bypassing(
        self, hass
    ):
        """A non-full primary already reporting bypass should still take the battery-backed remainder."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-99-bypass-remainder",
            device_name="sf800 pro primary 99 bypass remainder",
            product_model="SolarFlow 800 Pro",
            level=99,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-99-battery-remainder",
            device_name="sf800 pro secondary 99 battery remainder",
            product_model="SolarFlow 800 Pro",
            level=99,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=70,
        )
        FuseGroup("group3600-bypass99", 3600, primary.charge_limit or -1000, [primary, secondary])
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.byPass.update_value(1)
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert primary.state is DeviceState.SOCNEARLYFULL
        assert secondary.state is DeviceState.SOCNEARLYFULL
        primary.power_discharge.assert_awaited_once_with(170)
        secondary.power_discharge.assert_awaited_once_with(30)
        primary.power_bypass.assert_not_awaited()
        secondary.power_bypass.assert_not_awaited()

    async def test_keeps_a_recovering_secondary_out_of_discharge(self, hass):
        """A recovering secondary should not be used for battery discharge while a healthy primary can cover the load."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary",
            device_name="sf800 pro primary",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary",
            device_name="sf800 pro secondary",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=10,
            battery_output=10,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, -10, datetime.now())

        assert primary.state is DeviceState.SOCFULL
        assert secondary.state is DeviceState.RESERVE_RECOVERY
        primary.power_discharge.assert_awaited_once_with(100)
        secondary.power_discharge.assert_awaited_once_with(0)
        secondary.power_bypass.assert_not_awaited()

    async def test_recovering_secondary_reroutes_local_solar_to_home_during_positive_demand(self, hass):
        """A recovering secondary should still contribute its local PV to home demand in discharge mode."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-recovery-home-load",
            device_name="sf800 pro primary recovery home load",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=5,
            soc_set=100,
            home_output=800,
            battery_output=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-recovery-home-load",
            device_name="sf800 pro secondary recovery home load",
            product_model="SolarFlow 800 Pro",
            level=6,
            min_soc=5,
            reserve=5,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=125,
            output_limit=0,
            home_input=125,
            battery_input=250,
        )
        FuseGroup("group3600", 3600, primary.charge_limit or -1000, [primary, secondary])
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 1000, datetime.now())

        assert secondary.state is DeviceState.RESERVE_RECOVERY
        primary.power_discharge.assert_awaited_once_with(800)
        secondary.power_discharge.assert_awaited_once_with(125)
        secondary.power_charge.assert_not_awaited()
        secondary.power_bypass.assert_not_awaited()

    async def test_routes_discharge_remainder_away_from_a_recovering_primary(self, hass):
        """If the selected primary is still recovering, the remaining discharge load should stay on the non-recovering device."""
        first = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-first",
            device_name="sf800 pro first",
            product_model="SolarFlow 800 Pro",
            level=99,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=255,
        )
        second = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-second",
            device_name="sf800 pro second",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        manager = make_manager(
            hass,
            devices=(first, second),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=second.deviceId,
        )
        first.power_get = AsyncMock(return_value=True)
        second.power_get = AsyncMock(return_value=True)
        first.power_discharge = AsyncMock(side_effect=lambda power: power)
        second.power_discharge = AsyncMock(side_effect=lambda power: power)
        first.power_bypass = AsyncMock(return_value=0)
        second.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 81, datetime.now())

        assert first.state is DeviceState.SOCNEARLYFULL
        assert second.state is DeviceState.RESERVE_RECOVERY
        first.power_discharge.assert_awaited_once_with(336)
        second.power_discharge.assert_not_awaited()
        second.power_bypass.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_transfers_discharge_to_the_secondary_after_the_primary_becomes_empty(
        self, hass, low_soc_level, low_soc_state
    ):
        """Once the selected primary is empty, the secondary should take over the discharge load."""
        first = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-first-empty-primary",
            device_name="sf800 pro first empty primary",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        second = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-second-empty-primary",
            device_name="sf800 pro second empty primary",
            product_model="SolarFlow 800 Pro",
            level=99,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=255,
        )
        manager = make_manager(
            hass,
            devices=(first, second),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=first.deviceId,
        )
        first.power_get = AsyncMock(return_value=True)
        second.power_get = AsyncMock(return_value=True)
        first.power_discharge = AsyncMock(side_effect=lambda power: power)
        second.power_discharge = AsyncMock(side_effect=lambda power: power)
        first.power_bypass = AsyncMock(return_value=0)
        second.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 81, datetime.now())

        assert first.state is low_soc_state
        assert second.state is DeviceState.SOCNEARLYFULL
        first.power_discharge.assert_not_awaited()
        second.power_discharge.assert_awaited_once_with(336)
        first.power_bypass.assert_not_awaited()

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 400, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 200, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=system_2_primary.deviceId,
        )
        system_1.power_get = AsyncMock(return_value=True)
        system_2_primary.power_get = AsyncMock(return_value=True)
        system_1.power_discharge = AsyncMock(side_effect=lambda power: power)
        system_2_primary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 400, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        secondary_solar.power_get = AsyncMock(return_value=True)
        primary.power_get = AsyncMock(return_value=True)
        spill_secondary.power_get = AsyncMock(return_value=True)
        secondary_solar.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        spill_secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 700, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_primary_battery_only_when_secondary_battery_is_socempty_and_no_solar(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_rolls_primary_solar_into_primary_battery_when_secondary_battery_is_socempty(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_secondary_solar_but_not_secondary_battery_when_only_primary_battery_is_available(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_secondary_battery_only_when_primary_battery_is_socempty_and_no_solar(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert primary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_primary_solar_before_secondary_battery_when_primary_battery_is_socempty(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 500, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert primary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(200)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_demand_uses_primary_solar_then_secondary_solar_before_secondary_battery_when_primary_is_socempty(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    async def test_two_system_solar_charges_empty_secondary_when_on_discharge_path_and_all_solar_goes_to_home(
        self, hass
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
            level=5,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=300,  # all solar passing to home from previous discharge path
        )
        # secondary: pwr_produced = min(0, 0+0-0-300) = -300 (300 W solar → home)
        # charge_surplus = max(0, 300 - 300) = 0  (no leftover solar beyond homeOutput)
        # chargeable_produced_home = 300  (solar can be redirected to battery)
        assert secondary.state is DeviceState.SOCEMPTY

        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda p: p)
        secondary.power_charge = AsyncMock(side_effect=lambda p: p)
        secondary.power_discharge = AsyncMock(side_effect=lambda p: p)

        # p1 = -200: secondary is exporting 300 W to home, home only needs 100 W
        await _run_prepared_power_routing(manager, -200, datetime.now())

        secondary.power_charge.assert_awaited_once_with(-300)
        secondary.power_discharge.assert_not_awaited()

    async def test_two_system_solar_at_reserve_keeps_secondary_pv_on_home_when_household_uses_it(self, hass):
        """A secondary in reserve should keep normal household-load-first PV routing."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-reserve-secondary-solar-home",
            device_name="sf800 pro primary reserve secondary solar home",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-reserve-secondary-solar-home",
            device_name="sf800 pro secondary reserve secondary solar home",
            product_model="SolarFlow 800 Pro",
            level=10,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=300,
        )
        assert secondary.state is DeviceState.SOCRESERVE

        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        secondary.power_charge = AsyncMock(side_effect=lambda p: p)
        secondary.power_discharge = AsyncMock(side_effect=lambda p: p)

        await _run_prepared_power_routing(manager, -200, datetime.now())

        secondary.power_discharge.assert_awaited_once_with(100)
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    async def test_two_system_active_demand_keeps_current_secondary_solar_pass_through_before_extra_primary_pv(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-asymmetric-solar-both-batteries",
            device_name="sf800 pro active primary asymmetric solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=500,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-asymmetric-solar-both-batteries",
            device_name="sf800 pro active secondary asymmetric solar both batteries",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        assert primary.power_discharge.await_args_list[-1] == call(300)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    async def test_active_asymmetric_solar_follow_on_zero_p1_cycle_stays_on_discharge_path(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-asymmetric-solar-follow-on",
            device_name="sf800 pro active primary asymmetric solar follow on",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=500,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-asymmetric-solar-follow-on",
            device_name="sf800 pro active secondary asymmetric solar follow on",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        manager._execute_power_routing = AsyncMock()

        await _run_prepared_power_routing(manager, 0, datetime.now())

        intent = _manager_power_routing_intent(manager)
        assert intent.home_output_budget == 200  # noqa: PLR2004
        assert not intent.route_input

    async def test_active_asymmetric_solar_follow_on_zero_p1_cycle_does_not_zero_both_and_start_charging(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-asymmetric-solar-no-charge",
            device_name="sf800 pro active primary asymmetric solar no charge",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=500,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-asymmetric-solar-no-charge",
            device_name="sf800 pro active secondary asymmetric solar no charge",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_not_awaited()
        assert primary.power_discharge.await_args_list[-1] == call(100)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    async def test_primary_only_follow_on_zero_p1_cycle_stays_on_discharge_path(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-primary-only-follow-on",
            device_name="sf800 pro active primary primary only follow on",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=250,
            battery_output=0,
            battery_input=150,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-primary-only-follow-on",
            device_name="sf800 pro active secondary primary only follow on",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        manager._execute_power_routing = AsyncMock()

        await _run_prepared_power_routing(manager, 0, datetime.now())

        intent = _manager_power_routing_intent(manager)
        assert intent.home_output_budget == 250  # noqa: PLR2004
        assert not intent.route_input

    async def test_primary_only_follow_on_false_negative_p1_cycle_stays_on_discharge_path(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-primary-only-false-negative-follow-on",
            device_name="sf800 pro active primary primary only false negative follow on",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=250,
            battery_output=0,
            battery_input=150,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-primary-only-false-negative-follow-on",
            device_name="sf800 pro active secondary primary only false negative follow on",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        manager._execute_power_routing = AsyncMock()

        await _run_prepared_power_routing(manager, -250, datetime.now())

        intent = _manager_power_routing_intent(manager)
        assert intent.home_output_budget == 250  # noqa: PLR2004
        assert not intent.route_input

    async def test_primary_only_follow_on_false_negative_p1_cycle_enters_charge_only_after_debounce_window(self, hass):
        start = datetime.now()
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-primary-primary-only-false-negative-debounce",
            device_name="sf800 pro active primary primary only false negative debounce",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=250,
            battery_output=0,
            battery_input=150,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-active-secondary-primary-only-false-negative-debounce",
            device_name="sf800 pro active secondary primary only false negative debounce",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        manager._execute_power_routing = AsyncMock()

        await _run_prepared_power_routing(manager, -250, start)

        manager._reset_power_distribution_state()

        await _run_prepared_power_routing(manager, -250, start + timedelta(seconds=SmartMode.TIMEZERO - 1))

        manager._reset_power_distribution_state()

        await _run_prepared_power_routing(manager, -250, start + timedelta(seconds=SmartMode.TIMEZERO))

        intents = [await_args.args[0] for await_args in _execute_mock(manager).await_args_list]
        assert [(intent.route_input, intent.home_output_budget, intent.input_budget) for intent in intents] == [
            (False, 250, 0),
            (False, 250, 0),
            (True, 0, -500),
        ]

    async def test_two_system_follow_on_cycle_keeps_primary_solar_serving_home_when_primary_alone_covers_demand(
        self, hass
    ):
        initial_primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-primary-only-initial",
            device_name="sf800 pro primary primary only initial",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=400,
        )
        initial_secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-primary-only-initial",
            device_name="sf800 pro secondary primary only initial",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=100,
        )
        initial_manager = make_manager(
            hass,
            devices=(initial_primary, initial_secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=initial_primary.deviceId,
        )
        initial_primary.power_get = AsyncMock(return_value=True)
        initial_secondary.power_get = AsyncMock(return_value=True)
        initial_primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        initial_secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(initial_manager, 250, datetime.now())

        assert initial_primary.power_discharge.await_args_list[-1] == call(250)
        assert initial_secondary.power_discharge.await_args_list[-1] == call(0)

        follow_on_primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-primary-only-follow-on",
            device_name="sf800 pro primary primary only follow on",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=250,
            battery_output=0,
            battery_input=150,
        )
        follow_on_secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-primary-only-follow-on",
            device_name="sf800 pro secondary primary only follow on",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=100,
        )
        follow_on_manager = make_manager(
            hass,
            devices=(follow_on_primary, follow_on_secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=follow_on_primary.deviceId,
        )
        follow_on_primary.power_get = AsyncMock(return_value=True)
        follow_on_secondary.power_get = AsyncMock(return_value=True)
        follow_on_primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        follow_on_secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        follow_on_primary.power_charge = AsyncMock(side_effect=lambda power: power)
        follow_on_secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(follow_on_manager, 0, datetime.now())

        follow_on_primary.power_charge.assert_not_awaited()
        follow_on_secondary.power_charge.assert_not_awaited()
        assert follow_on_primary.power_discharge.await_args_list[-1] == call(250)
        assert follow_on_secondary.power_discharge.await_args_list[-1] == call(0)

    async def test_two_system_false_negative_follow_on_cycle_keeps_primary_solar_serving_home_when_primary_alone_covers_demand(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-primary-only-false-negative-cycle",
            device_name="sf800 pro primary primary only false negative cycle",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=250,
            battery_output=0,
            battery_input=150,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-primary-only-false-negative-cycle",
            device_name="sf800 pro secondary primary only false negative cycle",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=0,
            battery_output=0,
            battery_input=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -250, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_not_awaited()
        assert primary.power_discharge.await_args_list[-1] == call(250)
        assert secondary.power_discharge.await_args_list[-1] == call(0)

    async def test_two_system_follow_on_cycle_keeps_secondary_surplus_off_primary_when_both_pv_are_needed(self, hass):
        initial_primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-both-needed-initial",
            device_name="sf800 pro primary both needed initial",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=150,
        )
        initial_secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-both-needed-initial",
            device_name="sf800 pro secondary both needed initial",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=100,
        )
        initial_manager = make_manager(
            hass,
            devices=(initial_primary, initial_secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=initial_primary.deviceId,
        )
        initial_primary.power_get = AsyncMock(return_value=True)
        initial_secondary.power_get = AsyncMock(return_value=True)
        initial_primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        initial_secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(initial_manager, 200, datetime.now())

        assert initial_primary.power_discharge.await_args_list[-1] == call(150)
        assert initial_secondary.power_discharge.await_args_list[-1] == call(50)

        follow_on_primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-both-needed-follow-on",
            device_name="sf800 pro primary both needed follow on",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=150,
            battery_output=0,
            battery_input=0,
        )
        follow_on_secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-both-needed-follow-on",
            device_name="sf800 pro secondary both needed follow on",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=50,
            battery_output=0,
            battery_input=50,
        )
        follow_on_manager = make_manager(
            hass,
            devices=(follow_on_primary, follow_on_secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=follow_on_primary.deviceId,
        )
        follow_on_primary.power_get = AsyncMock(return_value=True)
        follow_on_secondary.power_get = AsyncMock(return_value=True)
        follow_on_primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        follow_on_secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        follow_on_primary.power_charge = AsyncMock(side_effect=lambda power: power)
        follow_on_secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(follow_on_manager, 0, datetime.now())

        follow_on_primary.power_charge.assert_not_awaited()
        follow_on_secondary.power_charge.assert_not_awaited()
        assert follow_on_primary.power_discharge.await_args_list[-1] == call(150)
        assert follow_on_secondary.power_discharge.await_args_list[-1] == call(50)

    async def test_two_system_false_negative_follow_on_cycle_keeps_both_systems_serving_home_when_both_pv_are_needed(
        self, hass
    ):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-both-needed-false-negative-cycle",
            device_name="sf800 pro primary both needed false negative cycle",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=150,
            battery_output=0,
            battery_input=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-both-needed-false-negative-cycle",
            device_name="sf800 pro secondary both needed false negative cycle",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=50,
            battery_output=0,
            battery_input=50,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -200, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_not_awaited()
        assert primary.power_discharge.await_args_list[-1] == call(150)
        assert secondary.power_discharge.await_args_list[-1] == call(50)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_primary_battery_only_when_secondary_battery_is_socempty_and_no_solar(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_rolls_primary_solar_into_primary_battery_when_secondary_battery_is_socempty(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), PV_HOME_PRIORITY_DEVICE_CASES)
    async def test_two_system_active_demand_uses_secondary_solar_but_not_secondary_battery_when_only_primary_battery_is_available(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert secondary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_keeps_secondary_battery_unavailable_even_when_both_systems_have_solar(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_secondary_battery_only_when_primary_battery_is_socempty_and_no_solar(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        assert primary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), PV_HOME_PRIORITY_DEVICE_CASES)
    async def test_two_system_active_demand_uses_primary_solar_before_secondary_battery_when_primary_battery_is_in_reserve(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert primary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        assert secondary.power_discharge.await_args_list[-1] == call(0)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_only_active_secondary_solar_when_primary_battery_is_socempty(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        assert primary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_uses_primary_solar_then_secondary_solar_before_secondary_battery_when_primary_is_socempty(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert primary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        assert secondary.power_discharge.await_args_list[-1] == call(200)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_two_system_active_demand_has_no_available_source_when_both_batteries_are_socempty_and_no_solar(
        self, hass, low_soc_level, low_soc_state
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), PV_HOME_PRIORITY_DEVICE_CASES)
    async def test_two_system_active_demand_uses_only_primary_solar_when_both_batteries_are_in_reserve(
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(300)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), PV_HOME_PRIORITY_DEVICE_CASES)
    async def test_two_system_active_demand_uses_only_secondary_solar_when_both_batteries_are_in_reserve(
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 200, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        primary.power_discharge.assert_not_awaited()
        assert secondary.power_discharge.await_args_list[-1] == call(300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), PV_HOME_PRIORITY_DEVICE_CASES)
    async def test_two_system_active_demand_uses_both_solar_sources_when_both_batteries_are_in_reserve(
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert primary.state is low_soc_state
        assert secondary.state is low_soc_state
        assert primary.power_discharge.await_args_list[-1] == call(200)
        assert secondary.power_discharge.await_args_list[-1] == call(100)

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 50, datetime.now())

        primary.power_discharge.assert_awaited_once_with(800)
        secondary.power_discharge.assert_not_awaited()

    async def test_clamps_false_negative_setpoints_created_by_full_device_solar_bypass(self, hass):
        """
        Solar bypass from a full device must not turn a still-positive demand cycle into a false charge setpoint.

        The concrete example would otherwise produce an intermediate negative net setpoint even
        though grid demand remains positive, so the manager must clamp that back to zero.
        """
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-false-negative-clamp",
            device_name="sf800 pro false negative clamp",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_input=300,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
        )
        device.power_get = AsyncMock(return_value=True)
        manager._execute_power_routing = AsyncMock()

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert device.state is DeviceState.SOCFULL
        intent = _manager_power_routing_intent(manager)
        assert intent.home_output_budget == 0
        assert not intent.route_input

    async def test_full_primary_stays_in_bypass_when_secondary_pv_covers_demand(self, hass):
        """A full bypassing primary should not get an output limit while secondary PV can cover demand."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-full-primary-secondary-covers",
            device_name="sf800 pro full primary secondary covers",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=300,
        )
        primary.solarInput.update_value(300)
        primary.byPass.update_value(1)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-pv-covers",
            device_name="sf800 pro secondary pv covers",
            product_model="SolarFlow 800 Pro",
            level=70,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=100,
            output_limit=0,
            home_input=100,
            battery_input=400,
        )
        secondary.solarInput.update_value(400)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 250, datetime.now())

        assert primary.state is DeviceState.SOCFULL
        primary.power_discharge.assert_not_awaited()
        primary.power_bypass.assert_not_awaited()
        secondary.power_discharge.assert_awaited_once_with(150)
        secondary.power_charge.assert_not_awaited()

    async def test_full_primary_discharges_after_primary_and_secondary_pv_are_exhausted(self, hass):
        """A full primary may discharge only after primary bypass PV and secondary PV cannot cover demand."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-full-primary-bypass-demand",
            device_name="sf800 pro full primary bypass demand",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=300,
        )
        primary.solarInput.update_value(300)
        primary.byPass.update_value(1)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-pv-demand",
            device_name="sf800 pro secondary pv demand",
            product_model="SolarFlow 800 Pro",
            level=95,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=35,
            output_limit=0,
            battery_input=35,
        )
        secondary.solarInput.update_value(35)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 150, datetime.now())

        assert primary.state is DeviceState.SOCFULL
        secondary.power_charge.assert_not_awaited()
        secondary.power_discharge.assert_awaited_once_with(35)
        primary.power_discharge.assert_awaited_once_with(415)
        primary.power_bypass.assert_not_awaited()

    async def test_full_secondary_stays_in_bypass_when_primary_can_cover_demand(self, hass):
        """A full secondary should contribute bypass PV only while the primary can cover the rest."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-charging-primary-covers-full-secondary",
            device_name="sf800 pro charging primary covers full secondary",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=200,
            output_limit=0,
            home_input=200,
            battery_input=500,
        )
        primary.solarInput.update_value(500)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-full-secondary-primary-covers",
            device_name="sf800 pro full secondary primary covers",
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
        secondary.solarInput.update_value(100)
        secondary.byPass.update_value(1)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 900, datetime.now())

        assert secondary.state is DeviceState.SOCFULL
        primary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(600)
        secondary.power_discharge.assert_not_awaited()
        secondary.power_bypass.assert_not_awaited()

    async def test_full_secondary_discharges_only_after_primary_capacity_is_exhausted(self, hass):
        """A full secondary should get battery output only for demand the primary cannot cover."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-charging-primary-limited-full-secondary",
            device_name="sf800 pro charging primary limited full secondary",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=200,
            output_limit=0,
            home_input=200,
            battery_input=500,
        )
        primary.solarInput.update_value(500)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-full-secondary-primary-limited",
            device_name="sf800 pro full secondary primary limited",
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
        secondary.solarInput.update_value(100)
        secondary.byPass.update_value(1)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 1400, datetime.now())

        assert secondary.state is DeviceState.SOCFULL
        primary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(800)
        secondary.power_discharge.assert_awaited_once_with(400)
        secondary.power_bypass.assert_not_awaited()

    async def test_keeps_an_existing_charge_ramp_running_through_a_small_positive_p1_swing(self, hass):
        """
        A slight positive p1 swing from command lag must not stop an already-charging device when a full PV device is in bypass.

        The full device still contributes 300W of PV into the home path, while the charging device has already ramped
        to 280W input. A transient +20W p1 reading should therefore continue charging at the remaining -260W target.
        """
        full_device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-full-pv-bypass",
            device_name="sf800 pro full pv bypass",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=300,
        )
        charging_device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-charging-lagged",
            device_name="sf800 pro charging lagged",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=280,
            output_limit=0,
            home_input=280,
        )
        full_device.fuseGrp.devices = [full_device, charging_device]
        charging_device.fuseGrp = full_device.fuseGrp
        manager = make_manager(
            hass,
            devices=(full_device, charging_device),
            operation=ManagerMode.MATCHING,
            primary_device_id=charging_device.deviceId,
            charge_time=datetime.min,
        )
        full_device.power_get = AsyncMock(return_value=True)
        charging_device.power_get = AsyncMock(return_value=True)
        manager._execute_power_routing = AsyncMock()

        await _run_prepared_power_routing(manager, 20, datetime.now())

        intent = _manager_power_routing_intent(manager)
        assert intent.input_budget == -260  # noqa: PLR2004
        assert intent.route_input

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-60)

    async def test_recovering_primary_keeps_solar_pass_through_while_the_secondary_covers_battery_remainder(self, hass):
        """A recovering primary may keep current solar pass-through while the secondary covers the battery-backed remainder."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-solar-primary",
            device_name="sf800 pro recovering solar primary",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-solar-secondary",
            device_name="sf800 pro recovering solar secondary",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_output=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert primary.state is DeviceState.RESERVE_RECOVERY
        primary.power_discharge.assert_awaited_once_with(100)
        secondary.power_discharge.assert_awaited_once_with(300)

    async def test_recovering_primary_and_secondary_solar_both_feed_home_before_secondary_battery_support(self, hass):
        """Produced PV from both the recovering primary and a healthy secondary should be consumed before battery support."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-produced-primary-706",
            device_name="sf800 pro recovering produced primary",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
        )
        secondary_solar = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-produced-secondary-707",
            device_name="sf800 pro recovering produced secondary",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=70,
        )
        discharge_secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-battery-secondary-708",
            device_name="sf800 pro recovering battery secondary",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_output=200,
        )
        primary.name = "recovering primary 706"
        secondary_solar.name = "secondary solar 707"
        discharge_secondary.name = "battery secondary 708"
        manager = make_manager(
            hass,
            devices=(primary, secondary_solar, discharge_secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary_solar.power_get = AsyncMock(return_value=True)
        discharge_secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary_solar.power_discharge = AsyncMock(side_effect=lambda power: power)
        discharge_secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert primary.state is DeviceState.RESERVE_RECOVERY
        primary.power_discharge.assert_awaited_once_with(100)
        secondary_solar.power_discharge.assert_awaited_once_with(70)
        discharge_secondary.power_discharge.assert_awaited_once_with(430)

    async def test_single_recovering_primary_keeps_solar_pass_through_without_a_zero_cap(self, hass):
        """A single recovering primary should hold PV pass-through at home demand instead of briefly dropping to zero."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-single-recovering-solar-primary",
            device_name="sf800 pro single recovering solar primary",
            product_model="SolarFlow 800 Pro",
            level=12,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_input=400,
        )
        manager = make_manager(
            hass,
            devices=(primary,),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert primary.state is DeviceState.RESERVE_RECOVERY
        primary.power_discharge.assert_awaited_once_with(200)

    async def test_recovering_secondary_keeps_current_pv_when_recovering_primary_can_cover_home(self, hass):
        """A recovering secondary's current PV pass-through should stop when primary PV can cover the full load."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-primary-covers-secondary-pv",
            device_name="sf800 pro recovering primary covers secondary pv",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=30,
            battery_input=670,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-secondary-keeps-pv",
            device_name="sf800 pro recovering secondary keeps pv",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=120,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert primary.state is DeviceState.RESERVE_RECOVERY
        assert secondary.state is DeviceState.RESERVE_RECOVERY
        primary.power_discharge.assert_awaited_once_with(150)
        secondary.power_discharge.assert_awaited_once_with(0)
        secondary.power_bypass.assert_not_awaited()

    async def test_recovering_devices_cap_output_to_pv_and_route_idle_secondary_pv_to_home(self, hass):
        """Both devices recovering with limited primary PV should cap to PV only and redirect secondary PV to home."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-low-pv-primary",
            device_name="sf800 pro recovering low pv primary",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=150,
            battery_output=120,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-idle-pv-secondary",
            device_name="sf800 pro recovering idle pv secondary",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=60,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert primary.state is DeviceState.RESERVE_RECOVERY
        assert secondary.state is DeviceState.RESERVE_RECOVERY
        primary.power_discharge.assert_awaited_once_with(30)
        secondary.power_discharge.assert_awaited_once_with(60)
        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_not_awaited()

    async def test_recovering_primary_keeps_solar_pass_through_even_with_a_healthy_secondary(self, hass):
        """A healthy secondary should not replace current PV pass-through from a recovering primary."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-solar-primary-with-secondary",
            device_name="sf800 pro recovering solar primary with secondary",
            product_model="SolarFlow 800 Pro",
            level=12,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_input=400,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-healthy-secondary-for-recovering-primary",
            device_name="sf800 pro healthy secondary for recovering primary",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert primary.state is DeviceState.RESERVE_RECOVERY
        primary.power_discharge.assert_awaited_once_with(200)
        secondary.power_discharge.assert_not_awaited()

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
            operation=ManagerMode.MATCHING,
            primary_device_id=first.deviceId,
            charge_time=datetime.min,
        )
        first.power_get = AsyncMock(return_value=True)
        second.power_get = AsyncMock(return_value=True)
        first.power_bypass = AsyncMock(return_value=0)
        first.power_charge = AsyncMock(side_effect=lambda power: power)
        second.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert first.state is DeviceState.SOCFULL
        assert second.state is DeviceState.INACTIVE
        first.power_bypass.assert_awaited_once()
        first.power_charge.assert_not_awaited()
        second.power_charge.assert_awaited_once_with(-300)

    async def test_routes_surplus_from_a_full_primary_to_the_secondary_during_charge_holdoff(self, hass):
        """A full selected primary should still hand surplus to a secondary in the same cycle while charge holdoff is active."""
        first = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-first-solar-surplus-holdoff",
            device_name="sf800 pro first solar surplus holdoff",
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
            device_id="sf800-pro-second-solar-surplus-holdoff",
            device_name="sf800 pro second solar surplus holdoff",
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
            operation=ManagerMode.MATCHING,
            primary_device_id=first.deviceId,
        )
        first.power_get = AsyncMock(return_value=True)
        second.power_get = AsyncMock(return_value=True)
        first.power_bypass = AsyncMock(return_value=0)
        first.power_charge = AsyncMock(side_effect=lambda power: power)
        second.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert first.state is DeviceState.SOCFULL
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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-400)

    async def test_charges_the_secondary_only_with_true_net_surplus_during_charge_holdoff(self, hass):
        """A full selected primary should preserve true net surplus for the secondary even before charge holdoff expires."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-both-solar-holdoff",
            device_name="sf800 pro primary both solar holdoff",
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
            device_id="sf800-pro-secondary-both-solar-holdoff",
            device_name="sf800 pro secondary both solar holdoff",
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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_bypass = AsyncMock(return_value=0)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_bypass.assert_awaited_once()
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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-50)
        primary.power_discharge.assert_awaited_once_with(150)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(
        ("primary_level", "expected_primary_state"),
        [
            (50, DeviceState.INACTIVE),
            (10, DeviceState.SOCRESERVE),
        ],
    )
    async def test_near_zero_surplus_keeps_primary_pv_on_home_while_secondary_charges_locally(
        self, hass, primary_level, expected_primary_state
    ):
        """A small negative P1 swing must not move home-serving primary PV into primary charging."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-primary-near-zero-home-floor-{expected_primary_state.name.lower()}",
            device_name=f"sf800 pro primary near zero home floor {expected_primary_state.name.lower()}",
            product_model="SolarFlow 800 Pro",
            level=primary_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=250,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-near-zero-local-charge",
            device_name="sf800 pro secondary near zero local charge",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=20,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -20, datetime.now())
        manager._reset_power_distribution_state()
        await _run_prepared_power_routing(manager, -20, datetime.now())

        assert primary.state is expected_primary_state
        primary.power_charge.assert_not_awaited()
        assert secondary.power_charge.await_args_list == [call(-20), call(-20)]
        assert primary.power_discharge.await_args_list == [call(250), call(250)]
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("p1", "expected_primary", "expected_secondary"), [(0, 100, 50), (-30, 100, 20)])
    async def test_zero_or_export_does_not_grow_selected_primary_output(
        self, hass, p1, expected_primary, expected_secondary
    ):
        """At zero/export, selected-primary charging PV should not grow output to replace battery output."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-primary-no-export-growth-{p1}",
            device_name=f"sf800 pro primary no export growth {p1}",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_input=200,
        )
        primary.solarInput.update_value(300)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-secondary-no-export-growth-{p1}",
            device_name=f"sf800 pro secondary no export growth {p1}",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=50,
            battery_output=50,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, p1, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(expected_primary)
        secondary.power_discharge.assert_awaited_once_with(expected_secondary)

    async def test_positive_import_may_grow_selected_primary_output(self, hass):
        """Positive import may still increase selected-primary PV output to cover real demand."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-positive-import-growth",
            device_name="sf800 pro primary positive import growth",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_input=200,
        )
        primary.solarInput.update_value(300)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-positive-import-growth",
            device_name="sf800 pro secondary positive import growth",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=50,
            battery_output=50,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 40, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(190)
        secondary.power_discharge.assert_awaited_once_with(0)

    async def test_charge_hysteresis_keeps_secondary_local_pv_when_primary_pv_serves_home(self, hass):
        """Charge hysteresis must not block a secondary from absorbing its own PV near the primary PV floor."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-hysteresis-home-floor",
            device_name="sf800 pro primary hysteresis home floor",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=250,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-hysteresis-local-charge",
            device_name="sf800 pro secondary hysteresis local charge",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=20,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -20, datetime.now())

        assert primary.state is DeviceState.INACTIVE
        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-20)
        primary.power_discharge.assert_awaited_once_with(250)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), PV_HOME_PRIORITY_DEVICE_CASES)
    async def test_reserve_primary_keeps_solar_pass_through_while_the_secondary_covers_battery_remainder(
        self, hass, low_soc_level, low_soc_state
    ):
        """A reserve primary may still pass through current solar while the secondary covers the battery-backed remainder."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-empty-solar-primary",
            device_name="sf800 pro empty solar primary",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-empty-solar-secondary",
            device_name="sf800 pro empty solar secondary",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_output=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert primary.state is low_soc_state
        primary.power_discharge.assert_awaited_once_with(100)
        secondary.power_discharge.assert_awaited_once_with(300)

    async def test_socempty_primary_redirects_home_pv_to_charging_before_secondary_remainder(self, hass):
        """A SOCEMPTY primary should charge from its own PV before preserving that PV for household load."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-socempty-solar-primary",
            device_name="sf800 pro socempty solar primary",
            product_model="SolarFlow 800 Pro",
            level=5,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-socempty-solar-secondary",
            device_name="sf800 pro socempty solar secondary",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
            battery_output=200,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert primary.state is DeviceState.SOCEMPTY
        primary.power_discharge.assert_awaited_once_with(0)
        primary.power_charge.assert_awaited_once_with(-100)
        secondary.power_charge.assert_not_awaited()

    async def test_socempty_primary_solar_does_not_charge_secondary_during_grid_import(self, hass):
        """pv_charge_first must not fire while the house is still importing from the grid.

        When the primary hits SOCEMPTY its solar passthrough is small and the
        house is still drawing from the grid (p1 > 0).  The pv_charge_first
        clamp must not convert the positive demand setpoint into a negative
        charge setpoint, which would flip the secondary (which has its own solar
        surplus) from output into charging mode and worsen the import.
        """
        # Primary: SOCEMPTY, 52 W solar passing straight through to home.
        # ac_mode is OUTPUT (device is in pass-through, not accepting AC input)
        # — this matches the real-world state where the device cannot be switched
        # to input because p1 is positive (house is importing).
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-import-pvcf-primary",
            device_name="sf800 pro import pvcf primary",
            product_model="SolarFlow 800 Pro",
            level=5,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            home_output=52,
            battery_input=0,
            battery_output=0,
        )
        # Secondary: healthy level, 317 W solar – 106 W stored as surplus, 211 W
        # serving home.  Before the fix the pv_charge_first clamp would redirect
        # secondary into input mode and consume 52 W of AC to charge primary.
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-import-pvcf-secondary",
            device_name="sf800 pro import pvcf secondary",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=211,
            battery_input=106,
            battery_output=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        # House is still importing 300 W from the grid – pv_charge_first must
        # not convert this into a charge setpoint.
        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert primary.state is DeviceState.SOCEMPTY
        # Secondary must never receive a charge command.
        secondary.power_charge.assert_not_awaited()
        # Secondary must keep contributing its home output.
        secondary.power_discharge.assert_awaited()
        assert secondary.power_discharge.call_args_list[-1].args[0] > 0

    @pytest.mark.parametrize(
        ("secondary_level", "expected_state"),
        [
            (13, DeviceState.RESERVE_RECOVERY),
            (5, DeviceState.SOCEMPTY),
            (10, DeviceState.SOCRESERVE),
        ],
    )
    async def test_grid_charging_targets_a_blocked_secondary_even_when_the_primary_is_full(
        self, hass, secondary_level, expected_state
    ):
        """With no solar and a charge request, a recovering or empty secondary should still be targeted even if the primary is full."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-grid-charge-primary-{secondary_level}",
            device_name=f"sf800 pro grid charge primary {secondary_level}",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-grid-charge-secondary-{secondary_level}",
            device_name=f"sf800 pro grid charge secondary {secondary_level}",
            product_model="SolarFlow 800 Pro",
            level=secondary_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -300, datetime.now())

        assert secondary.state is expected_state
        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-300)

    async def test_routes_surplus_charge_to_a_recovering_secondary(self, hass):
        """Surplus charge should be routed to a recovering secondary when the selected primary is already full."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary",
            device_name="sf800 pro primary",
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
            device_id="sf800-pro-secondary",
            device_name="sf800 pro secondary",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert primary.state is DeviceState.SOCFULL
        assert secondary.state is DeviceState.RESERVE_RECOVERY
        assert secondary.acMode.value == AcMode.OUTPUT
        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-300)

    @pytest.mark.parametrize(("low_soc_level", "low_soc_state"), LOW_SOC_DEVICE_CASES)
    async def test_routes_surplus_charge_to_an_empty_secondary(self, hass, low_soc_level, low_soc_state):
        """Surplus charge should be routed to an empty secondary when the selected primary is already full."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-empty",
            device_name="sf800 pro primary empty",
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
            device_id=f"sf800-pro-secondary-empty-{low_soc_state.name.lower()}",
            device_name=f"sf800 pro secondary empty {low_soc_state.name.lower()}",
            product_model="SolarFlow 800 Pro",
            level=low_soc_level,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert primary.state is DeviceState.SOCFULL
        assert secondary.state is low_soc_state
        assert secondary.acMode.value == AcMode.OUTPUT
        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-300)

    async def test_charges_the_non_full_primary_when_the_secondary_is_already_full(self, hass):
        """A negative setpoint should stay on the selected primary even when the full secondary is passing through 50W PV."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-charge-target",
            device_name="sf800 pro primary charge target",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-already-full",
            device_name="sf800 pro secondary already full",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
            home_output=50,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, -300, datetime.now())

        assert primary.state is DeviceState.INACTIVE
        assert secondary.state is DeviceState.SOCFULL
        primary.power_charge.assert_awaited_once_with(-300)
        secondary.power_bypass.assert_awaited_once()
        secondary.power_charge.assert_not_awaited()

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary_power_charge = primary.power_charge
        primary.power_charge = AsyncMock(side_effect=primary_power_charge)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_awaited_once_with(-300)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_charges_secondary_home_pv_when_primary_charge_can_replace_home_supply(self, hass):
        """Secondary home-serving PV can move to charge when the primary can cover it."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-replaces-secondary-home-pv",
            device_name="sf800 pro primary replaces secondary home pv",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=405,
            output_limit=0,
            home_input=405,
            battery_input=405,
        )
        primary.solarInput.update_value(550)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-home-pv-replaced-by-primary",
            device_name="sf800 pro secondary home pv replaced by primary",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=50,
            home_output=50,
        )
        secondary.solarInput.update_value(50)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_charge.assert_awaited_once_with(-355)
        secondary.power_charge.assert_awaited_once_with(-50)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_keeps_secondary_home_pv_on_home_when_primary_charge_has_no_pv(self, hass):
        """Home-serving secondary PV should remain protected without selected-primary PV evidence."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-grid-charge-cannot-replace-home-pv",
            device_name="sf800 pro primary grid charge cannot replace home pv",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=405,
            output_limit=0,
            home_input=405,
            battery_input=405,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-home-pv-stays-on-home",
            device_name="sf800 pro secondary home pv stays on home",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=50,
            home_output=50,
        )
        secondary.solarInput.update_value(50)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_awaited_once_with(50)

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
            operation=ManagerMode.MATCHING,
            primary_device_id=full_bypass.deviceId if full_bypass_device_is_primary else charging.deviceId,
        )
        full_bypass.power_get = AsyncMock(return_value=True)
        charging.power_get = AsyncMock(return_value=True)
        full_bypass.power_charge = AsyncMock(side_effect=lambda power: power)
        charging.power_charge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_discharge = AsyncMock(side_effect=lambda power: power)
        charging.power_discharge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, -200, datetime.now())

        assert full_bypass.state is DeviceState.SOCFULL
        charging.power_charge.assert_awaited_once_with(-300)
        full_bypass.power_charge.assert_not_awaited()
        charging.power_discharge.assert_not_awaited()

    async def test_negative_p1_with_same_group_full_primary_bypass_charges_secondary_during_hysteresis(self, hass):
        """A full bypassing primary should hand export to a same-group secondary even while charge hysteresis is active."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-full-primary-bypass-export",
            device_name="sf800 pro full primary bypass export",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=750,
        )
        primary.solarInput.update_value(750)
        primary.byPass.update_value(1)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-same-group-pv-charge",
            device_name="sf800 pro secondary same group pv charge",
            product_model="SolarFlow 800 Pro",
            level=95,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=40,
            output_limit=0,
            battery_input=40,
        )
        secondary.solarInput.update_value(150)
        FuseGroup("group3600-full-primary-export", 3600, primary.charge_limit, [primary, secondary])
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, -460, datetime.now())

        assert primary.state is DeviceState.SOCFULL
        primary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_not_awaited()
        primary.power_bypass.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-500)
        secondary.power_discharge.assert_not_awaited()

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -20, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_awaited_once_with(-100)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_positive_p1_with_bypassing_full_secondary_still_reduces_primary_charge(self, hass):
        """A bypassing SOCFULL secondary should not let charge holdoff keep a stale primary charge target."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-charging-with-full-secondary-bypass",
            device_name="sf800 pro primary charging with full secondary bypass",
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
            device_id="sf800-pro-full-secondary-bypass-for-grid-import",
            device_name="sf800 pro full secondary bypass for grid import",
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
        secondary.byPass.update_value(1)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 50, datetime.now())

        assert secondary.state is DeviceState.SOCFULL
        primary.power_charge.assert_awaited_once_with(-150)
        secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()
        secondary.power_bypass.assert_not_awaited()

    @pytest.mark.parametrize("charging_device_is_primary", [True, False])
    async def test_positive_p1_with_full_bypass_pv_reduces_the_charging_device(self, hass, charging_device_is_primary):
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
            operation=ManagerMode.MATCHING,
            primary_device_id=charging.deviceId if charging_device_is_primary else full_bypass.deviceId,
        )
        charging.power_get = AsyncMock(return_value=True)
        full_bypass.power_get = AsyncMock(return_value=True)
        charging.power_charge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_charge = AsyncMock(side_effect=lambda power: power)
        charging.power_discharge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_discharge = AsyncMock(side_effect=lambda power: power)
        full_bypass.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 70, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 50, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 50, datetime.now())

        primary.power_charge.assert_awaited_once_with(-50)
        secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_awaited_once_with(0)

    @pytest.mark.parametrize("selected_device", ["charging", "mixed"])
    async def test_positive_p1_with_mixed_secondary_output_preserves_pv_floor_when_charge_reaches_zero(
        self, hass, selected_device
    ):
        """A mixed PV and battery output should keep its PV floor when stale charging reaches zero."""
        charging = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-charging-with-mixed-output-{selected_device}",
            device_name=f"sf800 pro charging with mixed output {selected_device}",
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
        mixed = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-mixed-pv-battery-output-{selected_device}",
            device_name=f"sf800 pro mixed pv battery output {selected_device}",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=150,
            battery_output=50,
        )
        manager = make_manager(
            hass,
            devices=(charging, mixed),
            operation=ManagerMode.MATCHING,
            primary_device_id=charging.deviceId if selected_device == "charging" else mixed.deviceId,
        )
        charging.power_get = AsyncMock(return_value=True)
        mixed.power_get = AsyncMock(return_value=True)
        charging.power_charge = AsyncMock(side_effect=lambda power: power)
        mixed.power_charge = AsyncMock(side_effect=lambda power: power)
        charging.power_discharge = AsyncMock(side_effect=lambda power: power)
        mixed.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 150, datetime.now())

        if selected_device == "charging":
            charging.power_charge.assert_awaited_once_with(0)
            charging.power_discharge.assert_not_awaited()
        else:
            charging.power_charge.assert_not_awaited()
            charging.power_discharge.assert_awaited_once_with(0)

        mixed.power_charge.assert_not_awaited()
        mixed.power_discharge.assert_awaited_once_with(100)

    async def test_positive_p1_with_mixed_secondary_output_uses_primary_pv_after_charge_reaches_zero(self, hass):
        """After stale charging is exhausted, additional positive demand should use primary PV after the mixed PV floor."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-pv-after-mixed-output-floor",
            device_name="sf800 pro primary pv after mixed output floor",
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
        mixed = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-mixed-output-before-primary-pv",
            device_name="sf800 pro mixed output before primary pv",
            product_model="SolarFlow 800 Pro",
            level=70,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=150,
            battery_output=50,
        )
        manager = make_manager(
            hass,
            devices=(primary, mixed),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        mixed.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        mixed.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        mixed.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 250, datetime.now())

        primary.power_charge.assert_awaited_once_with(0)
        mixed.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(100)
        mixed.power_discharge.assert_awaited_once_with(100)

    @pytest.mark.parametrize(
        ("secondary_level", "recovery_margin", "expected_state"),
        [
            (13, 5, DeviceState.RESERVE_RECOVERY),
            (10, 0, DeviceState.SOCRESERVE),
        ],
    )
    async def test_positive_p1_with_discharge_blocked_secondary_pv_still_reduces_primary_charge(
        self, hass, secondary_level, recovery_margin, expected_state
    ):
        """A blocked secondary passing PV to the home should not make charge holdoff keep a stale primary target."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-primary-charging-with-blocked-secondary-{expected_state.name.lower()}",
            device_name=f"sf800 pro primary charging with blocked secondary {expected_state.name.lower()}",
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
            device_id=f"sf800-pro-blocked-secondary-pv-{expected_state.name.lower()}",
            device_name=f"sf800 pro blocked secondary pv {expected_state.name.lower()}",
            product_model="SolarFlow 800 Pro",
            level=secondary_level,
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
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=recovery_margin,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 50, datetime.now())

        assert secondary.state is expected_state
        primary.power_charge.assert_awaited_once_with(-150)
        secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_awaited_once_with(100)
        secondary.power_bypass.assert_not_awaited()

    async def test_positive_p1_with_socempty_secondary_pv_stops_home_output_before_reducing_primary_charge(self, hass):
        """A SOCEMPTY secondary should stop passing PV to the home before normal charge-lag reduction continues."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-charging-with-socempty-secondary",
            device_name="sf800 pro primary charging with socempty secondary",
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
            device_id="sf800-pro-socempty-secondary-pv",
            device_name="sf800 pro socempty secondary pv",
            product_model="SolarFlow 800 Pro",
            level=5,
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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 50, datetime.now())

        assert secondary.state is DeviceState.SOCEMPTY
        primary.power_charge.assert_awaited_once_with(-100)
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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 50, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-150)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_positive_p1_with_selected_primary_pv_reduces_secondary_charge_floor_without_touching_primary(
        self, hass
    ):
        """A secondary's active charge floor should cover positive demand before the selected primary is touched."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-pv-covering-home-while-secondary-charges",
            device_name="sf800 pro primary pv covering home while secondary charges",
            product_model="SolarFlow 800 Pro",
            level=35,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=250,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-active-charge-floor",
            device_name="sf800 pro secondary active charge floor",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=250,
            output_limit=0,
            home_input=250,
            battery_input=250,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 50, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-200)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_positive_p1_with_recovering_devices_reduces_primary_charge_before_secondary_charge(self, hass):
        """Positive demand should reduce recovering primary charge and keep secondary PV charging locally."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-primary-charge-lag",
            device_name="sf800 pro recovering primary charge lag",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=700,
            output_limit=0,
            home_input=700,
            battery_input=700,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-recovering-secondary-charge-lag",
            device_name="sf800 pro recovering secondary charge lag",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=120,
            output_limit=0,
            home_input=120,
            battery_input=120,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 150, datetime.now())

        assert primary.state is DeviceState.RESERVE_RECOVERY
        assert secondary.state is DeviceState.RESERVE_RECOVERY
        primary.power_charge.assert_awaited_once_with(-550)
        secondary.power_charge.assert_awaited_once_with(-120)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_positive_p1_with_mixed_full_and_blocked_secondary_pv_still_reduces_primary_charge(self, hass):
        """Mixed full and blocked PV pass-through devices should not make charge holdoff keep a stale primary target."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-charging-with-mixed-pv-secondaries",
            device_name="sf800 pro primary charging with mixed pv secondaries",
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
        full_secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-full-secondary-mixed-pv-pass-through",
            device_name="sf800 pro full secondary mixed pv pass through",
            product_model="SolarFlow 800 Pro",
            level=100,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=50,
        )
        blocked_secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-blocked-secondary-mixed-pv-pass-through",
            device_name="sf800 pro blocked secondary mixed pv pass through",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            home_output=50,
        )
        full_secondary.byPass.update_value(1)
        manager = make_manager(
            hass,
            devices=(primary, full_secondary, blocked_secondary),
            operation=ManagerMode.MATCHING,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        full_secondary.power_get = AsyncMock(return_value=True)
        blocked_secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        full_secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        blocked_secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        full_secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        blocked_secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        full_secondary.power_bypass = AsyncMock(return_value=0)
        blocked_secondary.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 50, datetime.now())

        assert full_secondary.state is DeviceState.SOCFULL
        assert blocked_secondary.state is DeviceState.RESERVE_RECOVERY
        primary.power_charge.assert_awaited_once_with(-150)
        full_secondary.power_charge.assert_not_awaited()
        blocked_secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_not_awaited()
        full_secondary.power_discharge.assert_not_awaited()
        blocked_secondary.power_discharge.assert_awaited_once_with(50)
        full_secondary.power_bypass.assert_not_awaited()
        blocked_secondary.power_bypass.assert_not_awaited()

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        secondary.power_charge.assert_awaited_once_with(-60)
        primary.power_charge.assert_awaited_once_with(-440)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_moves_secondary_home_pv_to_charge_when_primary_output_can_cover_demand(self, hass):
        """A secondary PV floor should charge locally when the primary can increase PV-backed home output."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-output-replaces-secondary-home-pv",
            device_name="sf800 pro primary output replaces secondary home pv",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=200,
        )
        primary.solarInput.update_value(300)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-home-pv-replaced-by-primary-output",
            device_name="sf800 pro secondary home pv replaced by primary output",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=50,
            battery_input=50,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-100)
        primary.power_discharge.assert_awaited_once_with(250)
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize(
        ("p1", "primary_charge_allowed"),
        [
            pytest.param(-69, False, id="below-unexplained-export-threshold"),
            pytest.param(-70, True, id="at-unexplained-export-threshold"),
        ],
    )
    async def test_primary_input_switch_requires_unexplained_export_threshold(self, hass, p1, primary_charge_allowed):
        """Controlled selected-primary output should not count toward the export threshold."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-primary-input-threshold-{p1}",
            device_name=f"sf800 pro primary input threshold {p1}",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=20,
            battery_input=120,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=20,
        )
        primary.solarInput.update_value(140)
        manager = make_manager(
            hass,
            devices=(primary,),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, p1, datetime.now())

        if primary_charge_allowed:
            primary.power_charge.assert_awaited_once()
            primary.power_discharge.assert_not_awaited()
        else:
            primary.power_charge.assert_not_awaited()
            primary.power_discharge.assert_awaited_once_with(20)

    async def test_primary_output_explains_meter_export_without_input_switch(self, hass):
        """Selected-primary PV-backed output should block an AC-input switch when it explains the export."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-output-explains-export",
            device_name="sf800 pro primary output explains export",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=80,
            battery_input=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=80,
        )
        primary.solarInput.update_value(180)
        manager = make_manager(
            hass,
            devices=(primary,),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        manager._execute_power_routing = AsyncMock()

        await _run_prepared_power_routing(manager, -60, datetime.now())

        intent = _manager_power_routing_intent(manager)
        assert not intent.route_input
        assert intent.home_output_budget == 80  # noqa: PLR2004
        assert not intent.selected_primary_input_allowed

    async def test_battery_backed_output_explains_meter_export_without_input_switch(self, hass):
        """Manager-trimmable battery-backed output should block a selected-primary AC-input switch."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-battery-output-explains-export",
            device_name="sf800 pro primary battery output explains export",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=100,
            battery_output=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=100,
        )
        manager = make_manager(
            hass,
            devices=(primary,),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        manager._execute_power_routing = AsyncMock()

        await _run_prepared_power_routing(manager, -80, datetime.now())

        intent = _manager_power_routing_intent(manager)
        assert not intent.route_input
        assert intent.home_output_budget == 20  # noqa: PLR2004
        assert not intent.selected_primary_input_allowed

    async def test_secondary_pv_cover_allows_primary_input_below_raw_export_threshold(self, hass):
        """Secondary PV serving the home may make selected-primary input safe below the raw export threshold."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-input-secondary-cover",
            device_name="sf800 pro primary input secondary cover",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=10,
            battery_input=120,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=10,
        )
        primary.solarInput.update_value(130)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-pv-cover-input-gate",
            device_name="sf800 pro secondary pv cover input gate",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=30,
        )
        secondary.solarInput.update_value(30)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -49, datetime.now())

        primary.power_charge.assert_awaited_once()
        primary.power_discharge.assert_not_awaited()

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        manager._execute_power_routing = AsyncMock()

        await _run_prepared_power_routing(manager, 120, datetime.now())

        intent = _manager_power_routing_intent(manager)
        assert intent.input_budget == -170  # noqa: PLR2004
        assert intent.route_input

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 120, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 220, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 120, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 120, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 120, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=first.deviceId,
        )
        first.power_get = AsyncMock(return_value=True)
        second.power_get = AsyncMock(return_value=True)
        first.power_charge = AsyncMock(side_effect=lambda power: power)
        second.power_charge = AsyncMock(side_effect=lambda power: power)
        first.power_discharge = AsyncMock(side_effect=lambda power: power)
        second.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 120, datetime.now())

        first.power_charge.reset_mock()
        second.power_charge.reset_mock()
        first.power_discharge.reset_mock()
        second.power_discharge.reset_mock()

        hass.states.async_set("sensor.power_actual", "120", {"unit_of_measurement": "W"})
        await manager.update_primary_device(manager.primarydevice, second.deviceId)

        assert manager._selected_primary_device() is second
        first.power_charge.assert_awaited_once_with(-60)
        second.power_charge.assert_awaited_once_with(0)
        first.power_discharge.assert_not_awaited()
        second.power_discharge.assert_not_awaited()

    async def test_charges_a_99_percent_primary_in_output_mode_before_the_secondary(self, hass):
        """
        A selected primary at 99% should still receive charge priority even if it was previously in AC output mode.

        At 99% the taper cap is 100W, so the primary absorbs up to 100W and the remaining surplus
        overflows to the secondary via normal routing.
        """
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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(return_value=0)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, -300, datetime.now())

        assert primary.state is DeviceState.SOCNEARLYFULL
        primary.power_discharge.assert_not_awaited()
        primary.power_charge.assert_awaited_once_with(-100)
        secondary.power_charge.assert_awaited_once_with(-200)

    async def test_routes_near_full_primary_pv_overflow_to_the_secondary(self, hass):
        """
        A near-full selected primary should keep only its tapered PV charge and hand excess PV to the secondary.

        The primary has 500W PV: 200W already serves the home and 300W is currently flowing into the battery.
        At 99% SoC it may keep only 100W for charging, so the remaining 200W must be routed to the secondary
        instead of being dropped when the primary charge target is reduced.
        """
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-near-full-pv-overflow",
            device_name="sf800 pro primary near full pv overflow",
            product_model="SolarFlow 800 Pro",
            level=99,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=300,
            output_limit=0,
            home_output=200,
            battery_input=300,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-near-full-pv-overflow",
            device_name="sf800 pro secondary near full pv overflow",
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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert primary.state is DeviceState.SOCNEARLYFULL
        primary.power_charge.assert_awaited_once_with(-100)
        secondary.power_charge.assert_awaited_once_with(-200)
        primary.power_discharge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    @pytest.mark.parametrize("charge_time", [datetime.min, datetime.now() + timedelta(seconds=30)])
    async def test_output_mode_near_full_primary_export_overflow_charges_secondary(self, hass, charge_time):
        """
        An output-mode near-full primary should keep serving PV and route export overflow to the secondary.

        This mirrors the June 5 export window: the selected primary is near-full,
        already serving most local PV to the home, and charging at its taper cap.
        The remaining export is explained by the active primary PV floor, so the
        primary input gate stays closed. The secondary must still absorb the
        primary overflow plus its own local PV, even during charge holdoff.
        """
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-primary-near-full-output-export",
            device_name="sf800 pro primary near full output export",
            product_model="SolarFlow 800 Pro",
            level=99,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=498,
            home_output=497,
            battery_input=100,
        )
        primary.solarInput.update_value(597)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-near-full-output-export",
            device_name="sf800 pro secondary near full output export",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            input_limit=0,
            output_limit=0,
            battery_input=43,
        )
        secondary.solarInput.update_value(43)
        primary.fuseGrp.devices = [primary, secondary]
        secondary.fuseGrp = primary.fuseGrp
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=charge_time,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.power_bypass = AsyncMock(return_value=0)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -413, datetime.now())

        assert primary.state is DeviceState.SOCNEARLYFULL
        primary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(497)
        primary.power_bypass.assert_not_awaited()
        secondary.power_charge.assert_awaited_once_with(-456)
        secondary.power_discharge.assert_not_awaited()

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        primary.power_get = AsyncMock(return_value=False)
        secondary.power_get = AsyncMock(return_value=True)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 81, datetime.now())

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
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=False)
        secondary.power_get = AsyncMock(return_value=True)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -300, datetime.now())

        secondary.power_charge.assert_awaited_once_with(-300)

    async def test_empty_primary_output_self_trimming_under_export(self, hass):
        """EMPTY_SOC_STATES primary trims its solar home output before charging at the residual setpoint.

        When the primary is outputting solar to the home (100 W) and the house is
        exporting (setpoint = -180 W), the staged code should trim the primary's
        discharge target by 100 W (absorbing that portion of the export), leaving a
        -80 W residual that goes to primary battery input.

        Without the fix the primary would receive both power_charge(-180) AND
        power_discharge(100) in the same cycle — a contradictory pair.
        """
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-empty-primary-trim",
            device_name="sf800 pro empty primary trim",
            product_model="SolarFlow 800 Pro",
            level=10,  # SOCRESERVE, which is in EMPTY_SOC_STATES
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            home_output=100,
            battery_input=0,
            battery_output=0,
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-secondary-ac-charging",
            device_name="sf800 pro secondary ac charging",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            home_input=30,
            battery_input=30,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
            discharge_devices=(primary,),
            charge_devices=(secondary,),
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.pwr_produced = -100  # 100 W solar passing through to home
        secondary.pwr_produced = 0
        routing = manager._power_routing_snapshot(primary, primary_aware=True)

        # setpoint = -180: house is exporting 180 W net.  Primary's 100 W solar
        # output should absorb 100 W of that export (trim), leaving -80 W as
        # the residual charge command sent to primary battery input.
        await manager._apply_primary_input(-180, datetime.now(), routing)

        assert primary.state is DeviceState.SOCRESERVE
        # Home output must be stopped because the discharge target was zeroed by the trim.
        primary.power_discharge.assert_awaited_once_with(0)
        # Primary is charged at the residual setpoint: 180 - 100 (trim) = 80 W.
        primary.power_charge.assert_awaited_once_with(-80)
        # Secondary must not receive any additional AC charge.
        secondary.power_charge.assert_not_awaited()


class TestP1RoutingPipeline:
    """Verify the high-level P1 routing pipeline."""

    async def test_p1_changed_returns_true_when_update_routes(self, hass):
        manager = make_manager(hass, operation=ManagerMode.MATCHING)
        manager.zero_fast = datetime.min
        manager.zero_next = datetime.min
        _mock_prepared_power_routing(manager)

        routed = await manager._p1_changed(make_p1_event(100))

        assert routed is True
        _prepare_mock(manager).assert_called_once()
        _execute_mock(manager).assert_awaited_once()

    async def test_p1_changed_returns_false_when_fast_delay_suppresses_update(self, hass):
        manager = make_manager(hass, operation=ManagerMode.MATCHING)
        manager.zero_fast = datetime.now() + timedelta(seconds=SmartMode.TIMEFAST)
        manager.zero_next = datetime.now() + timedelta(seconds=SmartMode.TIMEZERO)
        _mock_prepared_power_routing(manager)

        routed = await manager._p1_changed(make_p1_event(10))

        assert routed is False
        _prepare_mock(manager).assert_not_called()

    async def test_route_p1_update_writes_simulation_before_forced_route(self, hass):
        manager = make_manager(hass, operation=ManagerMode.MATCHING)
        intent, routing, setpoint = _mock_prepared_power_routing(manager, setpoint=200)
        time = datetime.now()

        with (
            patch.object(ZendureManager, "simulation", True),
            patch.object(manager, "writeSimulation") as write_simulation,
        ):
            routed = await manager._route_p1_update(200, time, force=True)

        assert routed is True
        write_simulation.assert_called_once_with(time, 200)
        _prepare_mock(manager).assert_called_once_with(200, time, setpoint)
        _execute_mock(manager).assert_awaited_once_with(intent, time, routing)

    def test_fast_p1_change_check_ignores_short_history_without_mutating(self, hass):
        manager = make_manager(hass, operation=ManagerMode.MATCHING)
        manager.p1_history.clear()
        manager.p1_history.append(0)

        fast_change = manager._is_fast_p1_change(1000)

        assert fast_change is False
        assert list(manager.p1_history) == [0]

    def test_fast_p1_change_check_detects_large_jump_without_mutating(self, hass):
        manager = make_manager(hass, operation=ManagerMode.MATCHING)
        manager.p1_history.clear()
        manager.p1_history.extend([0, 0])

        fast_change = manager._is_fast_p1_change(1000)

        assert fast_change is True
        assert list(manager.p1_history) == [0, 0]

    def test_record_p1_history_appends_without_reset(self, hass):
        manager = make_manager(hass, operation=ManagerMode.MATCHING)
        manager.p1_history.clear()
        manager.p1_history.extend([10, 20])

        manager._record_p1_history(30)

        assert list(manager.p1_history) == [10, 20, 30]

    def test_record_p1_history_resets_existing_window(self, hass):
        manager = make_manager(hass, operation=ManagerMode.MATCHING)
        manager.p1_history.clear()
        manager.p1_history.extend([10, 20])

        manager._record_p1_history(100, reset=True)

        assert list(manager.p1_history) == [100]


class TestP1SpikeFilter:
    """Verify optional short upward P1 spike suppression."""

    SPIKE_POWER = 1000
    DEFAULT_THRESHOLD = 800
    HIGH_THRESHOLD = 1200
    DEFAULT_DURATION = 3

    @staticmethod
    def _enable_spike_filter(
        manager, *, threshold: int = DEFAULT_THRESHOLD, duration: float = DEFAULT_DURATION
    ) -> None:
        manager.p1_history.clear()
        manager.p1_history.extend([0, 0])
        manager.spike_filter.update_value(1)
        manager.spike_filter_threshold.update_value(threshold)
        manager.spike_filter_duration.update_value(duration)

    async def test_suppresses_upward_spike_before_duration(self, hass):
        manager = make_manager(hass)
        self._enable_spike_filter(manager)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(self.SPIKE_POWER))

        _prepare_mock(manager).assert_not_called()
        assert list(manager.p1_history) == [0, 0]
        assert manager.p1_spike_started is not None

    async def test_fast_change_routes_sustained_spike_after_duration(self, hass):
        manager = make_manager(hass)
        self._enable_spike_filter(manager)
        manager.zero_fast = datetime.min
        manager.zero_next = datetime.now() + timedelta(seconds=SmartMode.TIMEZERO)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(self.SPIKE_POWER))
        assert list(manager.p1_history) == [0, 0]
        assert manager.p1_spike_started is not None
        manager.p1_spike_started = datetime.now() - timedelta(seconds=4)

        await manager._p1_changed(make_p1_event(self.SPIKE_POWER))

        _prepare_mock(manager).assert_called_once()
        await_args = _prepare_mock(manager).call_args
        assert await_args is not None
        assert await_args.args[0] == self.SPIKE_POWER
        assert manager.p1_spike_started is None
        assert list(manager.p1_history) == [self.SPIKE_POWER]

    async def test_drops_spike_candidate_when_reading_returns_before_duration(self, hass):
        manager = make_manager(hass)
        self._enable_spike_filter(manager)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(self.SPIKE_POWER))
        await manager._p1_changed(make_p1_event(0))

        _prepare_mock(manager).assert_called_once()
        await_args = _prepare_mock(manager).call_args
        assert await_args is not None
        assert await_args.args[0] == 0
        assert manager.p1_spike_started is None

    async def test_uses_configured_threshold(self, hass):
        manager = make_manager(hass)
        self._enable_spike_filter(manager, threshold=self.HIGH_THRESHOLD)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(self.SPIKE_POWER))

        _prepare_mock(manager).assert_called_once()
        assert manager.p1_spike_started is None

    async def test_filter_switch_off_preserves_existing_p1_handling(self, hass):
        manager = make_manager(hass)
        manager.p1_history.clear()
        manager.p1_history.extend([0, 0])
        manager.spike_filter.update_value(0)
        manager.spike_filter_threshold.update_value(self.DEFAULT_THRESHOLD)
        manager.spike_filter_duration.update_value(self.DEFAULT_DURATION)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(self.SPIKE_POWER))

        _prepare_mock(manager).assert_called_once()
        assert manager.p1_spike_started is None

    async def test_turning_filter_off_clears_pending_candidate(self, hass):
        manager = make_manager(hass)
        self._enable_spike_filter(manager)

        await manager._p1_changed(make_p1_event(self.SPIKE_POWER))
        assert manager.p1_spike_started is not None

        await manager.spike_filter.async_turn_off()

        assert manager.spike_filter.is_on is False
        assert manager.p1_spike_started is None


class TestP1ChargeLagFastPath:
    """Verify active charge correction can bypass normal P1 debounce."""

    @staticmethod
    def _block_normal_p1_debounce(manager) -> None:
        now = datetime.now()
        manager.zero_next = now + timedelta(seconds=SmartMode.TIMEZERO)
        manager.zero_fast = now + timedelta(seconds=SmartMode.TIMEFAST)
        manager.p1_charge_lag_last_update = now - SmartMode.P1_MIN_UPDATE

    @pytest.mark.parametrize("p1_value", [pytest.param(150, id="import"), pytest.param(-100, id="export")])
    async def test_active_charge_deviation_bypasses_p1_debounce(self, hass, p1_value):
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-fast-charge-{p1_value}",
            device_name=f"sf800 pro fast charge {p1_value}",
            product_model="SolarFlow 800 Pro",
            level=60,
            ac_mode=AcMode.INPUT,
            input_limit=300,
            output_limit=0,
            home_input=300,
            battery_input=300,
        )
        device.solarInput.update_value(300)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
            charge_devices=(device,),
        )
        self._block_normal_p1_debounce(manager)
        last_charge_lag_update = manager.p1_charge_lag_last_update
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(p1_value))

        _prepare_mock(manager).assert_called_once()
        await_args = _prepare_mock(manager).call_args
        assert await_args is not None
        assert await_args.args[0] == p1_value
        assert manager.p1_charge_lag_last_update > last_charge_lag_update

    async def test_active_charge_deviation_without_primary_keeps_existing_p1_debounce(self, hass):
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-fast-charge-without-primary",
            device_name="sf800 pro fast charge without primary",
            product_model="SolarFlow 800 Pro",
            level=60,
            ac_mode=AcMode.INPUT,
            input_limit=300,
            output_limit=0,
            home_input=300,
            battery_input=300,
        )
        device.solarInput.update_value(300)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            charge_devices=(device,),
        )
        self._block_normal_p1_debounce(manager)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(150))

        _prepare_mock(manager).assert_not_called()

    @pytest.mark.parametrize("charging_device_is_primary", [True, False])
    async def test_charge_telemetry_bypasses_p1_debounce_without_previous_manager_bucket(
        self, hass, charging_device_is_primary
    ):
        p1_value = 150
        charging = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-fast-telemetry-charge-{charging_device_is_primary}",
            device_name=f"sf800 pro fast telemetry charge {charging_device_is_primary}",
            product_model="SolarFlow 800 Pro",
            level=60,
            ac_mode=AcMode.INPUT,
            input_limit=600,
            output_limit=0,
            home_input=600,
            battery_input=600,
        )
        charging.solarInput.update_value(100)
        full_bypass = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-fast-telemetry-full-{charging_device_is_primary}",
            device_name=f"sf800 pro fast telemetry full {charging_device_is_primary}",
            product_model="SolarFlow 800 Pro",
            level=100,
            home_output=200,
        )
        full_bypass.solarInput.update_value(700)
        full_bypass.byPass.update_value(1)
        manager = make_manager(
            hass,
            devices=(charging, full_bypass),
            operation=ManagerMode.MATCHING,
            primary_device_id=charging.deviceId if charging_device_is_primary else full_bypass.deviceId,
        )
        self._block_normal_p1_debounce(manager)
        last_charge_lag_update = manager.p1_charge_lag_last_update
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(p1_value))

        _prepare_mock(manager).assert_called_once()
        await_args = _prepare_mock(manager).call_args
        assert await_args is not None
        assert await_args.args[0] == p1_value
        assert manager.p1_charge_lag_last_update > last_charge_lag_update

    async def test_first_charge_lag_deviation_after_normal_update_bypasses_p1_debounce(self, hass):
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-fast-charge-after-normal-update",
            device_name="sf800 pro fast charge after normal update",
            product_model="SolarFlow 800 Pro",
            level=60,
            ac_mode=AcMode.INPUT,
            input_limit=300,
            output_limit=0,
            home_input=300,
            battery_input=300,
        )
        device.solarInput.update_value(300)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
        )
        self._block_normal_p1_debounce(manager)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(150))

        _prepare_mock(manager).assert_called_once()

    async def test_active_charge_without_pv_keeps_existing_p1_debounce(self, hass):
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-fast-charge-without-pv",
            device_name="sf800 pro fast charge without pv",
            product_model="SolarFlow 800 Pro",
            level=60,
            ac_mode=AcMode.INPUT,
            input_limit=300,
            output_limit=0,
            home_input=300,
            battery_input=300,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
            charge_devices=(device,),
        )
        self._block_normal_p1_debounce(manager)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(150))

        _prepare_mock(manager).assert_not_called()

    async def test_bypassing_primary_with_charge_candidate_bypasses_p1_debounce(self, hass):
        p1_value = -100
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-fast-bypass-primary",
            device_name="sf800 pro fast bypass primary",
            product_model="SolarFlow 800 Pro",
            level=100,
            home_output=200,
        )
        primary.solarInput.update_value(200)
        primary.byPass.update_value(1)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-fast-bypass-secondary",
            device_name="sf800 pro fast bypass secondary",
            product_model="SolarFlow 800 Pro",
            level=60,
        )
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            discharge_devices=(primary,),
            idle_devices=(secondary,),
        )
        self._block_normal_p1_debounce(manager)
        last_charge_lag_update = manager.p1_charge_lag_last_update
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(p1_value))

        _prepare_mock(manager).assert_called_once()
        await_args = _prepare_mock(manager).call_args
        assert await_args is not None
        assert await_args.args[0] == p1_value
        assert manager.p1_charge_lag_last_update > last_charge_lag_update

    @pytest.mark.parametrize(
        "p1_value",
        [
            pytest.param(20, id="positive-edge"),
            pytest.param(-20, id="negative-edge"),
            pytest.param(10, id="positive-small"),
            pytest.param(-10, id="negative-small"),
        ],
    )
    async def test_charge_lag_deadband_keeps_existing_p1_debounce(self, hass, p1_value):
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-fast-deadband-{p1_value}",
            device_name=f"sf800 pro fast deadband {p1_value}",
            product_model="SolarFlow 800 Pro",
            level=60,
            ac_mode=AcMode.INPUT,
            input_limit=300,
            output_limit=0,
            home_input=300,
            battery_input=300,
        )
        device.solarInput.update_value(300)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
            charge_devices=(device,),
        )
        self._block_normal_p1_debounce(manager)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(p1_value))

        _prepare_mock(manager).assert_not_called()

    async def test_charge_lag_fast_path_respects_minimum_p1_interval(self, hass):
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-fast-charge-min-interval",
            device_name="sf800 pro fast charge min interval",
            product_model="SolarFlow 800 Pro",
            level=60,
            ac_mode=AcMode.INPUT,
            input_limit=300,
            output_limit=0,
            home_input=300,
            battery_input=300,
        )
        device.solarInput.update_value(300)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
            charge_devices=(device,),
        )
        self._block_normal_p1_debounce(manager)
        manager.p1_charge_lag_last_update = datetime.now()
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(150))

        _prepare_mock(manager).assert_not_called()

    async def test_charge_lag_fast_path_requires_active_charge_state(self, hass):
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-fast-no-charge-state",
            device_name="sf800 pro fast no charge state",
            product_model="SolarFlow 800 Pro",
            level=60,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
        )
        self._block_normal_p1_debounce(manager)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(150))

        _prepare_mock(manager).assert_not_called()


class TestP1ExportTrimFastPath:
    """Verify strong export can trim active battery output without starting charge."""

    @staticmethod
    def _block_normal_p1_debounce(manager, p1_value: int) -> None:
        now = datetime.now()
        manager.zero_next = now + timedelta(seconds=SmartMode.TIMEZERO)
        manager.zero_fast = now + timedelta(seconds=SmartMode.TIMEFAST)
        manager.p1_export_trim_last_update = now - SmartMode.P1_MIN_UPDATE
        manager.p1_history.clear()
        manager.p1_history.extend([p1_value, p1_value])

    @pytest.mark.parametrize("operation", [ManagerMode.MATCHING, ManagerMode.MATCHING_DISCHARGE])
    async def test_strong_export_bypasses_p1_debounce_and_trims_output_without_charging(self, hass, operation):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-export-trim-primary-{operation.name.lower()}",
            device_name=f"sf800 pro export trim primary {operation.name.lower()}",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=800,
            battery_output=700,
        )
        primary.solarInput.update_value(100)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id=f"sf800-pro-export-trim-recovering-secondary-{operation.name.lower()}",
            device_name=f"sf800 pro export trim recovering secondary {operation.name.lower()}",
            product_model="SolarFlow 800 Pro",
            level=13,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=80,
        )
        secondary.solarInput.update_value(90)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=operation,
            discharge_recovery_margin=5,
            primary_device_id=primary.deviceId,
        )
        self._block_normal_p1_debounce(manager, -650)
        last_export_trim_update = manager.p1_export_trim_last_update
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        routed = await manager._p1_changed(make_p1_event(-650))

        assert routed is True
        assert manager.p1_export_trim_last_update > last_export_trim_update
        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(150)
        secondary.power_discharge.assert_awaited_once_with(80)

    async def test_strong_export_trim_to_zero_stays_on_output_path(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-export-trim-zero-primary",
            device_name="sf800 pro export trim zero primary",
            product_model="SolarFlow 800 Pro",
            level=80,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_output=300,
            battery_output=300,
        )
        manager = make_manager(
            hass,
            devices=(primary,),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        self._block_normal_p1_debounce(manager, -500)
        primary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)

        routed = await manager._p1_changed(make_p1_event(-500))

        assert routed is True
        primary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(0)

    async def test_small_export_keeps_existing_p1_debounce(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-export-trim-small-export",
            device_name="sf800 pro export trim small export",
            product_model="SolarFlow 800 Pro",
            level=80,
            home_output=800,
            battery_output=700,
        )
        primary.solarInput.update_value(100)
        manager = make_manager(
            hass,
            devices=(primary,),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        self._block_normal_p1_debounce(manager, -100)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(-100))

        _prepare_mock(manager).assert_not_called()

    async def test_pv_only_export_keeps_existing_p1_debounce(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-export-trim-pv-only",
            device_name="sf800 pro export trim pv only",
            product_model="SolarFlow 800 Pro",
            level=80,
            home_output=100,
            battery_output=0,
        )
        primary.solarInput.update_value(100)
        manager = make_manager(
            hass,
            devices=(primary,),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        self._block_normal_p1_debounce(manager, -650)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(-650))

        _prepare_mock(manager).assert_not_called()

    async def test_unsupported_mode_keeps_existing_p1_debounce(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-export-trim-unsupported-mode",
            device_name="sf800 pro export trim unsupported mode",
            product_model="SolarFlow 800 Pro",
            level=80,
            home_output=800,
            battery_output=700,
        )
        primary.solarInput.update_value(100)
        manager = make_manager(
            hass,
            devices=(primary,),
            operation=ManagerMode.MATCHING_CHARGE,
            primary_device_id=primary.deviceId,
        )
        self._block_normal_p1_debounce(manager, -650)
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(-650))

        _prepare_mock(manager).assert_not_called()

    async def test_export_trim_fast_path_respects_minimum_p1_interval(self, hass):
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-export-trim-min-interval",
            device_name="sf800 pro export trim min interval",
            product_model="SolarFlow 800 Pro",
            level=80,
            home_output=800,
            battery_output=700,
        )
        primary.solarInput.update_value(100)
        manager = make_manager(
            hass,
            devices=(primary,),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
        )
        self._block_normal_p1_debounce(manager, -650)
        manager.p1_export_trim_last_update = datetime.now()
        _mock_prepared_power_routing(manager)

        await manager._p1_changed(make_p1_event(-650))

        _prepare_mock(manager).assert_not_called()


class TestZeroFastRecovery:
    """Verify that zero_fast is always restored after power distribution calls."""

    async def test_p1_changed_resets_zero_fast_after_successful_routing_pipeline(self, hass):
        """zero_fast must not stay at datetime.max after a normal _p1_changed cycle."""
        device = make_device(hass, level=50, home_output=100, battery_output=100)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
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

    async def test_p1_changed_resets_zero_fast_after_routing_stage_raises(self, hass):
        """zero_fast must be restored even when a routing pipeline stage raises an exception."""
        device = make_device(hass, level=50)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
        )
        manager.zero_next = datetime.min
        manager.zero_fast = datetime.min

        state = Mock()
        state.state = "100"
        event = Mock()
        event.data = {"new_state": state, "old_state": None, "entity_id": "sensor.power_actual"}

        with patch.object(manager, "_poll_devices_and_prepare_routing_state", side_effect=RuntimeError("boom")):
            await manager._p1_changed(event)

        assert manager.zero_fast != datetime.max
        assert manager.zero_fast < datetime.now() + timedelta(seconds=10)

    async def test_p1_changed_resets_zero_fast_after_cancelled_error(self, hass):
        """zero_fast must be restored even when a routing pipeline stage is cancelled."""
        device = make_device(hass, level=50)
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
        )
        manager.zero_next = datetime.min
        manager.zero_fast = datetime.min

        state = Mock()
        state.state = "100"
        event = Mock()
        event.data = {"new_state": state, "old_state": None, "entity_id": "sensor.power_actual"}

        with (
            patch.object(manager, "_poll_devices_and_prepare_routing_state", side_effect=asyncio.CancelledError),
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
            operation=ManagerMode.MATCHING,
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
        """zero_fast must be restored even when a forced routing stage raises during update_primary_device."""
        device = make_device(
            hass,
            device_id="primary",
            device_name="primary",
            level=50,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
        )

        p1_state = Mock()
        p1_state.state = "200"
        p1_state.attributes = {"unit_of_measurement": "W"}
        manager.hass.states = Mock()
        manager.hass.states.get = Mock(return_value=p1_state)

        manager.zero_fast = datetime.min

        with (
            patch.object(manager, "_poll_devices_and_prepare_routing_state", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            await manager.update_primary_device(manager.primarydevice, device.deviceId)

        assert manager.zero_fast != datetime.max
        assert manager.zero_fast < datetime.now() + timedelta(seconds=10)


class TestNearFullChargeTaper:
    """Manager routing tests for near-full (SOCNEARLYFULL) charge taper behavior."""

    async def test_near_full_sf800_pro_output_accounts_for_local_pv_taper(self, hass):
        """Local PV already flowing into the battery should raise the output target to honor taper."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-nearlyfull-local-pv-output",
            product_model="SolarFlow 800 Pro",
            level=98,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            home_output=250,
            battery_input=322,
            output_limit=250,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        assert device.state is DeviceState.SOCNEARLYFULL
        device.power_discharge.assert_awaited_once_with(422)

    async def test_near_full_sf800_pro_is_not_bypassed_when_reduced_to_zero(self, hass):
        """A near-full SF800 Pro should NOT switch to bypass; bypass is reserved for SOCFULL."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-nearlyfull-nobypass",
            product_model="SolarFlow 800 Pro",
            level=98,
            soc_set=100,
            home_input=100,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_bypass = AsyncMock(return_value=0)
        device.power_discharge = AsyncMock(return_value=0)
        device.power_charge = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 100, datetime.now())

        assert device.state is DeviceState.SOCNEARLYFULL
        device.power_bypass.assert_not_awaited()

    async def test_near_full_sf800_pro_discharge_is_not_restricted(self, hass):
        """A near-full SF800 Pro should discharge normally — SOCNEARLYFULL does not block discharge."""
        device = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="sf800-pro-nearlyfull-discharge",
            product_model="SolarFlow 800 Pro",
            level=96,
            soc_set=100,
            home_output=100,
        )
        manager = make_manager(
            hass,
            devices=(device,),
            operation=ManagerMode.MATCHING,
            primary_device_id=device.deviceId,
            discharge_devices=(device,),
        )
        device.power_get = AsyncMock(return_value=True)
        device.power_discharge = AsyncMock(side_effect=lambda power: power)
        device.power_bypass = AsyncMock(return_value=0)

        await _run_prepared_power_routing(manager, 300, datetime.now())

        assert device.state is DeviceState.SOCNEARLYFULL
        device.power_discharge.assert_awaited_once()
        called_power = device.power_discharge.call_args[0][0]
        assert called_power > 0


class TestChargeHoldoffTimers:
    """Verify the anti-oscillation charge holdoff uses the correct timer values."""

    def test_holdoff_duration_is_one_second(self, hass) -> None:
        """Holdoff is always 1 s."""
        from custom_components.zendure_ha.manager import CHARGE_HOLDOFF_SECONDS

        assert CHARGE_HOLDOFF_SECONDS == 1

        device = make_device(hass, device_id="holdoff-timer-device", device_name="holdoff timer device", level=50)
        manager = make_manager(hass, devices=(device,), operation=ManagerMode.MATCHING)

        now = datetime.now()

        # Trigger the holdoff by requesting charge while charge_time==datetime.max
        manager._apply_charge_holdoff(-200, now, allow_charge=True)

        elapsed = (manager.charge_time - now).total_seconds()
        assert abs(elapsed - 1) < 1


class TestLowSocImmediatePromotion:
    """Idle devices in low-SoC states are promoted to charging without waiting for a surplus threshold."""

    @pytest.mark.parametrize("level, state", LOW_SOC_DEVICE_CASES)
    async def test_idle_low_soc_device_is_promoted_to_charging_immediately(self, hass, level, state):
        """An idle empty or at-reserve device should receive a charge command even if the surplus is small."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="low-soc-primary",
            device_name="low soc primary",
            product_model="SolarFlow 800 Pro",
            level=60,
            min_soc=5,
            reserve=15,
            soc_set=100,
            home_output=100,
            battery_input=100,
        )
        idle_low = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="low-soc-idle-secondary",
            device_name="low soc idle secondary",
            product_model="SolarFlow 800 Pro",
            level=level,
            min_soc=5,
            reserve=15,
            soc_set=100,
            battery_input=800,
        )
        assert idle_low.state is state

        manager = make_manager(
            hass,
            devices=(primary, idle_low),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        idle_low.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        idle_low.power_charge = AsyncMock(side_effect=lambda power: power)
        idle_low.power_discharge = AsyncMock(side_effect=lambda power: power)

        # Small surplus — well below the startup threshold for a normal idle device.
        await _run_prepared_power_routing(manager, -50, datetime.now())

        idle_low.power_charge.assert_awaited_once()
        charged = idle_low.power_charge.call_args[0][0]
        assert charged < 0


class TestPrimaryNoLocalSolarDefers:
    """Primary that has no local solar defers charge allocation to a secondary with surplus."""

    async def test_primary_without_local_solar_defers_charge_to_secondary_with_surplus(self, hass):
        """Primary with no local solar defers charge allocation to secondary local surplus."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="no-solar-primary",
            device_name="no solar primary",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            home_input=300,  # currently charging from grid — no local solar
        )
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="local-solar-secondary",
            device_name="local solar secondary",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            battery_input=300,
            ac_mode=AcMode.OUTPUT,
            input_limit=0,
            output_limit=0,
        )
        secondary.solarInput.update_value(300)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.now() + timedelta(seconds=30),  # holdoff active
            charge_devices=(primary,),
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -300, datetime.now())

        # Primary should be zeroed; secondary absorbs the local surplus.
        primary.power_charge.assert_awaited_once_with(0)
        secondary.power_charge.assert_awaited_once()
        secondary_target = secondary.power_charge.call_args[0][0]
        assert secondary_target < 0

    async def test_blocked_primary_surplus_does_not_restart_secondary_ac_charge(self, hass):
        """
        Primary-local PV must not be transferred to secondary charging, and secondary-local PV stays local.

        The primary is already serving output and cannot switch to input.
        """
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="output-primary-local-surplus",
            device_name="output primary local surplus",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            home_output=250,
            battery_input=322,
            input_limit=0,
            output_limit=250,
        )
        primary.solarInput.update_value(572)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="secondary-own-small-surplus",
            device_name="secondary own small surplus",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            battery_input=72,
            input_limit=0,
            output_limit=0,
        )
        secondary.solarInput.update_value(72)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.now() + timedelta(seconds=30),
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, -8, datetime.now())

        primary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(250)
        secondary.power_charge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()

    async def test_blocked_primary_input_does_not_fall_back_to_weighted_secondary_charge(self, hass):
        """Gated selected-primary input must not fall back to weighted secondary charge."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="blocked-primary-input-output",
            device_name="blocked primary input output",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            home_output=250,
            battery_input=322,
            input_limit=0,
            output_limit=250,
        )
        primary.solarInput.update_value(572)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="active-secondary-local-charge-only",
            device_name="active secondary local charge only",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            home_input=72,
            battery_input=144,
            input_limit=72,
            output_limit=0,
        )
        secondary.solarInput.update_value(72)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        manager._reset_power_distribution_state()
        await manager._poll_devices_and_prepare_routing_state(-60)
        routing = manager._power_routing_snapshot(primary, primary_aware=True)

        await manager._apply_primary_input(-400, datetime.now(), routing, allow_selected_primary_input=False)

        primary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(178)
        secondary.power_charge.assert_awaited_once_with(0)
        secondary.power_discharge.assert_not_awaited()

    async def test_active_secondary_pv_input_is_not_counted_twice_as_ac_charge(self, hass):
        """An input-mode secondary keeps local PV by stopping duplicate AC input."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="export-shaped-primary",
            device_name="export shaped primary",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            home_output=462,
            battery_input=126,
            input_limit=0,
            output_limit=463,
        )
        primary.solarInput.update_value(588)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="export-shaped-secondary",
            device_name="export shaped secondary",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.INPUT,
            home_input=79,
            battery_input=158,
            input_limit=79,
            output_limit=0,
        )
        secondary.solarInput.update_value(79)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)

        await _run_prepared_power_routing(manager, 0, datetime.now())

        primary.power_charge.assert_not_awaited()
        primary.power_discharge.assert_awaited_once_with(383)
        secondary.power_charge.assert_awaited_once_with(0)
        secondary.power_discharge.assert_not_awaited()

    async def test_idle_secondary_pv_input_is_not_restarted_as_ac_charge(self, hass):
        """A PV-only secondary should not be restarted as AC input after being zeroed."""
        primary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="export-shaped-idle-primary",
            device_name="export shaped idle primary",
            product_model="SolarFlow 800 Pro",
            level=50,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            home_output=462,
            battery_input=126,
            input_limit=0,
            output_limit=463,
        )
        primary.solarInput.update_value(588)
        secondary = make_device(
            hass,
            device_cls=SolarFlow800Pro,
            device_id="export-shaped-idle-secondary",
            device_name="export shaped idle secondary",
            product_model="SolarFlow 800 Pro",
            level=40,
            min_soc=5,
            reserve=10,
            soc_set=100,
            ac_mode=AcMode.OUTPUT,
            battery_input=79,
            input_limit=0,
            output_limit=0,
        )
        secondary.solarInput.update_value(79)
        manager = make_manager(
            hass,
            devices=(primary, secondary),
            operation=ManagerMode.MATCHING,
            primary_device_id=primary.deviceId,
            charge_time=datetime.min,
            discharge_devices=(primary,),
            idle_devices=(secondary,),
        )
        primary.power_get = AsyncMock(return_value=True)
        secondary.power_get = AsyncMock(return_value=True)
        primary.power_charge = AsyncMock(side_effect=lambda power: power)
        secondary.power_charge = AsyncMock(side_effect=lambda power: power)
        primary.power_discharge = AsyncMock(side_effect=lambda power: power)
        secondary.power_discharge = AsyncMock(side_effect=lambda power: power)
        primary.pwr_produced = -588
        secondary.pwr_produced = -79
        routing = manager._power_routing_snapshot(primary, primary_aware=True)

        await manager._apply_primary_input(-400, datetime.now(), routing, allow_selected_primary_input=False)

        primary.power_charge.assert_not_awaited()
        secondary.power_charge.assert_not_awaited()
        secondary.power_discharge.assert_not_awaited()
