"""
LED State Manager for Eaton RFWC5 Z-Wave keypad.

This module owns the single source of truth for the 5-button LED bitmask and
serialises every read-modify-write to the Z-Wave device through an asyncio queue,
completely eliminating race conditions when multiple buttons change state
simultaneously.

Bitmask encoding (Indicator CC value 0-32):
  Button 1 → bit 0 (weight  1)
  Button 2 → bit 1 (weight  2)
  Button 3 → bit 2 (weight  4)
  Button 4 → bit 3 (weight  8)
  Button 5 → bit 4 (weight 16)
  All OFF  → special value 32 (device quirk)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import (
    BUTTON_BITMASKS,
    INDICATOR_ALL_OFF_VALUE,
    LED_WRITE_DEBOUNCE_S,
    NUM_BUTTONS,
    REFRESH_SETTLE_S,
    ZWAVE_INDICATOR_CC,
    ZWAVE_INDICATOR_PROPERTY,
)

_LOGGER = logging.getLogger(__name__)


class RFWC5LedManager:
    """
    Serialised LED state manager for one RFWC5 keypad.

    Usage pattern
    -------------
    1. On HA start, call `async_initialize()` which refreshes the device and
       reads the current indicator value to warm-up internal state.
    2. When a button switch entity is turned on/off, call
       `async_set_button(index, state)`.  The manager debounces rapid changes
       (mode=restart semantics) and issues a single set_value after the quiet
       period.
    3. When the Z-Wave indicator value changes from an external source (e.g. a
       direct key press), call `async_ingest_indicator(value)` so internal
       state and switch entities stay in sync.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        entry_id: str,
    ) -> None:
        self.hass = hass
        self.device_id = device_id
        self.entry_id = entry_id

        # Internal LED state – index 0-4 corresponds to buttons 1-5
        self._leds: list[bool] = [False] * NUM_BUTTONS
        # LED state snapshot from before the last refresh (used to detect button releases)
        self._previous_leds: list[bool] = [False] * NUM_BUTTONS

        # Listeners registered by switch entities so they can update HA state
        self._state_listeners: list[Callable[[int, bool], None]] = []

        # Debounce handle
        self._debounce_unsub: Callable | None = None

        # Serialisation lock – guards the refresh→read→compute→write sequence
        self._write_lock = asyncio.Lock()

        # Whether we have a valid baseline from the device
        self._initialised = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_initialize(self) -> None:
        """Refresh the device and read current LED state on startup."""
        try:
            await self._async_refresh_and_read()
            self._initialised = True
            _LOGGER.debug(
                "RFWC5 %s initialised with LED state %s",
                self.device_id,
                self._leds,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "RFWC5 %s could not read initial indicator value: %s",
                self.device_id,
                err,
            )

    def get_button_state(self, button_index: int) -> bool:
        """Return current LED state (0-based index)."""
        return self._leds[button_index]

    def get_previous_leds(self) -> list[bool]:
        """Return LED state snapshot from before the last refresh."""
        return self._previous_leds[:]

    async def async_refresh_and_read_indicator(self) -> None:
        """Public wrapper: refresh the indicator from the device and update LED state."""
        await self._async_refresh_and_read()

    def register_state_listener(self, listener: Callable[[int, bool], None]) -> None:
        """Register a callback(button_index, new_state) for state-push updates."""
        self._state_listeners.append(listener)

    def unregister_state_listener(self, listener: Callable[[int, bool], None]) -> None:
        """Unregister a previously registered listener."""
        self._state_listeners.discard(listener) if hasattr(
            self._state_listeners, "discard"
        ) else None
        try:
            self._state_listeners.remove(listener)
        except ValueError:
            pass

    @callback
    def async_ingest_indicator(self, raw_value: int) -> None:
        """
        Called when the Z-Wave indicator value changes externally.
        Decodes the bitmask and pushes state updates to switch entities.
        """
        _LOGGER.debug(
            "RFWC5 ingest_indicator: raw=%d decoded=%s",
            raw_value, self._decode(raw_value),
        )
        new_leds = self._decode(raw_value)
        changed = [i for i in range(NUM_BUTTONS) if new_leds[i] != self._leds[i]]
        self._leds = new_leds
        for idx in changed:
            self._notify_listeners(idx, self._leds[idx])

    async def async_set_button(self, button_index: int, state: bool) -> None:
        """
        Set a single button LED state and schedule a debounced Z-Wave write.
        Rapid calls cancel-and-restart the debounce timer (mode: restart).
        """
        _LOGGER.debug(
            "RFWC5 set_button called: index=%d state=%s current_leds=%s",
            button_index, state, self._leds,
        )
        self._leds[button_index] = state
        self._schedule_write()

    async def async_sync_all(self) -> None:
        """Force an immediate (non-debounced) write of current state."""
        if self._debounce_unsub is not None:
            self._debounce_unsub()
            self._debounce_unsub = None
        await self._async_write_now()

    # ------------------------------------------------------------------
    # Debounce
    # ------------------------------------------------------------------

    def _schedule_write(self) -> None:
        """Cancel any pending write and schedule a fresh one after the quiet period."""
        if self._debounce_unsub is not None:
            self._debounce_unsub()
            self._debounce_unsub = None

        @callback
        def _fire(_now: Any) -> None:
            self._debounce_unsub = None
            self.hass.async_create_task(self._async_write_now())

        self._debounce_unsub = async_call_later(
            self.hass, LED_WRITE_DEBOUNCE_S, _fire
        )

    # ------------------------------------------------------------------
    # Z-Wave read / write (serialised via lock)
    # ------------------------------------------------------------------

    async def _async_refresh_and_read(self) -> None:
        """Refresh the indicator value from the device and parse it."""
        async with self._write_lock:
            _LOGGER.debug(
                "RFWC5 refresh_and_read: indicator_entity=%s",
                self._indicator_entity_id(),
            )
            # Ask Z-Wave JS to pull latest value from device
            await self.hass.services.async_call(
                "zwave_js",
                "refresh_value",
                {"entity_id": self._indicator_entity_id()},
                blocking=True,
            )
            # Give the device a moment to settle before we read
            await asyncio.sleep(REFRESH_SETTLE_S)
            raw = self._read_indicator_from_ha()
            if raw is not None:
                self._previous_leds = self._leds[:]
                self._leds = self._decode(raw)

    async def _async_write_now(self) -> None:
        """Encode current LED state and send set_value to the device."""
        async with self._write_lock:
            target_value = self._encode(self._leds)
            _LOGGER.debug(
                "RFWC5 PRE-WRITE: leds=%s encoded_value=%d device_id=%s",
                self._leds, target_value, self.device_id,
            )
            try:
                await self.hass.services.async_call(
                    "zwave_js",
                    "set_value",
                    {
                        "entity_id": self._indicator_entity_id(),
                        "command_class": ZWAVE_INDICATOR_CC,
                        "property": ZWAVE_INDICATOR_PROPERTY,
                        "value": target_value,
                    },
                    blocking=True,
                )
                _LOGGER.debug("RFWC5 POST-WRITE: service call completed")
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "RFWC5 %s failed to write indicator value: %s",
                    self.device_id,
                    err,
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _indicator_entity_id(self) -> str:
        """Build the sensor/number entity_id used for refresh_value calls."""
        # Stored in hass.data during setup
        data = self.hass.data.get("rfwc5_controller", {}).get(self.entry_id, {})
        return data.get("indicator_entity", "")

    def _read_indicator_from_ha(self) -> int | None:
        """Read the current indicator value from HA state machine."""
        entity_id = self._indicator_entity_id()
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        try:
            return int(float(state.state))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _decode(raw: int) -> list[bool]:
        """Decode a raw indicator value (0-32) into a list of 5 booleans."""
        if raw == INDICATOR_ALL_OFF_VALUE:
            return [False] * NUM_BUTTONS
        return [(raw // bitmask) % 2 == 1 for bitmask in BUTTON_BITMASKS]

    @staticmethod
    def _encode(leds: list[bool]) -> int:
        """Encode a list of 5 booleans into a raw indicator value."""
        value = sum(bitmask for bitmask, on in zip(BUTTON_BITMASKS, leds) if on)
        return INDICATOR_ALL_OFF_VALUE if value == 0 else value

    def _notify_listeners(self, button_index: int, state: bool) -> None:
        for listener in self._state_listeners:
            try:
                listener(button_index, state)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("State listener error: %s", err)
