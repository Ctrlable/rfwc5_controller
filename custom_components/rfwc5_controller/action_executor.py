"""
Action executor for RFWC5 button presses.

Each button can be associated with a:
  - Stateful Scene switch  (switch.* from hugobloem/stateful_scenes)
  - HA Scene               (scene.*)
  - Automation             (automation.*)
  - Script                 (script.*)
  - Generic entity toggle  (any entity with on/off state)
  - None                   (LED-only, no action)

For action types that track on/off state (stateful_scene, entity_toggle),
this module also provides `get_tracked_state()` so the LED can mirror the
entity's real state on startup and on external state changes.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.const import (
    SERVICE_TURN_ON,
    SERVICE_TURN_OFF,
    SERVICE_TOGGLE,
    STATE_ON,
)

from .const import (
    ACTION_TYPE_AUTOMATION,
    ACTION_TYPE_HA_SCENE,
    ACTION_TYPE_NONE,
    ACTION_TYPE_SCRIPT,
    ACTION_TYPE_STATEFUL_SCENE,
    ACTION_TYPE_TOGGLE,
)

_LOGGER = logging.getLogger(__name__)


async def async_execute_action(
    hass: HomeAssistant,
    action_type: str,
    action_entity: str,
    desired_state: bool,
) -> None:
    """
    Execute the button's action based on its configured type and the new LED state.

    Args:
        hass:           HomeAssistant instance
        action_type:    One of the ACTION_TYPE_* constants
        action_entity:  entity_id of the target
        desired_state:  True = button turned ON, False = button turned OFF
    """
    if action_type == ACTION_TYPE_NONE or not action_entity:
        return

    try:
        if action_type == ACTION_TYPE_STATEFUL_SCENE:
            # stateful_scenes creates switch.* entities — toggle them directly
            await _call_switch(hass, action_entity, desired_state)

        elif action_type == ACTION_TYPE_HA_SCENE:
            # Scenes can only be turned on; pressing the button again re-activates
            if desired_state:
                await hass.services.async_call(
                    "scene",
                    "turn_on",
                    {"entity_id": action_entity},
                    blocking=True,
                )
            # When LED is turned off we don't "undo" a scene — that's intentional

        elif action_type == ACTION_TYPE_AUTOMATION:
            if desired_state:
                await hass.services.async_call(
                    "automation",
                    "trigger",
                    {"entity_id": action_entity, "skip_condition": False},
                    blocking=True,
                )
            else:
                # Turn off means disable the automation
                await hass.services.async_call(
                    "automation",
                    SERVICE_TURN_OFF,
                    {"entity_id": action_entity},
                    blocking=True,
                )

        elif action_type == ACTION_TYPE_SCRIPT:
            if desired_state:
                await hass.services.async_call(
                    "script",
                    SERVICE_TURN_ON,
                    {"entity_id": action_entity},
                    blocking=True,
                )
            else:
                await hass.services.async_call(
                    "script",
                    SERVICE_TURN_OFF,
                    {"entity_id": action_entity},
                    blocking=True,
                )

        elif action_type == ACTION_TYPE_TOGGLE:
            await _call_switch(hass, action_entity, desired_state)

    except Exception as err:  # noqa: BLE001
        _LOGGER.error(
            "Failed to execute action (type=%s entity=%s state=%s): %s",
            action_type,
            action_entity,
            desired_state,
            err,
        )


def get_tracked_state(
    hass: HomeAssistant,
    action_type: str,
    action_entity: str,
) -> bool | None:
    """
    Return the current on/off state of the linked entity, or None if not applicable.

    Returns None for HA Scenes (stateless), automations that merely trigger,
    and ACTION_TYPE_NONE.
    """
    if action_type in (ACTION_TYPE_NONE, ACTION_TYPE_HA_SCENE):
        return None

    if not action_entity:
        return None

    state = hass.states.get(action_entity)
    if state is None:
        return None

    return state.state == STATE_ON


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _call_switch(hass: HomeAssistant, entity_id: str, state: bool) -> None:
    """Call turn_on or turn_off on any domain that supports it."""
    domain = entity_id.split(".")[0]
    service = SERVICE_TURN_ON if state else SERVICE_TURN_OFF
    await hass.services.async_call(
        domain,
        service,
        {"entity_id": entity_id},
        blocking=True,
    )
