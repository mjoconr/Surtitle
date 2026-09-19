"""The process view: what the agent did, and why it stopped.

Reported from a real release session: the transcript showed nothing but `run_*`
rows. The reasoning that explained them was in a different panel, truncated to a
trailing fragment, unordered relative to the commands it produced, and gone
entirely once the conversation was reopened.

These tests cover the three things that fix it — the reasoning is stored per
step, the plan is stored per conversation, and a turn that stops says why — plus
the wire contract the browser needs to group any of it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from surtitle.config import Settings
from surtitle.core.agent import PROGRESS_STEPS, AgentLoop, age_work_log
from surtitle.core.events import EventKind
from surtitle.core.session import Session
from surtitle.llm.deepseek import StreamEvent, ToolCallDelta, Usage
from surtitle.store.db import REASONING_ROLE, Store
from surtitle.tools.fs_tools import ToolContext
from surtitle.tools.registry import TODO_TOOL, ToolRegistry, default_tool_list


class FakeClient:
    """A model that replays a fixed script, one stream per call."""

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ):
        self.calls.append({"messages": list(messages), "tools": tools})
        script = self.scripts[min(len(self.calls) - 1, len(self.scripts) - 1)]
        for event in script:
            yield event

    async def aclose(self) -> None:
        return None


def thinking_script(text: str, *, call: tuple[str, dict[str, Any]] | None = None):
    """A model round that thinks, then either calls a tool or finishes."""
    events = [StreamEvent(kind="reasoning", text=text)]
    if call is not None:
        name, arguments = call
        events.append(
            StreamEvent(
                kind="tool_call",
                tool_call=ToolCallDelta(
                    index=0, id="c1", name=name, arguments=json.dumps(arguments)
                ),
            )
        )
        events.append(StreamEvent(kind="done", finish_reason="tool_calls"))
    else:
        events.append(StreamEvent(kind="text", text="<say>Done.</say>"))
        events.append(StreamEvent(kind="done", finish_reason="stop"))
    events.append(StreamEvent(kind="usage", usage=Usage()))
    return events


def speaking_script(text: str, *, call: tuple[str, dict[str, Any]]) -> list[StreamEvent]:
    """A round that says something aloud and then calls a tool."""
    name, arguments = call
    return [
        StreamEvent(kind="text", text=f"<say>{text}</say>"),
        StreamEvent(
            kind="tool_call",
            tool_call=ToolCallDelta(index=0, id="c1", name=name, arguments=json.dumps(arguments)),
        ),
        StreamEvent(kind="done", finish_reason="tool_calls"),
        StreamEvent(kind="usage", usage=Usage()),
    ]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DEEPSEEK_API_KEY="test-key",
        DEEPGRAM_API_KEY="test-key",
        max_steps=4,
        thinking_enabled=True,
    )


@pytest.fixture
def stored(tmp_path):
    """A store with a project and a conversation to write against."""
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("P", tmp_path)
    record = store.create_session(project.id)
    return store, record.id


def make_loop(settings, tmp_path, client, *, store=None, session_id="", registry=None):
    return AgentLoop(
        settings,
        root=tmp_path,
        session_id=session_id,
        client=client,
        store=store,
        registry=registry if registry is not None else ToolRegistry([]),
    )


async def collect(loop: AgentLoop, user_text: str = "hello") -> list:
    return [event async for event in loop.run([], user_text)]


def kinds(events) -> list[str]:
    return [event.kind.value for event in events]


class TestReasoningIsStored:
    """One block of thinking per step, so a reopened turn can show its process."""

    async def test_a_finished_step_stores_its_thinking(self, settings, tmp_path, stored):
        store, session_id = stored
        client = FakeClient([thinking_script("Let me check the header first.")])
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await collect(loop)

        rows = [m for m in store.list_messages(session_id) if m.role == REASONING_ROLE]
        assert len(rows) == 1, "a step that thought must leave exactly one row"
        assert "check the header" in rows[0].content

    async def test_each_step_gets_its_own_row(self, settings, tmp_path, stored):
        """A running blob would have no way to say which step a thought belonged to."""
        store, session_id = stored
        client = FakeClient(
            [
                thinking_script("First thought.", call=("list_dir", {"path": "."})),
                thinking_script("Second thought."),
            ]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await collect(loop)

        rows = [m for m in store.list_messages(session_id) if m.role == REASONING_ROLE]
        assert len(rows) == 2, "each step's thinking must be stored separately"
        assert "First thought." in rows[0].content
        assert "Second thought." in rows[1].content

    async def test_thinking_is_stored_before_the_calls_it_produced(
        self, settings, tmp_path, stored
    ):
        """The stored order is what the process view is rebuilt from."""
        store, session_id = stored
        client = FakeClient(
            [
                thinking_script("I will list it.", call=("list_dir", {"path": "."})),
                thinking_script("Nothing there."),
            ]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await collect(loop)

        thinking = next(m for m in store.list_messages(session_id) if m.role == REASONING_ROLE)
        call = store.list_tool_calls(session_id)[0]
        assert thinking.created_at <= call.created_at, (
            "a step's reasoning must be stored at or before the calls it explains"
        )

    async def test_a_step_that_thought_nothing_stores_nothing(self, settings, tmp_path, stored):
        """An empty row would render as a Think block with no content."""
        store, session_id = stored
        client = FakeClient([[StreamEvent(kind="text", text="<say>Yes.</say>")]])
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await collect(loop)

        assert not [m for m in store.list_messages(session_id) if m.role == REASONING_ROLE]

    async def test_stored_thinking_is_not_searchable(self, tmp_path):
        """Reasoning is a private brainstorm, not a finding to retrieve later.

        It is full of self-corrections and of guesses the model declined to act
        on. Returning one later as "what you did before" would put a discarded
        idea back in front of the model as though it were established.
        """
        from surtitle.store.db import Store as FreshStore

        store = FreshStore(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        store.add_message(record.id, REASONING_ROLE, "maybe the batch job signal is I_IngestFlag")
        store.add_message(record.id, "assistant", "The signal is I_IngestReadyFlag.")

        hits = store.search_conversations("I_IngestFlag")
        assert not hits, "stored reasoning must never come back as retrieved history"

    async def test_thinking_is_capped_and_keeps_its_conclusion(self, settings, tmp_path, stored):
        """The tail is kept: reasoning ends with the decision it acted on."""
        from surtitle.core.agent import _REASONING_STORED_CHARS

        store, session_id = stored
        long_thought = "x" * (_REASONING_STORED_CHARS + 5000) + " FINAL DECISION"
        client = FakeClient([thinking_script(long_thought)])
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await collect(loop)

        stored_text = next(
            m for m in store.list_messages(session_id) if m.role == REASONING_ROLE
        ).content
        assert len(stored_text) <= _REASONING_STORED_CHARS + 1
        assert stored_text.endswith("FINAL DECISION"), (
            "truncating the tail throws away the part the step acted on"
        )


class TestStepIsOnTheWire:
    """Every grouped event must carry the step the browser groups it by."""

    async def test_tool_events_carry_their_step(self, settings, tmp_path):
        client = FakeClient(
            [
                thinking_script("Listing.", call=("list_dir", {"path": "."})),
                thinking_script("Done."),
            ]
        )
        loop = make_loop(settings, tmp_path, client)
        events = await collect(loop)

        calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
        results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
        assert calls and results
        for event in calls + results:
            assert event.data.get("step"), f"{event.kind} carries no step to group by"

    async def test_thinking_deltas_carry_their_step(self, settings, tmp_path):
        """Reasoning goes over the side channel, because it is not part of the
        turn's answer — but it must still say which step it belongs to."""
        client = FakeClient([thinking_script("One. Two.")])
        loop = make_loop(settings, tmp_path, client)
        seen: list = []

        async def emitter(event) -> None:
            seen.append(event)

        loop.set_emitter(emitter)
        await collect(loop)

        thinking = [e for e in seen if e.kind is EventKind.THINKING]
        assert thinking, "reasoning must still stream for the live view"
        assert all(event.data.get("step") for event in thinking)

    async def test_thinking_still_streams_as_deltas(self, settings, tmp_path):
        """Storing it per step must not batch the live view."""
        client = FakeClient([thinking_script("alpha beta gamma")])
        loop = make_loop(settings, tmp_path, client)
        seen: list = []

        async def emitter(event) -> None:
            seen.append(event)

        loop.set_emitter(emitter)
        await collect(loop)

        text = "".join(e.data["text"] for e in seen if e.kind is EventKind.THINKING)
        assert text == "alpha beta gamma"


class TestStopping:
    """A turn that ends must say how it ended."""

    async def test_a_finished_turn_says_complete(self, settings, tmp_path):
        client = FakeClient([thinking_script("Done thinking.")])
        loop = make_loop(settings, tmp_path, client)
        events = await collect(loop)

        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "complete"

    async def test_an_exhausted_budget_says_so(self, tmp_path):
        """The step cap is not a failure, and must not read as one."""
        settings = Settings(
            DEEPSEEK_API_KEY="test-key",
            DEEPGRAM_API_KEY="test-key",
            max_steps=2,
            thinking_enabled=False,
        )
        # Every round calls a tool, so the loop can only leave through the cap.
        script = thinking_script("Again.", call=("list_dir", {"path": "."}))
        client = FakeClient([script, script, script])
        loop = make_loop(settings, tmp_path, client)
        events = await collect(loop)

        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "step_limit"
        assert done.data.get("detail"), "the banner needs something to say"
        assert done.data.get("truncated") is True

    async def test_a_failed_turn_says_so(self, tmp_path):
        from surtitle.llm.deepseek import DeepSeekError

        class Failing(FakeClient):
            async def stream(self, messages, *, tools=None):
                raise DeepSeekError("connection reset")
                yield  # pragma: no cover - makes this a generator

        settings = Settings(DEEPSEEK_API_KEY="test-key", DEEPGRAM_API_KEY="test-key", max_steps=2)
        loop = make_loop(settings, tmp_path, Failing([[]]))
        events = await collect(loop)

        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "failed"
        assert done.data.get("detail")

    def test_the_budget_warning_precedes_every_progress_step_far_short_of_it(self):
        """The thresholds are ordered, so the warning is never the first thing said."""
        assert tuple(sorted(PROGRESS_STEPS)) == PROGRESS_STEPS
        assert PROGRESS_STEPS[0] < 10, "a user waits too long for the first reassurance"


class TestTheClosingRoundIsAudible:
    """A turn must not end with its answer only on screen.

    Reported from a real session: the turn opened with a spoken "Let me find the
    push route used for the earlier note before I write anything", did seventeen
    steps, and finished with the entire result — the KB note, the revision
    number, and the question about committing — in the display channel. The
    turn-level "must not be silent" guarantee was satisfied by the opening
    preamble, so nothing repaired it. To a user who is listening, the agent said
    it was going to look something up and then went quiet.
    """

    async def test_a_silent_final_round_is_given_a_voice(self, settings, tmp_path, stored):
        store, session_id = stored
        registry = ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"])
        client = FakeClient(
            [
                # Opens with speech, then does work.
                [
                    StreamEvent(kind="text", text="<say>Let me check the directory.</say>"),
                    StreamEvent(
                        kind="tool_call",
                        tool_call=ToolCallDelta(
                            index=0, id="c1", name="list_dir", arguments=json.dumps({"path": "."})
                        ),
                    ),
                    StreamEvent(kind="done", finish_reason="tool_calls"),
                    StreamEvent(kind="usage", usage=Usage()),
                ],
                # The answer, in the display channel only.
                [
                    StreamEvent(kind="text", text="The note is committed as r21566."),
                    StreamEvent(kind="done", finish_reason="stop"),
                    StreamEvent(kind="usage", usage=Usage()),
                ],
            ]
        )
        loop = make_loop(
            settings, tmp_path, client, store=store, session_id=session_id, registry=registry
        )

        events = await collect(loop)

        spoken = [e for e in events if e.kind is EventKind.SAY]
        assert any("r21566" in (e.data.get("text") or "") for e in spoken), (
            "the conclusion must be spoken, not only displayed"
        )
        answer = [m for m in store.list_messages(session_id) if m.role == "assistant"][-1]
        assert answer.spoken and "r21566" in answer.spoken
        assert "r21566" in answer.content, "the displayed answer is kept as it was"

    async def test_a_final_round_that_speaks_is_left_alone(self, settings, tmp_path, stored):
        """The repair must not double up on a turn that closed properly."""
        store, session_id = stored
        client = FakeClient(
            [
                [
                    StreamEvent(kind="text", text="<say>Let me look.</say>"),
                    StreamEvent(
                        kind="tool_call",
                        tool_call=ToolCallDelta(
                            index=0, id="c1", name="list_dir", arguments=json.dumps({"path": "."})
                        ),
                    ),
                    StreamEvent(kind="done", finish_reason="tool_calls"),
                    StreamEvent(kind="usage", usage=Usage()),
                ],
                [
                    StreamEvent(kind="text", text="<display>r21566</display>"),
                    StreamEvent(kind="text", text="<say>Committed as r21566.</say>"),
                    StreamEvent(kind="done", finish_reason="stop"),
                    StreamEvent(kind="usage", usage=Usage()),
                ],
            ]
        )
        loop = make_loop(
            settings,
            tmp_path,
            client,
            store=store,
            session_id=session_id,
            registry=ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"]),
        )

        events = await collect(loop)

        said = [(e.data.get("text") or "") for e in events if e.kind is EventKind.SAY]
        assert said == ["Let me look.", "Committed as r21566."], (
            f"the closing line is spoken once, not repaired on top of: {said}"
        )


class TestANoAnswerTurn:
    """A turn that did work must not end with nothing to read or hear.

    Reported from a real investigation: 28 steps, 38 tool calls, eleven minutes,
    and an assistant message containing only the `[work this turn]` log — no
    spoken text and no displayed text. The model's final round came back with
    nothing at all, and the loop treated that as a finished turn. From the user's
    side it is indistinguishable from the agent having stopped.
    """

    async def test_an_empty_final_round_is_asked_to_wrap_up(self, settings, tmp_path, stored):
        store, session_id = stored
        # A real registry, so the first round's call actually runs: the wrap-up
        # only applies to a turn that has work behind it.
        registry = ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"])
        # First round works, second returns nothing, third answers.
        client = FakeClient(
            [
                thinking_script("Listing.", call=("list_dir", {"path": "."})),
                [StreamEvent(kind="done", finish_reason="stop")],
                thinking_script("Here is what I found."),
            ]
        )
        loop = make_loop(
            settings, tmp_path, client, store=store, session_id=session_id, registry=registry
        )

        events = await collect(loop)

        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "complete"
        answers = [m for m in store.list_messages(session_id) if m.role == "assistant"]
        assert answers and answers[-1].spoken == "Done.", (
            "the wrap-up round's answer must be what is stored"
        )
        assert len(client.calls) == 3, "the empty round is retried once, then answered"

    async def test_the_wrap_up_does_not_re_run_the_work(self, settings, tmp_path, stored):
        """It is asked to stop calling tools and speak, not to start over."""
        store, session_id = stored
        client = FakeClient(
            [
                thinking_script("Listing.", call=("list_dir", {"path": "."})),
                [StreamEvent(kind="done", finish_reason="stop")],
                thinking_script("Done."),
            ]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)
        await collect(loop)

        nudge = client.calls[2]["messages"][-1]
        assert nudge["role"] == "user"
        assert "no spoken or displayed text" in nudge["content"]
        assert "Stop calling tools" in nudge["content"]

    async def test_a_second_empty_round_is_reported_not_stored_as_success(
        self, settings, tmp_path, stored
    ):
        store, session_id = stored
        client = FakeClient(
            [
                thinking_script("Listing.", call=("list_dir", {"path": "."})),
                [StreamEvent(kind="done", finish_reason="stop")],
                [StreamEvent(kind="done", finish_reason="stop")],
            ]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        events = await collect(loop)

        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "no_answer"
        assert done.data.get("failed") is True
        stored_tail = [m for m in store.list_messages(session_id) if m.role == "assistant"][-1]
        assert "without producing an answer" in stored_tail.content, (
            "the transcript must say why it is empty rather than looking truncated"
        )

    async def test_it_is_not_asked_forever(self, settings, tmp_path, stored):
        """One wrap-up round, then a reported failure — never a retry loop."""
        store, session_id = stored
        empty = [StreamEvent(kind="done", finish_reason="stop")]
        client = FakeClient(
            [thinking_script("Listing.", call=("list_dir", {"path": "."})), empty, empty, empty]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await collect(loop)

        assert len(client.calls) == 3, "the wrap-up is asked for once, not repeatedly"

    async def test_an_empty_first_round_is_asked_and_then_reported(
        self, settings, tmp_path, stored
    ):
        """A turn with no work behind it is not a silent success either.

        It used to be left alone, on the reasoning that a turn which did nothing
        has nothing to report. Two real turns on 2026-09-19 — 09:42 and 11:10 —
        were stored as `complete` with a zero-character assistant message: the
        model's first round returned nothing, the wrap-up was gated on the turn
        having done work, so it was never asked, and the user got silence with no
        banner and no Continue to press. That is the worst shape of "it stopped".
        """
        store, session_id = stored
        empty = [StreamEvent(kind="done", finish_reason="stop")]
        client = FakeClient([empty, empty])
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        events = await collect(loop)

        assert len(client.calls) == 2, "an empty first round is asked once, not ignored"
        assert "Stop calling tools" in client.calls[1]["messages"][-1]["content"], (
            "the round after the silence must be told to answer"
        )
        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "no_answer"
        assert done.data.get("failed") is True
        stored_tail = [m for m in store.list_messages(session_id) if m.role == "assistant"][-1]
        assert stored_tail.content.strip(), "an empty turn must not store an empty reply"
        assert "ran no commands" in stored_tail.content, (
            "with no work log there is nothing to point at, and saying there is would be a lie"
        )

    async def test_a_silent_closing_round_after_speech_is_asked_to_wrap_up(
        self, settings, tmp_path, stored
    ):
        """Reported from a real 0.8.1 session, and reproduced here.

        The turn opened with a spoken preamble — "let me read the precedent sim's
        harness, then write the sim" — worked four rounds and nine calls, and then
        finished on a round that produced nothing at all: no `<say>`, no
        `<display>`, no call. The wrap-up was decided on the *turn* ("has this turn
        produced anything"), and the opening preamble satisfied it, so the turn was
        stored as a success and the user got a preamble and then silence.
        """
        store, session_id = stored
        registry = ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"])
        client = FakeClient(
            [
                speaking_script("Let me read the harness.", call=("list_dir", {"path": "."})),
                # Nothing at all: no speech, no display, no call.
                [StreamEvent(kind="done", finish_reason="stop")],
                thinking_script("Here is what I found."),
            ]
        )
        loop = make_loop(
            settings, tmp_path, client, store=store, session_id=session_id, registry=registry
        )

        events = await collect(loop)

        assert len(client.calls) == 3, "a silent closing round must be asked to wrap up"
        assert "Stop calling tools" in client.calls[2]["messages"][-1]["content"], (
            "the round after the silence must be told to answer"
        )
        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "complete"

    async def test_speech_then_silence_is_reported_not_stored_as_a_success(
        self, settings, tmp_path, stored
    ):
        """Asked once, it still produced nothing — so the turn says so.

        This is the whole of the 0.8.1 report: a preamble, real work, and a
        closing round with nothing in it. A quiet `complete` is indistinguishable
        from the agent having stopped, so the failure is named, stored and spoken.
        """
        store, session_id = stored
        registry = ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"])
        empty = [StreamEvent(kind="done", finish_reason="stop")]
        client = FakeClient(
            [
                speaking_script("Let me read the harness.", call=("list_dir", {"path": "."})),
                empty,
                empty,
            ]
        )
        loop = make_loop(
            settings, tmp_path, client, store=store, session_id=session_id, registry=registry
        )

        events = await collect(loop)

        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "no_answer"
        assert done.data.get("failed") is True
        assert done.data.get("detail"), "the stop banner needs something to say"
        stored_tail = [m for m in store.list_messages(session_id) if m.role == "assistant"][-1]
        assert "without producing an answer" in stored_tail.content, (
            "a reopened conversation must show why it is empty"
        )

    def test_the_failure_is_spoken(self):
        import inspect

        from surtitle.core.session import Session

        source = inspect.getsource(Session._speak_problem)
        assert "no_answer" in source, "a silent turn must not also be silent about itself"


class TestATurnThatStopsWithThePlanOpen:
    """A pause mid-plan must not be recorded as the answer.

    Reported from the real 2026-09-19 session, eight times in one day: the agent
    worked, then ended a round with prose and no tool call — "let me now check the
    parser", "the parser is not written yet", "say go and I'll start at change 1"
    — and the loop read that as the answer. The turn was `complete`, the client
    showed no banner and offered no Continue, and the Plan tab went on claiming
    work was outstanding. Every one of those was answered by the user typing
    "continue".

    The plan is what makes this decidable rather than a guess about the shape of the
    prose: it is the machine-readable statement of what the agent believes is left.
    """

    @staticmethod
    def _registry():
        return ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"])

    async def test_a_round_that_stops_with_the_plan_open_is_asked_to_carry_on(
        self, settings, tmp_path, stored
    ):
        store, session_id = stored
        store.set_todos(
            session_id,
            [
                {"content": "Read the ingest module", "status": "completed"},
                {"content": "Write the parser", "status": "in_progress"},
            ],
        )
        client = FakeClient(
            [
                thinking_script("Listing.", call=("list_dir", {"path": "."})),
                # The pause: prose, and no call to carry it out.
                thinking_script("Next I will write the parser."),
                thinking_script("Modelled it."),
            ]
        )
        loop = make_loop(
            settings,
            tmp_path,
            client,
            store=store,
            session_id=session_id,
            registry=self._registry(),
        )

        events = await collect(loop)

        assert len(client.calls) == 3, "a pause with the plan open must be asked to carry on"
        nudge = client.calls[2]["messages"][-1]
        assert nudge["role"] == "user"
        assert "Write the parser" in nudge["content"], (
            "the ask must name the item the user can see, not just say 'continue'"
        )
        assert "Read the ingest module" not in nudge["content"], (
            "a ticked item is not outstanding and must not be named as one"
        )
        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "complete"

    async def test_the_ask_is_made_once_and_then_the_answer_stands(
        self, settings, tmp_path, stored
    ):
        """A model that answers twice without a call is answering, not stalling."""
        store, session_id = stored
        store.set_todos(session_id, [{"content": "Write the parser"}])
        client = FakeClient(
            [
                thinking_script("Listing.", call=("list_dir", {"path": "."})),
                thinking_script("I have stopped here."),
                thinking_script("Yes, still stopped."),
                thinking_script("And again."),
            ]
        )
        loop = make_loop(
            settings,
            tmp_path,
            client,
            store=store,
            session_id=session_id,
            registry=self._registry(),
        )

        events = await collect(loop)

        assert len(client.calls) == 3, "the ask is made once, not on every round"
        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "complete", (
            "the plan being open is not a turn-ending failure once it has been asked"
        )

    async def test_a_finished_plan_ends_the_turn(self, settings, tmp_path, stored):
        store, session_id = stored
        store.set_todos(
            session_id,
            [{"content": "Read the ingest module", "status": "completed"}],
        )
        client = FakeClient(
            [
                thinking_script("Listing.", call=("list_dir", {"path": "."})),
                thinking_script("All done."),
            ]
        )
        loop = make_loop(
            settings,
            tmp_path,
            client,
            store=store,
            session_id=session_id,
            registry=self._registry(),
        )

        await collect(loop)

        assert len(client.calls) == 2, "a ticked plan needs no ask"

    async def test_a_question_answered_without_tools_is_not_nudged(
        self, settings, tmp_path, stored
    ):
        """A plan open does not make every answer a pause in the work."""
        store, session_id = stored
        store.set_todos(session_id, [{"content": "Write the parser"}])
        client = FakeClient([thinking_script("The gate item is still open.")])
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        events = await collect(loop)

        assert len(client.calls) == 1, (
            "the user asked a question; it was answered, and no work was in flight"
        )
        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "complete"

    async def test_a_store_with_no_plan_is_left_alone(self, settings, tmp_path, stored):
        store, session_id = stored
        client = FakeClient(
            [
                thinking_script("Listing.", call=("list_dir", {"path": "."})),
                thinking_script("Nothing more to do."),
            ]
        )
        loop = make_loop(
            settings,
            tmp_path,
            client,
            store=store,
            session_id=session_id,
            registry=self._registry(),
        )

        await collect(loop)

        assert len(client.calls) == 2, "no plan means nothing to carry on with"


class TestTheSpokenReason:
    """A voice-first user hears why it stopped, in the same words as the screen."""

    async def test_each_reason_has_something_to_say(self):
        import inspect

        source = inspect.getsource(Session._speak_problem)
        for reason in ("step_limit", "failed", "llm", "internal"):
            assert reason in source, f"no spoken line for {reason}"

    async def test_the_step_budget_is_not_phrased_as_a_failure(self):
        import inspect

        source = inspect.getsource(Session._speak_problem)
        step_line = source[source.index('"step_limit"') :]
        step_line = step_line[: step_line.index("),")]
        assert "error" not in step_line.lower(), (
            "nothing went wrong; the turn simply reached its limit with work left"
        )
        assert "continue" in step_line.lower(), "it must say how to carry on"

    async def test_the_near_budget_warning_is_spoken_once(self):
        import inspect

        source = inspect.getsource(Session._run_turn)
        assert "warned_budget" in source, "the warning would repeat at every step past it"
        assert "NEAR_BUDGET_FRACTION" in source


class TestThePlan:
    """The agent's plan is conversation state, not a file."""

    def test_it_round_trips(self, tmp_path):
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        stored = store.set_todos(
            record.id,
            [
                {"content": "Slice the logs", "status": "completed"},
                {
                    "content": "Measure the travel distance",
                    "activeForm": "Measuring the travel distance",
                    "status": "in_progress",
                },
                {"content": "Write it up", "status": "pending"},
            ],
        )

        assert [item["content"] for item in stored] == [
            "Slice the logs",
            "Measure the travel distance",
            "Write it up",
        ]
        assert store.list_todos(record.id) == stored
        assert stored[1]["activeForm"] == "Measuring the travel distance"

    def test_writing_replaces_rather_than_merges(self, tmp_path):
        """A merge would leave dropped items showing as outstanding work."""
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        store.set_todos(record.id, [{"content": "One"}, {"content": "Two"}])
        store.set_todos(record.id, [{"content": "Two", "status": "completed"}])

        remaining = store.list_todos(record.id)
        assert [item["content"] for item in remaining] == ["Two"]
        assert remaining[0]["status"] == "completed"

    def test_an_unknown_status_does_not_lose_the_plan(self, tmp_path):
        """A plan that fails to save because the model invented a status is worse
        than one that reads as not-started."""
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        stored = store.set_todos(record.id, [{"content": "One", "status": "in-progress"}])
        assert stored == [{"content": "One", "activeForm": None, "status": "pending"}]

    def test_blank_items_are_dropped(self, tmp_path):
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        stored = store.set_todos(record.id, [{"content": "  "}, {"content": "Real work"}])
        assert [item["content"] for item in stored] == ["Real work"]

    def test_a_plan_belongs_to_one_conversation(self, tmp_path):
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        first = store.create_session(project.id)
        second = store.create_session(project.id)

        store.set_todos(first.id, [{"content": "Only here"}])
        assert store.list_todos(second.id) == []

    def test_the_plan_survives_a_deleted_conversation(self, tmp_path):
        """The rows cascade; a foreign key left behind would break every later write."""
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        store.set_todos(record.id, [{"content": "Gone soon"}])

        store.delete_session(record.id)
        assert store.list_todos(record.id) == []

    def test_the_tool_records_the_whole_list(self, tmp_path):
        from surtitle.tools.registry import _todo_write_handler

        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        ctx = ToolContext(root=tmp_path, session_id=record.id, project_id=project.id, store=store)

        result = _todo_write_handler(
            ctx,
            [
                {"content": "First", "status": "completed"},
                {"content": "Second", "status": "in_progress", "activeForm": "Doing second"},
            ],
        )

        assert result.ok
        assert "1/2 complete" in result.display
        assert "Doing second" in result.display
        assert len(store.list_todos(record.id)) == 2

    def test_the_tool_refuses_an_empty_plan(self, tmp_path):
        from surtitle.tools.registry import _todo_write_handler

        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        ctx = ToolContext(root=tmp_path, session_id=record.id, project_id=project.id, store=store)

        result = _todo_write_handler(ctx, [])
        assert not result.ok
        assert "non-empty" in (result.error or "")

    def test_the_tool_is_offered_to_the_model(self):
        schema = next(t for t in default_tool_list() if t.name == TODO_TOOL).to_openai_schema()
        params = schema["function"]["parameters"]
        assert "todos" in params["properties"]
        assert params["required"] == ["todos"]

    async def test_writing_a_plan_emits_it_for_the_ui(self, settings, tmp_path, stored):
        """The UI must be told, not left to parse a tool result for its own state."""
        store, session_id = stored
        registry = ToolRegistry([t for t in default_tool_list() if t.name == TODO_TOOL])
        client = FakeClient(
            [
                thinking_script(
                    "I will plan this.",
                    call=(
                        TODO_TOOL,
                        {
                            "todos": [
                                {"content": "Slice the logs", "status": "in_progress"},
                                {"content": "Measure the travel", "status": "pending"},
                            ]
                        },
                    ),
                ),
                thinking_script("Planned."),
            ]
        )
        loop = make_loop(
            settings, tmp_path, client, store=store, session_id=session_id, registry=registry
        )

        events = await collect(loop)

        plan_events = [e for e in events if e.kind is EventKind.TODOS]
        assert plan_events, "the plan must reach the UI as its own event"
        assert [item["content"] for item in plan_events[0].data["todos"]] == [
            "Slice the logs",
            "Measure the travel",
        ]


class TestTheTranscriptWindowKeepsTheEnd:
    """A capped read of a long transcript must lose the opening, not the tail.

    Both `list_messages` and `list_tool_calls` took the *first* `limit` rows. On a
    long session that silently showed the oldest part of it — and for
    `list_messages` the model read the same truncated view, which is the amnesia
    this whole area exists to prevent.
    """

    def test_messages_over_the_limit_keep_the_newest(self, tmp_path):
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        for index in range(500):
            store.add_message(record.id, "user", f"message {index}")

        kept = store.list_messages(record.id, limit=10)

        assert [m.content for m in kept] == [f"message {index}" for index in range(490, 500)], (
            "the newest ten, still in reading order"
        )

    def test_tool_calls_over_the_limit_keep_the_newest(self, tmp_path):
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        for index in range(300):
            store.add_tool_call(record.id, "run_shell", {"command": f"cmd {index}"})

        kept = store.list_tool_calls(record.id, limit=5)

        assert [c.arguments["command"] for c in kept] == [
            f"cmd {index}" for index in range(295, 300)
        ]

    def test_a_role_filter_is_applied_before_the_window_is_cut(self, tmp_path):
        """Otherwise the audit trail evicts the conversation it sits beside."""
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        store.add_message(record.id, "user", "the question")
        for step in range(50):
            store.add_message(record.id, REASONING_ROLE, f"thinking {step}")

        kept = store.list_messages(record.id, limit=5, roles=("user", "assistant"))

        assert [m.content for m in kept] == ["the question"]


class TestTheReplayedTurn:
    """A recent turn is replayed as it happened, not as a summary of itself.

    The prose work log keeps 320 characters of each outcome; the stored result
    holds up to 4,000. A model told only `read_file(x) -> 320 characters of it`
    reads the file again on the next turn, which is the amnesia this exists to
    stop: the record was there, and the reply was not.
    """

    @staticmethod
    def _session(store, record, root):
        async def noop(*_args, **_kwargs):
            return None

        return Session(
            session_id=record.id,
            project_id=record.project_id,
            root=root,
            settings=Settings(DEEPSEEK_API_KEY="k", SURTITLE_HOME=str(root)),
            store=store,
            deepseek=None,
            send=noop,
            send_audio=noop,
        )

    def test_a_recent_turn_replays_its_calls_and_their_results(self, tmp_path):
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        store.start_turn(record.id)
        store.add_message(record.id, "user", "what is the batch size?")
        store.add_tool_call(
            record.id,
            "read_file",
            {"path": "config.ini"},
            result="batch_size = 250; " * 40,
            ok=True,
            step=1,
        )
        store.add_message(
            record.id,
            "assistant",
            "The batch size is 250.\n\n[work this turn]\n- read_file(config.ini) -> batch_size = 250",
        )
        session = self._session(store, record, tmp_path)

        history = session._build_history()

        assert [message["role"] for message in history] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        call = history[1]["tool_calls"][0]
        assert call["function"]["name"] == "read_file"
        assert json.loads(call["function"]["arguments"]) == {"path": "config.ini"}
        assert history[2]["tool_call_id"] == call["id"], "the result must match its call"
        assert history[2]["content"].startswith("batch_size = 250")
        assert len(history[2]["content"]) > 320, "the stored result, not the log's line about it"
        assert history[3]["content"] == "The batch size is 250.", "the answer stands alone"
        assert "[work this turn]" not in json.dumps(history), (
            "the summary is replaced by the thing it summarised, not kept beside it"
        )

    def test_a_turn_past_the_window_keeps_its_prose_log(self, tmp_path):
        """Fidelity is spent on the recent turns; the rest keep the summary."""
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        for turn in range(1, 6):
            store.start_turn(record.id)
            store.add_message(record.id, "user", f"question {turn}")
            store.add_tool_call(
                record.id, "read_file", {"path": f"f{turn}.ini"}, result="r" * 400, ok=True
            )
            store.add_message(
                record.id,
                "assistant",
                f"answer {turn}\n\n[work this turn]\n- read_file(f{turn}.ini) -> rrr",
            )
        session = self._session(store, record, tmp_path)

        history = session._build_history()

        old = next(m for m in history if (m.get("content") or "").startswith("answer 1"))
        assert "[work this turn]" in old["content"], "an old turn keeps the prose log"
        assert len([m for m in history if m["role"] == "tool"]) == 2, (
            "only the two most recent turns replay structurally"
        )

    def test_a_failed_call_says_so_in_the_replay(self, tmp_path):
        """The log line said FAILED; the replayed result has to as well."""
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        store.start_turn(record.id)
        store.add_message(record.id, "user", "run it")
        store.add_tool_call(record.id, "run_shell", {"command": "false"}, result="exit 1", ok=False)
        store.add_message(
            record.id, "assistant", "It failed.\n\n[work this turn]\n- run_shell(false) -> FAILED"
        )
        session = self._session(store, record, tmp_path)

        history = session._build_history()

        tool = next(m for m in history if m["role"] == "tool")
        assert tool["content"].startswith("FAILED:")

    def test_a_turn_stamps_every_row_it_writes(self, tmp_path):
        """Without this the replay has to guess which call belongs to which answer."""
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        assert store.start_turn(record.id) == 1
        store.add_message(record.id, "user", "one")
        store.add_tool_call(record.id, "list_dir", {"path": "."})
        assert store.start_turn(record.id) == 2
        store.add_message(record.id, "user", "two")

        assert [m.turn for m in store.list_messages(record.id)] == [1, 2]
        assert [c.turn for c in store.list_tool_calls(record.id)] == [1]

    def test_an_older_database_gains_the_turn_columns(self, tmp_path):
        """A database written before the columns existed must still open.

        The tables are built here in the shape they had before the turn columns,
        which is the only way to exercise the upgrade rather than the fresh schema:
        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists,
        so a column added to the schema alone would never reach an install.
        """
        path = tmp_path / "db.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL,
                created_at REAL NOT NULL, last_opened_at REAL NOT NULL,
                auto_approved TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'New conversation', created_at REAL NOT NULL,
                updated_at REAL NOT NULL, archived_at REAL, last_end_reason TEXT,
                last_end_detail TEXT, last_end_steps INTEGER, last_end_at REAL);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, spoken TEXT, created_at REAL NOT NULL);
            CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                step INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL, arguments TEXT NOT NULL,
                result TEXT, ok INTEGER, approved INTEGER, duration_ms INTEGER,
                created_at REAL NOT NULL);
            """
        )
        conn.execute(
            "INSERT INTO projects (id, name, root, created_at, last_opened_at)"
            " VALUES ('p1', 'P', ?, 1.0, 1.0)",
            (str(tmp_path),),
        )
        conn.execute(
            "INSERT INTO sessions (id, project_id, title, created_at, updated_at)"
            " VALUES ('s1', 'p1', 'Old conversation', 1.0, 1.0)"
        )
        conn.commit()
        conn.close()

        store = Store(path)  # migrates on open
        assert store.start_turn("s1") == 1
        store.add_message("s1", "user", "one")
        store.add_tool_call("s1", "list_dir", {"path": "."})

        assert store.list_messages("s1")[-1].turn == 1
        assert store.list_tool_calls("s1")[-1].turn == 1
        assert store.get_session("s1").turn_seq == 1


class TestTheSessionEndpoint:
    """Reopening a conversation must return everything the process view needs."""

    def test_the_plan_is_returned_with_the_conversation(self, tmp_path):
        """The plan outlives the turn that wrote it, so reopening shows progress."""
        from starlette.testclient import TestClient

        from surtitle.server import create_app_for
        from surtitle.store.db import Store as FreshStore

        home = tmp_path / "home"
        home.mkdir()
        settings = Settings(
            DEEPSEEK_API_KEY="sk-test-deepseek-1234567890",
            SURTITLE_HOME=str(home),
            voice_enabled=False,
        )
        # The plan is written the way the agent writes it: `todo_write` runs
        # against the store the server itself uses, so this exercises the same
        # database the endpoint reads.
        store = FreshStore(settings.db_path)
        project = store.create_project("P", tmp_path / "work")
        record = store.create_session(project.id)
        store.set_todos(record.id, [{"content": "Slice the logs", "status": "in_progress"}])
        store.close()

        app = create_app_for(settings)
        with TestClient(app) as client:
            payload = client.get(f"/api/sessions/{record.id}").json()

        assert payload["todos"][0]["content"] == "Slice the logs"
        assert payload["todos"][0]["status"] == "in_progress"
        # The fields the transcript rebuild depends on.
        assert "tool_calls" in payload and "messages" in payload

    def test_the_last_turn_ending_comes_back_with_the_conversation(self, tmp_path):
        """A reopened conversation must state why its last turn ended.

        The browser inferred this from an unanswered transcript and named a cause
        it had no way to know. The record belongs to the server, so the endpoint
        has to carry it or a reload goes back to guessing.
        """
        from starlette.testclient import TestClient

        from surtitle.server import create_app_for
        from surtitle.store.db import Store as FreshStore

        home = tmp_path / "home"
        home.mkdir()
        settings = Settings(
            DEEPSEEK_API_KEY="sk-test-deepseek-1234567890",
            SURTITLE_HOME=str(home),
            voice_enabled=False,
        )
        store = FreshStore(settings.db_path)
        project = store.create_project("P", tmp_path / "work")
        record = store.create_session(project.id)
        store.record_turn_end(
            record.id, reason="step_limit", detail="Stopped after 40 steps.", steps=40
        )
        store.close()

        app = create_app_for(settings)
        with TestClient(app) as client:
            payload = client.get(f"/api/sessions/{record.id}").json()

        assert payload["last_end_reason"] == "step_limit"
        assert payload["last_end_steps"] == 40
        assert "40 steps" in payload["last_end_detail"]


class TestReasoningStaysOutOfTheModelPrompt:
    """Stored reasoning is a record for the user, never context for the model."""

    async def test_stored_reasoning_is_not_replayed_into_later_turns(
        self, settings, tmp_path, stored
    ):
        store, session_id = stored
        client = FakeClient([thinking_script("SECRET DELIBERATION"), thinking_script("Done.")])
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await collect(loop, "first")
        await collect(loop, "second")

        later = client.calls[-1]["messages"]
        blob = json.dumps(later)
        assert "SECRET DELIBERATION" not in blob, (
            "the model's own past thinking must not be fed back to it as a message"
        )

    async def test_a_stored_tool_excerpt_is_available_to_search_only_by_role(self, tmp_path):
        """Guards the search index against reasoning, not against tool output."""
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        store.add_message(record.id, "assistant", "The pump is node-117.")

        assert store.search_conversations("node-117"), (
            "ordinary transcript entries must stay searchable"
        )


@pytest.fixture(scope="module")
def script() -> str:
    """The browser bundle, as text. There is no browser here, so these assert on
    the shape of the code — the same approach the other web tests take."""
    web = Path(__file__).resolve().parent.parent / "src" / "surtitle" / "web"
    return (web / "js" / "app.js").read_text(encoding="utf-8")


class TestSeveralConversationsAtOnce:
    """One conversation must keep working while you read another.

    The server has always allowed it — two sockets on two conversations both
    answer — but the browser held a single socket and closed it on every switch.
    The conversation you left carried on running with nothing listening for its
    events, so its progress and its answer had no way back and the app looked
    like it could only do one thing at a time.
    """

    def test_a_socket_is_kept_per_conversation(self, script):
        assert "const connections = new Map();" in script, (
            "one socket for the app is what stopped a background chat"
        )
        assert "function connectionFor(sessionId)" in script

    def test_opening_a_conversation_does_not_close_the_others(self, script):
        block = script[script.index("function openConnection()") :]
        block = block[: block.index("\n}\n")]
        assert "socket.close();" in block, "the open conversation reconnects"
        assert "socket.connect(" in block
        assert "connections.delete" not in block, (
            "reconnecting must not sweep away the other conversations' sockets"
        )

    def test_events_of_another_conversation_are_not_rendered(self, script):
        assert "function handleSessionEvent(sessionId, event)" in script
        block = script[script.index("function handleSessionEvent(") :]
        block = block[: block.index("\n}\n")]
        assert "if (!open) {" in block, (
            "a background conversation's events must not be written into the transcript on screen"
        )

    def test_a_conversation_that_wants_you_is_marked(self, script):
        assert "awaitingApproval: true" in script
        assert "needs you" in script, "a prompt in another conversation must be visible"
        assert "row__badge" in script

    def test_a_project_switch_leaves_running_conversations_alone(self, script):
        block = script[script.index("async function selectProject(") :]
        block = block[: block.index("\n}\n")]
        assert "leaveSession()" in block, "the microphone follows the conversation on screen"
        assert "connections.clear" not in block and "closeConnection" not in block, (
            "switching project must not stop work in the project being left"
        )

    def test_the_microphone_is_handed_over_rather_than_shared(self, script):
        block = script[script.index("function leaveSession()") :]
        block = block[: block.index("\n}\n")]
        assert 'sendCommand("mic", { open: false })' in block
        assert "capture.setMuted(true)" in block

    def test_selecting_the_open_conversation_again_is_cheap(self, script):
        block = script[script.index("async function selectSession(") :]
        block = block[: block.index("\n}\n")]
        assert "if (state.session?.id === sessionId)" in block, (
            "re-selecting the open conversation must not replay and rebuild it"
        )


class TestTheChangingContextIsDeliveredInHistory:
    """The head of the request must stay still, or the provider cannot cache it.

    DeepSeek matches whole cache prefixes and a cache miss costs a fiftieth of a
    hit, so anything that changes between turns — the plan, the notebook, the
    project's top-level listing — is delivered *after* the history rather than
    inside the system prompt. The model still gets all of it every turn; only the
    position changed, and the position is the whole point.
    """

    async def test_the_note_follows_the_history_and_precedes_the_user(self, settings, tmp_path):
        client = FakeClient([thinking_script("Done.")])
        loop = AgentLoop(
            settings,
            root=tmp_path,
            client=client,
            context_note="## Your plan\n- [ ] One",
        )
        history = [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "earlier answer"},
        ]

        async for _event in loop.run(history, "now this"):
            pass

        sent = client.calls[0]["messages"]
        assert [message["role"] for message in sent] == [
            "system",
            "user",
            "assistant",
            "system",
            "user",
        ], "the stable prompt leads and the changing note follows the history"
        assert sent[3]["content"] == "## Your plan\n- [ ] One"

    async def test_an_empty_note_adds_no_message(self, settings, tmp_path):
        """A turn with nothing to report should not spend a message saying so."""
        client = FakeClient([thinking_script("Done.")])
        loop = AgentLoop(settings, root=tmp_path, client=client)

        async for _event in loop.run([], "hello"):
            pass

        sent = client.calls[0]["messages"]
        assert [message["role"] for message in sent] == ["system", "user"]


class TestAgeingAWorkLog:
    """The shape of a shortened work log.

    Every action keeps its tool and target, because "already read sim/README.md" is
    the whole reason the log exists — it is what stops the agent re-doing work it
    has finished. What goes is the tail of each outcome, and the actions past the
    twelfth.
    """

    @staticmethod
    def _content(lines: int) -> str:
        listing = "\n".join(f"- run_shell(cmd {n}) -> " + "x" * 300 for n in range(lines))
        return f"The pump is node-117.\n\n[work this turn]\n{listing}"

    def test_the_answer_above_the_log_is_untouched(self):
        aged = age_work_log(self._content(20))

        assert aged.startswith("The pump is node-117.")
        assert aged.count("[work this turn]") == 1

    def test_every_kept_action_has_its_tool_and_target(self):
        aged = age_work_log(self._content(20))

        assert "- run_shell(cmd 0)" in aged
        assert "- run_shell(cmd 11)" in aged, "the twelfth action is still there"

    def test_what_goes_is_the_weight(self):
        content = self._content(20)
        aged = age_work_log(content)

        assert "x" * 300 not in aged, "the tail of each outcome is what costs the most"
        assert len(aged) < len(content)

    def test_the_count_of_dropped_actions_is_stated(self):
        aged = age_work_log(self._content(20))

        assert "…and 8 more action(s)" in aged, "a silent cut reads as the whole log"

    def test_a_message_with_no_log_is_left_alone(self):
        assert age_work_log("Just an answer.") is None

    def test_a_log_short_enough_to_keep_is_not_rewritten(self):
        content = "Answer.\n\n[work this turn]\n- read_file(a.md) -> 3 lines"
        assert age_work_log(content) is None

    def test_an_empty_log_is_left_alone(self):
        assert age_work_log("Answer.\n\n[work this turn]\n") is None

    def test_the_store_keeps_both_copies(self, tmp_path):
        """The user's transcript is never rewritten to save the model tokens."""
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        message = store.add_message(record.id, "assistant", self._content(20))
        assert message.replay == message.content, "nothing has aged yet"

        shortened = age_work_log(message.content)
        assert shortened is not None
        store.set_model_content(message.id, shortened)

        reread = store.list_messages(record.id)[0]
        assert reread.replay == shortened, "the model is given the shortened form"
        assert reread.content == message.content, "the user keeps the full text"
