"""API-level local MQTT routing tests."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

from custom_components.zendure_ha.api import Api
from custom_components.zendure_ha.const import ConnectionMode
from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro

from .common import make_device


def test_zendure_topic_for_zensdk_device_is_ignored_without_warning(hass, caplog):
    """Plain ZenSDK devices should ignore Zendure MQTT topics without warning."""
    api = Api()
    device = make_device(hass, device_id="device-123")
    device.connection.update_value(ConnectionMode.ZENSDK)
    api.devices = {device.deviceId: device}
    msg = SimpleNamespace(topic=f"Zendure/sensor/{device.snNumber}/hyperTmp", payload=b"7")

    caplog.clear()
    with patch.object(hass, "add_job") as mock_add_job, caplog.at_level(logging.WARNING):
        api.mqttMsgLocal(Mock(), None, msg)

    mock_add_job.assert_not_called()
    assert "Unknown Zendure local MQTT message" not in caplog.text


def test_zendure_topic_for_hybrid_device_schedules_local_handler(hass):
    """Hybrid devices should still consume Zendure MQTT topics."""
    api = Api()
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="device-800-pro",
        product_model="SolarFlow 800 Pro",
    )
    device.connection.update_value(ConnectionMode.ZENSDK_WITH_LOCAL_MQTT)
    api.devices = {device.deviceId: device}
    msg = SimpleNamespace(topic=f"Zendure/sensor/{device.snNumber}/hyperTmp", payload=b"7")
    client = Mock()

    with patch.object(hass, "add_job") as mock_add_job:
        api.mqttMsgLocal(client, None, msg)

    mock_add_job.assert_called_once()
    callback, *args = mock_add_job.call_args.args
    assert callback.__self__ is device
    assert callback.__name__ == "handleLocalMqttMessage"
    assert args == [client, "sensor", "hyperTmp", "7"]


def test_unknown_zendure_topic_still_warns_for_unmatched_target(hass, caplog):
    """Unknown Zendure MQTT targets should keep the setup-race warning."""
    api = Api()
    device = make_device(
        hass,
        device_cls=SolarFlow800Pro,
        device_id="device-800-pro",
        product_model="SolarFlow 800 Pro",
    )
    device.connection.update_value(ConnectionMode.ZENSDK_WITH_LOCAL_MQTT)
    api.devices = {device.deviceId: device}
    msg = SimpleNamespace(topic="Zendure/sensor/unknown-serial/hyperTmp", payload=b"7")

    caplog.clear()
    with patch.object(hass, "add_job") as mock_add_job, caplog.at_level(logging.WARNING):
        api.mqttMsgLocal(Mock(), None, msg)

    mock_add_job.assert_not_called()
    assert (
        "Unknown Zendure local MQTT message for unknown-serial: "
        "Zendure/sensor/unknown-serial/hyperTmp; the device may still be setting up in the integration"
    ) in caplog.text
