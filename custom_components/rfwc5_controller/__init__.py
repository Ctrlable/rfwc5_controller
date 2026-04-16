"""
Eaton RFWC5 Z-Wave Keypad Controller — Home Assistant Custom Integration.

Architecture overview
---------------------
One config entry  = one physical RFWC5 keypad.

For each entry we:
  1. Create one RFWC5LedManager (LED bitmask owner + serialised Z-Wave writer)
  2. Register a state_changed listener on the Basic CC sensor so we detect
     button presses (values 10/20/30/40/50) and releases (value 0)
  3. Register a state_changed listener for any linked action entities so
     that external state changes (e.g. another automation turning a scene on)
     are reflected in the keypad LEDs automatically
  4. Create 5 switch entities (one per button) in the 'switch' platform

Race-condition strategy
-----------------------
ALL writes to the Z-Wave device go through RFWC5LedManager._async_write_now()
which holds an asyncio.Lock.  A coalescing timer collapses rapid consecutive
changes into one write.  A suppression window prevents action side-effects
from scheduling redundant writes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.const import STATE_ON

from .action_executor import async_execute_action
from .const import (
    ACTION_TYPE_COVER_CYCLE,
    ACTION_TYPE_HA_SCENE,
    ACTION_TYPE_NONE,
    CONF_BASIC_SENSOR,
    CONF_BUTTONS,
    CONF_BUTTON_ACTION_ENTITY,
    CONF_BUTTON_ACTION_TYPE,
    CONF_CONTROLLER_NODE_ID,
    CONF_DEVICE_ID,
    CONF_ENTITY_ID,
    CONF_MQTT_GATEWAY,
    CONF_MQTT_HOST,
    CONF_MQTT_PORT,
    CONF_MQTT_PREFIX,
    CONF_NODE_ID,
    DEFAULT_CONTROLLER_NODE_ID,
    DEFAULT_GROUP_LEVELS,
    DEFAULT_MQTT_GATEWAY,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_PREFIX,
    DOMAIN,
    LED_SUPPRESS_WINDOW_S,
    NUM_BUTTONS,
    PLATFORMS,
    SERVICE_REPROVISION,
    SERVICE_SET_BUTTON_LED,
    SERVICE_SYNC_LEDS,
)
from .cover_controller import CoverCycleController
from .led_manager import RFWC5LedManager
from .provisioner import MQTTProvisioner

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current schema version."""
    _LOGGER.info("Migrating RFWC5 entry from version %s", config_entry.version)

    new_data = {**config_entry.data}

    if config_entry.version < 2:
        # v1 → backfill MQTT provisioning fields
        new_data.setdefault(CONF_MQTT_PREFIX, DEFAULT_MQTT_PREFIX)
        new_data.setdefault(CONF_MQTT_GATEWAY, DEFAULT_MQTT_GATEWAY)
        new_data.setdefault(CONF_MQTT_HOST, "localhost")
        new_data.setdefault(CONF_MQTT_PORT, DEFAULT_MQTT_PORT)
        new_data.setdefault(CONF_NODE_ID, None)
        new_data.setdefault(CONF_CONTROLLER_NODE_ID, DEFAULT_CONTROLLER_NODE_ID)
        new_data.setdefault("provisioned", False)

    if config_entry.version < 4:
        # v2/v3 → backfill basic sensor field; remove any stale MQTT location fields
        new_data.setdefault(CONF_BASIC_SENSOR, "")
        new_data.pop("mqtt_location", None)
        new_data.pop("mqtt_device_name", None)

    hass.config_entries.async_update_entry(config_entry, data=new_data, version=4)
    _LOGGER.info("RFWC5 migration to version 4 successful")
    return True


async def _async_reload_on_update(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload the integration when the config entry is updated."""
    _LOGGER.info(
        "RFWC5 %s config updated — reloading integration", entry.entry_id
    )
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RFWC5 Controller from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    def _get_config(key: str, default=None):
        """Read from options first (reconfigure), fall back to original data."""
        return entry.options.get(key) or entry.data.get(key, default)

    device_id: str = _get_config(CONF_DEVICE_ID, "")
    indicator_entity: str = _get_config(CONF_ENTITY_ID, "")
    buttons_cfg: list[dict] = _get_config(CONF_BUTTONS, [])

    # Store entry-level runtime data
    hass.data[DOMAIN][entry.entry_id] = {
        "indicator_entity": indicator_entity,
        "buttons": buttons_cfg,
        "device_id": device_id,
    }

    # Create and initialise the LED manager
    manager = RFWC5LedManager(hass, device_id, entry.entry_id)
    hass.data[DOMAIN][entry.entry_id]["manager"] = manager

    # ---------------------------------------------------------------
    # Provision Z-Wave associations and group levels (first setup only)
    # ---------------------------------------------------------------
    if not entry.data.get("provisioned", False):
        provisioner = MQTTProvisioner(hass, entry)
        report = await provisioner.async_provision()

        if not report["success"]:
            _LOGGER.warning(
                "RFWC5 %s provisioning incomplete: %s",
                device_id, report,
            )
        else:
            _LOGGER.info(
                "RFWC5 %s provisioning complete: %s",
                device_id, report,
            )

        hass.data[DOMAIN][entry.entry_id]["provision_report"] = report
        # Only mark provisioned=True if it actually ran (node_id was set)
        mark_provisioned = report.get("error") != "node_id not configured"
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                "provisioned": mark_provisioned,
                "provision_report": report,
            },
        )
    else:
        _LOGGER.debug("RFWC5 %s already provisioned, skipping", device_id)
        hass.data[DOMAIN][entry.entry_id]["provision_report"] = entry.data.get(
            "provision_report", {}
        )

    # Initialise (refresh + read current indicator value)
    await manager.async_initialize()

    # ---------------------------------------------------------------
    # Track Basic CC sensor → detect button presses / releases
    # ---------------------------------------------------------------
    basic_sensor: str = _get_config(CONF_BASIC_SENSOR, "")

    if basic_sensor:
        # Auto-enable the entity if HA has it disabled
        ent_reg = er.async_get(hass)
        basic_entry = ent_reg.async_get(basic_sensor)
        if basic_entry and basic_entry.disabled:
            _LOGGER.info(
                "RFWC5 enabling disabled basic sensor entity: %s", basic_sensor
            )
            ent_reg.async_update_entity(basic_sensor, disabled_by=None)

        async def _handle_basic_value(value: int) -> None:
            """React to a Basic CC value reported by the keypad."""
            group_levels = _get_config("group_levels", DEFAULT_GROUP_LEVELS)
            btn_cfg = hass.data[DOMAIN][entry.entry_id]["buttons"]

            if value == 0:
                _LOGGER.info("RFWC5 %s button OFF detected — refreshing indicator", entry.entry_id)
                await manager.async_refresh_and_read_indicator()
                prev = manager.get_previous_leds()
                curr = manager._leds[:]
                for btn_idx in range(NUM_BUTTONS):
                    if prev[btn_idx] and not curr[btn_idx]:
                        cfg = btn_cfg[btn_idx]
                        await async_execute_action(
                            hass,
                            cfg.get(CONF_BUTTON_ACTION_TYPE),
                            cfg.get(CONF_BUTTON_ACTION_ENTITY),
                            False,
                            direction_key=f"{entry.entry_id}_{btn_idx}",
                        )
                        _LOGGER.info("RFWC5 button %d OFF → action fired", btn_idx + 1)
                        manager.suppress_external_writes(LED_SUPPRESS_WINDOW_S)
            else:
                matched = False
                for btn_idx, level in enumerate(group_levels):
                    if value == level:
                        cfg = btn_cfg[btn_idx]
                        await manager.async_set_button(btn_idx, True)
                        await async_execute_action(
                            hass,
                            cfg.get(CONF_BUTTON_ACTION_TYPE),
                            cfg.get(CONF_BUTTON_ACTION_ENTITY),
                            True,
                            direction_key=f"{entry.entry_id}_{btn_idx}",
                        )
                        _LOGGER.info(
                            "RFWC5 button %d ON (value=%d) → action fired",
                            btn_idx + 1, value,
                        )
                        manager.suppress_external_writes(LED_SUPPRESS_WINDOW_S)
                        matched = True
                        break
                if not matched:
                    _LOGGER.warning(
                        "RFWC5 Basic value %d matched no button. "
                        "Expected one of %s. Check group levels.",
                        value, group_levels,
                    )

        @callback
        def _on_basic_sensor_change(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            try:
                value = int(float(new_state.state))
            except (ValueError, TypeError):
                return
            hass.async_create_task(_handle_basic_value(value))

        unsub_basic = async_track_state_change_event(
            hass, [basic_sensor], _on_basic_sensor_change
        )
        hass.data[DOMAIN][entry.entry_id]["unsub_basic"] = unsub_basic
        _LOGGER.info(
            "RFWC5 %s tracking basic sensor: %s", entry.entry_id, basic_sensor
        )
    else:
        _LOGGER.warning(
            "RFWC5 %s no basic sensor configured — button presses will not be "
            "detected. Go to Configure to select the Basic sensor entity.",
            entry.entry_id,
        )

    # ---------------------------------------------------------------
    # Watch linked action entities for external state changes
    # so LEDs stay in sync when scenes/automations/covers change outside HA UI
    # ---------------------------------------------------------------
    tracked_entities: list[str] = []
    for cfg in buttons_cfg:
        atype = cfg.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE)
        aentity = cfg.get(CONF_BUTTON_ACTION_ENTITY, "")
        if atype not in (ACTION_TYPE_NONE, ACTION_TYPE_HA_SCENE) and aentity:
            if atype == ACTION_TYPE_COVER_CYCLE:
                # aentity is a CSV of cover entity_ids — track each one
                covers = [e.strip() for e in aentity.split(",") if e.strip()]
                tracked_entities.extend(covers)
            else:
                tracked_entities.append(aentity)

    _LOGGER.debug("RFWC5 %s tracking entities: %s", entry.entry_id, tracked_entities)

    if tracked_entities:
        @callback
        def _on_linked_entity_state_change(event: Event) -> None:
            """Sync LED when a linked entity changes state externally."""
            changed_entity = event.data.get("entity_id")
            for btn_idx, cfg in enumerate(buttons_cfg):
                atype = cfg.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE)
                aentity = cfg.get(CONF_BUTTON_ACTION_ENTITY, "")

                if atype == ACTION_TYPE_COVER_CYCLE:
                    covers = [e.strip() for e in aentity.split(",") if e.strip()]
                    if changed_entity not in covers:
                        continue
                    direction_key = f"{entry.entry_id}_{btn_idx}"
                    controller = CoverCycleController(hass, covers, direction_key)
                    # Advance OPENING→OPEN or CLOSING→CLOSED when HA confirms it
                    controller.sync_from_ha_state()
                    # LED is derived from internal state — no HA lag
                    is_on = controller.get_led_state()
                    _LOGGER.debug(
                        "RFWC5 cover entity changed: entity=%s led=%s button=%d",
                        changed_entity, is_on, btn_idx,
                    )
                elif aentity == changed_entity:
                    new_state = event.data.get("new_state")
                    if new_state is None:
                        continue
                    is_on = new_state.state == STATE_ON
                    _LOGGER.debug(
                        "RFWC5 linked entity changed: entity=%s state=%s button=%d",
                        changed_entity, is_on, btn_idx,
                    )
                else:
                    continue

                # Common: update LED state and schedule write (with suppression check)
                manager._leds[btn_idx] = is_on
                manager._notify_listeners(btn_idx, is_on)
                if time.monotonic() > manager._suppress_until:
                    manager._schedule_write()
                else:
                    _LOGGER.debug(
                        "RFWC5 suppressing write from external state change "
                        "— within suppression window"
                    )

        unsub_state = async_track_state_change_event(
            hass,
            tracked_entities,
            _on_linked_entity_state_change,
        )
        hass.data[DOMAIN][entry.entry_id]["unsub_state"] = unsub_state

    # ---------------------------------------------------------------
    # Register custom services
    # ---------------------------------------------------------------
    _register_services(hass)

    # ---------------------------------------------------------------
    # Forward to switch platform
    # ---------------------------------------------------------------
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload integration automatically whenever the user saves new options
    entry.async_on_unload(
        entry.add_update_listener(_async_reload_on_update)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry cleanly."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})

        # Cancel basic sensor state tracker
        unsub_basic = entry_data.get("unsub_basic")
        if unsub_basic:
            unsub_basic()

        # Cancel linked-entity state watcher
        unsub_state = entry_data.get("unsub_state")
        if unsub_state:
            unsub_state()

        # Cancel any pending coalesce timer
        manager: RFWC5LedManager | None = entry_data.get("manager")
        if manager and manager._debounce_unsub:
            manager._debounce_unsub()

    return unload_ok


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def _register_services(hass: HomeAssistant) -> None:
    """Register integration-level services (idempotent)."""

    if hass.services.has_service(DOMAIN, SERVICE_SYNC_LEDS):
        return  # already registered

    async def _sync_leds(call: Any) -> None:
        """Force sync all LEDs for a specific entry (or all entries)."""
        entry_id = call.data.get("entry_id")
        for eid, data in hass.data.get(DOMAIN, {}).items():
            if entry_id and eid != entry_id:
                continue
            manager: RFWC5LedManager = data.get("manager")
            if manager:
                await manager.async_sync_all()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_LEDS,
        _sync_leds,
    )

    async def _set_button_led(call: Any) -> None:
        """
        Directly set a specific button LED state.
        Useful for automation-driven overrides.
        data:
          entry_id: <config entry id>
          button:   1-5
          state:    true/false
        """
        entry_id = call.data.get("entry_id")
        button = int(call.data.get("button", 1)) - 1  # convert to 0-based
        state = bool(call.data.get("state", False))

        for eid, data in hass.data.get(DOMAIN, {}).items():
            if entry_id and eid != entry_id:
                continue
            manager: RFWC5LedManager = data.get("manager")
            if manager:
                await manager.async_set_button(button, state)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BUTTON_LED,
        _set_button_led,
    )

    async def _reprovision(call: Any) -> None:
        """Re-run Z-Wave MQTT provisioning for one or all keypads."""
        entry_id = call.data.get("entry_id")
        for eid, data in hass.data.get(DOMAIN, {}).items():
            if entry_id and eid != entry_id:
                continue
            cfg_entry = hass.config_entries.async_get_entry(eid)
            if cfg_entry is None:
                continue
            # Mark as not provisioned so the sequence runs again
            hass.config_entries.async_update_entry(
                cfg_entry,
                data={**cfg_entry.data, "provisioned": False},
            )
            prov = MQTTProvisioner(hass, cfg_entry)
            report = await prov.async_provision()
            data["provision_report"] = report
            hass.config_entries.async_update_entry(
                cfg_entry,
                data={**cfg_entry.data, "provisioned": True, "provision_report": report},
            )
            if not report["success"]:
                _LOGGER.warning(
                    "RFWC5 %s reprovision incomplete: %s",
                    data["device_id"], report,
                )
            else:
                _LOGGER.info(
                    "RFWC5 %s reprovision complete: %s",
                    data["device_id"], report,
                )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REPROVISION,
        _reprovision,
    )
