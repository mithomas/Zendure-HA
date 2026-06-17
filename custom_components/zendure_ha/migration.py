"""Migration helpers for Zendure integration."""

import logging
from functools import partial
from pathlib import Path
from typing import cast

from homeassistant.components.persistent_notification import async_create
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state as rs
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .device import ZendureBattery
from .entity import snakecase

_LOGGER = logging.getLogger(__name__)


class Migration:
    """Handles device/entity rename migrations."""

    _changes: list = []
    _update: bool | None = None

    repairs = {
        "i_o_t_state": "iotstate",
        "o_t_a_state": "otastate",
        "l_c_n_state": "lcnstate",
        "local_a_p_i_enable": "local_apienable",
        "is_error": "",
    }

    @staticmethod
    def check_device(hass: HomeAssistant, device_id: str, name: str, model: str, sn: str) -> None:
        """Track cloud-side device renames via name_by_user for the next migration."""
        device_registry = dr.async_get(hass)

        fallback = f"{model.replace(' ', '').replace('SolarFlow', 'Sf')} {sn[-3:] if sn is not None else ''}".strip()
        unique = "".join(name.split())
        identifier = device_id or name
        if not identifier:
            return

        existing = device_registry.async_get_device(identifiers={(DOMAIN, identifier)})
        if existing is None:
            for ident in [name, name.lower(), unique, fallback, fallback.lower()]:
                existing = device_registry.async_get_device(identifiers={(DOMAIN, ident)})
                if existing is not None:
                    break

        if not existing:
            return

        # check for wrong identifier
        if next(iter(existing.identifiers))[1] != device_id:
            _LOGGER.warning("Migrating device '%s' -> name='%s' id='%s'", existing.name, name, device_id)
            device_registry.async_update_device(existing.id, new_identifiers={(DOMAIN, device_id)})

        # Reconcile orphaned recorder states_meta rows on every startup
        entity_registry = er.async_get(hass)
        device_entities = er.async_entries_for_device(entity_registry, existing.id, True)
        old_prefix = snakecase(model.lower()) if model else ""
        short_model = model.replace(" ", "").replace("SolarFlow", "Sf") if model else ""
        sn_suffix = sn[-3:] if sn else ""
        new_prefix_base = f"{short_model.lower()} {sn_suffix}".strip()
        new_prefix = snakecase(new_prefix_base) if new_prefix_base else ""
        if old_prefix and new_prefix and old_prefix != new_prefix:
            changes_recorder: list[tuple[str, str]] = []
            for entity in device_entities:
                if entity.translation_key is None:
                    continue
                uniqueid = snakecase(entity.translation_key)
                if uniqueid.startswith("aggr") and uniqueid.endswith("total"):
                    uniqueid = uniqueid.replace("_total", "")
                old_id = f"{entity.domain}.{snakecase(f'{old_prefix}_{uniqueid}')}"
                new_id = entity.entity_id
                if old_id != new_id:
                    changes_recorder.append((old_id, new_id))
            if changes_recorder:
                async_call_later(hass, 5, partial(Migration._reconcile_recorder, hass, changes_recorder))

        if name != existing.name:
            _LOGGER.warning("Migrating device '%s' -> name='%s' id='%s'", existing.name, name, device_id)
            device_registry.async_update_device(existing.id, name=name, name_by_user=None)

    @staticmethod
    def _update_files(hass: HomeAssistant, changes: list[tuple[str, str]]) -> bool:
        """Replace old entity IDs with new ones in storage and config files."""
        file_modified = False

        def update_file(path: Path) -> None:
            nonlocal file_modified
            try:
                content = path.read_text(encoding="utf-8")
                updated = content
                for old_id, new_id in changes:
                    updated = updated.replace(old_id, new_id)
                if updated != content:
                    path.write_text(updated, encoding="utf-8")
                    file_modified = True
            except Exception as e:
                _LOGGER.error("Error migrating file %s: %s", path, e)

        storage_dir = Path(hass.config.path(".storage"))
        for path in storage_dir.iterdir():
            if any(path.name.startswith(f) for f in ["core.automation", "lovelace", "energy"]):
                update_file(path)

        config_path = Path(hass.config.config_dir)
        for path in config_path.rglob("*"):
            if path.is_dir():
                continue
            if any(part.startswith(".") for part in path.relative_to(config_path).parts):
                continue
            if path.suffix in (".yaml", ".json"):
                update_file(path)

        return file_modified

    @staticmethod
    async def async_migrate(hass: HomeAssistant, entryid: str) -> None:
        """One-time migration run via async_migrate_entry: fix device identifiers and entity IDs."""
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        data = rs.async_get(hass)
        changes: list[tuple[str, str]] = []

        devices = dr.async_entries_for_config_entry(device_registry, entryid)
        for device in devices:
            if not any(ident[0] == DOMAIN for ident in device.identifiers):
                continue

            try:
                if device.serial_number:
                    new_identifiers = set(device.identifiers) | {(DOMAIN, device.serial_number)}
                    if new_identifiers != set(device.identifiers):
                        device_registry.async_update_device(device.id, new_identifiers=new_identifiers)

                # Get the best possible name for the device: prefer name_by_user,
                # then name, then try to infer from entities.
                entities = er.async_entries_for_device(entity_registry, device.id, True)
                if not (name := device.name_by_user or device.name) or "_" in name:
                    continue

                if device.via_device_id:
                    # is a battery device, change name to the new format
                    name, _, _ = ZendureBattery.get_battery_type(cast("str", device.serial_number))
                if name != device.name:
                    _LOGGER.info("Promoting device name '%s' -> '%s'", device.name, name)
                    device_registry.async_update_device(device.id, name=name, name_by_user=None)

                short_model = device.model.replace(" ", "").replace("SolarFlow", "Sf") if device.model else ""
                sn = device.serial_number or ""
                sn_suffix = sn[-3:] if sn else ""
                prefix_base = f"{short_model.lower()} {sn_suffix}".strip()
                entity_prefix = snakecase(prefix_base) if prefix_base else snakecase(name)

                for entity in entities:
                    try:
                        # rename only entities which belong to the zendure_ha domain
                        if entity.platform == DOMAIN:
                            if entity.translation_key is None:
                                entity_registry.async_remove(entity.entity_id)
                                _LOGGER.debug("Removed orphan entity %s", entity.entity_id)
                                continue

                            uniqueid = (
                                snakecase(v)
                                if (v := Migration.repairs.get(entity.translation_key)) is not None
                                else snakecase(entity.translation_key)
                            )
                            if uniqueid == "":
                                entity_registry.async_remove(entity.entity_id)
                                continue

                            if uniqueid.startswith("aggr") and uniqueid.endswith("total"):
                                uniqueid = uniqueid.replace("_total", "")
                            unique_id = snakecase(f"{entity_prefix}_{uniqueid}")
                            entityid = f"{entity.domain}.{unique_id}"

                            if (
                                entity.entity_id != entityid
                                or entity.unique_id != unique_id
                                or entity.translation_key != uniqueid
                            ):
                                if entity.entity_id != entityid:
                                    entity_registry.async_remove(entityid)
                                if (rstate := data.last_states.pop(entity.entity_id, None)) is not None:
                                    data.last_states[entityid] = rstate
                                entity_registry.async_update_entity(
                                    entity.entity_id,
                                    new_unique_id=unique_id,
                                    new_entity_id=entityid,
                                    translation_key=uniqueid,
                                )
                                _LOGGER.debug("Migrated entity %s -> %s", entity.entity_id, entityid)
                                changes.append((entity.entity_id, entityid))
                    except Exception as e:
                        _LOGGER.error("Failed to migrate entity %s: %s", entity.entity_id, e)
            except Exception as e:
                _LOGGER.error("Failed to migrate device %s: %s", device.name, e)

        # update template config entries
        modified = 0
        for entry in hass.config_entries.async_entries():
            new_data = dict(entry.data or {})
            new_options = dict(entry.options or {})
            if len(new_data) == 0 and len(new_options) == 0:
                continue

            def change_id(data: dict, oid: str, nid: str) -> bool:
                changed = False
                for key, value in data.items():
                    if isinstance(value, dict):
                        changed |= change_id(value, oid, nid)
                    elif isinstance(value, list):
                        for i, item in enumerate(value):
                            if isinstance(item, str) and oid in item:
                                value[i] = item.replace(oid, nid)
                                changed = True
                    elif isinstance(value, str) and oid in value:
                        data[key] = value.replace(oid, nid)
                        changed = True
                return changed

            changed = False
            for oid, nid in changes:
                changed |= change_id(new_data, oid, nid)
                changed |= change_id(new_options, oid, nid)

            if changed:
                hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)
                hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
                modified += 1
        _LOGGER.info("Modified %d template entities", modified)

        if changes and await hass.async_add_executor_job(Migration._update_files, hass, changes):
            await rs.RestoreStateData.async_save_persistent_states(hass)
            async_create(
                hass,
                f"Zendure migration updated {len(changes)} entities. Please restart "
                "Home Assistant to ensure all automations and dashboards use the new "
                "entity IDs.",
                title="Zendure Migration",
                notification_id="zendure_migration",
            )
        _LOGGER.info("Zendure async_migrate complete: %d entity changes", len(changes))

    @staticmethod
    async def _reconcile_recorder(
        hass: HomeAssistant,
        changes: list[tuple[str, str]],
        _now: object | None = None,
    ) -> None:
        """Merge orphaned states_meta rows into current entity IDs."""

        def _do_reconcile() -> None:
            try:
                from homeassistant.helpers.recorder import get_instance
                from sqlalchemy import text

                recorder = get_instance(hass)
                if recorder is not None and recorder.engine is not None:
                    with recorder.engine.connect() as conn:
                        for old_id, new_id in changes:
                            old_row = conn.execute(
                                text("SELECT metadata_id FROM states_meta WHERE entity_id = :eid"),
                                {"eid": old_id},
                            ).fetchone()
                            if not old_row:
                                continue

                            new_row = conn.execute(
                                text("SELECT metadata_id FROM states_meta WHERE entity_id = :eid"),
                                {"eid": new_id},
                            ).fetchone()
                            if new_row:
                                conn.execute(
                                    text("UPDATE states_meta SET entity_id = :back WHERE metadata_id = :mid"),
                                    {"back": new_id + "_back", "mid": new_row[0]},
                                )
                            conn.execute(
                                text("UPDATE states_meta SET entity_id = :new WHERE metadata_id = :mid"),
                                {"new": new_id, "mid": old_row[0]},
                            )
                            _LOGGER.info("Reconciled states_meta '%s' -> '%s'", old_id, new_id)

                            old_stat = conn.execute(
                                text("SELECT id FROM statistics_meta WHERE statistic_id = :sid"),
                                {"sid": old_id},
                            ).fetchone()
                            if not old_stat:
                                continue

                            new_stat = conn.execute(
                                text("SELECT id FROM statistics_meta WHERE statistic_id = :sid"),
                                {"sid": new_id},
                            ).fetchone()
                            if new_stat:
                                conn.execute(
                                    text("UPDATE statistics_meta SET statistic_id = :back WHERE id = :mid"),
                                    {"back": new_id + "_back", "mid": new_stat[0]},
                                )
                            conn.execute(
                                text("UPDATE statistics_meta SET statistic_id = :new WHERE id = :mid"),
                                {"new": new_id, "mid": old_stat[0]},
                            )
                            _LOGGER.info("Reconciled statistics_meta '%s' -> '%s'", old_id, new_id)

                        conn.commit()
            except Exception as e:
                _LOGGER.error("Error reconciling recorder metadata: %s", e)

        await hass.async_add_executor_job(_do_reconcile)
