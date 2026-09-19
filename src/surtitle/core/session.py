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

import array
import asyncio
import base64
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from surtitle.config import Settings
from surtitle.core.agent import (
    NEAR_BUDGET_FRACTION,
    PROGRESS_STEPS,
    AgentLoop,
    ApprovalBroker,
    RepeatCallGuard,
)
from surtitle.core.events import Event, EventKind, SessionState
from surtitle.core.speak import Chunk, ChunkKind
from surtitle.llm.deepseek import ChatMessage, DeepSeekClient
from surtitle.stats import RunStats, context_window, is_peak, resolve_price
from surtitle.store.db import Store
from surtitle.tools.environment import environment_summary
from surtitle.tools.project_config import load_project_config
from surtitle.tools.registry import default_registry, mount_mcp_tools
from surtitle.voice.engine import (
    SttEngine,
    TtsEngine,
    VoiceBundle,
    build_voice,
)
from surtitle.voice.stt import TranscriptEvent

__all__ = ["Session", "SessionManager"]

log = logging.getLogger(__name__)

# Step counts at which a long, silent turn says it is still working. Escalating,
# so the first is early enough to reassure and the rest are rare. The schedule
# lives beside the step budget it describes, so the two cannot drift apart.
_PROGRESS_STEPS = PROGRESS_STEPS

# How many of the most recent conversational messages to replay into the model as
# context. "Most recent" is the load-bearing part — see `_build_history`.
_HISTORY_LIMIT = 40

# A session whose connection left mid-turn is kept for a reload to reclaim, then
# swept. The grace period bounds how long a turn that never finishes can hold its
# sockets open.
_ORPHAN_SWEEP_SECONDS = 2.0
_ORPHAN_GRACE_SECONDS = 300.0

# Audio opcodes for binary WebSocket frames.
_OP_AUDIO_IN = 0x01
_OP_AUDIO_OUT = 0x02

# Peak amplitude below which a listening session counts as silent. Roughly the
# noise floor of a muted or unconnected microphone; speech is far above it.
_SILENCE_PEAK = 0.005


def _peak_level(frame: bytes) -> float:
    """Loudest absolute sample in a PCM16 little-endian frame, as 0..1.

    Uses ``array`` and the built-in ``min``/``max`` so the scan runs at C speed.
    A malformed odd-length frame is truncated rather than raising; diagnostics must
    never be the thing that breaks a session.
    """
    usable = len(frame) - (len(frame) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(frame[:usable])
    if not samples:
        return 0.0
    return max(max(samples), -min(samples)) / 32768.0


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
    # Owned by the session, not by a turn: a call repeated on the *next* turn is
    # still a repeat, and a guard rebuilt per turn cannot see one. What it hands
    # back when it refuses is the output of the call it is remembering.
    _repeat_guard: RepeatCallGuard = field(default_factory=RepeatCallGuard)
    stt: SttEngine | None = None
    tts: TtsEngine | None = None
    # Why voice is partly or wholly unavailable, if it is. Reported in the
    # `ready` payload so the UI can say what to fix instead of showing a dead
    # microphone button.
    voice_problem: str | None = None
    voice_fix: str | None = None
    voice_backends: dict[str, str] = field(default_factory=dict)
    events: int = 0
    _turn: asyncio.Task[None] | None = None
    _outbox: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)
    _drainer: asyncio.Task[None] | None = None
    _closed: bool = False
    _state: _SessionState = field(default_factory=_SessionState)
    _audio_seconds: float = 0.0
    # Diagnostics: how much audio this listening session actually delivered, and
    # how many times the microphone has been opened.
    _frames_in: int = 0
    _mic_opens: int = 0
    # Loudest sample (0..1) seen this listening session.
    _peak_in: float = 0.0
    # True while the agent's own voice is playing, and whether anything has been
    # transcribed since it began. Loudness cannot tell a person from the speakers;
    # a transcript can, and only a transcript may interrupt a turn.
    _speaking: bool = False
    _speech_since_playback: bool = False
    # When audio for the agent's voice last went to the browser, and how much of
    # it there was. Together they answer "should we still be able to hear
    # ourselves speaking?" without depending on the synthesiser reporting that it
    # has finished.
    _last_audio_out_at: float = 0.0
    _last_audio_out_seconds: float = 0.0
    _suppression_released: bool = False
    _suppression_watchdog: asyncio.Task[None] | None = None
    # An utterance being held for continuation: the text so far, whether a hold
    # is already scheduled, how long it may last, and the task that will deliver
    # it. Deliberately not a lock around the handler — see `_on_transcript`.
    _utterance_buffer: str = ""
    _utterance_pending: bool = False
    _utterance_deadline: float = 0.0
    _commit_task: asyncio.Task[None] | None = None
    # Set when an interrupted turn was already written to the transcript.
    _rolled_back: bool = False
    # Requests that arrived while a turn was running, in order. Held rather than
    # refused: a turn can run for minutes, and "stop it first or wait" turns the
    # user's next thought into an error message.
    _queue: list[tuple[str, str]] = field(default_factory=list)
    # Per-turn accounting, reported when the turn ends. The run-wide totals live
    # in `RunStats`; these are what the turn-end record and its log line report,
    # because "how big did this turn get, and what did it say" is the question a
    # turn that stopped without explaining itself leaves behind.
    _turn_started_at: float = 0.0
    _turn_prompt_tokens: int = 0
    _turn_completion_tokens: int = 0
    # Characters actually handed to the synthesiser this turn, which is the only
    # honest answer to "did the user hear anything".
    _turn_spoken_chars: int = 0
    registry: Any = None
    mcp_manager: Any = None
    project_config: Any = None
    # Process-wide usage counters, owned by the app state. Optional so a session
    # built directly in a test does not have to supply one.
    stats: RunStats | None = None
    # The version-control section of the prompt, built once per session. Reading
    # it means locating the tools and asking the working copy what it is, which is
    # several subprocesses; the prompt is rebuilt every turn, so it is not
    # something to re-derive each time. `vcs_status` is what gives the live state.
    _vcs_section: str | None = None

    # --- lifecycle -------------------------------------------------------
    def rebind(
        self,
        *,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        send_audio: Callable[[bytes], Awaitable[None]],
    ) -> None:
        """Point this session at a replacement connection.

        A browser that reconnects mid-answer must not cost the user that answer.
        Reusing the live session and moving its transport keeps the running turn
        alive, delivers the events it already queued to the new socket, and avoids
        tearing down the Deepgram sockets underneath it.
        """
        self.send = send
        self.send_audio = send_audio

    async def start(self) -> None:
        """Wire up the voice pipeline and start the drainer."""
        bundle: VoiceBundle = build_voice(
            self.settings,
            on_transcript=self._on_transcript,
            on_audio=self._on_audio_out,
            on_started=self._on_speaking_started,
            on_finished=self._on_speaking_finished,
            on_error=self._on_voice_problem,
            on_speed_fallback=self._on_speed_fallback,
        )
        self.stt = bundle.stt
        self.tts = bundle.tts
        self.voice_backends = {
            "stt": self.settings.stt_backend,
            "tts": self.settings.tts_backend,
        }
        if bundle.problem is not None:
            self.voice_problem = bundle.problem.reason
            self.voice_fix = bundle.problem.fix
            # Loud, because a configured engine that did not start is the single
            # hardest voice failure to diagnose from the outside.
            log.warning("voice partially disabled: %s", bundle.problem.reason)
            if bundle.problem.fix:
                log.warning("voice fix: %s", bundle.problem.fix)

        if self.stt is not None:
            await self.stt.start()
        if self.tts is not None:
            await self.tts.start()

        # Project-scoped configuration: MCP servers, tool trust, LibreOffice
        # path. Read once per session so a mid-session edit cannot change the
        # tool surface underneath a running turn.
        self.project_config = load_project_config(self.root)
        self.registry = await self._build_registry()

        self._drainer = asyncio.create_task(self._drain_outbox(), name="session-outbox")
        self._suppression_watchdog = asyncio.create_task(
            self._watch_echo_suppression(), name="echo-suppression-watchdog"
        )

        trusted = self.store.get_project(self.project_id)
        if trusted is not None and trusted.auto_approved:
            self.approvals.trust(trusted.auto_approved)
        if self.project_config.trusted_tools:
            self.approvals.trust(self.project_config.trusted_tools)

        log.info(
            "session ready: id=%s project=%s root=%s voice=%s (stt=%s tts=%s)",
            self.session_id,
            self.project_id,
            self.root,
            self.settings.voice_enabled and self.tts is not None and self.stt is not None,
            self.settings.stt_backend if self.stt else "off",
            self.settings.tts_backend if self.tts else "off",
        )
        await self.announce()

    async def announce(self) -> None:
        """Tell the connected browser what this session is and where it stands.

        Emitted on start and again whenever a reconnect rebinds the session, since
        a browser that was away missed whatever was sent in the meantime and would
        otherwise sit showing a stale state.
        """
        await self.emit(
            EventKind.READY,
            session_id=self.session_id,
            project_id=self.project_id,
            root=str(self.root),
            # Both directions are required for a *spoken* conversation, which is
            # what the microphone button represents. A session that only speaks,
            # or only listens, reports voice_enabled=false and explains why in
            # voice_problem rather than silently disabling the feature.
            voice_enabled=(
                self.settings.voice_enabled and self.tts is not None and self.stt is not None
            ),
            voice_problem=self.voice_problem,
            voice_fix=self.voice_fix,
            voice_backends=self.voice_backends,
            model=self.settings.deepseek_model,
            # Two numbers, because they answer different questions. The window is
            # what the model accepts; the budget is what we intend to send, and it
            # is deliberately far smaller. The browser measures against the budget
            # and shows the window beside it.
            context_window=context_window(self.settings.deepseek_model, self.settings),
            context_budget=self.settings.context_budget,
            stt_api=self.settings.stt_api if self.stt else None,
            sample_rate=self.settings.tts_sample_rate,
            capture_rate=self.settings.stt_sample_rate,
            trusted_tools=self.approvals.trusted,
            mcp_servers=sorted(self.mcp_manager.server_names) if self.mcp_manager else [],
            mcp_failures=self.mcp_manager.failures if self.mcp_manager else [],
            environment=environment_summary(self.root),
            # The concrete state, so the status indicator is right immediately
            # rather than only after the next transition.
            state=self._state.state.value,
            resumed=True,
        )

        # A prompt that is still open is re-asked, because the browser that
        # reconnected is not the one that was shown it. The session is waiting on
        # an answer either way, so failing to restate the question leaves the user
        # with "needs approval", nothing to click, and a turn that never moves.
        for request in self.approvals.pending_announcements():
            await self.emit(EventKind.APPROVAL_REQUEST, **request)

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
        log.info("session closed: id=%s", self.session_id)
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
        if self._commit_task is not None:
            self._commit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._commit_task
            self._commit_task = None
        if self._suppression_watchdog is not None:
            self._suppression_watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._suppression_watchdog
            self._suppression_watchdog = None
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
        if not frame:
            return
        self._frames_in += 1
        self._audio_seconds += len(frame) / 2 / self.settings.stt_sample_rate
        # Track the loudest sample seen. Frame counts alone cannot tell a working
        # microphone from one delivering silence, and that distinction decides
        # whether a missing transcript is a capture problem or a recognition one.
        self._peak_in = max(self._peak_in, _peak_level(frame))
        if self.stt is None:
            # Reported once per session: audio arriving with no recogniser means
            # voice is disabled or the configured engine could not start, not
            # that capture failed.
            if self._frames_in == 1:
                log.warning(
                    "audio arriving but speech recognition is not running "
                    "(voice_enabled=%s, backend=%s, problem=%s)",
                    self.settings.voice_enabled,
                    self.settings.stt_backend,
                    self.voice_problem or "none",
                )
            return
        self.stt.push_audio(frame)

    async def handle_mic(self, open_: bool) -> None:
        """Open or close the microphone stream."""
        self._state.mic_open = open_
        if open_:
            # Reset the accounting per listening session, so a report after a
            # toggle describes that attempt rather than the session total.
            self._frames_in = 0
            self._audio_seconds = 0.0
            self._peak_in = 0.0
            self._mic_opens += 1
            log.info(
                "microphone opened (#%d); awaiting audio%s",
                self._mic_opens,
                "" if self.stt is not None else " -- but speech recognition is not running",
            )
        else:
            silent = self._peak_in < _SILENCE_PEAK
            log.info(
                "microphone closed (#%d); received %d frame(s), %.2fs of audio, peak %.3f%s",
                self._mic_opens,
                self._frames_in,
                self._audio_seconds,
                self._peak_in,
                " -- the browser sent silence" if silent and self._frames_in else "",
            )
            if self._frames_in == 0:
                log.warning(
                    "no audio arrived for this listening session -- the problem is in "
                    "the browser's capture, not recognition"
                )
            elif silent:
                log.warning(
                    "all %d frame(s) were silent -- capture is running but delivering no "
                    "signal, so a missing transcript is a capture problem",
                    self._frames_in,
                )
        if self.stt is None:
            return
        self.stt.set_suppression(open_ and self._is_speaking())
        self._announce_suppression(open_ and self._is_speaking())
        if not open_ and self._state.interim:
            self._state.interim = ""
            await self.emit(EventKind.INTERIM, text="")

    async def handle_text(self, text: str, *, interrupt: bool = False) -> None:
        """Run a typed turn, exactly as if it had been spoken.

        ``interrupt`` is the Push action. The running turn is stopped and this
        message takes its place rather than waiting behind it. It goes to the
        *front* of the queue *before* the stop, because stopping a turn drains that
        queue on the way out — and a pushed message that queued second would
        otherwise watch the work it interrupted finish first.
        """
        cleaned = text.strip()
        if not cleaned or self._closed:
            return
        if interrupt and self._turn_in_flight():
            self._queue.insert(0, (cleaned, "typed"))
            # Not marked queued: it is going now, and the client should treat it as
            # the current request — the turn it displaces is being cancelled.
            await self.emit(EventKind.USER_TEXT, text=cleaned, source="typed")
            await self.cancel_turn()
            return
        await self._deliver(cleaned, "typed")

    def clear_queue(self) -> None:
        """Drop requests held behind the running turn. Stop means stop."""
        if not self._queue:
            return
        log.info("dropping %d queued request(s)", len(self._queue))
        self._queue.clear()

    async def _deliver(self, text: str, source: str) -> None:
        """Start a turn, or hold the request until the running one finishes.

        Both entry points — typed and spoken — come through here. A person typing
        while the agent works used to be refused with "still working on the
        previous request. Stop it first or wait", and an utterance arriving in the
        same window was dropped with no message at all. Neither is defensible when
        a turn can run for minutes: the request is kept, shown, and runs next.

        The message is emitted now rather than when it starts, because a request
        that vanishes from the composer with nothing on screen reads as lost.
        """
        if self._closed:
            return
        if self._turn_in_flight():
            self._queue.append((text, source))
            log.info("queued a %s request behind the running turn: %r", source, text[:80])
            await self.emit(EventKind.USER_TEXT, text=text, source=source, queued=True)
            return
        await self.emit(EventKind.USER_TEXT, text=text, source=source)
        self._turn = asyncio.create_task(self._run_turn(text), name="agent-turn")

    def _drain_queue(self) -> None:
        """Start the next held request, once the turn that held it has finished.

        Called from the end of `_run_turn`, including the paths where that turn
        was stopped or failed: the queued request is the user's most recent
        intent, and a turn ending for any reason is the moment to honour it.
        """
        if self._closed or not self._queue:
            return
        current = asyncio.current_task()
        if self._turn is not None and not self._turn.done() and self._turn is not current:
            # Something else is running and will drain when it finishes.
            return
        text, source = self._queue.pop(0)
        log.info("starting the queued %s request: %r", source, text[:80])
        self._turn = asyncio.create_task(self._run_turn(text), name="agent-turn")

    async def _on_transcript(self, event: TranscriptEvent) -> None:
        """Handle one transcription update, without blocking the recogniser.

        Updates are accumulated rather than replaced. The two backends report
        differently — Flux sends the transcript for the turn so far, a word-level
        stream sends successive fragments — and overwriting would hand the model
        only the final fragment of a sentence. :meth:`_accumulate` handles
        cumulative and fragment-shaped updates with the same logic.

        An end-of-turn **schedules** delivery rather than performing it, because
        the delivery waits to see whether the sentence continues. Awaiting that
        wait here would stall the recogniser's pump, so the rest of the sentence
        could not arrive — which is exactly the text the wait exists to collect.
        """
        if event.text.strip() and self._speaking:
            # Someone is talking over the agent. This is the only reliable signal
            # that distinguishes a person from the speakers.
            self._speech_since_playback = True
        self._state.interim = self._accumulate(self._state.interim, event.text)
        await self.emit(
            EventKind.INTERIM,
            text=self._state.interim,
            final=event.final,
            end_of_turn=event.is_end_of_turn,
        )

        if not event.is_end_of_turn:
            return

        utterance = self._state.interim.strip()
        self._state.interim = ""
        if not utterance:
            # A turn boundary with nothing transcribable: the user may simply
            # have paused. Starting a turn on silence would answer nothing.
            return

        now = asyncio.get_running_loop().time()
        if not self._utterance_pending:
            # The first boundary of this thought: how long we may wait for the
            # rest of it before answering what we have.
            self._utterance_pending = True
            self._utterance_deadline = (
                now + max(self.settings.stt_merge_hold_ms, self.settings.stt_merge_max_ms) / 1000.0
            )
        # Extra text that arrived while a commit was already scheduled does not
        # need a second wait — the scheduled commit will collect it.
        if self._commit_task is not None and not self._commit_task.done():
            self._utterance_buffer = f"{self._utterance_buffer} {utterance}".strip()
            return

        self._utterance_buffer = utterance
        self._commit_task = asyncio.create_task(self._commit_speech(), name="speech-commit")

    async def _commit_speech(self) -> None:
        """Deliver what was said, once speech has actually stopped.

        A recogniser's end of turn is not always the end of a sentence. On a real
        session "So we could work out a simulation" and "of this." were reported
        1.5 seconds apart as two turns, so the agent answered half a sentence and
        the fragments after it cancelled the turn before it existed.

        The wait is per arrival of *new* text, not a flat delay: once a full
        window passes with nothing further transcribed, the sentence is over and
        waiting longer only delays the answer. That is what keeps two separate
        questions from being run together, which a fixed hold on every utterance
        would do to anyone who pauses between sentences.
        """
        hold = max(0.05, self.settings.stt_merge_hold_ms / 1000.0)
        if self.settings.stt_merge_hold_ms <= 0:
            hold = 0.0
        cap = max(hold, self.settings.stt_merge_max_ms / 1000.0)
        loop = asyncio.get_running_loop()
        started = loop.time()
        seen = self._utterance_buffer

        while hold:
            await asyncio.sleep(hold)
            if self._utterance_buffer == seen:
                break
            seen = self._utterance_buffer
            if loop.time() - started >= cap:
                log.info("held utterance reached its ceiling; delivering it")
                break

        utterance = self._utterance_buffer.strip()
        self._utterance_buffer = ""
        self._utterance_pending = False
        if utterance:
            await self._start_spoken_turn(utterance)

    async def _start_spoken_turn(self, utterance: str) -> None:
        """Begin a turn from what was said aloud."""
        # A new spoken turn: barge in on anything still playing first.
        if self._is_speaking():
            await self.barge_in()
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn
        log.info(
            "utterance from audio (%d frame(s), %.2fs): %r",
            self._frames_in,
            self._audio_seconds,
            utterance[:120],
        )
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
    async def _speak_problem(self, data: dict[str, Any]) -> None:
        """Say aloud that the turn ended badly, or stopped short of finishing.

        A voice-first user is listening, not reading. The error is displayed, but
        silence is indistinguishable from the agent having quietly stopped — which
        is exactly how a turn that exhausted its step budget was reported. The
        screen keeps the detail; the spoken channel gets one short sentence saying
        what happened and what to do.

        The step-budget case is deliberately not phrased as a failure: nothing went
        wrong, the turn simply reached its limit with work still to do, and the
        user can continue it from where it got to.
        """
        if self.tts is None:
            return
        kind = str(data.get("reason") or data.get("kind_detail") or "")
        spoken = {
            "step_limit": (
                "I reached the step limit for one turn, so I've stopped part-way. "
                "Say continue and I'll carry on from here."
            ),
            "no_answer": (
                "I finished that turn without saying anything, which is no use to you. "
                "The work is on screen — ask me again and I'll answer properly."
            ),
            "failed": "That turn ended with an error, so I stopped. The detail is on screen.",
            "llm": "I lost the connection to the model, so that turn stopped.",
            "internal": "Something went wrong part-way through that turn.",
        }.get(kind, "That turn ended before it finished.")
        with contextlib.suppress(Exception):
            await self._speak_chunk(Chunk(ChunkKind.SAY, spoken, final=True))

    async def _speak_progress(self, step: int) -> None:
        """Say that a long turn is still going.

        A turn that makes tool calls for minutes without speaking is
        indistinguishable from an agent that has hung — that is exactly how one was
        reported, after eight minutes and thirty-three tool calls. One short line
        removes the ambiguity; the tool activity is already on screen for anyone
        watching it.

        Deliberately **not** the final utterance of the turn. Marking it final makes
        the synthesiser report that speaking has finished, which releases echo
        suppression while the agent is still working and still going to speak:
        the speaking state flips mid-turn and the agent's own voice is let back in
        through the microphone.
        """
        if self.tts is None:
            return
        with contextlib.suppress(Exception):
            await self._speak_chunk(
                Chunk(ChunkKind.SAY, f"Still working on this, about {step} steps in.", final=False)
            )

    async def _speak_budget(self, step: int, budget: int) -> None:
        """Warn, once, that the step budget for this turn is nearly spent.

        A turn that reaches its limit simply stops, and until this existed the
        stop arrived with no warning at all: the agent went quiet mid-task and the
        user had to guess whether it had finished. Saying so in advance turns a
        surprise into an expected hand-over — and names the way to continue.

        Not final, for the same reason as the progress line: the turn is still
        running and will speak again when it stops.
        """
        if self.tts is None:
            return
        # How many steps are left, counted from the model's own budget rather than
        # a fixed threshold, so a configured SURTITLE_MAX_STEPS is described
        # correctly at any value.
        remaining = max(0, budget - step)
        left = "a few steps" if remaining <= 5 else f"about {remaining} steps"
        with contextlib.suppress(Exception):
            await self._speak_chunk(
                Chunk(
                    ChunkKind.SAY,
                    f"I'm {left} from my limit for one turn. If I stop before "
                    "finishing, say continue and I'll pick up where I left off.",
                    final=False,
                )
            )

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
            system_prompt=self._system_prompt(),
            context_note=self._context_note(),
            repeat_guard=self._repeat_guard,
        )
        loop.set_emitter(self._emit_side_channel)

        history = self._build_history()

        # Per-turn accounting, reset here rather than at the end so that a turn
        # which dies without a `done` still reports what it actually used.
        self._turn_started_at = time.time()
        self._turn_prompt_tokens = 0
        self._turn_completion_tokens = 0
        self._turn_spoken_chars = 0
        if self.store and self.session_id:
            with contextlib.suppress(Exception):
                # The previous turn's ending is no longer the answer to "why does
                # this look stopped", now that a new turn is running.
                self.store.start_turn(self.session_id)

        # Progress narration: a long tool-using turn is otherwise silent, and
        # silence reads as "it stopped". Announced at increasing step counts, and
        # only while the agent has not said anything of its own.
        announced: set[int] = set()
        spoke = False
        # Set once the near-budget warning has been spoken, so it is said once per
        # turn rather than at every step past the threshold.
        warned_budget = False

        async def on_chunk(chunk: Chunk) -> None:
            nonlocal spoke
            if chunk.kind is ChunkKind.SAY and chunk.text.strip():
                spoke = True
            await self._speak_chunk(chunk)

        try:
            async for event in loop.run(history, user_text, on_chunk=on_chunk):
                if event.kind is EventKind.APPROVAL_REQUEST:
                    await self._on_approval_requested(event.data)
                elif event.kind is EventKind.ERROR:
                    await self._speak_problem(event.data)
                elif event.kind is EventKind.DONE:
                    # Written down before it is spoken: the record is what a
                    # reload and the next investigation read, and speaking can
                    # fail on its own.
                    await self._record_turn_end(event.data)
                    if event.data.get("reason") not in (None, "complete"):
                        # A turn that ends for any reason other than finishing says so
                        # aloud, in the same sentence the transcript shows. A turn cut
                        # short used to go quiet: the indicator slid back to "Idle" and
                        # nothing distinguished "done" from "gave up", so the only way
                        # to find out was to ask again.
                        await self._speak_problem(event.data)
                elif event.kind is EventKind.STATE and not spoke:
                    current = int(event.data.get("step") or 0)
                    due = next(
                        (n for n in _PROGRESS_STEPS if n <= current and n not in announced),
                        None,
                    )
                    if due is not None:
                        announced.add(due)
                        await self._speak_progress(current)
                    # Approaching the budget is the one moment where going quiet is
                    # worst: the turn is about to stop with work outstanding, and
                    # the user has no way to know whether to wait or to speak. Say
                    # it once, so the stop that follows is expected.
                    budget = int(getattr(self.settings, "max_steps", 0) or 0)
                    if (
                        not warned_budget
                        and budget > 0
                        and current >= budget * NEAR_BUDGET_FRACTION
                    ):
                        warned_budget = True
                        await self._speak_budget(current, budget)
                await self._emit_or_queue(event)
        except asyncio.CancelledError:
            if self.tts is not None:
                with contextlib.suppress(Exception):
                    await self.tts.barge_in()

            # Record what the turn produced before it was interrupted. Skipping this
            # is what made the agent look like it had forgotten the conversation: the
            # user's message is stored by the loop while the assistant's reply was
            # not, so every interrupted exchange vanished from the history the model
            # receives on the next turn, and the user's question appeared unanswered.
            if self.store and self.session_id:
                with contextlib.suppress(Exception):
                    self.store.add_message(
                        self.session_id,
                        "assistant",
                        loop.partial_text,
                        spoken=loop.partial_spoken or None,
                    )
                    self._rolled_back = True

            with contextlib.suppress(Exception):
                await self.emit(EventKind.STATE, state=SessionState.IDLE.value, reason="stopped")
            if self.store and self.session_id:
                with contextlib.suppress(Exception):
                    # The user stopped it, so this is a fact about the record
                    # rather than something to explain on screen: the client has
                    # no banner for it, deliberately.
                    self.store.record_turn_end(self.session_id, reason="cancelled")
            raise
        except Exception as exc:  # a turn failure must not kill the session
            log.exception("turn failed")
            await self.emit(EventKind.ERROR, message=f"{type(exc).__name__}: {exc}")
        else:
            # The model has finished producing this turn's text. Tell the synthesiser
            # so that once the queued sentences have been spoken it reports the end
            # of speaking, which releases echo suppression.
            #
            # Nothing did this before. `is_speaking` therefore stayed true from the
            # first reply onwards, so `handle_mic` re-armed suppression on every
            # later press of the mic button, every transcript was silently
            # discarded as the agent's own voice, and the microphone looked broken
            # from the second exchange onward — in every browser, with the audio
            # demonstrably arriving and loud.
            if self.tts is not None:
                self.tts.end_of_turn()
        finally:
            # Counters are intentionally left in place: they describe the listening
            # session, and clearing them here is what made a failed second attempt
            # look identical to a failed first one.
            # A request that arrived while this turn ran goes next, whatever ended
            # this one — finished, stopped, or failed.
            self._drain_queue()

    # Instruction files, by conventional location. Root first, then `docs/`,
    # because a project that keeps its orientation material in `docs/` was
    # otherwise invisible: the file that documented how to reach the machines sat
    # in `docs/ACCESS_METHOD.md` and was never read.
    _INSTRUCTION_FILES = (
        "AGENTS.md",
        "CLAUDE.md",
        ".cursorrules",
        "CONTRIBUTING.md",
        "docs/AGENTS.md",
        "docs/SAFETY_RULES.md",
        # The two documents that answer "how do I reach the systems" and "what can
        # I call". Small enough to be resident, and the reason this list is
        # consulted at all.
        "docs/ACCESS_METHOD.md",
        "docs/AGENT_INTERFACE.md",
        "docs/CURRENT_STATE.md",
        "docs/ARCHITECTURE.md",
        "ACCESS_METHOD.md",
        "AGENT_INTERFACE.md",
    )
    # Sized for the *routing* layer, not for the whole documentation set: the
    # project's AGENTS.md plus its safety rules, which are the two things always
    # worth having resident. Everything else is named in the prompt and read on
    # demand.
    #
    # Trying to inject the interface guides as well does not work and is not
    # desirable: in a real project they totalled over 100 KB, so any budget either
    # squeezed out the important short file or pushed the conversation out of the
    # window before the user had said anything. A routing document that points at
    # its own detail is the better arrangement, and most projects already write
    # one.
    _INSTRUCTION_MAX_CHARS = 20000

    def _system_prompt(self) -> str:
        """Build the system prompt, including the project's own instructions.

        A project that documents how to work in it - AGENTS.md, an access guide, a
        current-state note - should not have to hope the agent thinks to read it.
        Loading the routing layer up front means the agent starts primed with the
        project's own conventions, and makes those files a supported way to steer
        it.

        Two deliberate limits, learned from a project whose documentation ran to
        over 100 KB:

        * Only the **routing layer** is resident - the project's AGENTS.md and its
          safety rules. Everything else is named so the agent can read it when the
          task calls for it. Injecting the full interface guides either squeezed out
          the short important file or pushed the conversation out of the window
          before the user had spoken.
        * A file too large for the remaining budget is **skipped, not truncated**,
          because a document cut to a tenth of itself reads as the whole document.
          The one exception is the project's primary instruction file, which is
          always present and cut short if it must be: it carries the project's
          conventions and its accumulated learnings, so its absence would mean
          starting a session knowing nothing. When that happens the file is named
          under "Instructions shown in part" so a fragment is never mistaken for
          the whole.
        """
        from surtitle.core.agent import build_system_prompt

        prompt = build_system_prompt(self.root.name)
        sections: list[str] = []

        # The notebook, the project briefing and the current plan used to be
        # sections here. They are precisely the parts that change between turns,
        # and this prompt is the *head* of every request — so keeping them here
        # invalidated the provider's prefix cache on nearly every turn of a coding
        # session, and a cache miss costs fifty times a hit. They are delivered
        # after the history now: see `_context_note`.

        config = self.project_config
        if config is not None and config.instructions:
            sections.append(
                "## Project instructions (from .surtitle.json)\n" + config.instructions.strip()
            )

        # Candidate documents, most load-bearing first.
        candidates: list[tuple[str, Path]] = []
        for name in self._INSTRUCTION_FILES:
            candidate = self.root / name
            if candidate.is_file():
                candidates.append((name, candidate))
        # A user-global instruction file, so conventions that are not per-project
        # still reach the agent. Loaded last: more specific wins.
        global_file = self.settings.data_dir / "AGENTS.md"
        if global_file.is_file():
            candidates.append(("$SURTITLE_HOME/AGENTS.md", global_file))

        loaded: list[str] = []
        loaded_names: set[str] = set()
        skipped: list[str] = []
        truncated: list[str] = []
        budget = self._INSTRUCTION_MAX_CHARS

        for position, (name, path) in enumerate(candidates):
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text:
                continue
            if len(text) > budget:
                # The project's primary instruction file is always present, cut
                # short if it has to be. It is where conventions and cross-session
                # learnings live, and it is the one file whose absence means the
                # agent starts a session knowing nothing about the project — which
                # is the loss this whole priming stack exists to prevent. Every
                # other file is named rather than shredded, because a document cut
                # to a tenth of itself reads as the whole document.
                if position == 0:
                    truncated.append(name)
                elif len(text) > budget * 2:
                    skipped.append(name)
                    continue
                text = f"{text[:budget]}\n... [truncated; read the file for the rest]"
            budget -= len(text) + len(name)
            loaded.append(f"### {name}\n{text}")
            loaded_names.add(name)
            if budget <= 0:
                break

        if loaded:
            sections.append(
                "## Project instructions\n"
                "This project's own conventions and current state. Follow them, and "
                "prefer them over your assumptions about how the project works. If "
                "they describe how to reach a system, use that path rather than "
                "inventing one. More specific files take precedence over broader "
                "ones.\n\n" + "\n\n".join(loaded)
            )

        if truncated:
            # Being explicit matters more than usual here: the whole reason
            # oversized documents are normally skipped is that a fragment gets
            # mistaken for the whole thing.
            sections.append(
                "## Instructions shown in part\n"
                "These were longer than the space available, so only the start is "
                "above. Read the file itself before relying on it:\n"
                + "\n".join(f"- {name}" for name in truncated)
            )

        # Documentation the agent has not been shown, named so it knows it exists.
        # Built from what was actually injected: a file skipped for size is exactly
        # the one that must still be mentioned, and filtering by candidate name hid
        # the document explaining how to reach the machines.
        unread: list[str] = []
        for directory in (self.root, self.root / "docs"):
            if not directory.is_dir():
                continue
            try:
                entries = sorted(directory.glob("*.md"))
            except OSError:
                continue
            for entry in entries:
                relative = entry.relative_to(self.root).as_posix()
                if relative in loaded_names:
                    continue
                # README files are conventional entry points the agent already knows
                # to consult, and the list stays shorter without them.
                if entry.name.lower().startswith("readme"):
                    continue
                unread.append(relative)

        # Files dropped for size come first: most likely to matter, least likely to
        # be stumbled upon.
        mention = list(dict.fromkeys([*skipped, *unread]))
        if mention:
            sections.append(
                "## Other documentation in this project\n"
                "Not loaded here. **If the task needs to reach a system, a service or "
                "an API, read the access or interface document below before acting** - "
                "it will name the tool, the addressing scheme and any token "
                "requirement, and guessing at those does not work. Otherwise read "
                "whichever is relevant:\n" + "\n".join(f"- {name}" for name in mention[:30])
            )

        # What version control looks like from here. The prompt states the policy
        # — ask before saving work — but only this can say whether git and svn
        # actually exist on this machine, which copy is being used, and whether
        # the project is a working copy at all. Without it the agent offers to
        # commit into a folder that has no repository, or stays silent about
        # version control on a machine where the tools were just installed.
        version_control = self._version_control_section()
        if version_control:
            sections.append(version_control)

        return f"{prompt}\n\n" + "\n\n".join(sections) if sections else prompt

    def _context_note(self) -> str:
        """What the agent should know that changes between turns.

        Delivered as a message *after* the conversation rather than inside the
        system prompt. The system prompt is the head of every request, and
        DeepSeek's context cache matches whole prefixes: rewriting the head
        invalidates everything behind it, at fifty times the cost of a hit. This
        block is the part that would have caused that rewrite — the plan changes
        whenever the agent uses `todo_write`, the notebook whenever it remembers
        something, and the briefing whenever it creates a file in the project root.

        Everything here is still given to the agent on every turn; only its
        position changed.
        """
        sections: list[str] = []

        # Durable facts the agent recorded in earlier sessions. This is what lets
        # knowledge accumulate across conversations.
        from surtitle.tools.environment import project_notes

        notes = project_notes(self.root)
        if notes:
            sections.append(
                "## Project notebook\n"
                "Facts recorded in earlier sessions. Treat these as established "
                "unless they conflict with something you observe now; if a note is "
                "wrong, correct it with the remember tool.\n\n" + notes
            )

        # Orientation, so the first turn is not blind. Capped: this is a signpost,
        # not content.
        try:
            listing = sorted(
                entry.name for entry in self.root.iterdir() if not entry.name.startswith(".")
            )[:40]
        except OSError:
            listing = []
        if listing:
            sections.append(
                f"## Project briefing\nWorking directory: {self.root}\n"
                f"Top level: {', '.join(listing)}"
            )

        # The plan the user is looking at right now.
        #
        # It is rendered in the UI's Plan tab and it survives the end of the turn,
        # so an item left unfinished is a standing claim, in front of the user,
        # that work is outstanding. The only todo tool *writes*, so without this
        # the agent could neither see its own plan nor answer "which item is still
        # unticked?" — and in a real session it could not, when the user asked
        # exactly that. Replayed every turn, so losing the conversation cannot
        # lose the plan with it.
        plan = self.store.list_todos(self.session_id) if self.store is not None else []
        if plan:
            done = sum(1 for item in plan if item["status"] == "completed")
            rows = []
            for item in plan:
                mark = {"completed": "[x]", "in_progress": "[>]"}.get(item["status"], "[ ]")
                rows.append(f"- {mark} {item['content']}")
            open_items = [item["content"] for item in plan if item["status"] != "completed"]
            section = (
                "## Your plan, as the user is looking at it\n"
                f"This is the Plan tab in their window right now ({done}/{len(plan)} "
                "done). It stays on screen after the turn ends, so an unfinished item "
                "reads as work you abandoned — and they may ask you about an item by "
                "name, so this list is the only thing that lets you answer. Keep it "
                "true before you finish: `todo_write` the whole list as items start "
                "and finish, rather than leaving one in progress while you answer "
                "something else.\n\n" + "\n".join(rows)
            )
            if open_items:
                section += (
                    "\n\nNot finished on that list: "
                    + "; ".join(open_items)
                    + ". Either finish them, tick them off, or say plainly that they "
                    "are not done."
                )
            sections.append(section)

        return "\n\n".join(sections)

    def _version_control_section(self) -> str:
        """The version-control facts for this project, or "" when there are none."""
        if self._vcs_section is not None:
            return self._vcs_section

        from surtitle.vcs import provision, repo

        try:
            rows = provision.status(self.settings, verify=False)
            state = repo.detect(self.root, settings=self.settings)
        except Exception:  # priming must never cost the session
            self._vcs_section = ""
            return ""

        available = [row for row in rows if row.available]
        missing = [row.name for row in rows if not row.available]
        lines: list[str] = []

        if available:
            described = ", ".join(
                " ".join(
                    part for part in (row.name, row.version, f"({row.source}, {row.path})") if part
                )
                for row in available
            )
            lines.append(f"Installed and runnable from run_shell and run_python: {described}.")
        if missing:
            lines.append(
                f"Not installed on this machine: {', '.join(missing)}. The user can "
                "install them from the tray (or `surtitle tools install`); do not "
                "assume they are absent from the project's history."
            )
        lines.append(state.describe())
        if state.system == "none" and available:
            lines.append(
                "Nothing here is versioned yet. If the user wants history, ask "
                "before initialising or checking anything out."
            )
        lines.append(
            "Read `vcs_guide` before your first version-control action; use "
            "`vcs_status` for the live state, and `vcs_commit` only after the user "
            "has agreed to a commit and to the level of detail."
        )
        self._vcs_section = "## Version control\n" + "\n".join(f"- {line}" for line in lines)
        return self._vcs_section

    async def _emit_or_queue(self, event: Event) -> None:
        """Send a control-flow event, mirroring only what the UI needs."""
        if event.kind.value.startswith("_"):
            return
        # Keep the session's own idea of where it stands in step with what the
        # turn reports. `_set_state` covers the voice transitions, but the agent's
        # states arrived only as events, so this field went stale: the session
        # still believed it was SPEAKING while the user was being asked to approve
        # something, and the `speaking -> idle` transition that followed then
        # broadcast an idle state that wiped the approval prompt off the screen
        # while the turn went on waiting for an answer.
        if event.kind is EventKind.STATE:
            value = event.data.get("state")
            if isinstance(value, str):
                with contextlib.suppress(ValueError):
                    self._state.state = SessionState(value)
        self._count(event)
        self.events += 1
        event.seq = self.events
        await self._outbox.put(event)

    async def _emit_side_channel(self, event: Event) -> None:
        """Receive thinking and usage events that are not part of the turn stream."""
        self._count(event)
        self.events += 1
        event.seq = self.events
        await self._outbox.put(event)

    def _count(self, event: Event) -> None:
        """Feed the run's usage counters.

        Called from both emit paths because neither sees everything: usage
        blocks arrive on the side channel, tool calls and turn boundaries on the
        turn stream. Counting in the agent loop instead would put the counters
        behind a second call site in a second module for no gain, and counting
        in the browser would lose a turn the user never watched.
        """
        if event.kind is EventKind.USAGE:
            # Accumulated whether or not the run-wide counters are wired up: the
            # turn-end record is written for every turn, including in tests and
            # in a session built without stats.
            self._turn_prompt_tokens += int(event.data.get("prompt_tokens") or 0)
            self._turn_completion_tokens += int(event.data.get("completion_tokens") or 0)
        if self.stats is None:
            return
        if event.kind is EventKind.USAGE:
            self.stats.record_usage(
                event.data,
                price=resolve_price(self.settings.deepseek_model, self.settings),
                peak=is_peak(),
            )
        else:
            self.stats.record_event(event.kind)

    async def _record_turn_end(self, data: dict[str, Any]) -> None:
        """Write down why the turn ended, and say so in the log.

        Both halves were missing, and for the same reason: a turn that stopped
        left a transcript that merely looked unfinished. Nothing logged its
        ending except the step-limit path, and nothing stored it at all, so "it
        just stopped" could not be answered from the record — which is why
        explaining one report needed the database read by hand.
        """
        reason = str(data.get("reason") or "complete")
        steps = int(data.get("steps") or 0)
        detail = str(data.get("detail") or "")
        elapsed = max(0.0, time.time() - self._turn_started_at) if self._turn_started_at else 0.0
        log.info(
            "turn ended: reason=%s steps=%d duration=%.1fs spoken=%d chars tokens=%d/%d%s",
            reason,
            steps,
            elapsed,
            self._turn_spoken_chars,
            self._turn_prompt_tokens,
            self._turn_completion_tokens,
            f" -- {detail}" if detail else "",
        )
        if self.store and self.session_id:
            with contextlib.suppress(Exception):
                self.store.record_turn_end(
                    self.session_id, reason=reason, detail=detail, steps=steps
                )

    def _build_history(self) -> list[ChatMessage]:
        """Rebuild model context from the stored transcript.

        The window is the **newest** ``_HISTORY_LIMIT`` conversational messages,
        and it is counted over the conversation only: reasoning is stored one row
        per step, so counting it against the window pushed the conversation itself
        out of the model's context within a single turn. The failure that caused is
        worth naming, because it does not look like a bug from the outside — the
        agent would re-derive work it had already finished and committed earlier in
        the same session, then report that "somebody" had already done it, because
        its own record of doing it was no longer replayed. See the prompt's
        standing rule to check what it already did: that rule is only usable if the
        record is actually here.

        ``_rolled_back`` marks a turn whose exchange was already written during
        cancellation, so the current user message must not be appended twice.
        """
        messages = self.store.list_messages(
            self.session_id, limit=_HISTORY_LIMIT, roles=("user", "assistant")
        )
        history: list[ChatMessage] = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.content
        ]
        if self._rolled_back:
            self._rolled_back = False
            return history
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
        # Counted here rather than from the loop's own partial text: the loop
        # finishes the turn with a repaired closing line when the model left the
        # last round silent, and that line is synthesised without touching the
        # loop's field. Counting what is actually sent to the synthesiser is the
        # only version of "did the user hear anything" that is true.
        self._turn_spoken_chars += len(chunk.text.strip())
        self.tts.speak(chunk.text, final=chunk.final)

    # --- speaking / barge-in --------------------------------------------
    def _is_speaking(self) -> bool:
        return bool(self.tts and self.tts.is_speaking)

    async def _on_audio_out(self, audio: bytes, sequence: int) -> None:
        """Relay synthesised audio to the browser as a binary frame.

        The clock is stamped as the audio leaves, which is what the suppression
        watchdog measures against: while the agent's voice is still going out,
        suppression is doing its job, and when it has stopped going out for
        longer than this audio could still be playing, suppression is stale.
        """
        self._last_audio_out_at = asyncio.get_running_loop().time()
        self._last_audio_out_seconds = len(audio) / 2 / max(1, self.settings.tts_sample_rate)
        self._suppression_released = False
        await self.send_audio(audio)

    async def _watch_echo_suppression(self) -> None:
        """Lift echo suppression when it outlives the agent's own audio.

        Suppression stops the recogniser transcribing the agent's own voice. It
        is armed when speech starts and released when the synthesiser says it has
        finished — and if that release never comes, every transcript is silently
        discarded for the rest of the session. That is not hypothetical: in a real
        session the counter reported 25 discarded Flux transcripts in one burst,
        including a complete sentence, tens of seconds after playback had stopped.
        From the user's side the microphone simply stops working.

        The bound is measured against the audio actually sent, so an ordinary
        pause between sentences does not release it: only a silence longer than
        the audio that is already in flight could account for.
        """
        margin = max(0, self.settings.echo_suppression_max_ms) / 1000.0
        while not self._closed:
            await asyncio.sleep(0.5)
            if self.stt is None or not self._speaking or self._suppression_released:
                continue
            now = asyncio.get_running_loop().time()
            silent_for = now - self._last_audio_out_at
            if silent_for <= self._last_audio_out_seconds + margin:
                continue
            self._suppression_released = True
            log.warning(
                "echo suppression had outlived the agent's audio by %.1fs "
                "(%d transcript(s) were discarded while it was on); releasing it",
                silent_for - self._last_audio_out_seconds,
                getattr(self.stt, "_suppressed_transcripts", 0),
            )
            self.stt.set_suppression(False)
            self._announce_suppression(False)

    def _announce_suppression(self, suppressed: bool) -> None:
        """Tell the UI whether what the user says can currently reach the agent.

        Discarded transcripts are the one voice failure that leaves no trace on
        screen: the microphone looks open, audio is arriving, and the words
        simply do not appear. In the session this was found in, a complete
        sentence was transcribed and thrown away and nothing said so. The UI shows
        this state so that failure is visible while it is happening.
        """
        self._outbox.put_nowait(
            Event(kind=EventKind.STATE, seq=0, data={"echo_suppressed": bool(suppressed)})
        )

    async def _on_speaking_started(self) -> None:
        """Mark the session as speaking and suppress echo-contaminated finals.

        Suppression is enabled here rather than driven by the client so that it
        cannot drift out of sync with what is actually being played.
        """
        self._set_state(SessionState.SPEAKING)
        self._speaking = True
        self._speech_since_playback = False
        # Start the staleness clock at the moment speech begins.
        #
        # The watchdog below measures silence against the last audio frame *sent*,
        # and during a long think — or simply between turns — that timestamp is
        # minutes old. Without this the watchdog's next tick saw that ancient gap,
        # declared suppression stale, and lifted it about 0.2s after the reply
        # started, leaving the microphone live for the whole of it. The symptom in
        # the log was a warning that suppression "had outlived the agent's audio by
        # 115.6s (0 transcripts were discarded)" — the counter was zero because
        # suppression had not actually been doing anything.
        self._last_audio_out_at = asyncio.get_running_loop().time()
        self._last_audio_out_seconds = 0.0
        self._suppression_released = False

        # Anything the user said while the previous turn was finishing must be
        # handed over BEFORE suppression starts. Suppression exists to stop the
        # agent transcribing its own voice, but a transcript already captured is
        # the user's, and dropping it makes an utterance vanish — which is what
        # happened on a second attempt to speak.
        pending = self._state.interim.strip()
        self._state.interim = ""
        if pending:
            # Was `and not self._turn_in_flight()`, which dropped the utterance
            # silently whenever the agent was still working. It is queued now.
            await self._deliver(pending, "voice")

        if self.stt is not None:
            self.stt.set_suppression(True)
            self._announce_suppression(True)
        log.info("speaking started; echo suppression on")

    def _turn_in_flight(self) -> bool:
        """True while an agent turn is still running."""
        return self._turn is not None and not self._turn.done()

    async def _on_speaking_finished(self) -> None:
        """Return to idle once the last sentence has been synthesised."""
        self._speaking = False
        if self.stt is not None:
            self.stt.set_suppression(False)
            self._announce_suppression(False)
        # Worth a line: while this never ran, echo suppression stayed on for the
        # rest of the session and every later transcript was discarded — which is
        # indistinguishable from a dead microphone, and produced no log output.
        log.info("speaking finished; echo suppression released")
        if self._state.state is SessionState.SPEAKING:
            self._set_state(SessionState.LISTENING if self._state.mic_open else SessionState.IDLE)

    async def barge_in(self) -> None:
        """Stop speaking immediately and return to listening."""
        self._speaking = False
        if self.tts is not None:
            with contextlib.suppress(Exception):
                await self.tts.barge_in()
        if self.stt is not None:
            self.stt.set_suppression(False)
            self._announce_suppression(False)
        self._set_state(SessionState.LISTENING if self._state.mic_open else SessionState.IDLE)

    async def cancel_turn(self, *, require_speech: bool = False) -> None:
        """Stop the running turn, and any speech.

        ``require_speech`` guards the interruption path against the agent hearing
        itself. The client detects loudness, which cannot tell a person from the
        speakers, so only a *transcript* may cancel work in progress. An explicit
        stop passes ``require_speech=False`` and always wins.
        """
        if require_speech and not self._speech_since_playback:
            log.info(
                "ignoring an interruption with no transcribed speech behind it "
                "(the agent's own voice is the likely cause)"
            )
            return
        running = self._turn
        if running is not None and not running.done():
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await running
            # Only clear it if this is still the turn this call stopped. Finishing a
            # turn drains the queue, and that may already have installed the next
            # one — clearing *that* would leave a live turn untracked, so the next
            # stop or push could not reach it.
            if self._turn is running:
                self._turn = None
        else:
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

    async def _on_speed_fallback(self, speed: float) -> None:
        """Deepgram refused the speed, so ask the browser to play at that pace.

        Without this the chosen speed is silently lost and the voice comes out at
        its natural pace, which reads as the voice changing between turns.
        """
        log.info("speech speed %.2f will be applied during playback instead", speed)
        await self.emit(
            EventKind.STATE,
            state=self._state.state.value,
            speech_speed=speed,
            kind_detail="speed_fallback",
        )

    # --- usage -----------------------------------------------------------
    @property
    def state(self) -> SessionState:
        return self._state.state

    @property
    def closed(self) -> bool:
        """True once this session has been torn down and can no longer answer.

        A closed session keeps its object and, until the connection notices, its
        socket: its engines are stopped, `emit` drops every event, and a turn
        started on it runs to completion without reaching the screen or the
        speaker. Reported from a real session as "restarted and it seems broken,
        text and voice", where a page refresh fixed it by building a fresh one.
        """
        return self._closed


class SessionManager:
    """Tracks live sessions so the WebSocket route can clean up reliably."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        # Which connection currently owns each session. A page open in two tabs,
        # or a reconnect, produces a second connection for a conversation that is
        # already live; the newest one owns it.
        self._owner: dict[str, str] = {}
        # Sessions whose connection left while work was still in flight, and when.
        # They are kept so a reload can reclaim the turn instead of destroying it,
        # and swept once they fall idle or the grace period runs out.
        self._orphaned_at: dict[str, float] = {}
        self._sweeper: asyncio.Task[None] | None = None

    async def acquire(self, session: Session, token: str) -> tuple[Session, bool]:
        """Return the session this connection should use.

        Returns ``(session, started_now)``. A reconnect for a conversation that is
        already live reuses the existing session and simply rebinds its transport,
        so an in-flight turn survives.

        This used to register the new session and retire the old one. Two tabs on
        the same conversation then destroyed each other in a loop: the retired
        tab's socket closed, its client reconnected, which retired the other, and
        so on. Every retirement cancelled whatever turn was running, so a spoken
        question was transcribed, sent, and answered into nothing — the reply went
        to a session that had just been closed. That is what "voice is converting
        but no action" was.
        """
        existing = self._sessions.get(session.session_id)
        self._orphaned_at.pop(session.session_id, None)
        if existing is None:
            self._sessions[session.session_id] = session
            self._owner[session.session_id] = token
            return session, True

        existing.rebind(send=session.send, send_audio=session.send_audio)
        self._owner[session.session_id] = token
        return existing, False

    async def release(self, session_id: str, token: str) -> None:
        """Detach a connection, closing the session only if it still owns it.

        A superseded connection finishing its handler must not close the session
        that took over from it.

        Nor may a connection leave with work still running. Closing here cancelled
        the turn, and a browser reload disconnects before it reconnects, so
        reloading mid-turn destroyed the answer the user was waiting for: the new
        connection found no session and started an empty one, while a pending
        approval became unanswerable because the question outlived the only place
        it was ever displayed.
        """
        if self._owner.get(session_id) != token:
            return
        self._owner.pop(session_id, None)
        session = self._sessions.get(session_id)
        if session is not None:
            if session._turn_in_flight():
                self._orphaned_at.setdefault(session_id, asyncio.get_running_loop().time())
                self._ensure_sweeper()
                return
            self._sessions.pop(session_id, None)
            self._orphaned_at.pop(session_id, None)
            # Logged because the alternative was silence: a connection that was
            # still open went on dispatching into the closed session, and the only
            # visible symptom was a conversation that had stopped answering.
            log.info("closing session %s: its connection left with nothing in flight", session_id)
            await session.close()

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_orphans(), name="session-sweeper")

    async def _sweep_orphans(self) -> None:
        """Close sessions left with no connection, once they stop working.

        A stopped turn may still be reclaimed by a reload, so the sweep waits for
        the work to finish; the deadline bounds how long a session that never goes
        idle can hold its microphone and speech sockets open.
        """
        while True:
            await asyncio.sleep(_ORPHAN_SWEEP_SECONDS)
            now = asyncio.get_running_loop().time()
            for session_id, since in list(self._orphaned_at.items()):
                session = self._sessions.get(session_id)
                if session is None or session_id in self._owner:
                    self._orphaned_at.pop(session_id, None)
                    continue
                if session._turn_in_flight() and now - since < _ORPHAN_GRACE_SECONDS:
                    continue
                self._orphaned_at.pop(session_id, None)
                self._sessions.pop(session_id, None)
                log.info("closing orphaned session %s left with no connection", session_id)
                await session.close()

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> None:
        """Close and forget a session outright, whoever owns it.

        Used when the conversation itself goes away — archived or deleted — where
        the point is precisely to stop it listening.
        """
        self._owner.pop(session_id, None)
        self._orphaned_at.pop(session_id, None)
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweeper
            self._sweeper = None
        self._orphaned_at.clear()
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
