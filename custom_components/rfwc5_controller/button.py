"""
Button platform for Eaton RFWC5 Z-Wave Keypad Controller.

One button per keypad device: "Run Provisioning".
Pressing it runs the MQTT provisioner immediately, regardless of the
stored provisioned flag, and updates the provisioning status sensor.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CONTROLLER_NODE_ID,
    CONF_MQTT_GATEWAY,
    CONF_MQTT_PREFIX,
    CONF_NODE_ID,
    DEFAULT_CONTROLLER_NODE_ID,
    DEFAULT_MQTT_GATEWAY,
    DEFAULT_MQTT_PREFIX,
    DOMAIN,
)
from .provisioner import MQTTProvisioner

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one reprovision button per keypad."""
    async_add_entities([RFWC5ReprovisionButton(hass=hass, entry=entry)])


class RFWC5ReprovisionButton(ButtonEntity):
    """Button that triggers Z-Wave provisioning for one RFWC5 keypad."""

    _attr_has_entity_name = True
    _attr_name = "Run Provisioning"
    _attr_icon = "mdi:cog-sync"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reprovision_button"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get("keypad_name", "RFWC5 Keypad"),
            manufacturer="Eaton",
            model="RFWC5",
        )

    async def async_press(self) -> None:
        """Run provisioning when the button is pressed."""
        provisioner = MQTTProvisioner(self.hass, self._entry)
        report = await provisioner.async_provision()

        # Persist the result and update provisioned flag
        mark_provisioned = report.get("error") != "node_id not configured"
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                "provisioned": mark_provisioned,
                "provision_report": report,
            },
        )

        # Push the new report into runtime data so the status sensor updates
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if entry_data is not None:
            entry_data["provision_report"] = report

        _LOGGER.info("RFWC5 reprovision button result: %s", report)
