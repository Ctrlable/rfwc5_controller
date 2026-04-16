"""
Switch platform for Eaton RFWC5 Z-Wave Keypad Controller.

Each of the 5 buttons on the keypad is represented as a switch entity.

  switch.<keypad_name>_button_1  … switch.<keypad_name>_button_5

Turning a switch ON/OFF:
  1. Updates the LED manager's internal state for that button
  2. Triggers the debounced Z-Wave write (collapses rapid changes)
  3. Fires the configured action (scene, automation, script, toggle…)

External state changes (e.g. a stateful scene turning on from another
automation) are pushed back into the switch via the LED manager's listener
callback registered in async_added_to_hass.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .action_executor import async_execute_action, get_tracked_state
from .const import (
    ACTION_TYPE_COVER_CYCLE,
    CONF_BUTTONS,
    CONF_BUTTON_ACTION_ENTITY,
    CONF_BUTTON_ACTION_TYPE,
    CONF_BUTTON_LABEL,
    CONF_DEVICE_ID,
    DOMAIN,
    NUM_BUTTONS,
)
from .cover_controller import CoverCycleController
from .led_manager import RFWC5LedManager

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 5 switch entities for the keypad."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    manager: RFWC5LedManager = entry_data["manager"]
    buttons_cfg: list[dict] = entry_data["buttons"]
    device_id: str = entry_data["device_id"]

    entities = [
        RFWC5ButtonSwitch(
            hass=hass,
            entry=entry,
            manager=manager,
            button_index=i,
            config=buttons_cfg[i],
            device_id=device_id,
        )
        for i in range(NUM_BUTTONS)
    ]

    async_add_entities(entities)


class RFWC5ButtonSwitch(SwitchEntity):
    """Represents one physical button on the RFWC5 keypad as a HA switch."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        manager: RFWC5LedManager,
        button_index: int,
        config: dict[str, Any],
        device_id: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._manager = manager
        self._index = button_index
        self._config = config
        self._device_id = device_id

        label: str = config.get(CONF_BUTTON_LABEL, f"Button {button_index + 1}")
        self._attr_name = label
        self._attr_unique_id = f"{entry.entry_id}_button_{button_index + 1}"
        self._attr_icon = "mdi:gesture-tap-button"

    # ------------------------------------------------------------------
    # DeviceInfo — groups all 5 switches under one device card
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        keypad_name = self._entry.data.get("keypad_name", "RFWC5 Keypad")
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=keypad_name,
            manufacturer="Eaton",
            model="RFWC5",
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._manager.get_button_state(self._index)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Register with the LED manager to receive push state updates."""
        self._manager.register_state_listener(self._on_manager_state_change)

        # Seed initial state from the linked action entity if applicable
        action_type = self._config.get(CONF_BUTTON_ACTION_TYPE)
        action_entity = self._config.get(CONF_BUTTON_ACTION_ENTITY, "")
        tracked = get_tracked_state(self.hass, action_type, action_entity)
        if tracked is not None:
            # Directly set manager state without triggering write (already in sync)
            self._manager._leds[self._index] = tracked

        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._manager.unregister_state_listener(self._on_manager_state_change)

    @callback
    def _on_manager_state_change(self, button_index: int, new_state: bool) -> None:
        """Called by LedManager when this button's LED state changes."""
        if button_index == self._index:
            self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Turn on / off
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._apply_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._apply_state(False)

    async def _apply_state(self, state: bool) -> None:
        action_type = self._config.get(CONF_BUTTON_ACTION_TYPE)
        action_entity = self._config.get(CONF_BUTTON_ACTION_ENTITY, "")

        if action_type == ACTION_TYPE_COVER_CYCLE:
            # Cover cycle: fire the cycle command; don't force LED here —
            # the cover state change will update LED via the entity watcher
            cover_entities = [e.strip() for e in action_entity.split(",") if e.strip()]
            if cover_entities:
                direction_key = f"{self._entry.entry_id}_{self._index}"
                controller = CoverCycleController(
                    self.hass, cover_entities, direction_key
                )
                await controller.async_cycle()
            return

        # 1. Update LED manager (schedules coalesced Z-Wave write)
        await self._manager.async_set_button(self._index, state)

        # 2. Push HA state immediately so UI feels responsive
        self.async_write_ha_state()

        # 3. Execute the configured action
        await async_execute_action(
            self.hass, action_type, action_entity, state,
            direction_key=f"{self._entry.entry_id}_{self._index}",
        )
