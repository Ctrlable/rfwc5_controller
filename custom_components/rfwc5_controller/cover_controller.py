"""
Lutron-style cyclic cover control for the Eaton RFWC5 keypad.

Cycle logic
-----------
  If ANY cover is moving (opening or closing) → STOP all
  If all covers are closed or avg position <= 5 → OPEN all
  If all covers are open or avg position >= 95  → CLOSE all
  If stopped mid-travel → resume last direction
    (tracked per controller instance via class-level dict)

Position thresholds (5 / 95) instead of exact 0 / 100 handle covers that
never quite reach fully-open or fully-closed state.

LED state
---------
  ON  = any cover has position > 5 OR state is open / opening
  OFF = all covers have position <= 5 OR state is closed / closing
"""

from __future__ import annotations

import logging

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


class CoverCycleController:
    """
    Manages one keypad button's cyclic Up / Stop / Down control over one or
    more cover entities.

    direction_key must be unique per entry+button so that direction memory is
    scoped correctly when multiple keypads share a HA instance.
    e.g.  f"{entry.entry_id}_{button_index}"
    """

    # Class-level direction memory: key → "open" | "close"
    # Shared across all instances so direction survives cover_controller
    # object recreation (e.g. after integration reload).
    _last_directions: dict[str, str] = {}

    def __init__(
        self,
        hass: HomeAssistant,
        cover_entities: list[str],
        direction_key: str,
    ) -> None:
        self.hass = hass
        self.cover_entities = cover_entities
        self._direction_key = direction_key

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_position(self, entity_id: str) -> int | None:
        """
        Return current_position (0-100) for a cover entity.
        Falls back to 100 for "open" state and 0 for "closed" state if
        the cover does not report a numeric position attribute.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        pos = state.attributes.get("current_position")
        if pos is not None:
            return int(pos)
        # State-based fallback for covers without position reporting
        if state.state == COVER_STATE_OPEN:
            return 100
        if state.state == COVER_STATE_CLOSED:
            return 0
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_dominant_state(self) -> str:
        """
        Determine the next command to issue.

        Returns one of: "open", "close", "stop"
        """
        states: list[str] = []
        positions: list[int] = []

        for entity_id in self.cover_entities:
            state = self.hass.states.get(entity_id)
            if state:
                states.append(state.state)
                pos = self._get_position(entity_id)
                if pos is not None:
                    positions.append(pos)

        if not states:
            return "open"

        # ANY cover moving → STOP all
        if any(s in (COVER_STATE_OPENING, COVER_STATE_CLOSING) for s in states):
            return "stop"

        if positions:
            avg = sum(positions) / len(positions)
            if avg <= 5:
                return "open"   # effectively closed → open
            if avg >= 95:
                return "close"  # effectively open → close
            # Mid-travel stopped → resume last direction
            return CoverCycleController._last_directions.get(
                self._direction_key, "open"
            )

        # No position data available — fall back to state strings
        if all(s == COVER_STATE_CLOSED for s in states):
            return "open"
        if all(s == COVER_STATE_OPEN for s in states):
            return "close"

        # Mixed states → resume last direction
        return CoverCycleController._last_directions.get(
            self._direction_key, "open"
        )

    async def async_cycle(self) -> str:
        """
        Execute the next cycle command.
        Returns the command string ("open", "close", or "stop").
        """
        command = self.get_dominant_state()

        service_map = {
            "open":  COVER_SERVICE_OPEN,
            "close": COVER_SERVICE_CLOSE,
            "stop":  COVER_SERVICE_STOP,
        }

        # Remember movement direction (not stop) for mid-travel resume
        if command in ("open", "close"):
            CoverCycleController._last_directions[self._direction_key] = command

        await self.hass.services.async_call(
            "cover",
            service_map[command],
            {"entity_id": self.cover_entities},
            blocking=True,
        )

        _LOGGER.info(
            "CoverCycle [%s]: issued %s to %s",
            self._direction_key, command, self.cover_entities,
        )
        return command

    def get_led_state(self) -> bool:
        """
        Compute LED state from current cover positions / states.

        ON  = any cover position > 5 OR state is open / opening
        OFF = all covers position <= 5 OR state is closed / closing
        """
        for entity_id in self.cover_entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            pos = self._get_position(entity_id)
            if pos is not None and pos > 5:
                return True
            if state.state in (COVER_STATE_OPEN, COVER_STATE_OPENING):
                return True
        return False
