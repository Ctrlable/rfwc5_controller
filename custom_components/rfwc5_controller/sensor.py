"""
Sensor platform for Eaton RFWC5 Z-Wave Keypad Controller.

One sensor per button showing a human-readable summary of the button's
current config:

  "<label> → none"
  "<label> → <action_type>: <action_entity>"

The sensor re-renders automatically whenever the config entry is updated
(e.g. after a select or text entity writes a new value).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ACTION_TYPE_LABELS,
    ACTION_TYPE_NONE,
    CONF_BUTTONS,
    CONF_BUTTON_ACTION_ENTITY,
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
    """Set up one action-summary sensor per button plus a provisioning status sensor."""
    entities: list[SensorEntity] = [
        RFWC5ButtonActionSensor(hass=hass, entry=entry, button_index=i)
        for i in range(NUM_BUTTONS)
    ]
    entities.append(RFWC5ProvisioningStatusSensor(hass=hass, entry=entry))
    async_add_entities(entities)


class RFWC5ButtonActionSensor(SensorEntity):
    """Shows the current action config for one RFWC5 button."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:information-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, button_index: int) -> None:
        self.hass = hass
        self._entry = entry
        self._index = button_index
        n = button_index + 1
        self._attr_unique_id = f"{entry.entry_id}_button_{n}_action"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _config(self) -> dict[str, Any]:
        """Always read from live entry data so reloads pick up changes."""
        return self._entry.data[CONF_BUTTONS][self._index]

    # ------------------------------------------------------------------
    # Entity identity / grouping
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        label = self._config.get(CONF_BUTTON_LABEL, f"Button {self._index + 1}")
        return f"{label} Action"

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
        cfg = self._config
        label = cfg.get(CONF_BUTTON_LABEL, f"Button {self._index + 1}")
        atype = cfg.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE)
        aentity = cfg.get(CONF_BUTTON_ACTION_ENTITY, "")
        friendly_type = ACTION_TYPE_LABELS.get(atype, atype)
        if atype == ACTION_TYPE_NONE:
            return f"{label} → None"
        if aentity:
            state = self.hass.states.get(aentity.split(",")[0].strip())
            entity_name = (
                state.attributes.get("friendly_name") or aentity
                if state else aentity
            )
        else:
            entity_name = ""
        return f"{label} → {friendly_type}: {entity_name}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self._config
        return {
            "action_type": cfg.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE),
            "action_entity": cfg.get(CONF_BUTTON_ACTION_ENTITY, ""),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Re-render whenever the config entry is updated externally."""
        self.async_on_remove(
            self._entry.add_update_listener(self._on_entry_updated)
        )

    async def _on_entry_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        self._entry = entry
        self.async_write_ha_state()


class RFWC5ProvisioningStatusSensor(SensorEntity):
    """Shows the Z-Wave provisioning status for one RFWC5 keypad."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:check-network"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_provisioning_status"

    @property
    def name(self) -> str:
        return "Provisioning Status"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get("keypad_name", "RFWC5 Keypad"),
            manufacturer="Eaton",
            model="RFWC5",
        )

    @property
    def native_value(self) -> str:
        report = (
            self.hass.data
            .get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("provision_report")
        )
        if report is None:
            return "unknown"
        return "ok" if report.get("success") else "incomplete"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return (
            self.hass.data
            .get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("provision_report", {})
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._entry.add_update_listener(self._on_entry_updated)
        )

    async def _on_entry_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        self._entry = entry
        self.async_write_ha_state()
