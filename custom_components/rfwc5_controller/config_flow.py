"""Config flow for the Eaton RFWC5 Z-Wave Keypad Controller."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    ACTION_TYPE_AUTOMATION,
    ACTION_TYPE_COVER_CYCLE,
    ACTION_TYPE_HA_SCENE,
    ACTION_TYPE_LABELS,
    ACTION_TYPE_NONE,
    ACTION_TYPE_SCRIPT,
    ACTION_TYPE_STATEFUL_SCENE,
    ACTION_TYPE_TOGGLE,
    CONF_BASIC_SENSOR,
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


# ---------------------------------------------------------------------------
# Selector helpers
# ---------------------------------------------------------------------------

def _action_type_selector() -> SelectSelector:
    """Return a SelectSelector with friendly labels for all action types."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                {"value": k, "label": v}
                for k, v in ACTION_TYPE_LABELS.items()
            ],
            mode=SelectSelectorMode.LIST,
        )
    )


def _entity_selector_for_action_type(action_type: str) -> EntitySelector | None:
    """
    Return an EntitySelector filtered to the relevant domain(s) for the
    given action type, or None if no entity is required.
    """
    if action_type == ACTION_TYPE_STATEFUL_SCENE:
        return EntitySelector(EntitySelectorConfig(domain="switch"))

    if action_type == ACTION_TYPE_HA_SCENE:
        return EntitySelector(EntitySelectorConfig(domain="scene"))

    if action_type == ACTION_TYPE_AUTOMATION:
        return EntitySelector(EntitySelectorConfig(domain="automation"))

    if action_type == ACTION_TYPE_SCRIPT:
        return EntitySelector(EntitySelectorConfig(domain="script"))

    if action_type == ACTION_TYPE_TOGGLE:
        return EntitySelector(
            EntitySelectorConfig(
                domain=[
                    "switch", "light", "input_boolean",
                    "fan", "cover", "climate", "media_player", "lock",
                ]
            )
        )

    if action_type == ACTION_TYPE_COVER_CYCLE:
        # multiple=True: HA returns a list; we join to CSV for storage
        return EntitySelector(
            EntitySelectorConfig(domain="cover", multiple=True)
        )

    return None  # ACTION_TYPE_NONE or unknown


# ---------------------------------------------------------------------------
# General helpers
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


def _default_button_config(index: int) -> dict[str, Any]:
    return {
        CONF_BUTTON_LABEL: f"Button {index + 1}",
        CONF_BUTTON_ACTION_TYPE: ACTION_TYPE_NONE,
        CONF_BUTTON_ACTION_ENTITY: "",
    }


def _covers_from_entity_value(raw: Any) -> str:
    """
    Normalise the cover_cycle entity field to a comma-joined string.
    EntitySelector(multiple=True) returns a list; existing stored value
    is already a CSV string.
    """
    if isinstance(raw, list):
        return ",".join(e.strip() for e in raw if e.strip())
    return raw or ""


def _covers_to_list(csv: str) -> list[str]:
    """Split a stored CSV cover entity string back into a list for multi-select default."""
    return [e.strip() for e in csv.split(",") if e.strip()]


# ---------------------------------------------------------------------------
# Config Flow
# ---------------------------------------------------------------------------

class RFWC5ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Multi-step config flow:
      Step 1 – user:         Pick Z-Wave device + keypad name
      Step 2 – indicator:    Pick indicator entity (sensor/number)
      Step 3 – basic_sensor: Pick Basic CC sensor entity
      Step 4 – mqtt:         Z-Wave JS UI MQTT settings + node ID
      Step 5 – buttons:      Labels + action types for all 5 buttons
      Step 6 – entities:     Entity pickers filtered by action type
    """

    VERSION = 4

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: Select the Z-Wave device and keypad name."""
        # Seed from existing entry when the user initiates a reconfigure
        if not self._data and self.context.get("source") == "reconfigure":
            try:
                existing = self._get_reconfigure_entry()
                if existing:
                    self._data = dict(existing.data)
            except AttributeError:
                pass  # older HA version without _get_reconfigure_entry

        zwave_devices = _get_zwave_devices(self.hass)
        if not zwave_devices:
            return self.async_abort(reason="no_zwave_devices")

        if user_input is not None:
            self._data[CONF_DEVICE_ID] = user_input[CONF_DEVICE_ID]
            self._data["keypad_name"] = user_input["keypad_name"]
            return await self.async_step_indicator()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_ID,
                    default=self._data.get(CONF_DEVICE_ID),
                ): vol.In(zwave_devices),
                vol.Required(
                    "keypad_name",
                    default=self._data.get("keypad_name", "RFWC5 Keypad"),
                ): str,
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
        """Step 2: Select the indicator sensor/number entity."""
        if user_input is not None:
            self._data[CONF_ENTITY_ID] = user_input[CONF_ENTITY_ID]
            if CONF_BUTTONS not in self._data:
                self._data[CONF_BUTTONS] = [
                    _default_button_config(i) for i in range(NUM_BUTTONS)
                ]
            return await self.async_step_basic_sensor()

        device_id = self._data.get(CONF_DEVICE_ID, "")
        zwave_devices = _get_zwave_devices(self.hass)
        device_name = zwave_devices.get(device_id, device_id)
        current = self._data.get(CONF_ENTITY_ID, "")

        schema = vol.Schema(
            {
                vol.Optional(CONF_ENTITY_ID, default=current): EntitySelector(
                    EntitySelectorConfig(domain=["sensor", "number"])
                ),
            }
        )

        return self.async_show_form(
            step_id="indicator",
            data_schema=schema,
            description_placeholders={"device_name": device_name},
        )

    async def async_step_basic_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3: Select the Basic CC sensor entity for button press detection."""
        if user_input is not None:
            self._data[CONF_BASIC_SENSOR] = user_input.get(CONF_BASIC_SENSOR, "")
            return await self.async_step_mqtt()

        current = self._data.get(CONF_BASIC_SENSOR, "")

        schema = vol.Schema(
            {
                vol.Optional(CONF_BASIC_SENSOR, default=current): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
            }
        )

        return self.async_show_form(
            step_id="basic_sensor",
            data_schema=schema,
            description_placeholders={},
        )

    async def async_step_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 4: Configure Z-Wave JS UI MQTT settings and RFWC5 node ID."""
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
                    default=self._data.get(CONF_NODE_ID),
                ): int,
                vol.Required(
                    CONF_CONTROLLER_NODE_ID,
                    default=self._data.get(CONF_CONTROLLER_NODE_ID, DEFAULT_CONTROLLER_NODE_ID),
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
        """Step 5: Configure labels and action types for all 5 buttons."""
        if user_input is not None:
            for i in range(NUM_BUTTONS):
                n = i + 1
                self._data[CONF_BUTTONS][i] = {
                    CONF_BUTTON_LABEL: user_input[f"button_{n}_label"],
                    CONF_BUTTON_ACTION_TYPE: user_input[f"button_{n}_action_type"],
                    CONF_BUTTON_ACTION_ENTITY: self._data[CONF_BUTTONS][i].get(
                        CONF_BUTTON_ACTION_ENTITY, ""
                    ),
                }
            return await self.async_step_entities()

        schema_dict: dict = {}
        for i in range(NUM_BUTTONS):
            n = i + 1
            defaults = self._data[CONF_BUTTONS][i]
            schema_dict[vol.Required(
                f"button_{n}_label",
                default=defaults.get(CONF_BUTTON_LABEL, f"Button {n}"),
            )] = str
            schema_dict[vol.Required(
                f"button_{n}_action_type",
                default=defaults.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE),
            )] = _action_type_selector()

        return self.async_show_form(
            step_id="buttons",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 6: Entity pickers for buttons that have an action, filtered by domain."""
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
                action_type = self._data[CONF_BUTTONS][i][CONF_BUTTON_ACTION_TYPE]
                raw = user_input.get(f"button_{n}_entity", "")
                self._data[CONF_BUTTONS][i][CONF_BUTTON_ACTION_ENTITY] = (
                    _covers_from_entity_value(raw)
                    if action_type == ACTION_TYPE_COVER_CYCLE
                    else raw
                )
            return self.async_create_entry(title=title, data=self._data)

        schema_dict: dict = {}
        for i in active:
            n = i + 1
            action_type = self._data[CONF_BUTTONS][i][CONF_BUTTON_ACTION_TYPE]
            current = self._data[CONF_BUTTONS][i].get(CONF_BUTTON_ACTION_ENTITY, "")
            selector = _entity_selector_for_action_type(action_type)
            if selector is None:
                continue

            if action_type == ACTION_TYPE_COVER_CYCLE:
                default = _covers_to_list(current)
            else:
                default = current

            schema_dict[vol.Optional(f"button_{n}_entity", default=default)] = selector

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
    """Allow re-configuring all keypad settings."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        # Merge options over data so reconfigure pre-populates all fields
        self._data: dict[str, Any] = {**config_entry.data, **config_entry.options}

    def _current(self, key: str, default: Any = None) -> Any:
        """Return current value — options first (most recent), then data."""
        return self._entry.options.get(key) or self._entry.data.get(key, default)

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
        """Step 2: Re-select the indicator sensor/number entity."""
        if user_input is not None:
            self._data[CONF_ENTITY_ID] = user_input[CONF_ENTITY_ID]
            return await self.async_step_basic_sensor()

        device_id = self._data.get(CONF_DEVICE_ID, "")
        zwave_devices = _get_zwave_devices(self.hass)
        device_name = zwave_devices.get(device_id, device_id)
        current = self._data.get(CONF_ENTITY_ID, "")

        schema = vol.Schema(
            {
                vol.Optional(CONF_ENTITY_ID, default=current): EntitySelector(
                    EntitySelectorConfig(domain=["sensor", "number"])
                ),
            }
        )

        return self.async_show_form(
            step_id="indicator",
            data_schema=schema,
            description_placeholders={"device_name": device_name},
        )

    async def async_step_basic_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 3: Re-select the Basic CC sensor entity."""
        if user_input is not None:
            self._data[CONF_BASIC_SENSOR] = user_input.get(CONF_BASIC_SENSOR, "")
            return await self.async_step_mqtt()

        current = self._data.get(CONF_BASIC_SENSOR, "")

        schema = vol.Schema(
            {
                vol.Optional(CONF_BASIC_SENSOR, default=current): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
            }
        )

        return self.async_show_form(
            step_id="basic_sensor",
            data_schema=schema,
            description_placeholders={},
        )

    async def async_step_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 4: Re-configure Z-Wave JS UI MQTT settings and RFWC5 node ID."""
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
                    default=self._data.get(CONF_CONTROLLER_NODE_ID, DEFAULT_CONTROLLER_NODE_ID),
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
        """Step 5: Configure labels and action types for all 5 buttons."""
        buttons_cfg: list[dict] = self._data.get(
            CONF_BUTTONS,
            [_default_button_config(i) for i in range(NUM_BUTTONS)],
        )

        if user_input is not None:
            for i in range(NUM_BUTTONS):
                n = i + 1
                buttons_cfg[i] = {
                    CONF_BUTTON_LABEL: user_input[f"button_{n}_label"],
                    CONF_BUTTON_ACTION_TYPE: user_input[f"button_{n}_action_type"],
                    CONF_BUTTON_ACTION_ENTITY: buttons_cfg[i].get(
                        CONF_BUTTON_ACTION_ENTITY, ""
                    ),
                }
            self._data[CONF_BUTTONS] = buttons_cfg
            return await self.async_step_entities()

        schema_dict: dict = {}
        for i in range(NUM_BUTTONS):
            n = i + 1
            cfg = buttons_cfg[i]
            schema_dict[vol.Required(
                f"button_{n}_label",
                default=cfg.get(CONF_BUTTON_LABEL, f"Button {n}"),
            )] = str
            schema_dict[vol.Required(
                f"button_{n}_action_type",
                default=cfg.get(CONF_BUTTON_ACTION_TYPE, ACTION_TYPE_NONE),
            )] = _action_type_selector()

        return self.async_show_form(
            step_id="buttons",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 6: Entity pickers for buttons that have an action, filtered by domain."""
        buttons_cfg: list[dict] = self._data.get(CONF_BUTTONS, [])
        active = [
            i for i in range(NUM_BUTTONS)
            if buttons_cfg[i][CONF_BUTTON_ACTION_TYPE] != ACTION_TYPE_NONE
        ]

        if not active:
            return self.async_create_entry(title="", data=self._data)

        if user_input is not None:
            for i in active:
                n = i + 1
                action_type = buttons_cfg[i][CONF_BUTTON_ACTION_TYPE]
                raw = user_input.get(f"button_{n}_entity", "")
                buttons_cfg[i][CONF_BUTTON_ACTION_ENTITY] = (
                    _covers_from_entity_value(raw)
                    if action_type == ACTION_TYPE_COVER_CYCLE
                    else raw
                )
            self._data[CONF_BUTTONS] = buttons_cfg
            return self.async_create_entry(title="", data=self._data)

        schema_dict: dict = {}
        for i in active:
            n = i + 1
            action_type = buttons_cfg[i][CONF_BUTTON_ACTION_TYPE]
            current = buttons_cfg[i].get(CONF_BUTTON_ACTION_ENTITY, "")
            selector = _entity_selector_for_action_type(action_type)
            if selector is None:
                continue

            if action_type == ACTION_TYPE_COVER_CYCLE:
                default = _covers_to_list(current)
            else:
                default = current

            schema_dict[vol.Optional(f"button_{n}_entity", default=default)] = selector

        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema(schema_dict),
        )
