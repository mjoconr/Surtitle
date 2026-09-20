"""Typed events exchanged between the agent loop and the browser.

The browser is a thin view: it renders :class:`Event` objects and sends
:class:`ClientCommand` objects back. Keeping both sides on one typed protocol is
what makes barge-in and approvals reliable instead of best-effort.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "ClientCommand",
    "CommandKind",
    "Event",
    "EventKind",
    "SessionState",
]


class SessionState(StrEnum):
    """Coarse agent state, rendered as the status indicator in the UI."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    TOOL = "tool"
    AWAITING_APPROVAL = "awaiting_approval"
    SPEAKING = "speaking"
    ERROR = "error"


class EventKind(StrEnum):
    """Server to client message types."""

    READY = "ready"
    STATE = "state"
    INTERIM = "interim"
    USER_TEXT = "user_text"
    AGENT_TEXT = "agent_text"
    SAY = "say"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    APPROVAL_REQUEST = "approval_request"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    TODOS = "todos"
    GOAL = "goal"
    USAGE = "usage"
    # A change in the voice layer's health: a problem, or the end of one. Sent
    # separately from ERROR because "recognised again" is not an error and must not
    # be rendered as one.
    VOICE = "voice"
    ERROR = "error"
    DONE = "done"


class CommandKind(StrEnum):
    """Client to server message types."""

    HELLO = "hello"
    AUDIO = "audio"
    MIC = "mic"
    TEXT = "text"
    BARGE_IN = "barge_in"
    # The browser's playback queue has drained: the agent has actually stopped
    # making sound. The server cannot work this out for itself — it finishes
    # *synthesising* long before the queued audio has finished playing, and with a
    # local voice the gap is seconds.
    PLAYBACK = "playback"
    APPROVAL = "approval"
    CANCEL = "cancel"
    SET_MODE = "set_mode"
    PING = "ping"


@dataclass(slots=True)
class Event:
    """A single server-to-client message."""

    kind: EventKind
    seq: int = 0
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Wire representation, dropping empty payload fields."""
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["data"] = {k: v for k, v in self.data.items() if v is not None}
        return payload


@dataclass(slots=True)
class ClientCommand:
    """A single client-to-server message."""

    kind: CommandKind
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> ClientCommand:
        """Build a command from a decoded JSON frame.

        Unknown command kinds are rejected with ``ValueError`` so the caller can
        report a protocol error instead of silently ignoring input.
        """
        raw_kind = raw.get("kind")
        try:
            kind = CommandKind(raw_kind)
        except ValueError as exc:
            raise ValueError(f"unknown command kind: {raw_kind!r}") from exc
        data = raw.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("command 'data' must be an object")
        return cls(kind=kind, data=data)
