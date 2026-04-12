"""
Eaton RFWC5 Z-Wave Keypad Controller — Home Assistant Custom Integration.

Architecture overview
---------------------
One config entry  = one physical RFWC5 keypad.

For each entry we:
  1. Create one RFWC5LedManager (LED bitmask owner + serialised Z-Wave writer)
  2. Register a zwave_js value_updated event listener that feeds incoming
     indicator changes into the LedManager → updates switch entities
  3. Register a state_changed listener for any linked action entities so
     that external state changes (e.g. another automation turning a scene on)
     are reflected in the keypad LEDs automatically
  4. Create 5 switch entities (one per button) in the 'switch' platform

Race-condition strategy
-----------------------
ALL writes to the Z-Wave device go through RFWC5LedManager._async_write_now()
which holds an asyncio.Lock.  The lock prevents concurrent refresh→read→write
sequences.  On top of that, a 1-second debounce timer collapses rapid
consecutive button presses into one write.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.const import STATE_ON

from .const import (
    ACTION_TYPE_HA_SCENE,
    ACTION_TYPE_NONE,
    CONF_BUTTONS,
    CONF_BUTTON_ACTION_ENTITY,
    CONF_BUTTON_ACTION_TYPE,
    CONF_DEVICE_ID,
    CONF_ENTITY_ID,
    DOMAIN,
    NUM_BUTTONS,
    PLATFORMS,
    SERVICE_SYNC_LEDS,
    SERVICE_SET_BUTTON_LED,
)
from .led_manager import RFWC5LedManager

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RFWC5 Controller from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    device_id: str = entry.data[CONF_DEVICE_ID]
    indicator_entity: str = entry.data[CONF_ENTITY_ID]
    buttons_cfg: list[dict] = entry.data[CONF_BUTTONS]

    # Store entry-level runtime data
    hass.data[DOMAIN][entry.entry_id] = {
        "indicator_entity": indicator_entity,
        "buttons": buttons_cfg,
        "device_id": device_id,
    }

    # Create and initialise the LED manager
    manager = RFWC5LedManager(hass, device_id, entry.entry_id)
    hass.data[DOMAIN][entry.entry_id]["manager"] = manager

    # Initialise (refresh + read current indicator value)
    await manager.async_initialize()

    # ---------------------------------------------------------------
    # Listen for Z-Wave value_updated events → feed into LedManager
    # ---------------------------------------------------------------
    @callback
    def _on_zwave_value_updated(event: Event) -> None:
        """Handle zwave_js value_updated events for the indicator CC."""
        if event.data.get("device_id") != device_id:
            return
        if event.data.get("command_class_name") != "Indicator":
            return
        raw = event.data.get("value")
        if raw is not None:
            manager.async_ingest_indicator(int(raw))

    unsub_zwave = hass.bus.async_listen(
        "zwave_js_value_updated", _on_zwave_value_updated
    )
    hass.data[DOMAIN][entry.entry_id]["unsub_zwave"] = unsub_zwave

    # ---------------------------------------------------------------
    # Watch linked action entities for external state changes
    # so LEDs stay in sync when scenes/automations change outside HA UI
    # ---------------------------------------------------------------
    tracked_entities: list[str] = []
    for cfg in buttons_cfg:
        atype = cfg.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE)
        aentity = cfg.get(CONF_BUTTON_ACTION_ENTITY, "")
        if atype not in (ACTION_TYPE_NONE, ACTION_TYPE_HA_SCENE) and aentity:
            tracked_entities.append(aentity)

    _LOGGER.warning("RFWC5 %s tracking entities: %s", entry.entry_id, tracked_entities)

    if tracked_entities:
        @callback
        def _on_linked_entity_state_change(event: Event) -> None:
            """Sync LED when a linked entity changes state externally."""
            changed_entity = event.data.get("entity_id")
            for btn_idx, cfg in enumerate(buttons_cfg):
                if cfg.get(CONF_BUTTON_ACTION_ENTITY) == changed_entity:
                    new_state = event.data.get("new_state")
                    if new_state is not None:
                        is_on = new_state.state == STATE_ON
                        _LOGGER.warning(
                            "RFWC5 linked entity changed: entity=%s state=%s button=%d",
                            changed_entity, is_on, btn_idx,
                        )
                        # Update manager state without triggering another write immediately
                        # (use internal attribute directly to avoid extra debounce)
                        manager._leds[btn_idx] = is_on
                        manager._notify_listeners(btn_idx, is_on)
                        manager._schedule_write()

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

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry cleanly."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})

        # Cancel Z-Wave event listener
        unsub = entry_data.get("unsub_zwave")
        if unsub:
            unsub()

        # Cancel state watcher
        unsub_state = entry_data.get("unsub_state")
        if unsub_state:
            unsub_state()

        # Cancel any pending debounce
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
