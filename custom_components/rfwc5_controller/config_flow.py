"""Config flow for the Eaton RFWC5 Z-Wave Keypad Controller."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, device_registry as dr

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
      Step 1 – user:       Pick Z-Wave device + keypad name
      Step 2 – indicator:  Pick indicator entity (scoped to the selected device)
      Step 3 – buttons:    Configure each button (label, action type, entity)
    """

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._current_button = 0

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: Select the Z-Wave device and keypad name."""
        zwave_devices = _get_zwave_devices(self.hass)
        if not zwave_devices:
            return self.async_abort(reason="no_zwave_devices")

        if user_input is not None:
            self._data[CONF_DEVICE_ID] = user_input[CONF_DEVICE_ID]
            self._data["keypad_name"] = user_input["keypad_name"]
            return await self.async_step_indicator()

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): vol.In(zwave_devices),
                vol.Required("keypad_name", default="RFWC5 Keypad"): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            description_placeholders={"device_count": str(len(zwave_devices))},
        )

    async def async_step_indicator(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2: Select the indicator entity for the already-chosen device."""
        device_id = self._data[CONF_DEVICE_ID]
        indicator_entities = _get_indicator_entities(self.hass, device_id)

        if not indicator_entities:
            return self.async_abort(reason="no_indicator_entities")

        if user_input is not None:
            self._data[CONF_ENTITY_ID] = user_input[CONF_ENTITY_ID]
            self._data[CONF_BUTTONS] = [
                _default_button_config(i) for i in range(NUM_BUTTONS)
            ]
            self._current_button = 0
            return await self.async_step_button()

        zwave_devices = _get_zwave_devices(self.hass)
        device_name = zwave_devices.get(device_id, device_id)

        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITY_ID): vol.In(indicator_entities),
            }
        )

        return self.async_show_form(
            step_id="indicator",
            data_schema=schema,
            description_placeholders={"device_name": device_name},
        )

    async def async_step_button(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3 (repeated for each button): Configure label, action type and entity."""
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
# Options Flow (reconfigure device, indicator, and buttons)
# ---------------------------------------------------------------------------

class RFWC5OptionsFlow(config_entries.OptionsFlow):
    """Allow re-configuring the keypad device, indicator, and button assignments."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.data)
        self._current_button = 0

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Entry point — start with device selection."""
        self._current_button = 0
        return await self.async_step_device()

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: Re-select the Z-Wave device."""
        zwave_devices = _get_zwave_devices(self.hass)
        if not zwave_devices:
            return self.async_abort(reason="no_zwave_devices")

        if user_input is not None:
            self._data[CONF_DEVICE_ID] = user_input[CONF_DEVICE_ID]
            return await self.async_step_indicator()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_ID,
                    default=self._data.get(CONF_DEVICE_ID),
                ): vol.In(zwave_devices),
            }
        )

        return self.async_show_form(
            step_id="device",
            data_schema=schema,
        )

    async def async_step_indicator(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2: Re-select the indicator entity for the chosen device."""
        device_id = self._data[CONF_DEVICE_ID]
        indicator_entities = _get_indicator_entities(self.hass, device_id)

        if not indicator_entities:
            return self.async_abort(reason="no_indicator_entities")

        if user_input is not None:
            self._data[CONF_ENTITY_ID] = user_input[CONF_ENTITY_ID]
            self._current_button = 0
            return await self.async_step_button()

        zwave_devices = _get_zwave_devices(self.hass)
        device_name = zwave_devices.get(device_id, device_id)

        # Pre-select current entity if it still exists for this device
        current = self._data.get(CONF_ENTITY_ID, "")
        default_entity = current if current in indicator_entities else next(iter(indicator_entities))

        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITY_ID, default=default_entity): vol.In(indicator_entities),
            }
        )

        return self.async_show_form(
            step_id="indicator",
            data_schema=schema,
            description_placeholders={"device_name": device_name},
        )

    async def async_step_button(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3 (repeated for each button): Configure label, action type and entity."""
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
