"""Migration behavior tests."""

from __future__ import annotations

from importlib import import_module
from unittest.mock import patch

from custom_components.zendure_ha.const import DOMAIN
from custom_components.zendure_ha.migration import Migration

from .common import make_config_entry


def test_check_device_does_not_rename_model_serial_entity_ids_for_sf_800_pro_balkon(hass):
    """Existing model/serial entity IDs should survive cloud name changes."""
    recorder_reconcile_delay = 5
    entry = make_config_entry()
    entry.add_to_hass(hass)

    device_registry_module = import_module("homeassistant.helpers.device_registry")
    entity_registry_module = import_module("homeassistant.helpers.entity_registry")
    device_registry = device_registry_module.async_get(hass)
    entity_registry = entity_registry_module.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "device-1")},
        name="SolarFlow 800 Pro",
        model="SolarFlow 800 Pro",
        serial_number="TESTSER894",
    )
    entity_entry = entity_registry.async_get_or_create(
        "switch",
        DOMAIN,
        "sf800pro_894_lamp_switch",
        object_id_base="sf800pro_894_lamp_switch",
        config_entry=entry,
        device_id=device_entry.id,
        translation_key="lamp_switch",
    )

    assert entity_entry.entity_id == "switch.sf800pro_894_lamp_switch"

    Migration._changes = []
    Migration._update = None  # pyright: ignore[reportAttributeAccessIssue]

    with patch(
        "custom_components.zendure_ha.migration.async_call_later",
        return_value=lambda: None,
    ) as mock_call_later:
        Migration.check_device(
            hass,
            "device-1",
            "SF 800 Pro Balkon",
            "SolarFlow 800 Pro",
            "TESTSER894",
        )

    updated_device = device_registry.async_get(device_entry.id)
    updated_entity = entity_registry.async_get(entity_entry.entity_id)

    assert updated_device is not None
    assert updated_device.name == "SF 800 Pro Balkon"
    assert updated_entity is not None
    assert updated_entity.entity_id == "switch.sf800pro_894_lamp_switch"
    assert updated_entity.unique_id == "sf800pro_894_lamp_switch"
    assert updated_entity.translation_key == "lamp_switch"
    assert Migration._changes == []
    assert Migration._update is None  # pyright: ignore[reportAttributeAccessIssue]

    mock_call_later.assert_called_once()
    assert mock_call_later.call_args.args[1] == recorder_reconcile_delay
    assert mock_call_later.call_args.args[2].func is Migration._reconcile_recorder


def test_check_device_does_not_rename_model_serial_entity_ids_when_name_is_unchanged(hass):
    """Existing model/serial entity IDs should survive unchanged-name startup checks."""
    recorder_reconcile_delay = 5
    entry = make_config_entry()
    entry.add_to_hass(hass)

    device_registry_module = import_module("homeassistant.helpers.device_registry")
    entity_registry_module = import_module("homeassistant.helpers.entity_registry")
    device_registry = device_registry_module.async_get(hass)
    entity_registry = entity_registry_module.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "device-1")},
        name="SF 800 Pro Balkon",
        model="SolarFlow 800 Pro",
        serial_number="TESTSER894",
    )
    entity_entry = entity_registry.async_get_or_create(
        "switch",
        DOMAIN,
        "sf800pro_894_lamp_switch",
        object_id_base="sf800pro_894_lamp_switch",
        config_entry=entry,
        device_id=device_entry.id,
        translation_key="lamp_switch",
    )

    assert entity_entry.entity_id == "switch.sf800pro_894_lamp_switch"

    Migration._changes = []
    Migration._update = None  # pyright: ignore[reportAttributeAccessIssue]

    with patch(
        "custom_components.zendure_ha.migration.async_call_later",
        return_value=lambda: None,
    ) as mock_call_later:
        Migration.check_device(
            hass,
            "device-1",
            "SF 800 Pro Balkon",
            "SolarFlow 800 Pro",
            "TESTSER894",
        )

    updated_device = device_registry.async_get(device_entry.id)
    updated_entity = entity_registry.async_get(entity_entry.entity_id)

    assert updated_device is not None
    assert updated_device.name == "SF 800 Pro Balkon"
    assert updated_entity is not None
    assert updated_entity.entity_id == "switch.sf800pro_894_lamp_switch"
    assert updated_entity.unique_id == "sf800pro_894_lamp_switch"
    assert updated_entity.translation_key == "lamp_switch"
    assert Migration._changes == []
    assert Migration._update is None  # pyright: ignore[reportAttributeAccessIssue]

    mock_call_later.assert_called_once()
    assert mock_call_later.call_args.args[1] == recorder_reconcile_delay
    assert mock_call_later.call_args.args[2].func is Migration._reconcile_recorder


async def test_async_migrate_uses_three_digit_battery_entity_ids(hass):
    """async_migrate should use the standard 3-digit serial suffix for battery entities."""
    entry = make_config_entry()
    entry.add_to_hass(hass)

    device_registry_module = import_module("homeassistant.helpers.device_registry")
    entity_registry_module = import_module("homeassistant.helpers.entity_registry")
    device_registry = device_registry_module.async_get(hass)
    entity_registry = entity_registry_module.async_get(hass)

    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "CE3A31513")},
        name="AB2000X 31513",
        model="AB2000X",
        serial_number="CE3A31513",
    )
    entity_entry = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "ab2000x_max_temp",
        object_id_base="ab2000x_max_temp",
        config_entry=entry,
        device_id=device_entry.id,
        translation_key="max_temp",
    )

    assert entity_entry.entity_id == "sensor.ab2000x_max_temp"

    with patch.object(Migration, "_update_files", return_value=False):
        await Migration.async_migrate(hass, entry.entry_id)

    updated_entity = entity_registry.async_get("sensor.ab2000x_513_max_temp")
    assert updated_entity is not None
    assert updated_entity.entity_id == "sensor.ab2000x_513_max_temp"
    assert updated_entity.unique_id == "ab2000x_513_max_temp"


async def test_check_entities_does_not_read_translations_on_event_loop(hass):
    """check_entities must not perform blocking translation file I/O on the event loop."""
    from importlib import import_module

    from custom_components.zendure_ha.entity import EntityDevice

    entry = make_config_entry()
    entry.add_to_hass(hass)

    device_registry_module = import_module("homeassistant.helpers.device_registry")
    device_registry = device_registry_module.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "device-1")},
        name="SolarFlow 800 Pro",
        model="SolarFlow 800 Pro",
        serial_number="TESTSER894",
    )

    previous_check_entity = EntityDevice.checkEntity
    EntityDevice.checkEntity = None
    try:
        device = EntityDevice(hass, "empty", "empty")
        with patch("custom_components.zendure_ha.entity.Path.read_text", side_effect=AssertionError):
            device.check_entities(device_entry, "sf800pro_894")
    finally:
        EntityDevice.checkEntity = previous_check_entity


def test_check_entities_does_not_rename_model_serial_entity_ids_when_device_name_differs(hass):
    """
    EntityDevice.check_entities must not rename entities to device-name-based IDs.

    Entities are keyed by model+serial prefix (e.g. sf800pro_767_bat_in_out).
    When the device has a user-chosen name like 'SF 800 Pro WZ Balkon',
    check_entities must not rename them to sf_800_pro_wz_balkon_bat_in_out.
    """
    from importlib import import_module
    from unittest.mock import patch

    from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro

    from .common import make_config_entry

    entry = make_config_entry()
    entry.add_to_hass(hass)

    device_registry_module = import_module("homeassistant.helpers.device_registry")
    entity_registry_module = import_module("homeassistant.helpers.entity_registry")
    device_registry = device_registry_module.async_get(hass)
    entity_registry = entity_registry_module.async_get(hass)

    sn = "TESTSER767"
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "device-1"), (DOMAIN, sn)},
        name="SF 800 Pro WZ Balkon",
        model="SolarFlow 800 Pro",
        serial_number=sn,
    )
    entity_entry = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "sf800pro_767_bat_in_out",
        object_id_base="sf800pro_767_bat_in_out",
        config_entry=entry,
        device_id=device_entry.id,
        translation_key="bat_in_out",
    )

    assert entity_entry.entity_id == "sensor.sf800pro_767_bat_in_out"

    with (
        patch(
            "custom_components.zendure_ha.device.async_get_clientsession",
            return_value=__import__("unittest.mock", fromlist=["Mock"]).Mock(),
        ),
        patch("custom_components.zendure_ha.migration.Migration.check_device"),
    ):
        definition = {
            "deviceKey": "device-1",
            "deviceName": "SF 800 Pro WZ Balkon",
            "productKey": "PK",
            "productModel": "SolarFlow 800 Pro",
            "snNumber": sn,
            "ip": "",
        }
        SolarFlow800Pro(hass, "device-1", "SF 800 Pro WZ Balkon", definition)

    updated_entity = entity_registry.async_get("sensor.sf800pro_767_bat_in_out")
    assert updated_entity is not None, "entity was wrongly renamed by check_entities"
    assert updated_entity.entity_id == "sensor.sf800pro_767_bat_in_out"


def test_check_entities_finds_device_by_device_id_when_sn_identifier_stripped(hass):
    """
    check_entities must still run when Migration stripped the SN identifier.

    After Migration.check_device replaces identifiers with {(DOMAIN, device_id)},
    the SN lookup fails.  EntityDevice must fall back to deviceId so that
    wrongly-renamed entities are still corrected.
    """
    from importlib import import_module
    from unittest.mock import patch

    from custom_components.zendure_ha.devices.solarflow800 import SolarFlow800Pro

    from .common import make_config_entry

    entry = make_config_entry()
    entry.add_to_hass(hass)

    device_registry_module = import_module("homeassistant.helpers.device_registry")
    entity_registry_module = import_module("homeassistant.helpers.entity_registry")
    device_registry = device_registry_module.async_get(hass)
    entity_registry = entity_registry_module.async_get(hass)

    sn = "TESTSER767"
    # Device only has deviceId identifier (SN was stripped by Migration)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "device-1")},
        name="SF 800 Pro WZ Balkon",
        model="SolarFlow 800 Pro",
        serial_number=sn,
    )
    # Entity was wrongly renamed to device-name-based ID
    entity_entry = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "sf800pro_767_bat_in_out",
        object_id_base="sf_800_pro_wz_balkon_bat_in_out",
        config_entry=entry,
        device_id=device_entry.id,
        translation_key="bat_in_out",
    )

    assert entity_entry.entity_id == "sensor.sf_800_pro_wz_balkon_bat_in_out"

    with (
        patch(
            "custom_components.zendure_ha.device.async_get_clientsession",
            return_value=__import__("unittest.mock", fromlist=["Mock"]).Mock(),
        ),
        patch("custom_components.zendure_ha.migration.Migration.check_device"),
    ):
        definition = {
            "deviceKey": "device-1",
            "deviceName": "SF 800 Pro WZ Balkon",
            "productKey": "PK",
            "productModel": "SolarFlow 800 Pro",
            "snNumber": sn,
            "ip": "",
        }
        SolarFlow800Pro(hass, "device-1", "SF 800 Pro WZ Balkon", definition)

    # Entity should be renamed back to model+serial prefix
    updated_entity = entity_registry.async_get("sensor.sf800pro_767_bat_in_out")
    assert updated_entity is not None, "check_entities did not find device by deviceId fallback"
    assert updated_entity.entity_id == "sensor.sf800pro_767_bat_in_out"
