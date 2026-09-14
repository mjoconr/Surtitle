"""The agent loop.

One user turn is a loop, not a single request: the model may call tools, read
the results, and continue. Three things make this loop specific to a *voice*
agent rather than a generic tool-calling harness:

1. **Narration before action.** The prompt contract requires a ``<say>`` block
   before tool calls, so the user hears "let me check that" instead of dead air
   while a PDF is parsed. The loop surfaces each spoken chunk as soon as it is
   complete rather than batching at the end of the turn.

2. **The speak layer splits output.** Everything the model writes is fed through
   :class:`~surtitle.core.speak.SpeakParser`, which decides what is audible.
   If the model forgets the tags, :func:`repair_fallback` still produces
   something to say, so a malformed turn is never silent.

3. **Approval is interactive.** A mutating tool call pauses the loop, asks the
   UI, and resumes — or returns a refusal to the model so it can adapt, which is
   far more useful than aborting the turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from surtitle.config import Settings
from surtitle.core.events import Event, EventKind, SessionState
from surtitle.core.speak import Chunk, ChunkKind, SpeakParser, repair_fallback
from surtitle.llm.deepseek import ChatMessage, DeepSeekClient, DeepSeekError, ToolCallDelta
from surtitle.store.db import Store
from surtitle.tools import environment
from surtitle.tools.fs_tools import ToolContext, ToolResult
from surtitle.tools.registry import ToolRegistry, default_registry

__all__ = ["AgentLoop", "ApprovalBroker", "build_system_prompt"]

log = logging.getLogger(__name__)

# Sentinel event kind used internally to hand the assembled assistant message
# back from the completion generator to the turn loop. It is never emitted to
# the UI: the loop consumes it and continues.
_COMPLETION = "_completion"

# How many characters of a tool result are shown to the user in the transcript
# before it is collapsed into an expandable detail.
_DISPLAY_SUMMARY_CHARS = 160


def build_system_prompt(root_name: str) -> str:
    """The prompt that establishes the two-channel output contract.

    This is the single most important string in the project. Without the
    ``<say>``/``<display>`` contract the agent reads its entire output aloud,
    including tables and file paths, which is exactly the behaviour that makes
    voice agents unbearable to use.
    """
    return f"""You are Surtitle, a voice-first agent working inside the user's
project directory "{root_name}". The user is *listening* to you, usually while
doing something else, so what you say has to be worth hearing.

## How to write your replies

Wrap anything meant to be spoken in <say> tags. Everything not inside a <say>
tag is shown on screen but never spoken.

- <say>Keep spoken text to one or two short sentences.</say>
- <display>Put tables, code, file listings, long numbers and paths here.</display>

Rules for good spoken output:
- Never speak a file path, URL, or code aloud. Put those in <display>.
- Never read out a table or a list of more than three items. Say what it means.
- Never read out a number with more than two digits unless it is the point.
- Spell out what matters, not what you did. "Revenue is up eight percent" beats
  "I have completed parsing the spreadsheet and computed the delta".
- Before a slow tool call, say what you are about to do in one short sentence.
  The user should never sit in silence wondering whether you heard them.
- After tool results, say the conclusion. Do not narrate the steps.

Example of a good turn:

<say>Let me open the Q3 report and check the revenue line.</say>
<display>read_file("reports/q3.pdf") -> 42 pages, revenue column parsed as float</display>
<say>Q3 revenue is 1.24 million, up eight percent. I'll build the summary sheet now.</say>
<display>make_spreadsheet("q3-summary.xlsx", ...) -> created, 2 sheets, 14 rows</display>
<say>Done. The summary spreadsheet is in your project folder.</say>

## How to work

**Check what you already did before doing it again.** Your previous turns are in
the conversation, and each one records the work it performed under
`[work this turn]`. If a file has already been read or a command already run,
use that result. Re-reading the same files and re-running the same commands every
turn wastes the user's time and money, and it is the single most common way to be
useless here.

### Never state an assumption as fact

This is the standing rule of this project, not a style preference. Stating a guess
as fact is the most damaging thing you can do here: the user acts on it, and it is
far more expensive to discover later than a moment of uncertainty would have been.

Before you state anything factual, know which of these it is:

- **Checked** — you read it in a file this session, or saw it in a command output.
- **Told** — it came from the user, or from a file stating it.
- **Assumed** — you inferred it, or it is how things usually work.

Only the first two may be stated plainly. Assumptions must be labelled as such and
offered for checking:

> I have not confirmed this, but the pattern in `plant.hosts` suggests 4C-120 is
> on the same bus. Shall I check?

Concretely:

- Do not fill a gap in your knowledge with a plausible value. A machine name, a
  port, a path, a version, a number in a report — if you did not see it, say you
  did not see it.
- Do not describe what a file or command contains before you have opened or run it.
- When a request is ambiguous, ask which one is meant rather than picking the most
  likely reading. One clarifying question is cheaper than doing the wrong work
  well, and cheaper than the user discovering later that you were never sure.
- If you cannot verify something, say what would verify it and offer to do that.
- "I don't know yet" is a complete and acceptable answer. Volume is not a
  substitute for knowing.

**The workspace is authoritative; your memory of it is not.** Tool results and
file contents show the current state. Anything you concluded earlier may since have
changed, and anything you assumed earlier was never established. When they conflict,
trust what you observe now.

**Resolve what you can by looking, and only ask about what you cannot.** Do not ask
the user where something lives or how it currently behaves when you can find out by
reading, searching or running a command. Asking is for choices that are theirs to
make, and for genuine ambiguity that inspection cannot settle. One well-aimed
question is worth more than a confident guess, but a question you could have
answered yourself wastes their time.

**Work in an order, and say what it is.** For anything beyond a single lookup:

1. Understand the question well enough to know what evidence would answer it.
2. Find that evidence — the specific file, command or tool that carries it.
3. Read the evidence before drawing a conclusion.
4. Answer, and name what you based it on.

Do not narrate this as a plan and then skip it. Two or three tool calls that
establish the facts beat ten that circle around them.

**Read the result of every command, including how it exited.** A command that
failed and one that printed nothing look alike if you only skim the output.

**Do not re-derive what is already established.** If earlier in this conversation
you found that a machine is down, or a value, or where a file lives, carry that
forward rather than rediscovering it.

- Read the relevant files before answering questions about them. Do not guess
  at the contents of a document you have not opened.
- Prefer the dedicated tools over writing code: make_pdf, make_spreadsheet and
  make_chart produce correctly formatted files. Use run_python for anything else.
- When a task needs several steps, do them in order and keep the user informed
  with brief spoken updates rather than a running commentary.
- If something fails, say so plainly in a <say> block and explain the next
  option. Do not apologise more than once.
- Finish every turn with a <say> block, even if it is only "Done." Silence makes
  the user think you have stopped listening.

Your project root is the current working directory for every tool call. You
cannot read or write outside it, and you should not try: if the user asks for a
file elsewhere, explain that it is outside the project."""


@dataclass(slots=True)
class _PendingApproval:
    """One outstanding approval request."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    future: asyncio.Future[tuple[bool, bool]]
    """Resolves to (allowed, remember) — remember means trust this tool."""


class ApprovalBroker:
    """Coordinates tool approvals between the agent loop and the UI.

    Registration and waiting are deliberately two steps. The loop registers a
    request *before* it announces it, so a UI that answers immediately still
    finds something waiting. :meth:`decision` additionally tolerates an answer
    that arrived before the wait began, which removes the ordering hazard
    entirely rather than relying on event-loop timing.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingApproval] = {}
        # Answers that arrived before anyone awaited them, kept so a decision is
        # never lost to a race.
        self._decisions: dict[str, tuple[bool, bool]] = {}
        self._auto_approved: set[str] = set()

    def trust(self, tool_names: list[str]) -> None:
        """Pre-approve tools for this session (the "always allow" choice)."""
        self._auto_approved.update(tool_names)

    def is_trusted(self, tool_name: str) -> bool:
        return tool_name in self._auto_approved

    @property
    def trusted(self) -> list[str]:
        return sorted(self._auto_approved)

    def register(
        self, call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> asyncio.Future[tuple[bool, bool]]:
        """Create the waiter for a request that is about to be announced."""
        future: asyncio.Future[tuple[bool, bool]] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = _PendingApproval(
            call_id=call_id, tool_name=tool_name, arguments=arguments, future=future
        )
        return future

    # Kept for backwards compatibility with callers that only need the future.
    def request(
        self, call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> asyncio.Future[tuple[bool, bool]]:
        """Alias for :meth:`register`."""
        return self.register(call_id, tool_name, arguments)

    def resolve(self, call_id: str, *, allowed: bool, remember: bool = False) -> bool:
        """Answer a request.

        Returns ``False`` only when the answer was already recorded, which makes
        a duplicate click harmless instead of an error.
        """
        pending = self._pending.pop(call_id, None)
        if pending is None:
            # Either already answered, or the answer beat the wait. Recording it
            # is what makes the second case safe.
            if call_id in self._decisions:
                return False
            self._decisions[call_id] = (allowed, remember)
            if remember and allowed:
                self._auto_approved.add(call_id)
            return False

        if remember and allowed:
            self._auto_approved.add(pending.tool_name)
        self._decisions[call_id] = (allowed, remember)
        if not pending.future.done():
            pending.future.set_result((allowed, remember))
        return True

    async def decision(
        self, call_id: str, future: asyncio.Future[tuple[bool, bool]]
    ) -> tuple[bool, bool]:
        """Await the user's answer, cleaning up if the turn is cancelled."""
        if call_id in self._decisions:
            return self._decisions.pop(call_id)
        try:
            return await future
        except asyncio.CancelledError:
            self._pending.pop(call_id, None)
            if not future.done():
                future.cancel()
            raise

    def cancel_all(self) -> None:
        """Reject everything outstanding. Used when a turn is cancelled."""
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result((False, False))
        self._pending.clear()

    @property
    def pending_ids(self) -> list[str]:
        return list(self._pending)


# Consecutive identical calls tolerated before the guard steps in, then how often it
# repeats itself. Mirrors the escalating thresholds DSH uses, because a model that
# ignores the first nudge often needs a firmer one.
_REPEAT_THRESHOLDS = (3, 5, 8)
# Result previews are re-shown instead of the file listing, so the model has the
# content it is asking for and no reason to ask again.
_REPEAT_PREVIEW_CHARS = 300


@dataclass(slots=True)
class _CallSignature:
    """Identity of a tool call: the tool and its arguments, order-independent."""

    name: str
    arguments: str

    @classmethod
    def of(cls, name: str, arguments: dict[str, Any]) -> _CallSignature:
        try:
            canonical = json.dumps(arguments, sort_keys=True, default=str)
        except (TypeError, ValueError):
            canonical = str(arguments)
        return cls(name=name, arguments=canonical)


class RepeatCallGuard:
    """Refuse consecutive identical tool calls, and remind the model what it has.

    A model that repeats a call is stuck: the result is not going to change, so
    another call cannot make progress. Observed in practice, the same files were
    read and the same commands re-run on every turn with nothing stopping it.

    DSH solves this with a counter and an injected reminder rather than with prompt
    advice, which is the right shape: advice can be ignored, and this cannot be
    ignored for long because the repeated call returns no new information.

    Thresholds escalate so the first nudge is gentle and a persistent repeat gets a
    firm instruction to stop.
    """

    def __init__(self) -> None:
        self._last: _CallSignature | None = None
        self._count = 0
        self._last_preview = ""
        self.blocked = 0

    def check(self, name: str, arguments: dict[str, Any]) -> str | None:
        """Return a reminder to show instead of running this call, or ``None``.

        ``None`` means the call is allowed. A returned string means the call is
        refused and the string is fed back as the tool result.
        """
        signature = _CallSignature.of(name, arguments)
        if signature == self._last:
            self._count += 1
        else:
            self._last = signature
            self._count = 1
            self._last_preview = ""
        return None

    def observe_result(self, preview: str) -> None:
        """Remember the latest result so a repeat can be answered with it."""
        self._last_preview = preview

    def reminder(self) -> str | None:
        """The refusal text once a call has been repeated to the first threshold.

        From that point *every* further identical call is refused, not just the ones
        landing exactly on a threshold. Allowing the calls in between would let the
        model slip a few more repeats through and make no more progress than before.
        """
        if self._count < _REPEAT_THRESHOLDS[0]:
            return None
        self.blocked += 1
        preview = self._last_preview[: _REPEAT_PREVIEW_CHARS * 4]
        detail = f"\n\nThe result you already have:\n{preview}" if preview else ""
        base = (
            f"Repeated tool call refused: {self._last.name if self._last else 'this tool'} has "
            f"now been called {self._count} times in a row with identical arguments, and the "
            "result cannot change."
        )
        if self._count >= _REPEAT_THRESHOLDS[-1]:
            # Repeated well past the point of usefulness: the instruction is blunt.

            instruction = (
                "Stop repeating this call. Either use the result you already have, take a "
                "clearly different action, or tell the user what you cannot determine."
            )
        else:
            instruction = (
                "Inspect the result you already have and either use it, take a different "
                "action, or finish. Do not call this tool again with these arguments."
            )
        return f"{base} {instruction}{detail}"


@dataclass(slots=True)
class _TurnState:
    """Mutable bookkeeping for one user turn."""

    messages: list[ChatMessage] = field(default_factory=list)
    step: int = 0
    assistant_text: list[str] = field(default_factory=list)
    spoken_text: list[str] = field(default_factory=list)
    # One line per tool call, so the transcript records the work as well as the
    # answer. Without this the model rebuilds history from bare prose, cannot see
    # that it already read a file or ran a command, and re-does everything on every
    # turn — which is exactly what happened in practice.
    actions: list[str] = field(default_factory=list)


class AgentLoop:
    """Runs one user turn against DeepSeek, emitting events as it goes."""

    def __init__(
        self,
        settings: Settings,
        *,
        root: Any,
        project_id: str = "",
        session_id: str = "",
        store: Store | None = None,
        registry: ToolRegistry | None = None,
        approvals: ApprovalBroker | None = None,
        client: DeepSeekClient | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.settings = settings
        self.root = root
        self.project_id = project_id
        self.session_id = session_id
        self.store = store
        self.registry = registry or default_registry()
        self.approvals = approvals or ApprovalBroker()
        self._client = client
        self._owns_client = client is None
        self.system_prompt = system_prompt or build_system_prompt(root.name)
        self._seq = 0
        self._cancelled = asyncio.Event()
        # Guards against a model that gets stuck calling the same tool with the
        # same arguments, which cannot make progress.
        self._repeat_guard = RepeatCallGuard()
        # What this turn has produced so far. The session records these when a turn
        # is interrupted, so a cancelled exchange is not lost from history.
        self.partial_text: str = ""
        self.partial_spoken: str = ""
        # Side-channel emitter for events that are informative rather than
        # control-flow (thinking, usage). Set by the session.
        self._emitter: Callable[[Event], Awaitable[None]] | None = None

    # --- event plumbing --------------------------------------------------
    def _event(self, kind: EventKind, **data: Any) -> Event:
        self._seq += 1
        return Event(kind=kind, seq=self._seq, data=data)

    def cancel(self) -> None:
        """Ask the turn to stop at the next safe point."""
        self._cancelled.set()
        self.approvals.cancel_all()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    async def _client_or_create(self) -> DeepSeekClient:
        if self._client is None:
            self._client = DeepSeekClient(self.settings)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # --- main entry point ------------------------------------------------
    async def run(
        self,
        history: list[ChatMessage],
        user_text: str,
        *,
        on_chunk: Callable[[Chunk], Awaitable[None]] | None = None,
    ) -> AsyncIterator[Event]:
        """Execute one turn, yielding events.

        ``on_chunk`` is called for every parsed chunk as it is produced, which is
        how the session forwards spoken text to text-to-speech without waiting
        for the turn to finish. It is a callback rather than a yielded event
        because audio must start streaming while the loop is still running.
        """
        state = _TurnState(messages=[*history, {"role": "user", "content": user_text}])
        self.partial_text = ""
        self.partial_spoken = ""
        self._repeat_guard = RepeatCallGuard()
        client = await self._client_or_create()

        if self.store and self.session_id:
            self.store.add_message(self.session_id, "user", user_text)
            # Name the conversation from its first user message, which is what
            # makes the session list readable without spending a model call.
            existing = self.store.get_session(self.session_id)
            if existing is not None and existing.title == "New conversation":
                self.store.touch_session(self.session_id, title=_title_from(user_text))
            else:
                self.store.touch_session(self.session_id)

        try:
            while state.step < self.settings.max_steps:
                if self._cancelled.is_set():
                    yield self._event(
                        EventKind.STATE, state=SessionState.IDLE.value, reason="cancelled"
                    )
                    return

                state.step += 1
                yield self._event(
                    EventKind.STATE, state=SessionState.THINKING.value, step=state.step
                )

                assistant_message: ChatMessage | None = None
                tool_calls: list[ToolCallDelta] = []
                async for event in self._stream_completion(client, state, on_chunk=on_chunk):
                    if event.kind == _COMPLETION:
                        assistant_message = event.data.get("assistant_message")
                        tool_calls = event.data.get("tool_calls") or []
                        continue
                    yield event

                if assistant_message is None:  # pragma: no cover - defensive
                    raise RuntimeError("completion stream ended without a result")

                if tool_calls:
                    state.messages.append(assistant_message)
                    async for event in self._run_tools(tool_calls, state, on_chunk=on_chunk):
                        yield event
                    if self._cancelled.is_set():
                        yield self._event(
                            EventKind.STATE, state=SessionState.IDLE.value, reason="cancelled"
                        )
                        return
                    continue

                # No tool calls: the turn is finished.
                if self.store and self.session_id:
                    self.store.add_message(
                        self.session_id,
                        "assistant",
                        _with_actions("".join(state.assistant_text), state.actions),
                        spoken=" ".join(state.spoken_text) or None,
                    )
                yield self._event(EventKind.DONE, steps=state.step)
                return

            # Step cap reached.
            yield self._event(
                EventKind.ERROR,
                message=(
                    f"Stopped after {self.settings.max_steps} steps without finishing. "
                    "The task may be too broad — try asking for a smaller piece of it."
                ),
                kind_detail="step_limit",
            )
            yield self._event(EventKind.DONE, steps=state.step, truncated=True)

        except DeepSeekError as exc:
            yield self._event(EventKind.ERROR, message=str(exc), kind_detail="llm")
            yield self._event(EventKind.DONE, steps=state.step, failed=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a turn failure is reported, not raised
            log.exception("agent turn failed")
            yield self._event(
                EventKind.ERROR,
                message=f"Something went wrong during that turn: {type(exc).__name__}: {exc}",
                kind_detail="internal",
            )
            yield self._event(EventKind.DONE, steps=state.step, failed=True)

    # --- one completion --------------------------------------------------
    async def _stream_completion(
        self,
        client: DeepSeekClient,
        state: _TurnState,
        *,
        on_chunk: Callable[[Chunk], Awaitable[None]] | None,
    ) -> AsyncIterator[Event]:
        """Stream one completion, forwarding text through the speak layer.

        Yields UI events as they happen, then a single ``_COMPLETION`` sentinel
        carrying the assembled assistant message and completed tool calls. Using
        a sentinel keeps every event on one stream, so ordering is preserved
        without the caller having to correlate two channels.
        """
        parser = SpeakParser()
        calls: dict[int, ToolCallDelta] = {}
        text_parts: list[str] = []

        messages: list[ChatMessage] = [{"role": "system", "content": self.system_prompt}]
        messages.extend(state.messages)

        async for stream_event in client.stream(messages, tools=self.registry.to_openai_tools()):
            if stream_event.kind == "reasoning" and stream_event.text:
                # Thinking is streamed for transparency, never spoken and never
                # mixed into the visible answer.
                await self._emit(self._event(EventKind.THINKING, text=stream_event.text))
                continue

            if stream_event.kind == "text" and stream_event.text:
                text_parts.append(stream_event.text)
                for chunk in parser.feed(stream_event.text):
                    async for event in self._handle_chunk(chunk, state, on_chunk):
                        yield event

            elif stream_event.kind == "tool_call" and stream_event.tool_call is not None:
                calls[stream_event.tool_call.index] = stream_event.tool_call

            elif stream_event.kind == "usage" and stream_event.usage is not None:
                await self._emit(self._event(EventKind.USAGE, **stream_event.usage.to_dict()))

        # Flush whatever the parser is still holding.
        for chunk in parser.finish():
            async for event in self._handle_chunk(chunk, state, on_chunk):
                yield event

        # Guarantee the turn is not silent when the model ignored the contract.
        if not state.spoken_text:
            fallback = repair_fallback("".join(text_parts))
            if fallback:
                state.spoken_text.append(fallback)
                if on_chunk is not None:
                    await on_chunk(Chunk(ChunkKind.SAY, fallback, final=True))
                yield self._event(EventKind.SAY, text=fallback, final=True)

        assistant_message: ChatMessage = {
            "role": "assistant",
            "content": "".join(text_parts) or None,
        }
        ordered = [calls[index] for index in sorted(calls)]
        if ordered:
            assistant_message["tool_calls"] = [call.to_message_dict() for call in ordered]

        yield Event(
            kind=_COMPLETION,
            seq=0,
            data={"assistant_message": assistant_message, "tool_calls": ordered},
        )

    async def _handle_chunk(
        self,
        chunk: Chunk,
        state: _TurnState,
        on_chunk: Callable[[Chunk], Awaitable[None]] | None,
    ) -> AsyncIterator[Event]:
        """Route one parsed chunk to text-to-speech and to the transcript."""
        if chunk.kind is ChunkKind.SAY:
            if not chunk.text:
                return
            state.spoken_text.append(chunk.text)
            self.partial_spoken = " ".join(state.spoken_text)
            # Audio starts here, while the loop is still running: this is what
            # makes the reply feel immediate rather than batched.
            if on_chunk is not None:
                await on_chunk(chunk)
            yield self._event(EventKind.SAY, text=chunk.text, final=chunk.final)
            return

        if not chunk.text:
            return
        state.assistant_text.append(chunk.text)
        self.partial_text = "".join(state.assistant_text)
        yield self._event(EventKind.AGENT_TEXT, text=chunk.text, final=chunk.final)

    # --- tools -----------------------------------------------------------
    async def _run_tools(
        self,
        calls: list[ToolCallDelta],
        state: _TurnState,
        *,
        on_chunk: Callable[[Chunk], Awaitable[None]] | None,
    ) -> AsyncIterator[Event]:
        """Execute tool calls, honouring the approval gate."""
        for call in calls:
            if self._cancelled.is_set():
                return

            arguments, parse_error = call.parsed_arguments()
            if parse_error is not None:
                # Tell the model its own output was malformed; it usually fixes it.
                result = ToolResult(
                    ok=False,
                    error=(
                        f"Could not run {call.name}: {parse_error}. "
                        "Emit the arguments as a single valid JSON object."
                    ),
                )
                state.messages.append(self._tool_message(call, result))
                yield self._event(
                    EventKind.TOOL_RESULT,
                    call_id=self._call_id(call),
                    name=call.name,
                    ok=False,
                    display=f"{call.name}: invalid arguments",
                    error=result.error,
                )
                continue

            assert arguments is not None

            # Refuse a call already made repeatedly with identical arguments: the
            # result cannot change, so another call cannot make progress. The
            # reminder carries the result already obtained, so the model has what it
            # was asking for and a concrete reason to do something different.
            if self._repeat_guard.check(call.name, arguments) is None:
                repeated = self._repeat_guard.reminder()
                if repeated is not None:
                    state.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": self._call_id(call),
                            "name": call.name,
                            "content": repeated,
                        }
                    )
                    state.actions.append(f"{call.name} - refused as a repeated call")
                    yield self._event(
                        EventKind.TOOL_RESULT,
                        call_id=self._call_id(call),
                        name=call.name,
                        ok=False,
                        display=f"{call.name} refused: repeated identical call",
                        error=repeated.splitlines()[0],
                    )
                    continue

            if self._needs_approval(call.name, arguments):
                call_id = self._call_id(call)
                # Register the request BEFORE announcing it. The UI can answer as
                # soon as it sees the event, so creating the waiter afterwards
                # would let a fast answer arrive before anything was waiting for
                # it, and the turn would hang forever.
                decision = self.approvals.register(call_id, call.name, arguments)

                yield self._event(EventKind.STATE, state=SessionState.AWAITING_APPROVAL.value)
                yield self._event(
                    EventKind.APPROVAL_REQUEST,
                    call_id=call_id,
                    name=call.name,
                    summary=self.registry.summary_for(call.name),
                    arguments=_redact_arguments(arguments),
                    mutating=self.registry.is_mutating(call.name),
                )

                allowed, remember = await self.approvals.decision(call_id, decision)
                if allowed and remember and call.name == "install_packages":
                    # Record the approved set, so installing the same packages
                    # again (after a fresh clone, say) does not ask a second time.
                    with contextlib.suppress(Exception):
                        environment.remember_requirements(
                            self.root, [str(p) for p in arguments.get("packages", [])]
                        )
                if not allowed:
                    result = ToolResult(
                        ok=False,
                        error=(
                            "The user declined this action. Do not retry it. "
                            "Ask what they would prefer, or suggest an alternative."
                        ),
                        display=f"{call.name} declined",
                    )
                    state.messages.append(self._tool_message(call, result))
                    state.actions.append(f"{call.name} — declined by the user")
                    yield self._event(
                        EventKind.TOOL_RESULT,
                        call_id=self._call_id(call),
                        name=call.name,
                        ok=False,
                        display=f"{call.name} declined by user",
                        declined=True,
                    )
                    if self.store and self.session_id:
                        self.store.add_tool_call(
                            self.session_id,
                            call.name,
                            arguments,
                            step=state.step,
                            ok=False,
                            approved=False,
                        )
                    continue

            yield self._event(
                EventKind.TOOL_CALL,
                call_id=self._call_id(call),
                name=call.name,
                arguments=_redact_arguments(arguments),
            )
            yield self._event(EventKind.STATE, state=SessionState.TOOL.value, tool=call.name)

            started = time.monotonic()
            result = await self.registry.dispatch(
                call.name, ToolContext(root=self.root, session_id=self.session_id), arguments
            )
            duration_ms = int((time.monotonic() - started) * 1000)

            if self.store and self.session_id:
                self.store.add_tool_call(
                    self.session_id,
                    call.name,
                    arguments,
                    step=state.step,
                    result=(result.display or result.error or "")[:1000],
                    ok=result.ok,
                    approved=True,
                    duration_ms=duration_ms,
                )

            state.messages.append(self._tool_message(call, result))
            state.actions.append(_action_line(call.name, arguments, result))
            self._repeat_guard.observe_result(result.display or result.error or "")

            yield self._event(
                EventKind.TOOL_RESULT,
                call_id=self._call_id(call),
                name=call.name,
                ok=result.ok,
                display=result.display,
                error=result.error,
                duration_ms=duration_ms,
                artifacts=result.artifacts or [],
                truncated=result.truncated,
            )

            for artifact in result.artifacts or []:
                yield self._event(EventKind.ARTIFACT, path=artifact, tool=call.name)

    def _needs_approval(self, tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
        """Decide whether this call needs the user's approval.

        The registry applies any tool-specific narrowing, so ``install_packages``
        only asks about packages this project has not already approved.
        """
        if not self.registry.requires_approval(tool_name, arguments):
            return False
        return not self.approvals.is_trusted(tool_name)

    @staticmethod
    def _call_id(call: ToolCallDelta) -> str:
        return call.id or f"call_{call.index}"

    @staticmethod
    def _tool_message(call: ToolCallDelta, result: ToolResult) -> ChatMessage:
        return {
            "role": "tool",
            "tool_call_id": call.id or f"call_{call.index}",
            "name": call.name,
            "content": result.to_model_payload(),
        }

    async def _emit(self, event: Event) -> None:
        """Send an event to the side channel, if one is registered."""
        if self._emitter is not None:
            await self._emitter(event)

    def set_emitter(self, emitter: Callable[[Event], Awaitable[None]] | None) -> None:
        """Register a side-channel emitter for events not yielded to the caller."""
        self._emitter = emitter


def _title_from(user_text: str, *, limit: int = 60) -> str:
    """Derive a short session title from the first user message."""
    cleaned = " ".join(user_text.split())
    if not cleaned:
        return "New conversation"
    if len(cleaned) <= limit:
        return cleaned
    # Prefer cutting at a word boundary so the title does not end mid-word.
    truncated = cleaned[:limit].rsplit(" ", 1)[0]
    return f"{truncated or cleaned[:limit]}…"


def _action_line(name: str, arguments: dict[str, Any], result: ToolResult) -> str:
    """One compact line describing a tool call, for the persisted transcript.

    Deliberately terse: it is replayed into the model's context on every later
    turn, so it has to earn its tokens. The value is that the model can see it has
    *already* looked somewhere or run something, which is what stops it repeating
    the same reads and commands turn after turn.
    """
    target = ""
    for key in ("path", "pattern", "command", "source"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            target = " ".join(value.split())
            break
    if len(target) > 100:
        target = f"{target[:100]}…"

    status = result.display or ("ok" if result.ok else (result.error or "failed"))
    status = " ".join(str(status).split())
    if len(status) > 100:
        status = f"{status[:100]}…"

    if not result.ok:
        status = f"FAILED: {status}"
    return f"{name}({target}) -> {status}" if target else f"{name} -> {status}"


def _with_actions(text: str, actions: list[str]) -> str:
    """Append the turn's tool activity to its answer.

    Stored with the answer rather than as separate messages because it is context
    for the model, not a conversational turn: the transcript should read as
    work-then-answer, and this is what the model sees when history is rebuilt.
    """
    if not actions:
        return text
    listing = "\n".join(f"- {action}" for action in actions)
    block = f"[work this turn]\n{listing}"
    return f"{text}\n\n{block}" if text.strip() else block


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Shrink tool arguments for display and storage.

    Large payloads are replaced with a short preview so the transcript stays
    readable and the database does not grow without bound.
    """
    redacted: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > _DISPLAY_SUMMARY_CHARS:
            redacted[key] = (
                f"{value[:_DISPLAY_SUMMARY_CHARS]}… (+{len(value) - _DISPLAY_SUMMARY_CHARS} chars)"
            )
        elif isinstance(value, (list, dict)):
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            if len(encoded) > _DISPLAY_SUMMARY_CHARS:
                redacted[key] = f"<{len(value)} items, {len(encoded)} chars>"
            else:
                redacted[key] = value
        else:
            redacted[key] = value
    return redacted
