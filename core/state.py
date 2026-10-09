"""The orchestrator's turn state machine (PDF section "Orchestrator, states").

Only the transitions in :data:`TRANSITIONS` are legal. Interrupt and errors may
go back to Listening (or Idle) from anywhere. Every change is published as a
:class:`~core.events.StateChanged` event and logged with the turn id.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from core.events import StateChanged, TurnState

logger = logging.getLogger(__name__)

S = TurnState

TRANSITIONS: dict[TurnState, frozenset[TurnState]] = {
    S.IDLE: frozenset({S.LISTENING, S.THINKING}),
    S.LISTENING: frozenset({S.THINKING, S.IDLE}),
    S.THINKING: frozenset({S.SPEAKING, S.CHECKING}),
    S.CHECKING: frozenset({S.ACTING, S.AWAITING_YES, S.SPEAKING}),
    S.AWAITING_YES: frozenset({S.ACTING, S.SPEAKING}),
    S.ACTING: frozenset({S.SPEAKING}),
    S.SPEAKING: frozenset({S.LISTENING, S.IDLE, S.AWAITING_YES}),
}

# Interrupt and error recovery may jump here from any state.
RESET_STATES = frozenset({S.LISTENING, S.IDLE})


class IllegalTransition(RuntimeError):
    """A transition the PDF state table does not allow."""


class StateMachine:
    """Holds the current state and checks every move against :data:`TRANSITIONS`.

    Args:
        publish: called with a :class:`StateChanged` event after each move.
        strict: raise on an illegal move (tests) instead of logging a warning.
    """

    def __init__(
        self,
        publish: Callable[[StateChanged], None] | None = None,
        initial: TurnState = S.IDLE,
        strict: bool = False,
    ) -> None:
        self._state = initial
        self._publish = publish
        self._strict = strict
        self._lock = threading.Lock()

    @property
    def state(self) -> TurnState:
        """The current state."""
        return self._state

    def can(self, new: TurnState) -> bool:
        """True if moving to ``new`` is legal from the current state."""
        return new == self._state or new in TRANSITIONS[self._state] or new in RESET_STATES

    def to(self, new: TurnState, turn_id: str = "") -> None:
        """Move to ``new``. Illegal moves raise (strict) or are logged and applied."""
        with self._lock:
            old = self._state
            if new == old:
                return
            if not self.can(new):
                message = f"illegal transition {old.value} -> {new.value}"
                if self._strict:
                    raise IllegalTransition(message)
                logger.warning("[%s] %s", turn_id, message)
            self._state = new
        logger.info("[%s] state %s -> %s", turn_id, old.value, new.value)
        if self._publish is not None:
            self._publish(StateChanged(old, new, turn_id=turn_id))

    def reset(self, live: bool, turn_id: str = "") -> None:
        """Go back to Listening (live mode) or Idle, from any state."""
        self.to(S.LISTENING if live else S.IDLE, turn_id)
