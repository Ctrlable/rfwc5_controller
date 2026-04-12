"""
Text platform for Eaton RFWC5 Z-Wave Keypad Controller.

One text entity per button for editing the linked action entity_id.
The value is validated against the HA state machine before being
persisted; unknown entity IDs are rejected with a warning.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_BUTTONS,
    CONF_BUTTON_ACTION_ENTITY,
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
    """Set up one linked-entity text field per button."""
    async_add_entities(
        RFWC5ButtonEntityText(hass=hass, entry=entry, button_index=i)
        for i in range(NUM_BUTTONS)
    )


class RFWC5ButtonEntityText(TextEntity):
    """Free-text field for the action entity_id of one RFWC5 button."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:link-variant"
    _attr_mode = TextMode.TEXT
    _attr_native_max = 255
    _attr_pattern = None

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
        self._attr_unique_id = f"{entry.entry_id}_button_{n}_entity"

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
        return f"{label} Entity"

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
    def native_value(self) -> str:
        return self._config.get(CONF_BUTTON_ACTION_ENTITY, "")

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

    async def async_set_value(self, value: str) -> None:
        """Validate entity_id, persist, and reload the config entry."""
        if value and self.hass.states.get(value) is None:
            _LOGGER.warning(
                "RFWC5 button %d: entity_id '%s' not found in HA state machine"
                " — update ignored",
                self._index + 1,
                value,
            )
            return
        new_buttons = [dict(b) for b in self._entry.data[CONF_BUTTONS]]
        new_buttons[self._index][CONF_BUTTON_ACTION_ENTITY] = value
        new_data = {**self._entry.data, CONF_BUTTONS: new_buttons}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        await self.hass.config_entries.async_reload(self._entry.entry_id)
