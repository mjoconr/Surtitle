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
from pathlib import Path
from typing import Any

import pytest

from surtitle.config import Settings
from surtitle.core.agent import PROGRESS_STEPS, AgentLoop
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
        store.add_message(
            record.id, REASONING_ROLE, "maybe the bale gate signal is I_GrabBalePresent"
        )
        store.add_message(record.id, "assistant", "The signal is I_GrabConveyorBalePresent.")

        hits = store.search_conversations("I_GrabBalePresent")
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

    async def test_an_ordinary_empty_turn_is_untouched(self, settings, tmp_path):
        """A turn that did nothing at all has no work to report; leave it alone."""
        client = FakeClient([[StreamEvent(kind="done", finish_reason="stop")]])
        loop = make_loop(settings, tmp_path, client)

        events = await collect(loop)

        done = next(e for e in events if e.kind is EventKind.DONE)
        assert done.data.get("reason") == "complete"
        assert len(client.calls) == 1

    def test_the_failure_is_spoken(self):
        import inspect

        from surtitle.core.session import Session

        source = inspect.getsource(Session._speak_problem)
        assert "no_answer" in source, "a silent turn must not also be silent about itself"


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
        store.add_message(record.id, "assistant", "The pump is 4C-117.")

        assert store.search_conversations("4C-117"), (
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
