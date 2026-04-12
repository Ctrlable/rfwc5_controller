# RFWC5 Controller — Home Assistant Custom Integration

Control your **Eaton RFWC5 / RFWC5D** Z-Wave 5-button scene keypad from Home Assistant with full LED feedback, stateful scene support, and race-condition-free updates.

---

## The Problem This Solves

The RFWC5 uses **Z-Wave Indicator CC (class 135)** to represent all 5 button LEDs as a single bitmask integer (0–32). This means:

- Every LED change requires: `refresh_value` → read current bitmask → flip one bit → `set_value`
- If two scenes change state within milliseconds of each other, two automations race to read-modify-write the same value → **one write clobbers the other → wrong LEDs**

### Solution

This integration introduces a **serialised LED State Manager** per keypad:

```
Multiple simultaneous state changes
          │
          ▼
  RFWC5LedManager
  ┌─────────────────────────────────┐
  │  Internal LED state [bool × 5]  │
  │  asyncio.Lock  (serialises I/O) │
  │  1s debounce (collapses bursts) │
  └──────────────┬──────────────────┘
                 │  single write
                 ▼
        zwave_js.set_value
```

All writes go through the manager. Rapid changes debounce into **one Z-Wave write** per quiet period.

---

## Features

- ✅ **UI Config Flow** — add and reconfigure keypads through Settings → Integrations
- ✅ **Scalable** — unlimited keypads, each a separate config entry
- ✅ **Race-condition-free** — async lock + debounce
- ✅ **Auto LED sync** — LEDs automatically mirror linked entity states
- ✅ **All action types**: Stateful Scene, HA Scene, Automation, Script, Entity Toggle
- ✅ **Custom services**: `rfwc5_controller.sync_leds`, `rfwc5_controller.set_button_led`
- ✅ **5 switch entities per keypad** — fully controllable from dashboard/automations

---

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Install **RFWC5 Controller**
3. Restart Home Assistant

### Manual

```bash
cp -r custom_components/rfwc5_controller \
      /config/custom_components/rfwc5_controller
```
Restart Home Assistant.

---

## Configuration

### Step 1 — Find your Indicator entity

In HA, go to **Developer Tools → States** and search for your RFWC5 device. Look for a `sensor.*` or `number.*` entity whose name contains "Indicator". Its entity_id will look like:

```
sensor.rfwc5_keypad_indicator_value
```

### Step 2 — Add the integration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **RFWC5 Controller**
3. Select your Z-Wave device from the dropdown
4. Select the Indicator entity
5. Give the keypad a name (e.g. "Living Room Keypad")
6. Configure each of the 5 buttons:
   - **Label**: Display name (e.g. "Movie Mode")
   - **Action Type**: What pressing the button does
   - **Target Entity**: The entity_id to control

### Action Types

| Action Type | entity_id format | Behaviour |
|---|---|---|
| `stateful_scene` | `switch.scene_name` | Turns the stateful_scenes switch on/off; LED mirrors switch state |
| `ha_scene` | `scene.scene_name` | Activates scene on press (LED stays on until manually turned off) |
| `automation` | `automation.my_automation` | Triggers automation on press; disables on press-off |
| `script` | `script.my_script` | Runs script on press |
| `entity_toggle` | any `domain.entity` | Toggles any entity; LED mirrors entity state |
| `none` | — | LED-only control, no action fired |

---

## Bitmask Reference

| Value | B5 | B4 | B3 | B2 | B1 |
|---|---|---|---|---|---|
| 32 | OFF | OFF | OFF | OFF | OFF |
| 1  | OFF | OFF | OFF | OFF | ON  |
| 2  | OFF | OFF | OFF | ON  | OFF |
| 3  | OFF | OFF | OFF | ON  | ON  |
| …  | … | … | … | … | … |
| 31 | ON  | ON  | ON  | ON  | ON  |

Formula:
```
value = b1×1 + b2×2 + b3×4 + b4×8 + b5×16
value = 32  (when all OFF)
```

---

## Services

### `rfwc5_controller.sync_leds`
Force re-sync all LEDs to the keypad. Call this after HA restart.

```yaml
service: rfwc5_controller.sync_leds
data:
  entry_id: "abc123"  # optional, omit for all keypads
```

### `rfwc5_controller.set_button_led`
Directly set one button's LED from an automation.

```yaml
service: rfwc5_controller.set_button_led
data:
  entry_id: "abc123"
  button: 3
  state: true
```

---

## Example Automation — sync on HA start

```yaml
automation:
  - alias: "Sync RFWC5 LEDs on startup"
    trigger:
      - platform: homeassistant
        event: start
    action:
      - delay: "00:00:10"  # give Z-Wave time to settle
      - service: rfwc5_controller.sync_leds
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| LEDs don't update | Check the indicator entity in Developer Tools → States |
| Wrong entity shown | Look for sensor/number with "Indicator" in the name |
| Race condition still occurring | Increase `LED_WRITE_DEBOUNCE_S` in `const.py` |
| Buttons not triggering actions | Verify entity_id is correct and the entity exists |

---

## Architecture

```
Config Entry (one per keypad)
│
├── RFWC5LedManager
│   ├── _leds[5]              ← single source of truth
│   ├── asyncio.Lock          ← serialises Z-Wave I/O
│   ├── debounce timer        ← collapses rapid changes
│   └── state listeners[]     ← notifies switch entities
│
├── 5× RFWC5ButtonSwitch
│   ├── async_turn_on/off     → manager.async_set_button()
│   │                         → async_execute_action()
│   └── _on_manager_state_change → async_write_ha_state()
│
├── zwave_js_value_updated listener
│   └── manager.async_ingest_indicator()
│
└── state_changed listener (linked entities)
    └── manager._leds[i] = new_state → _schedule_write()
```

---

## Compatibility

- Home Assistant 2023.x or newer
- Z-Wave JS integration
- Eaton RFWC5 / RFWC5D (uses 300-series Z-Wave chip)
- Optional: [stateful_scenes](https://github.com/hugobloem/stateful_scenes) for stateful scene support
