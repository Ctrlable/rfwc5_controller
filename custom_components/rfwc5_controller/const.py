"""Constants for the Eaton RFWC5 Z-Wave Keypad Controller integration."""

DOMAIN = "rfwc5_controller"

# Number of buttons on the RFWC5
NUM_BUTTONS = 5

# Bitmask weights per button (button 1=bit0, button 2=bit1, … button 5=bit4)
BUTTON_BITMASKS = [1, 2, 4, 8, 16]

# When all buttons are OFF the Indicator value must be 32 (device quirk)
INDICATOR_ALL_OFF_VALUE = 32

# Z-Wave Indicator CC value property
ZWAVE_INDICATOR_CC = 135
ZWAVE_INDICATOR_PROPERTY = "value"
ZWAVE_INDICATOR_PROPERTY_KEY = 0

# Config entry keys
CONF_NODE_ID = "node_id"
CONF_DEVICE_ID = "device_id"        # Z-Wave JS device_id (used for service calls)
CONF_ENTITY_ID = "indicator_entity" # sensor/number entity that holds indicator value
CONF_BUTTONS = "buttons"             # list of 5 button config dicts

# Per-button config keys
CONF_BUTTON_LABEL = "label"
CONF_BUTTON_ACTION_TYPE = "action_type"
CONF_BUTTON_ACTION_ENTITY = "action_entity"

# Action type choices
ACTION_TYPE_STATEFUL_SCENE = "stateful_scene"   # switch.* created by stateful_scenes
ACTION_TYPE_HA_SCENE = "ha_scene"               # scene.*
ACTION_TYPE_AUTOMATION = "automation"           # automation.*
ACTION_TYPE_SCRIPT = "script"                   # script.*
ACTION_TYPE_TOGGLE = "entity_toggle"            # any entity with on/off state
ACTION_TYPE_NONE = "none"

ACTION_TYPES = [
    ACTION_TYPE_STATEFUL_SCENE,
    ACTION_TYPE_HA_SCENE,
    ACTION_TYPE_AUTOMATION,
    ACTION_TYPE_SCRIPT,
    ACTION_TYPE_TOGGLE,
    ACTION_TYPE_NONE,
]

# Services exposed by this integration
SERVICE_SYNC_LEDS = "sync_leds"
SERVICE_SET_BUTTON_LED = "set_button_led"

# Debounce delay in seconds before writing to Z-Wave
LED_WRITE_DEBOUNCE_S = 1.0

# How long to wait after refresh_value before reading indicator state (ms → s)
REFRESH_SETTLE_S = 0.5

# Platforms we register
PLATFORMS = ["switch", "sensor", "select", "text"]
