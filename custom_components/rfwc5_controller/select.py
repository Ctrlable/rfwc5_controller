"""
Select platform for Eaton RFWC5 Z-Wave Keypad Controller.

Two select entities per button:
  1. RFWC5ButtonActionTypeSelect  — action type dropdown (friendly labels)
  2. RFWC5ActionEntitySelect      — domain-filtered entity picker
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ACTION_TYPE_AUTOMATION,
    ACTION_TYPE_COVER_CYCLE,
    ACTION_TYPE_HA_SCENE,
    ACTION_TYPE_LABELS,
    ACTION_TYPE_NONE,
    ACTION_TYPE_SCRIPT,
    ACTION_TYPE_STATEFUL_SCENE,
    ACTION_TYPE_TOGGLE,
    CONF_BUTTONS,
    CONF_BUTTON_ACTION_ENTITY,
    CONF_BUTTON_ACTION_TYPE,
    CONF_BUTTON_LABEL,
    DOMAIN,
    NUM_BUTTONS,
)

_LOGGER = logging.getLogger(__name__)

# Reverse map: friendly label → raw action type key
_LABEL_TO_ACTION_TYPE: dict[str, str] = {v: k for k, v in ACTION_TYPE_LABELS.items()}

# Domains to query per action type; None = entity select unavailable
_ACTION_TYPE_DOMAINS: dict[str, list[str] | None] = {
    ACTION_TYPE_STATEFUL_SCENE: ["switch"],
    ACTION_TYPE_HA_SCENE:       ["scene"],
    ACTION_TYPE_AUTOMATION:     ["automation"],
    ACTION_TYPE_SCRIPT:         ["script"],
    ACTION_TYPE_TOGGLE:         ["light", "switch", "fan", "input_boolean", "media_player", "climate"],
    ACTION_TYPE_COVER_CYCLE:    None,  # multi-entity — configure via reconfigure flow
    ACTION_TYPE_NONE:           None,
}

# Regex: extract entity_id from "Friendly Name (domain.entity_id)"
_ENTITY_ID_RE = re.compile(r"\(([^)]+)\)$")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up action-type and action-entity select entities per button."""
    entities: list[SelectEntity] = []
    for i in range(NUM_BUTTONS):
        entities.append(RFWC5ButtonActionTypeSelect(hass=hass, entry=entry, button_index=i))
        entities.append(RFWC5ActionEntitySelect(hass=hass, entry=entry, button_index=i))
    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get("keypad_name", "RFWC5 Keypad"),
        manufacturer="Eaton",
        model="RFWC5",
    )


class RFWC5ButtonActionTypeSelect(SelectEntity):
    """Dropdown for the action type of one RFWC5 button (shows friendly labels)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:dip-switch"

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
        self._attr_options = list(ACTION_TYPE_LABELS.values())

    @property
    def _config(self) -> dict[str, Any]:
        return self._entry.data[CONF_BUTTONS][self._index]

    @property
    def name(self) -> str:
        label = self._config.get(CONF_BUTTON_LABEL, f"Button {self._index + 1}")
        return f"{label} Action Type"

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def current_option(self) -> str:
        raw = self._config.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE)
        return ACTION_TYPE_LABELS.get(raw, raw)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._entry.add_update_listener(self._on_entry_updated)
        )

    async def _on_entry_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        self._entry = entry
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Reverse-lookup to raw key, clear action entity, persist, reload."""
        raw_key = _LABEL_TO_ACTION_TYPE.get(option, option)
        new_buttons = [dict(b) for b in self._entry.data[CONF_BUTTONS]]
        new_buttons[self._index][CONF_BUTTON_ACTION_TYPE] = raw_key
        new_buttons[self._index][CONF_BUTTON_ACTION_ENTITY] = ""
        new_data = {**self._entry.data, CONF_BUTTONS: new_buttons}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        await self.hass.config_entries.async_reload(self._entry.entry_id)


class RFWC5ActionEntitySelect(SelectEntity):
    """Domain-filtered entity picker for the action entity of one RFWC5 button."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:link-variant"

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

    @property
    def _config(self) -> dict[str, Any]:
        return self._entry.data[CONF_BUTTONS][self._index]

    @property
    def name(self) -> str:
        label = self._config.get(CONF_BUTTON_LABEL, f"Button {self._index + 1}")
        return f"{label} Entity"

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def available(self) -> bool:
        action_type = self._config.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE)
        return _ACTION_TYPE_DOMAINS.get(action_type) is not None

    @property
    def options(self) -> list[str]:
        action_type = self._config.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE)
        domains = _ACTION_TYPE_DOMAINS.get(action_type)
        if not domains:
            return []
        opts: list[str] = []
        for state in sorted(self.hass.states.async_all(), key=lambda s: s.entity_id):
            if any(state.entity_id.startswith(f"{d}.") for d in domains):
                opts.append(self._format_entity_id(state.entity_id))
        # Ensure current value is always present (e.g., stale entity_id)
        current_id = self._config.get(CONF_BUTTON_ACTION_ENTITY, "")
        if current_id:
            current_opt = self._format_entity_id(current_id)
            if current_opt not in opts:
                opts.insert(0, current_opt)
        return opts

    @property
    def current_option(self) -> str | None:
        entity_id = self._config.get(CONF_BUTTON_ACTION_ENTITY, "")
        if not entity_id:
            return None
        return self._format_entity_id(entity_id)

    def _format_entity_id(self, entity_id: str) -> str:
        """Return 'Friendly Name (entity_id)' or bare entity_id if no state."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return entity_id
        friendly = state.attributes.get("friendly_name") or entity_id
        return f"{friendly} ({entity_id})"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._entry.add_update_listener(self._on_entry_updated)
        )

    async def _on_entry_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        self._entry = entry
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Extract entity_id from 'Friendly Name (entity_id)', persist, reload."""
        m = _ENTITY_ID_RE.search(option)
        entity_id = m.group(1) if m else option
        new_buttons = [dict(b) for b in self._entry.data[CONF_BUTTONS]]
        new_buttons[self._index][CONF_BUTTON_ACTION_ENTITY] = entity_id
        new_data = {**self._entry.data, CONF_BUTTONS: new_buttons}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        await self.hass.config_entries.async_reload(self._entry.entry_id)
