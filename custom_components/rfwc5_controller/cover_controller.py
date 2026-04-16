"""
Lutron-style cyclic cover control for the Eaton RFWC5 keypad.

State machine
-------------
The controller maintains an INTERNAL state that updates IMMEDIATELY on
button press — it does not wait for HA cover state, which may lag by
several seconds. HA state is only used to:
  1. Seed the initial state when first used (UNKNOWN → detect current position)
  2. Confirm terminal states — when HA reports fully open/closed, we advance
     OPENING → OPEN or CLOSING → CLOSED

Cycle transitions
-----------------
  OPEN / STOPPED_OPEN  → send close_cover → CLOSING
  CLOSING              → send stop_cover  → STOPPED_CLOSE
  STOPPED_CLOSE        → send open_cover  → OPENING
  OPENING              → send stop_cover  → STOPPED_OPEN
  CLOSED               → send open_cover  → OPENING
  UNKNOWN              → seed from HA state, then apply cycle transition

LED state (internal, instant — no HA lag)
-----------------
  ON  = OPEN, OPENING, STOPPED_OPEN
  OFF = CLOSED, CLOSING, STOPPED_CLOSE, UNKNOWN-seeded-as-closed

State is scoped per direction_key (f"{entry_id}_{button_index}") and stored
at class level so it survives controller object recreation (e.g. integration
reload).
"""

from __future__ import annotations

import logging
import time
from enum import Enum

from homeassistant.core import HomeAssistant

from .const import (
    COVER_SERVICE_CLOSE,
    COVER_SERVICE_OPEN,
    COVER_SERVICE_STOP,
    COVER_STATE_CLOSED,
    COVER_STATE_CLOSING,
    COVER_STATE_OPEN,
    COVER_STATE_OPENING,
)

_LOGGER = logging.getLogger(__name__)


class CoverMovementState(Enum):
    """Internal state machine values — updated immediately on button press."""
    UNKNOWN = "unknown"
    OPENING = "opening"            # we sent open_cover
    CLOSING = "closing"            # we sent close_cover
    STOPPED_OPEN = "stopped_open"  # stopped while opening (mid-travel)
    STOPPED_CLOSE = "stopped_close"  # stopped while closing (mid-travel)
    OPEN = "open"                  # HA confirmed fully open (position >= 95%)
    CLOSED = "closed"              # HA confirmed fully closed (position <= 5%)


class CoverCycleController:
    """
    Manages one keypad button's cyclic Up / Stop / Down control for one or
    more cover entities.

    direction_key must be unique per entry+button so that state memory is
    scoped correctly when multiple keypads share a HA instance.
    e.g.  f"{entry.entry_id}_{button_index}"
    """

    # Class-level state store — survives object recreation
    _states: dict[str, CoverMovementState] = {}

    # Monotonic timestamp of last command per key — used for debounce
    _last_command_time: dict[str, float] = {}

    # Minimum seconds between accepted commands
    COMMAND_DEBOUNCE_S = 0.4

    def __init__(
        self,
        hass: HomeAssistant,
        cover_entities: list[str],
        direction_key: str,
    ) -> None:
        self.hass = hass
        self.cover_entities = cover_entities
        self._key = direction_key

    # ------------------------------------------------------------------
    # Internal state accessors
    # ------------------------------------------------------------------

    def _get_internal_state(self) -> CoverMovementState:
        return CoverCycleController._states.get(self._key, CoverMovementState.UNKNOWN)

    def _set_internal_state(self, state: CoverMovementState) -> None:
        CoverCycleController._states[self._key] = state
        _LOGGER.debug("CoverCycle [%s]: internal state → %s", self._key, state.value)

    # ------------------------------------------------------------------
    # HA state seeding (UNKNOWN only)
    # ------------------------------------------------------------------

    def _seed_from_ha_state(self) -> CoverMovementState:
        """
        Derive an initial internal state from HA cover state.
        Called once when internal state is UNKNOWN.
        """
        ha_states: list[str] = []
        positions: list[int] = []

        for entity_id in self.cover_entities:
            state = self.hass.states.get(entity_id)
            if state:
                ha_states.append(state.state)
                pos = state.attributes.get("current_position")
                if pos is not None:
                    positions.append(int(pos))

        if not ha_states:
            return CoverMovementState.UNKNOWN

        # Currently moving
        if any(s == COVER_STATE_OPENING for s in ha_states):
            return CoverMovementState.OPENING
        if any(s == COVER_STATE_CLOSING for s in ha_states):
            return CoverMovementState.CLOSING

        # Use average position to determine stopped state
        if positions:
            avg = sum(positions) / len(positions)
            if avg >= 95:
                return CoverMovementState.OPEN
            if avg <= 5:
                return CoverMovementState.CLOSED
            # Mid-position stopped — assume was opening (LED = ON is safer default)
            return CoverMovementState.STOPPED_OPEN

        # No position attribute — use state string
        if all(s == COVER_STATE_CLOSED for s in ha_states):
            return CoverMovementState.CLOSED
        return CoverMovementState.OPEN

    # ------------------------------------------------------------------
    # Cycle logic
    # ------------------------------------------------------------------

    def _determine_next_command(self) -> tuple[str, CoverMovementState]:
        """
        Return (ha_service_name, next_internal_state) based on current
        internal state. Seeds from HA if state is UNKNOWN.
        """
        current = self._get_internal_state()

        if current == CoverMovementState.UNKNOWN:
            current = self._seed_from_ha_state()
            self._set_internal_state(current)

        if current in (CoverMovementState.OPEN, CoverMovementState.STOPPED_OPEN):
            return (COVER_SERVICE_CLOSE, CoverMovementState.CLOSING)

        if current == CoverMovementState.CLOSING:
            return (COVER_SERVICE_STOP, CoverMovementState.STOPPED_CLOSE)

        if current == CoverMovementState.STOPPED_CLOSE:
            return (COVER_SERVICE_OPEN, CoverMovementState.OPENING)

        if current == CoverMovementState.OPENING:
            return (COVER_SERVICE_STOP, CoverMovementState.STOPPED_OPEN)

        if current == CoverMovementState.CLOSED:
            return (COVER_SERVICE_OPEN, CoverMovementState.OPENING)

        # Fallback (UNKNOWN after failed seed)
        return (COVER_SERVICE_OPEN, CoverMovementState.OPENING)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_cycle(self) -> str:
        """
        Execute the next cycle command.

        Updates internal state IMMEDIATELY before the service call so that
        subsequent button presses see the correct state even if HA hasn't
        updated yet.

        Returns the service name called, or "debounced" if the call was
        suppressed by the debounce guard.
        """
        # Debounce rapid double-presses
        now = time.monotonic()
        last = CoverCycleController._last_command_time.get(self._key, 0.0)
        if now - last < self.COMMAND_DEBOUNCE_S:
            _LOGGER.debug(
                "CoverCycle [%s]: debounced (%.2fs since last command)",
                self._key, now - last,
            )
            return "debounced"

        CoverCycleController._last_command_time[self._key] = now

        service, next_state = self._determine_next_command()

        # Capture state BEFORE update so we can log correctly and revert on error
        prev_state = self._get_internal_state()

        # Update internal state IMMEDIATELY — do not wait for HA cover state
        self._set_internal_state(next_state)

        _LOGGER.info(
            "CoverCycle [%s]: %s → %s → state now %s",
            self._key,
            prev_state.value,
            service,
            next_state.value,
        )

        try:
            await self.hass.services.async_call(
                "cover",
                service,
                {"entity_id": self.cover_entities},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "CoverCycle [%s]: service call %s failed — reverting state to %s: %s",
                self._key, service, prev_state.value, err,
            )
            self._set_internal_state(prev_state)

        return service

    def sync_from_ha_state(self) -> None:
        """
        Called by the HA state change listener to confirm terminal states.

        Only advances OPENING → OPEN or CLOSING → CLOSED when HA reports
        a fully open or fully closed position. Does NOT override intermediate
        states (STOPPED_OPEN, STOPPED_CLOSE) with noisy HA updates.
        """
        current = self._get_internal_state()

        for entity_id in self.cover_entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue

            pos = state.attributes.get("current_position")

            if state.state == COVER_STATE_OPEN or (
                pos is not None and int(pos) >= 95
            ):
                if current == CoverMovementState.OPENING:
                    self._set_internal_state(CoverMovementState.OPEN)
                    _LOGGER.debug(
                        "CoverCycle [%s]: HA confirmed OPEN", self._key
                    )
                break

            if state.state == COVER_STATE_CLOSED or (
                pos is not None and int(pos) <= 5
            ):
                if current == CoverMovementState.CLOSING:
                    self._set_internal_state(CoverMovementState.CLOSED)
                    _LOGGER.debug(
                        "CoverCycle [%s]: HA confirmed CLOSED", self._key
                    )
                break

    def get_led_state(self) -> bool:
        """
        Return LED state based on internal state — instant, no HA lag.

        ON  = OPEN, OPENING, STOPPED_OPEN
        OFF = CLOSED, CLOSING, STOPPED_CLOSE (or UNKNOWN if fully closed)
        """
        current = self._get_internal_state()

        if current == CoverMovementState.UNKNOWN:
            current = self._seed_from_ha_state()

        return current in (
            CoverMovementState.OPEN,
            CoverMovementState.OPENING,
            CoverMovementState.STOPPED_OPEN,
        )
