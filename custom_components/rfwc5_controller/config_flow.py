"""Config flow for the Eaton RFWC5 Z-Wave Keypad Controller."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, device_registry as dr

from .const import (
    ACTION_TYPE_AUTOMATION,
    ACTION_TYPE_HA_SCENE,
    ACTION_TYPE_NONE,
    ACTION_TYPE_SCRIPT,
    ACTION_TYPE_STATEFUL_SCENE,
    ACTION_TYPE_TOGGLE,
    ACTION_TYPES,
    CONF_BUTTON_ACTION_ENTITY,
    CONF_BUTTON_ACTION_TYPE,
    CONF_BUTTON_LABEL,
    CONF_BUTTONS,
    CONF_CONTROLLER_NODE_ID,
    CONF_DEVICE_ID,
    CONF_ENTITY_ID,
    CONF_MQTT_GATEWAY,
    CONF_MQTT_HOST,
    CONF_MQTT_PORT,
    CONF_MQTT_PREFIX,
    CONF_NODE_ID,
    DEFAULT_CONTROLLER_NODE_ID,
    DEFAULT_MQTT_GATEWAY,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_PREFIX,
    DOMAIN,
    NUM_BUTTONS,
)

_LOGGER = logging.getLogger(__name__)

# Domains to search for each action type when building entity pickers
_ACTION_TYPE_DOMAINS: dict[str, list[str]] = {
    ACTION_TYPE_STATEFUL_SCENE: ["switch"],
    ACTION_TYPE_HA_SCENE: ["scene"],
    ACTION_TYPE_AUTOMATION: ["automation"],
    ACTION_TYPE_SCRIPT: ["script"],
    ACTION_TYPE_TOGGLE: [
        "switch", "light", "input_boolean", "fan",
        "cover", "climate", "media_player", "lock",
    ],
}

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


def _get_entities_for_action_type(
    hass: HomeAssistant, action_type: str
) -> dict[str, str]:
    """Return {entity_id: friendly_name} for states whose domain matches the action type."""
    domains = _ACTION_TYPE_DOMAINS.get(action_type, [])
    result: dict[str, str] = {}
    for state in hass.states.async_all():
        if state.domain in domains:
            friendly = state.attributes.get("friendly_name", state.entity_id)
            result[state.entity_id] = f"{friendly} ({state.entity_id})"
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
      Step 1 – user:      Pick Z-Wave device + keypad name
      Step 2 – indicator: Pick indicator entity (scoped to selected device)
      Step 3 – mqtt:      Z-Wave JS UI MQTT settings + node ID
      Step 4 – buttons:   Set labels + action types for all 5 buttons at once
      Step 5 – entities:  Entity pickers, shown only for non-"none" buttons,
                          filtered to the correct domain per action type
    """

    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

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
            return await self.async_step_mqtt()

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

    async def async_step_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3: Configure Z-Wave JS UI MQTT settings and RFWC5 node ID."""
        if user_input is not None:
            self._data[CONF_MQTT_PREFIX] = user_input[CONF_MQTT_PREFIX]
            self._data[CONF_MQTT_GATEWAY] = user_input[CONF_MQTT_GATEWAY]
            self._data[CONF_MQTT_HOST] = user_input[CONF_MQTT_HOST]
            self._data[CONF_MQTT_PORT] = user_input[CONF_MQTT_PORT]
            self._data[CONF_NODE_ID] = user_input[CONF_NODE_ID]
            self._data[CONF_CONTROLLER_NODE_ID] = user_input[CONF_CONTROLLER_NODE_ID]
            return await self.async_step_buttons()

        schema = vol.Schema(
            {
                vol.Required(CONF_MQTT_PREFIX, default=DEFAULT_MQTT_PREFIX): str,
                vol.Required(CONF_MQTT_GATEWAY, default=DEFAULT_MQTT_GATEWAY): str,
                vol.Required(CONF_MQTT_HOST, default="localhost"): str,
                vol.Required(CONF_MQTT_PORT, default=DEFAULT_MQTT_PORT): int,
                vol.Required(CONF_NODE_ID): int,
                vol.Required(
                    CONF_CONTROLLER_NODE_ID, default=DEFAULT_CONTROLLER_NODE_ID
                ): int,
            }
        )

        return self.async_show_form(
            step_id="mqtt",
            data_schema=schema,
            description_placeholders={},
        )

    async def async_step_buttons(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3: Configure labels and action types for all 5 buttons at once."""
        if user_input is not None:
            for i in range(NUM_BUTTONS):
                n = i + 1
                self._data[CONF_BUTTONS][i] = {
                    CONF_BUTTON_LABEL: user_input[f"button_{n}_label"],
                    CONF_BUTTON_ACTION_TYPE: user_input[f"button_{n}_action_type"],
                    CONF_BUTTON_ACTION_ENTITY: "",
                }
            return await self.async_step_entities()

        schema_dict: dict = {}
        for i in range(NUM_BUTTONS):
            n = i + 1
            defaults = self._data[CONF_BUTTONS][i]
            schema_dict[vol.Required(f"button_{n}_label", default=defaults[CONF_BUTTON_LABEL])] = str
            schema_dict[vol.Required(f"button_{n}_action_type", default=defaults[CONF_BUTTON_ACTION_TYPE])] = vol.In(ACTION_TYPES)

        return self.async_show_form(
            step_id="buttons",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 4: Entity pickers for buttons that need one, filtered by domain."""
        active = [
            i for i in range(NUM_BUTTONS)
            if self._data[CONF_BUTTONS][i][CONF_BUTTON_ACTION_TYPE] != ACTION_TYPE_NONE
        ]

        title = self._data.get("keypad_name", "RFWC5 Keypad")

        if not active:
            return self.async_create_entry(title=title, data=self._data)

        if user_input is not None:
            for i in active:
                n = i + 1
                self._data[CONF_BUTTONS][i][CONF_BUTTON_ACTION_ENTITY] = (
                    user_input.get(f"button_{n}_entity", "")
                )
            return self.async_create_entry(title=title, data=self._data)

        schema_dict = {}
        for i in active:
            n = i + 1
            action_type = self._data[CONF_BUTTONS][i][CONF_BUTTON_ACTION_TYPE]
            entities = _get_entities_for_action_type(self.hass, action_type)
            current = self._data[CONF_BUTTONS][i].get(CONF_BUTTON_ACTION_ENTITY, "")
            field = vol.Optional(f"button_{n}_entity", default=current)
            schema_dict[field] = vol.In(entities) if entities else str

        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema(schema_dict),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> RFWC5OptionsFlow:
        return RFWC5OptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options Flow (reconfigure device, indicator, buttons, and entities)
# ---------------------------------------------------------------------------

class RFWC5OptionsFlow(config_entries.OptionsFlow):
    """Allow re-configuring the keypad device, indicator, and button assignments."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.data)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Entry point — start with device selection."""
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
            return await self.async_step_mqtt()

        zwave_devices = _get_zwave_devices(self.hass)
        device_name = zwave_devices.get(device_id, device_id)

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

    async def async_step_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3: Re-configure Z-Wave JS UI MQTT settings and RFWC5 node ID."""
        if user_input is not None:
            self._data[CONF_MQTT_PREFIX] = user_input[CONF_MQTT_PREFIX]
            self._data[CONF_MQTT_GATEWAY] = user_input[CONF_MQTT_GATEWAY]
            self._data[CONF_MQTT_HOST] = user_input[CONF_MQTT_HOST]
            self._data[CONF_MQTT_PORT] = user_input[CONF_MQTT_PORT]
            self._data[CONF_NODE_ID] = user_input[CONF_NODE_ID]
            self._data[CONF_CONTROLLER_NODE_ID] = user_input[CONF_CONTROLLER_NODE_ID]
            return await self.async_step_buttons()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MQTT_PREFIX,
                    default=self._data.get(CONF_MQTT_PREFIX, DEFAULT_MQTT_PREFIX),
                ): str,
                vol.Required(
                    CONF_MQTT_GATEWAY,
                    default=self._data.get(CONF_MQTT_GATEWAY, DEFAULT_MQTT_GATEWAY),
                ): str,
                vol.Required(
                    CONF_MQTT_HOST,
                    default=self._data.get(CONF_MQTT_HOST, "localhost"),
                ): str,
                vol.Required(
                    CONF_MQTT_PORT,
                    default=self._data.get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT),
                ): int,
                vol.Required(
                    CONF_NODE_ID,
                    default=self._data.get(CONF_NODE_ID, 0),
                ): int,
                vol.Required(
                    CONF_CONTROLLER_NODE_ID,
                    default=self._data.get(
                        CONF_CONTROLLER_NODE_ID, DEFAULT_CONTROLLER_NODE_ID
                    ),
                ): int,
            }
        )

        return self.async_show_form(
            step_id="mqtt",
            data_schema=schema,
            description_placeholders={},
        )

    async def async_step_buttons(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 4: Configure labels and action types for all 5 buttons at once."""
        if user_input is not None:
            for i in range(NUM_BUTTONS):
                n = i + 1
                self._data[CONF_BUTTONS][i] = {
                    CONF_BUTTON_LABEL: user_input[f"button_{n}_label"],
                    CONF_BUTTON_ACTION_TYPE: user_input[f"button_{n}_action_type"],
                    CONF_BUTTON_ACTION_ENTITY: "",
                }
            return await self.async_step_entities()

        schema_dict: dict = {}
        for i in range(NUM_BUTTONS):
            n = i + 1
            defaults = self._data[CONF_BUTTONS][i]
            schema_dict[vol.Required(f"button_{n}_label", default=defaults[CONF_BUTTON_LABEL])] = str
            schema_dict[vol.Required(f"button_{n}_action_type", default=defaults[CONF_BUTTON_ACTION_TYPE])] = vol.In(ACTION_TYPES)

        return self.async_show_form(
            step_id="buttons",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 4: Entity pickers for buttons that need one, filtered by domain."""
        active = [
            i for i in range(NUM_BUTTONS)
            if self._data[CONF_BUTTONS][i][CONF_BUTTON_ACTION_TYPE] != ACTION_TYPE_NONE
        ]

        if not active:
            return self.async_create_entry(title="", data=self._data)

        if user_input is not None:
            for i in active:
                n = i + 1
                self._data[CONF_BUTTONS][i][CONF_BUTTON_ACTION_ENTITY] = (
                    user_input.get(f"button_{n}_entity", "")
                )
            return self.async_create_entry(title="", data=self._data)

        schema_dict = {}
        for i in active:
            n = i + 1
            action_type = self._data[CONF_BUTTONS][i][CONF_BUTTON_ACTION_TYPE]
            entities = _get_entities_for_action_type(self.hass, action_type)
            current = self._data[CONF_BUTTONS][i].get(CONF_BUTTON_ACTION_ENTITY, "")
            field = vol.Optional(f"button_{n}_entity", default=current)
            schema_dict[field] = vol.In(entities) if entities else str

        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema(schema_dict),
        )
