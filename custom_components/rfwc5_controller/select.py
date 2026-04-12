"""
Select platform for Eaton RFWC5 Z-Wave Keypad Controller.

One select entity per button for choosing the action type.
Changing the selection immediately persists to the config entry and
reloads the entry so state-watchers are rebuilt with the new type.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ACTION_TYPE_NONE,
    ACTION_TYPES,
    CONF_BUTTONS,
    CONF_BUTTON_ACTION_TYPE,
    CONF_BUTTON_LABEL,
    DOMAIN,
    NUM_BUTTONS,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one action-type select entity per button."""
    async_add_entities(
        RFWC5ButtonActionTypeSelect(hass=hass, entry=entry, button_index=i)
        for i in range(NUM_BUTTONS)
    )


class RFWC5ButtonActionTypeSelect(SelectEntity):
    """Dropdown for the action type of one RFWC5 button."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:dip-switch"
    _attr_options = ACTION_TYPES

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        button_index: int,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._index = button_index
        n = button_index + 1
        self._attr_unique_id = f"{entry.entry_id}_button_{n}_action_type"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _config(self) -> dict[str, Any]:
        return self._entry.data[CONF_BUTTONS][self._index]

    # ------------------------------------------------------------------
    # Entity identity / grouping
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        label = self._config.get(CONF_BUTTON_LABEL, f"Button {self._index + 1}")
        return f"{label} Action Type"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get("keypad_name", "RFWC5 Keypad"),
            manufacturer="Eaton",
            model="RFWC5",
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def current_option(self) -> str:
        return self._config.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Re-render if config entry is updated by another entity."""
        self.async_on_remove(
            self._entry.add_update_listener(self._on_entry_updated)
        )

    async def _on_entry_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        self._entry = entry
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def async_select_option(self, option: str) -> None:
        """Persist the new action type and reload the entry."""
        new_buttons = [dict(b) for b in self._entry.data[CONF_BUTTONS]]
        new_buttons[self._index][CONF_BUTTON_ACTION_TYPE] = option
        new_data = {**self._entry.data, CONF_BUTTONS: new_buttons}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        await self.hass.config_entries.async_reload(self._entry.entry_id)
