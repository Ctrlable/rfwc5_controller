"""Config flow for the Eaton RFWC5 Z-Wave Keypad Controller."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, device_registry as dr
import homeassistant.helpers.config_validation as cv

from .const import (
    ACTION_TYPE_NONE,
    ACTION_TYPES,
    CONF_BUTTON_ACTION_ENTITY,
    CONF_BUTTON_ACTION_TYPE,
    CONF_BUTTON_LABEL,
    CONF_BUTTONS,
    CONF_DEVICE_ID,
    CONF_ENTITY_ID,
    DOMAIN,
    NUM_BUTTONS,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_zwave_devices(hass: HomeAssistant) -> dict[str, str]:
    """Return {device_id: friendly_name} for all Z-Wave JS devices."""
    dev_reg = dr.async_get(hass)
    result: dict[str, str] = {}
    for device in dev_reg.devices.values():
        for identifier in device.identifiers:
            if identifier[0] == "zwave_js":
                result[device.id] = device.name_by_user or device.name or device.id
                break
    return result


def _get_indicator_entities(hass: HomeAssistant, device_id: str) -> dict[str, str]:
    """Return {entity_id: friendly_name} for sensor/number entities on a Z-Wave device."""
    ent_reg = er.async_get(hass)
    result: dict[str, str] = {}
    for entry in ent_reg.entities.values():
        if entry.device_id == device_id and entry.domain in ("sensor", "number"):
            friendly = entry.name or entry.original_name or entry.entity_id
            result[entry.entity_id] = f"{friendly} ({entry.entity_id})"
    return result


def _default_button_config(index: int) -> dict[str, Any]:
    return {
        CONF_BUTTON_LABEL: f"Button {index + 1}",
        CONF_BUTTON_ACTION_TYPE: ACTION_TYPE_NONE,
        CONF_BUTTON_ACTION_ENTITY: "",
    }


# ---------------------------------------------------------------------------
# Config Flow
# ---------------------------------------------------------------------------

class RFWC5ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Multi-step config flow:
      Step 1 – user:       Pick Z-Wave device + indicator entity + keypad name
      Step 2 – buttons:    Configure each button (label, action type, entity)
    """

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._current_button = 0

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: Select the Z-Wave device and indicator entity."""
        errors: dict[str, str] = {}

        zwave_devices = _get_zwave_devices(self.hass)
        if not zwave_devices:
            return self.async_abort(reason="no_zwave_devices")

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            # Validate indicator entity belongs to selected device
            indicator_entities = _get_indicator_entities(self.hass, device_id)
            if user_input[CONF_ENTITY_ID] not in indicator_entities:
                errors[CONF_ENTITY_ID] = "invalid_entity"
            else:
                self._data.update(user_input)
                self._data[CONF_BUTTONS] = [
                    _default_button_config(i) for i in range(NUM_BUTTONS)
                ]
                self._current_button = 0
                return await self.async_step_button()

        # Build indicator entity choices based on first device if not yet chosen
        first_device = next(iter(zwave_devices))
        indicator_entities = _get_indicator_entities(self.hass, first_device)

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): vol.In(zwave_devices),
                vol.Required(CONF_ENTITY_ID): vol.In(indicator_entities) if indicator_entities else str,
                vol.Required("keypad_name", default="RFWC5 Keypad"): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"device_count": str(len(zwave_devices))},
        )

    async def async_step_button(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2 (repeated for each button): Configure label, action type and entity."""
        errors: dict[str, str] = {}
        idx = self._current_button

        if user_input is not None:
            self._data[CONF_BUTTONS][idx] = {
                CONF_BUTTON_LABEL: user_input[CONF_BUTTON_LABEL],
                CONF_BUTTON_ACTION_TYPE: user_input[CONF_BUTTON_ACTION_TYPE],
                CONF_BUTTON_ACTION_ENTITY: user_input.get(CONF_BUTTON_ACTION_ENTITY, ""),
            }
            self._current_button += 1
            if self._current_button < NUM_BUTTONS:
                return await self.async_step_button()

            # All buttons configured — create entry
            title = self._data.get("keypad_name", "RFWC5 Keypad")
            return self.async_create_entry(title=title, data=self._data)

        defaults = self._data[CONF_BUTTONS][idx]

        schema = vol.Schema(
            {
                vol.Required(CONF_BUTTON_LABEL, default=defaults[CONF_BUTTON_LABEL]): str,
                vol.Required(
                    CONF_BUTTON_ACTION_TYPE,
                    default=defaults[CONF_BUTTON_ACTION_TYPE],
                ): vol.In(ACTION_TYPES),
                vol.Optional(
                    CONF_BUTTON_ACTION_ENTITY,
                    default=defaults[CONF_BUTTON_ACTION_ENTITY],
                ): str,
            }
        )

        return self.async_show_form(
            step_id="button",
            data_schema=schema,
            errors=errors,
            description_placeholders={"button_number": str(idx + 1)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> RFWC5OptionsFlow:
        return RFWC5OptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options Flow (reconfigure any button without removing the integration)
# ---------------------------------------------------------------------------

class RFWC5OptionsFlow(config_entries.OptionsFlow):
    """Allow re-configuring button assignments after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.data)
        self._current_button = 0

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Entry point — jump straight to button 1."""
        self._current_button = 0
        return await self.async_step_button()

    async def async_step_button(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        idx = self._current_button
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data[CONF_BUTTONS][idx] = {
                CONF_BUTTON_LABEL: user_input[CONF_BUTTON_LABEL],
                CONF_BUTTON_ACTION_TYPE: user_input[CONF_BUTTON_ACTION_TYPE],
                CONF_BUTTON_ACTION_ENTITY: user_input.get(CONF_BUTTON_ACTION_ENTITY, ""),
            }
            self._current_button += 1
            if self._current_button < NUM_BUTTONS:
                return await self.async_step_button()

            return self.async_create_entry(title="", data=self._data)

        defaults = self._data[CONF_BUTTONS][idx]
        schema = vol.Schema(
            {
                vol.Required(CONF_BUTTON_LABEL, default=defaults[CONF_BUTTON_LABEL]): str,
                vol.Required(
                    CONF_BUTTON_ACTION_TYPE,
                    default=defaults[CONF_BUTTON_ACTION_TYPE],
                ): vol.In(ACTION_TYPES),
                vol.Optional(
                    CONF_BUTTON_ACTION_ENTITY,
                    default=defaults.get(CONF_BUTTON_ACTION_ENTITY, ""),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="button",
            data_schema=schema,
            errors=errors,
            description_placeholders={"button_number": str(idx + 1)},
        )
