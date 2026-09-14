"""Live session orchestration.

A :class:`Session` owns everything for one conversation: the speech clients, the
agent loop, the approval broker, and the event stream that fans out to the
browser. The WebSocket handler above it stays thin — receive a frame, hand it to
the session, send whatever the session emits.

The concurrency shape matters. One task runs the agent turn; another drains the
outbox to the socket. Barge-in must be able to interrupt the first from the
second, so the turn is held as a cancellable task and cancellation propagates
through the loop into any running tool subprocess.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from surtitle.config import Settings
from surtitle.core.agent import AgentLoop, ApprovalBroker
from surtitle.core.events import Event, EventKind, SessionState
from surtitle.core.speak import Chunk, ChunkKind
from surtitle.llm.deepseek import ChatMessage, DeepSeekClient
from surtitle.store.db import Store
from surtitle.tools.environment import environment_summary
from surtitle.tools.project_config import load_project_config
from surtitle.tools.registry import default_registry, mount_mcp_tools
from surtitle.voice.stt import SpeechToText, TranscriptEvent
from surtitle.voice.tts import TextToSpeech

__all__ = ["Session", "SessionManager"]

log = logging.getLogger(__name__)

# How many transcript messages to replay into the model as context.
_HISTORY_LIMIT = 40

# Audio opcodes for binary WebSocket frames.
_OP_AUDIO_IN = 0x01
_OP_AUDIO_OUT = 0x02


@dataclass(slots=True)
class _SessionState:
    """Mutable per-connection state."""

    state: SessionState = SessionState.IDLE
    mic_open: bool = False
    interim: str = ""


@dataclass(slots=True)
class Session:
    """One live conversation over one WebSocket."""

    session_id: str
    project_id: str
    root: Path
    settings: Settings
    store: Store
    deepseek: DeepSeekClient
    send: Callable[[dict[str, Any]], Awaitable[None]]
    send_audio: Callable[[bytes], Awaitable[None]]

    approvals: ApprovalBroker = field(default_factory=ApprovalBroker)
    stt: SpeechToText | None = None
    tts: TextToSpeech | None = None
    events: int = 0
    _turn: asyncio.Task[None] | None = None
    _outbox: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)
    _drainer: asyncio.Task[None] | None = None
    _closed: bool = False
    _state: _SessionState = field(default_factory=_SessionState)
    _audio_seconds: float = 0.0
    registry: Any = None
    mcp_manager: Any = None
    project_config: Any = None

    # --- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Wire up the voice pipeline and start the drainer."""
        if self.settings.voice_enabled and self.settings.deepgram_key():
            self.stt = SpeechToText(
                self.settings,
                on_transcript=self._on_transcript,
                on_error=self._on_voice_problem,
            )
            self.tts = TextToSpeech(
                self.settings,
                on_audio=self._on_audio_out,
                on_started=self._on_speaking_started,
                on_finished=self._on_speaking_finished,
                on_error=self._on_voice_problem,
            )
            await self.stt.start()
            await self.tts.start()

        # Project-scoped configuration: MCP servers, tool trust, LibreOffice
        # path. Read once per session so a mid-session edit cannot change the
        # tool surface underneath a running turn.
        self.project_config = load_project_config(self.root)
        self.registry = await self._build_registry()

        self._drainer = asyncio.create_task(self._drain_outbox(), name="session-outbox")

        trusted = self.store.get_project(self.project_id)
        if trusted is not None and trusted.auto_approved:
            self.approvals.trust(trusted.auto_approved)
        if self.project_config.trusted_tools:
            self.approvals.trust(self.project_config.trusted_tools)

        await self.emit(
            EventKind.READY,
            session_id=self.session_id,
            project_id=self.project_id,
            root=str(self.root),
            voice_enabled=self.settings.voice_enabled and self.tts is not None,
            model=self.settings.deepseek_model,
            stt_api=self.settings.stt_api if self.stt else None,
            sample_rate=self.settings.tts_sample_rate,
            capture_rate=self.settings.stt_sample_rate,
            trusted_tools=self.approvals.trusted,
            mcp_servers=sorted(self.mcp_manager.server_names) if self.mcp_manager else [],
            mcp_failures=self.mcp_manager.failures if self.mcp_manager else [],
            environment=environment_summary(self.root),
        )

    async def _build_registry(self) -> Any:
        """Assemble the tool surface, including any MCP servers for this project."""
        registry = default_registry()
        servers = list(getattr(self.project_config, "enabled_mcp_servers", []) or [])
        if not servers:
            return registry
        # A fresh registry so MCP tools do not leak into other sessions.
        from surtitle.tools.registry import ToolRegistry

        registry = ToolRegistry()
        from surtitle.tools.mcp import McpManager

        self.mcp_manager = McpManager(servers, root=self.root)
        with contextlib.suppress(Exception):
            await mount_mcp_tools(registry, self.mcp_manager)
        return registry

    async def close(self) -> None:
        """Tear everything down, cancelling any in-flight turn."""
        self._closed = True
        await self.cancel_turn()
        if self.mcp_manager is not None:
            with contextlib.suppress(Exception):
                await self.mcp_manager.close()
            self.mcp_manager = None
        if self._drainer is not None:
            self._drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._drainer
            self._drainer = None
        # Bounded teardown: a stalled socket must not hold the disconnect path
        # open, and an un-awaited task is what produces "Task was destroyed but it
        # is pending" on shutdown.
        for client in (self.stt, self.tts):
            if client is None:
                continue
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.stop(), timeout=5.0)
        self.stt = None
        self.tts = None

    # --- outbound --------------------------------------------------------
    async def emit(self, kind: EventKind, **data: Any) -> None:
        """Queue an event for delivery to the browser."""
        if self._closed:
            return
        self.events += 1
        await self._outbox.put(Event(kind=kind, seq=self.events, data=data))

    async def _drain_outbox(self) -> None:
        """Forward queued events to the socket, in order, until closed."""
        try:
            while not self._closed:
                event = await self._outbox.get()
                try:
                    await self.send(event.to_dict())
                except Exception:
                    # A failed send means the socket is gone; stop quietly and
                    # let the route tear the session down.
                    log.debug("event send failed; ending outbox drain")
                    return
        except asyncio.CancelledError:
            raise

    def _set_state(self, state: SessionState) -> None:
        """Update and broadcast the coarse session state."""
        if self._state.state == state:
            return
        self._state.state = state
        # Fire-and-forget: the drainer serialises delivery, and awaiting here
        # would deadlock callers that hold the agent's execution.
        self._outbox.put_nowait(Event(kind=EventKind.STATE, seq=0, data={"state": state.value}))

    # --- inbound: text and audio ----------------------------------------
    async def handle_audio(self, frame: bytes) -> None:
        """Accept a PCM16 frame from the browser microphone."""
        if self.stt is None or not frame:
            return
        # The browser signals barge-in by sending a zero-length frame.
        self._audio_seconds += len(frame) / 2 / self.settings.stt_sample_rate
        self.stt.push_audio(frame)

    async def handle_mic(self, open_: bool) -> None:
        """Open or close the microphone stream."""
        self._state.mic_open = open_
        if self.stt is None:
            return
        self.stt.set_suppression(open_ and self._is_speaking())
        if not open_ and self._state.interim:
            self._state.interim = ""
            await self.emit(EventKind.INTERIM, text="")

    async def handle_text(self, text: str) -> None:
        """Run a typed turn, exactly as if it had been spoken."""
        cleaned = text.strip()
        if not cleaned:
            return
        if self._turn is not None and not self._turn.done():
            await self.emit(
                EventKind.ERROR,
                message="Still working on the previous request. Stop it first or wait.",
            )
            return
        await self.emit(EventKind.USER_TEXT, text=cleaned, source="typed")
        self._turn = asyncio.create_task(self._run_turn(cleaned), name="agent-turn")

    async def _on_transcript(self, event: TranscriptEvent) -> None:
        """Handle a transcription update from Deepgram.

        Updates are accumulated rather than replaced. The two backends report
        differently — Flux sends the transcript for the turn so far, a word-level
        stream sends successive fragments — and overwriting would hand the model
        only the final fragment of a sentence. :meth:`_accumulate` handles
        cumulative and fragment-shaped updates with the same logic.
        """
        self._state.interim = self._accumulate(self._state.interim, event.text)
        await self.emit(
            EventKind.INTERIM,
            text=self._state.interim,
            final=event.final,
            end_of_turn=event.is_end_of_turn,
        )

        if event.is_end_of_turn:
            utterance = self._state.interim.strip()
            self._state.interim = ""
            if not utterance:
                # A turn boundary with nothing transcribable: the user may simply
                # have paused. Starting a turn on silence would answer nothing.
                return

            # A new spoken turn: barge in on anything still playing first.
            if self._is_speaking():
                await self.barge_in()
            if self._turn is not None and not self._turn.done():
                self._turn.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._turn
            await self.emit(EventKind.USER_TEXT, text=utterance, source="voice")
            self._turn = asyncio.create_task(self._run_turn(utterance), name="agent-turn")

    @staticmethod
    def _accumulate(existing: str, incoming: str) -> str:
        """Merge a transcription update into the running transcript.

        The rule is **replace, not append**, because each update already *is* the
        transcript for the turn so far. Measured against the live Flux endpoint on
        a real utterance, the sequence grew and was revised in place:

            3 words  "Read this part"
            3 words  "Read this project"      <- "part" revised to "project"
            4 words  "Read this project and"
            5 words  "Read this project and tell"
            4 words  "Read this project until" <- revised back, shorter
            6 words  "Read this project and tell me"
            …
           13 words  "Read this project and tell me, uh, if the current status of it"

        Appending these produces the duplication this method used to cause:

            "Read this part Read this project Read this project and Read this …"

        The incoming text wins even when it is shorter, because a shorter update is
        a *correction* of the same utterance rather than a fragment of it.

        The one thing replacement must not do is accept an empty update: the
        end-of-turn message carries no transcript, and treating that as the new
        text discarded everything spoken before it — the original bug.
        """
        if not incoming or not incoming.strip():
            # Never let an empty update erase what has been transcribed.
            return existing
        return incoming

    # --- the turn --------------------------------------------------------
    async def _run_turn(self, user_text: str) -> None:
        """Run one agent turn, streaming speech and events as they are produced."""
        loop = AgentLoop(
            self.settings,
            root=self.root,
            project_id=self.project_id,
            session_id=self.session_id,
            store=self.store,
            approvals=self.approvals,
            client=self.deepseek,
            registry=self.registry,
        )
        loop.set_emitter(self._emit_side_channel)

        history = self._build_history()

        try:
            async for event in loop.run(history, user_text, on_chunk=self._speak_chunk):
                if event.kind is EventKind.APPROVAL_REQUEST:
                    await self._on_approval_requested(event.data)
                await self._emit_or_queue(event)
        except asyncio.CancelledError:
            if self.tts is not None:
                with contextlib.suppress(Exception):
                    await self.tts.barge_in()
            with contextlib.suppress(Exception):
                await self.emit(EventKind.STATE, state=SessionState.IDLE.value, reason="stopped")
            raise
        except Exception as exc:  # a turn failure must not kill the session
            log.exception("turn failed")
            await self.emit(EventKind.ERROR, message=f"{type(exc).__name__}: {exc}")
        finally:
            self._audio_seconds = 0.0

    async def _emit_or_queue(self, event: Event) -> None:
        """Send a control-flow event, mirroring only what the UI needs."""
        if event.kind.value.startswith("_"):
            return
        self.events += 1
        event.seq = self.events
        await self._outbox.put(event)

    async def _emit_side_channel(self, event: Event) -> None:
        """Receive thinking and usage events that are not part of the turn stream."""
        self.events += 1
        event.seq = self.events
        await self._outbox.put(event)

    def _build_history(self) -> list[ChatMessage]:
        """Rebuild model context from the stored transcript."""
        messages = self.store.list_messages(self.session_id, limit=_HISTORY_LIMIT)
        history: list[ChatMessage] = []
        for message in messages:
            if message.role in {"user", "assistant"} and message.content:
                history.append({"role": message.role, "content": message.content})
        # The current user message is appended by the loop, so drop the copy the
        # loop's own run() would duplicate.
        if history and history[-1]["role"] == "user":
            history.pop()
        return history

    async def _speak_chunk(self, chunk: Chunk) -> None:
        """Forward a completed sentence to text-to-speech immediately."""
        if chunk.kind is not ChunkKind.SAY or self.tts is None:
            return
        if not chunk.text.strip():
            return
        self.tts.speak(chunk.text, final=chunk.final)

    # --- speaking / barge-in --------------------------------------------
    def _is_speaking(self) -> bool:
        return bool(self.tts and self.tts.is_speaking)

    async def _on_audio_out(self, audio: bytes, sequence: int) -> None:
        """Relay synthesised audio to the browser as a binary frame."""
        await self.send_audio(audio)

    async def _on_speaking_started(self) -> None:
        """Mark the session as speaking and suppress echo-contaminated finals.

        Suppression is enabled here rather than driven by the client so that it
        cannot drift out of sync with what is actually being played.
        """
        self._set_state(SessionState.SPEAKING)

        # Anything the user said while the previous turn was finishing must be
        # handed over BEFORE suppression starts. Suppression exists to stop the
        # agent transcribing its own voice, but a transcript already captured is
        # the user's, and dropping it makes an utterance vanish — which is what
        # happened on a second attempt to speak.
        pending = self._state.interim.strip()
        self._state.interim = ""
        if pending and not self._turn_in_flight():
            log.info("delivering speech captured before playback: %r", pending[:80])
            await self.emit(EventKind.USER_TEXT, text=pending, source="voice")
            self._turn = asyncio.create_task(self._run_turn(pending), name="agent-turn")

        if self.stt is not None:
            self.stt.set_suppression(True)

    def _turn_in_flight(self) -> bool:
        """True while an agent turn is still running."""
        return self._turn is not None and not self._turn.done()

    async def _on_speaking_finished(self) -> None:
        """Return to idle once the last sentence has been synthesised."""
        if self.stt is not None:
            self.stt.set_suppression(False)
        if self._state.state is SessionState.SPEAKING:
            self._set_state(SessionState.LISTENING if self._state.mic_open else SessionState.IDLE)

    async def barge_in(self) -> None:
        """Interrupt speech immediately: called when the user starts talking."""
        if self.tts is not None:
            with contextlib.suppress(Exception):
                await self.tts.barge_in()
        if self.stt is not None:
            self.stt.set_suppression(False)
        self._set_state(SessionState.LISTENING if self._state.mic_open else SessionState.IDLE)

    async def cancel_turn(self) -> None:
        """Stop the running turn and any speech."""
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn
        self._turn = None
        self.approvals.cancel_all()
        await self.barge_in()

    # --- approvals -------------------------------------------------------
    async def _on_approval_requested(self, data: dict[str, Any]) -> None:
        """Log every approval request.

        An approval prompt is a visible interruption; it must be traceable to a
        decision the agent actually made rather than appearing unexplained.
        """
        arguments = data.get("arguments") or {}
        summary = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(arguments.items())[:3])
        log.info(
            "approval requested: tool=%s args=(%s) call_id=%s",
            data.get("name"),
            summary,
            data.get("call_id"),
        )

    async def handle_approval(self, call_id: str, *, allowed: bool, remember: bool) -> None:
        """Record the user's decision so the waiting loop can resume."""
        resolved = self.approvals.resolve(call_id, allowed=allowed, remember=remember)
        if remember and allowed:
            # Persist the trust decision for this project so the user is not
            # asked the same question on every future session.
            project = self.store.get_project(self.project_id)
            existing = set(project.auto_approved) if project else set()
            existing.update(self.approvals.trusted)
            self.store.set_auto_approved(self.project_id, sorted(existing))
        if not resolved and call_id not in self.approvals.pending_ids:
            log.debug("approval for %s arrived before or after its request", call_id)

    # --- voice problems --------------------------------------------------
    async def _on_voice_problem(self, message: str) -> None:
        """Surface a voice-layer problem without ending the session."""
        await self.emit(EventKind.ERROR, message=message, kind_detail="voice", recoverable=True)

    # --- usage -----------------------------------------------------------
    @property
    def state(self) -> SessionState:
        return self._state.state


class SessionManager:
    """Tracks live sessions so the WebSocket route can clean up reliably."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def add(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.remove(session_id)

    @property
    def count(self) -> int:
        return len(self._sessions)


def decode_client_frame(raw: bytes) -> tuple[str, Any]:
    """Decode a binary frame from the browser into (opcode, payload).

    The browser sends audio as ``0x01 || pcm`` and control messages as
    ``0x02 || json``. Keeping audio binary avoids base64's 33% overhead on a
    stream that runs continuously while the microphone is open.
    """
    if not raw:
        raise ValueError("empty frame")
    opcode = raw[0]
    body = raw[1:]
    if opcode == _OP_AUDIO_IN:
        return "audio", body
    if opcode == _OP_AUDIO_OUT:
        import json

        return "json", json.loads(body.decode("utf-8"))
    raise ValueError(f"unknown frame opcode {opcode}")


def encode_b64_audio(data: bytes) -> str:
    """Base64 helper, used by tests and diagnostic endpoints."""
    return base64.b64encode(data).decode("ascii")
