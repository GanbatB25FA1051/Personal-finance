"""Authentication session helpers."""

from collections.abc import MutableMapping
from typing import Any


def clear_session_state(session_state: MutableMapping[str, Any]) -> None:
    """Remove all session-scoped data when a user logs out."""
    for key in list(session_state):
        del session_state[key]
