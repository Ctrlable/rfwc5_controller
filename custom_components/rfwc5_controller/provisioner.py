"""
MQTT-based Z-Wave provisioning for the Eaton RFWC5 keypad.

Uses the Z-Wave JS UI MQTT API to:

  Part A — Set group associations (groups 1-5 per button + group 255 lifeline)
    Calls addAssociations for each group.  The operation is idempotent —
    adding an existing association is silently ignored by the device.

  Part B — Set Configuration CC 112 group level parameters 1-5
    Calls writeValue for each parameter via MQTT.

MQTT commands are fire-and-forget in this architecture; the report uses
"sent" instead of "ok" because we cannot easily verify the response.

Returns a provisioning report dict:
  {
    "associations": {1: "sent", 2: "sent", ...},
    "levels":       {1: "sent", 2: "sent", ...},
    "success": True/False,
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONTROLLER_NODE_ID,
    CONF_MQTT_GATEWAY,
    CONF_MQTT_PREFIX,
    CONF_NODE_ID,
    DEFAULT_CONTROLLER_NODE_ID,
    DEFAULT_MQTT_GATEWAY,
    DEFAULT_MQTT_PREFIX,
    DEFAULT_GROUP_LEVELS,
    PROVISION_SETTLE_S,
    PROVISION_WRITE_S,
    ZWAVE_ASSOCIATION_GROUP_IDS,
    ZWAVE_CONFIG_CC,
)

_LOGGER = logging.getLogger(__name__)


class MQTTProvisioner:
    """Provisions one RFWC5 keypad via the Z-Wave JS UI MQTT API."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry

        data = entry.data
        self._node_id: int | None = data.get(CONF_NODE_ID)
        self._controller_node_id: int = data.get(
            CONF_CONTROLLER_NODE_ID, DEFAULT_CONTROLLER_NODE_ID
        )
        self._prefix: str = data.get(CONF_MQTT_PREFIX, DEFAULT_MQTT_PREFIX)
        self._gateway: str = data.get(CONF_MQTT_GATEWAY, DEFAULT_MQTT_GATEWAY)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def async_provision(self) -> dict[str, Any]:
        """Run the full MQTT provisioning sequence and return a report."""
        if self._node_id is None:
            _LOGGER.warning(
                "RFWC5 provisioner: node_id not configured — skipping provisioning"
            )
            return {"associations": {}, "levels": {}, "success": False,
                    "error": "node_id not configured"}

        _LOGGER.info(
            "RFWC5 MQTT provisioner: node_id=%d prefix=%s gateway=%s",
            self._node_id, self._prefix, self._gateway,
        )

        assoc_results = await self._provision_associations()
        level_results = await self._provision_levels()

        success = (
            all(v == "sent" for v in assoc_results.values())
            and all(v == "sent" for v in level_results.values())
        )

        report: dict[str, Any] = {
            "associations": assoc_results,
            "levels": level_results,
            "success": success,
        }
        _LOGGER.info("RFWC5 MQTT provisioner: report=%s", report)
        return report

    # ------------------------------------------------------------------
    # Part A — Group Associations
    # ------------------------------------------------------------------

    async def _provision_associations(self) -> dict[int, str]:
        results: dict[int, str] = {}

        for group_id in ZWAVE_ASSOCIATION_GROUP_IDS:
            try:
                # 1. Query current associations (fire-and-forget — just for logging)
                get_topic = self._topic("getAssociations")
                get_payload = {
                    "args": [
                        {"nodeId": self._node_id, "endpoint": 0},
                        group_id,
                    ]
                }
                await self._mqtt_publish(get_topic, get_payload)
                await asyncio.sleep(PROVISION_SETTLE_S)

                # 2. Add association (idempotent)
                add_topic = self._topic("addAssociations")
                add_payload = {
                    "args": [
                        {"nodeId": self._node_id, "endpoint": 0},
                        group_id,
                        [{"nodeId": self._controller_node_id}],
                    ]
                }
                await self._mqtt_publish(add_topic, add_payload)
                await asyncio.sleep(PROVISION_WRITE_S)

                _LOGGER.info(
                    "RFWC5 MQTT provisioner: Group %d association sent", group_id
                )
                results[group_id] = "sent"

            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "RFWC5 MQTT provisioner: Group %d association error — %s",
                    group_id, err,
                )
                results[group_id] = "error"

        return results

    # ------------------------------------------------------------------
    # Part B — Group Level Configuration (CC 112)
    # ------------------------------------------------------------------

    async def _provision_levels(self) -> dict[int, str]:
        results: dict[int, str] = {}
        topic = self._topic("writeValue")

        for group_index, expected_value in enumerate(DEFAULT_GROUP_LEVELS):
            param_number = group_index + 1  # 1-based

            try:
                payload = {
                    "args": [
                        {
                            "nodeId": self._node_id,
                            "commandClassName": "Configuration",
                            "commandClass": ZWAVE_CONFIG_CC,
                            "endpoint": 0,
                            "property": param_number,
                            "propertyName": param_number,
                            "value": expected_value,
                        },
                        expected_value,
                    ]
                }
                await self._mqtt_publish(topic, payload)
                await asyncio.sleep(PROVISION_WRITE_S)

                _LOGGER.info(
                    "RFWC5 MQTT provisioner: Config param %d set to %d sent",
                    param_number, expected_value,
                )
                results[param_number] = "sent"

            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "RFWC5 MQTT provisioner: Config param %d error — %s",
                    param_number, err,
                )
                results[param_number] = "error"

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _topic(self, method: str) -> str:
        """Build the MQTT publish topic for a Z-Wave JS UI API method."""
        return (
            f"{self._prefix}/_CLIENTS/ZWAVE_GATEWAY-{self._gateway}"
            f"/api/{method}/set"
        )

    async def _mqtt_publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish a JSON payload to an MQTT topic via HA's mqtt service."""
        await self.hass.services.async_call(
            "mqtt",
            "publish",
            {
                "topic": topic,
                "payload": json.dumps(payload),
                "qos": 1,
            },
            blocking=True,
        )
