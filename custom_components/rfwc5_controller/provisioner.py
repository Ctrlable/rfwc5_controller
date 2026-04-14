"""
Z-Wave provisioning for the Eaton RFWC5 keypad.

Runs automatically when a new config entry is created (and on demand via
the rfwc5_controller.reprovision service).  The sequence ensures:

  Part A — Z-Wave Association CC
    Groups 1–5 (one per button) and group 255 (lifeline) are associated
    to the Z-Wave controller node so button presses are reported back.

  Part B — Configuration CC 112 group levels
    Parameters 1–5 set the Basic CC value sent on each button press to
    10 / 20 / 30 / 40 / 50.  These values are read back to verify the
    write succeeded.

Returns a provisioning report dict:
  {
    "associations": {1: "ok", 2: "added", …, 255: "ok"},
    "levels":       {1: "ok", 2: "set",   …, 5: "failed"},
    "success": True/False,
  }
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_GROUP_LEVELS,
    DOMAIN,
    PROVISION_SETTLE_S,
    PROVISION_WRITE_S,
    ZWAVE_ASSOCIATION_GROUP_IDS,
    ZWAVE_CONFIG_CC,
)

_LOGGER = logging.getLogger(__name__)


class RFWC5Provisioner:
    """Provisions one RFWC5 keypad: associations + group levels."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        entry_id: str,
    ) -> None:
        self.hass = hass
        self.device_id = device_id
        self.entry_id = entry_id

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def async_provision(self) -> dict[str, Any]:
        """Run the full provisioning sequence and return a report."""
        _LOGGER.info("RFWC5 %s: starting provisioning", self.device_id)

        assoc_results = await self._provision_associations()
        level_results = await self._provision_levels()

        success = (
            all(v in ("ok", "added") for v in assoc_results.values())
            and all(v in ("ok", "set") for v in level_results.values())
        )

        report: dict[str, Any] = {
            "associations": assoc_results,
            "levels": level_results,
            "success": success,
        }
        _LOGGER.info("RFWC5 %s: provisioning report: %s", self.device_id, report)
        return report

    # ------------------------------------------------------------------
    # Part A — Group Associations
    # ------------------------------------------------------------------

    async def _provision_associations(self) -> dict[int, str]:
        results: dict[int, str] = {}

        for group_id in ZWAVE_ASSOCIATION_GROUP_IDS:
            try:
                # Read current members
                members = await self._get_association_members(group_id)
                controller_node_id = self._controller_node_id()

                if controller_node_id is not None and controller_node_id in members:
                    _LOGGER.info(
                        "RFWC5 %s Group %d: already configured",
                        self.device_id, group_id,
                    )
                    results[group_id] = "ok"
                else:
                    await self.hass.services.async_call(
                        "zwave_js",
                        "add_association",
                        {
                            "device_id": self.device_id,
                            "group": group_id,
                            "endpoint": 0,
                        },
                        blocking=True,
                    )
                    await asyncio.sleep(PROVISION_WRITE_S)
                    _LOGGER.info(
                        "RFWC5 %s Group %d: added",
                        self.device_id, group_id,
                    )
                    results[group_id] = "added"

            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "RFWC5 %s Group %d: error — %s",
                    self.device_id, group_id, err,
                )
                results[group_id] = "error"

            await asyncio.sleep(PROVISION_SETTLE_S)

        return results

    # ------------------------------------------------------------------
    # Part B — Group Level Configuration (CC 112)
    # ------------------------------------------------------------------

    async def _provision_levels(self) -> dict[int, str]:
        results: dict[int, str] = {}

        for group_index, expected_value in enumerate(DEFAULT_GROUP_LEVELS):
            property_key = group_index + 1  # 1-based

            try:
                # Refresh from device then settle
                await self._refresh_config_level(property_key)
                await asyncio.sleep(PROVISION_SETTLE_S)

                current = self._read_config_level(property_key)

                if current == expected_value:
                    _LOGGER.info(
                        "RFWC5 %s Group %d level: already %d",
                        self.device_id, property_key, expected_value,
                    )
                    results[property_key] = "ok"
                else:
                    # Write expected value
                    await self.hass.services.async_call(
                        "zwave_js",
                        "set_value",
                        {
                            "device_id": self.device_id,
                            "command_class": ZWAVE_CONFIG_CC,
                            "property": "level",
                            "property_key": property_key,
                            "value": expected_value,
                        },
                        blocking=True,
                    )
                    await asyncio.sleep(PROVISION_WRITE_S)

                    # Verify write
                    await self._refresh_config_level(property_key)
                    await asyncio.sleep(PROVISION_SETTLE_S)
                    verified = self._read_config_level(property_key)

                    if verified == expected_value:
                        _LOGGER.info(
                            "RFWC5 %s Group %d level: set to %d",
                            self.device_id, property_key, expected_value,
                        )
                        results[property_key] = "set"
                    else:
                        _LOGGER.warning(
                            "RFWC5 %s Group %d level: FAILED to verify"
                            " (expected %d, got %s)",
                            self.device_id, property_key, expected_value, verified,
                        )
                        results[property_key] = "failed"

            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "RFWC5 %s Group %d level: error — %s",
                    self.device_id, property_key, err,
                )
                results[property_key] = "error"

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_association_members(self, group_id: int) -> list[int]:
        """Return node IDs currently in an association group."""
        try:
            result = await self.hass.services.async_call(
                "zwave_js",
                "get_associations",
                {
                    "device_id": self.device_id,
                    "group": group_id,
                },
                blocking=True,
                return_response=True,
            )
            # Z-Wave JS returns a dict; node IDs are under "members" or similar
            if isinstance(result, dict):
                members = result.get("members", result.get("nodes", []))
                return [
                    m if isinstance(m, int) else m.get("nodeId", m.get("node_id", -1))
                    for m in members
                ]
        except Exception:  # noqa: BLE001
            pass
        return []

    def _controller_node_id(self) -> int | None:
        """Look up the Z-Wave controller node ID from HA's zwave_js domain."""
        try:
            zwave_data = self.hass.data.get("zwave_js", {})
            for entry_data in zwave_data.values():
                client = entry_data.get("client")
                if client and hasattr(client, "driver") and client.driver:
                    return client.driver.controller.own_node_id
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _refresh_config_level(self, property_key: int) -> None:
        """Ask Z-Wave JS to pull the latest config level value from the device."""
        entity_id = self._config_level_entity_id(property_key)
        if not entity_id:
            return
        try:
            await self.hass.services.async_call(
                "zwave_js",
                "refresh_value",
                {"entity_id": entity_id},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "RFWC5 %s: refresh_value for config level %d failed: %s",
                self.device_id, property_key, err,
            )

    def _read_config_level(self, property_key: int) -> int | None:
        """Read the current config level from HA state machine."""
        entity_id = self._config_level_entity_id(property_key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        try:
            return int(float(state.state))
        except (ValueError, TypeError):
            return None

    def _config_level_entity_id(self, property_key: int) -> str:
        """
        Return the entity_id for the Z-Wave config parameter entity.

        Z-Wave JS creates number/sensor entities for Configuration CC parameters.
        The entity_id is looked up from hass.data stored during setup, or
        discovered by scanning HA states.
        """
        data = self.hass.data.get(DOMAIN, {}).get(self.entry_id, {})
        levels_map: dict[int, str] = data.get("config_level_entities", {})
        if property_key in levels_map:
            return levels_map[property_key]

        # Fallback: scan states for a zwave_js entity on this device
        # that matches CC 112 property "level" property_key N
        for state in self.hass.states.async_all():
            attrs = state.attributes
            if (
                attrs.get("device_id") == self.device_id
                and attrs.get("command_class") == ZWAVE_CONFIG_CC
                and attrs.get("property") == "level"
                and attrs.get("property_key") == property_key
            ):
                return state.entity_id

        return ""
