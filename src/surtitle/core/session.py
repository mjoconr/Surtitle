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
    stt: SpeechToText | None = None
    tts: TextToSpeech | None = None
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
    # Set when an interrupted turn was already written to the transcript.
    _rolled_back: bool = False
    registry: Any = None
    mcp_manager: Any = None
    project_config: Any = None

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
                on_speed_fallback=self._on_speed_fallback,
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

        log.info(
            "session ready: id=%s project=%s root=%s voice=%s",
            self.session_id,
            self.project_id,
            self.root,
            self.settings.voice_enabled and self.tts is not None,
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
            voice_enabled=self.settings.voice_enabled and self.tts is not None,
            model=self.settings.deepseek_model,
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
            # voice is disabled or the key is missing, not that capture failed.
            if self._frames_in == 1:
                log.warning(
                    "audio arriving but speech recognition is not running "
                    "(voice_enabled=%s, key=%s)",
                    self.settings.voice_enabled,
                    bool(self.settings.deepgram_key()),
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
        """Say aloud that the turn ended badly.

        A voice-first user is listening, not reading. The error is displayed, but
        silence is indistinguishable from the agent having quietly stopped — which
        is exactly how a turn that exhausted its step budget was reported. The
        screen keeps the detail; the spoken channel gets one short sentence saying
        what happened and what to do.
        """
        if self.tts is None:
            return
        spoken = {
            "step_limit": (
                "I ran out of steps before finishing that. Ask me to carry on, "
                "or give me a smaller piece of it."
            ),
            "llm": "I lost the connection to the model, so that turn stopped.",
            "internal": "Something went wrong part-way through that turn.",
        }.get(str(data.get("kind_detail") or ""), "That turn ended before it finished.")
        with contextlib.suppress(Exception):
            await self._speak_chunk(Chunk(ChunkKind.SAY, spoken, final=True))

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
        )
        loop.set_emitter(self._emit_side_channel)

        history = self._build_history()

        try:
            async for event in loop.run(history, user_text, on_chunk=self._speak_chunk):
                if event.kind is EventKind.APPROVAL_REQUEST:
                    await self._on_approval_requested(event.data)
                elif event.kind is EventKind.ERROR:
                    await self._speak_problem(event.data)
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
            pass

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
        * A file too large for the remaining budget is **skipped, not truncated**.
          A document cut to a tenth of itself reads as the whole document, which is
          worse than not showing it at all.
        """
        from surtitle.core.agent import build_system_prompt

        prompt = build_system_prompt(self.root.name)
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
        budget = self._INSTRUCTION_MAX_CHARS

        for name, path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text:
                continue
            if len(text) > budget:
                if len(text) > budget * 2:
                    # Showing half a document invites the agent to treat a fragment
                    # as the whole thing, which is worse than not showing it. Name
                    # it instead so it is read deliberately.
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

        return f"{prompt}\n\n" + "\n\n".join(sections) if sections else prompt

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
        """Rebuild model context from the stored transcript.

        ``_rolled_back`` marks a turn whose exchange was already written during
        cancellation, so the current user message must not be appended twice.
        """
        if self._rolled_back:
            self._rolled_back = False
            messages = self.store.list_messages(self.session_id, limit=_HISTORY_LIMIT)
            return [
                {"role": message.role, "content": message.content}
                for message in messages
                if message.role in {"user", "assistant"} and message.content
            ]
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
        self._speaking = True
        self._speech_since_playback = False

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
        log.info("speaking started; echo suppression on")

    def _turn_in_flight(self) -> bool:
        """True while an agent turn is still running."""
        return self._turn is not None and not self._turn.done()

    async def _on_speaking_finished(self) -> None:
        """Return to idle once the last sentence has been synthesised."""
        self._speaking = False
        if self.stt is not None:
            self.stt.set_suppression(False)
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


class SessionManager:
    """Tracks live sessions so the WebSocket route can clean up reliably."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        # Which connection currently owns each session. A page open in two tabs,
        # or a reconnect, produces a second connection for a conversation that is
        # already live; the newest one owns it.
        self._owner: dict[str, str] = {}

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
        """
        if self._owner.get(session_id) != token:
            return
        self._owner.pop(session_id, None)
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> None:
        """Close and forget a session outright, whoever owns it.

        Used when the conversation itself goes away — archived or deleted — where
        the point is precisely to stop it listening.
        """
        self._owner.pop(session_id, None)
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
