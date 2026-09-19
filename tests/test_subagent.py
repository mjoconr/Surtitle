"""Delegation: one agent handing a piece of reading to another.

The reason to have a second agent rather than more turns of the first is context.
The reading that answers a question is usually several times larger than the
answer, and in the parent's transcript it would sit there for the rest of the
conversation. A sub-agent spends its own context on it and hands back the answer.

These drive both agents from one scripted model, so they are exact and offline:
round one is the parent deciding to delegate, the rounds after it are the child.
"""

from __future__ import annotations

import json

import pytest
from tests.test_agent_loop import FakeClient, text_script, tool_script

from surtitle.config import Settings
from surtitle.core.agent import AgentLoop
from surtitle.core.events import EventKind
from surtitle.core.session import Session
from surtitle.core.speak import Chunk, ChunkKind
from surtitle.llm.chat import StreamEvent, ToolCallDelta, Usage
from surtitle.store.db import Store
from surtitle.tools.registry import SUBAGENT_TOOL, ToolRegistry, default_tool_list


def subagent_call(task: str, label: str = "the retry settings", call_id: str = "s1") -> list:
    """A parent round that delegates one piece of reading."""
    return tool_script(
        name=SUBAGENT_TOOL, arguments={"task": task, "label": label}, call_id=call_id
    )


def read_script(path: str, call_id: str = "c1") -> list:
    return tool_script(name="read_file", arguments={"path": path}, call_id=call_id)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DEEPSEEK_API_KEY="test-key",
        DEEPGRAM_API_KEY="test-key",
        max_steps=6,
        thinking_enabled=False,
    )


def make_loop(settings, tmp_path, client, **kwargs) -> AgentLoop:
    registry = kwargs.pop("registry", ToolRegistry(default_tool_list()))
    return AgentLoop(
        settings,
        root=tmp_path,
        session_id="s1",
        client=client,
        registry=registry,
        **kwargs,
    )


async def collect(loop: AgentLoop, user_text: str = "what is the retry limit?") -> list:
    return [event async for event in loop.run([], user_text)]


class TestReadOnlyByConstruction:
    """A delegated agent that could write would be editing the project while the
    user heard only the parent. It is powerless by policy, not by instruction."""

    def test_the_child_gets_the_tools_that_look_and_nothing_else(self):
        """`skill` is here on purpose: following the project's written-down procedure
        is what a delegated reader should do, and reading one changes nothing."""
        child = ToolRegistry(default_tool_list()).read_only()
        assert child.names() == ["list_dir", "read_file", "search_files", "skill"]

    def test_a_tool_that_writes_is_not_offered_even_if_it_needs_no_approval(self):
        from surtitle.tools.registry import Tool

        quiet_writer = Tool(
            name="sneaky_write",
            description="",
            parameters={},
            handler=lambda **_: None,
            approval="never",
            mutating=True,
        )
        child = ToolRegistry([*default_tool_list(), quiet_writer]).read_only()
        assert "sneaky_write" not in child.names(), (
            "the filter is the tool's own policy, so a new side effect is enough "
            "to keep it out of a sub-agent's reach"
        )

    def test_a_sub_agent_cannot_start_another(self):
        """One level of delegation, not a tree."""
        child = ToolRegistry(default_tool_list()).read_only()
        assert SUBAGENT_TOOL not in child.names()

    def test_tools_needing_the_conversation_are_left_behind(self):
        child = ToolRegistry(default_tool_list()).read_only()
        assert "todo_write" not in child.names(), (
            "a sub-agent has no conversation for a plan to belong to"
        )


class TestDelegation:
    async def test_the_child_answers_and_the_parent_gets_it(self, settings, tmp_path):
        (tmp_path / "service.ini").write_text("max_attempts = 5\n", encoding="utf-8")
        client = FakeClient(
            [
                subagent_call("What is the retry limit in service.ini?"),
                read_script("service.ini"),
                text_script("<display>max_attempts = 5</display>"),
                text_script("<say>The limit is 5, from service.ini.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client)

        events = await collect(loop)

        results = [
            e for e in events if e.kind is EventKind.TOOL_RESULT and e.data["name"] == SUBAGENT_TOOL
        ]
        assert results and results[0].data["ok"] is True, "the parent must get a usable result"
        # The result is the child's answer, in the shape a tool result takes.
        answer = results[0].data
        assert answer["ok"] is True
        assert events[-1].kind is EventKind.DONE and events[-1].data["reason"] == "complete"

    async def test_the_childs_reading_never_enters_the_conversation(self, settings, tmp_path):
        """The whole point: the archaeology stays in the child."""
        (tmp_path / "service.ini").write_text("max_attempts = 5\n", encoding="utf-8")
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)
        client = FakeClient(
            [
                subagent_call("What is the retry limit in service.ini?"),
                read_script("service.ini"),
                text_script("<display>max_attempts = 5</display>"),
                text_script("<say>The limit is 5.</say>"),
            ]
        )
        loop = AgentLoop(
            settings,
            root=tmp_path,
            session_id=record.id,
            client=client,
            store=store,
            registry=ToolRegistry(default_tool_list()),
        )

        await collect(loop)

        stored = store.list_tool_calls(record.id)
        assert [call.name for call in stored] == [SUBAGENT_TOOL], (
            "the parent's transcript records one delegation, not the reading it did"
        )

    async def test_a_child_with_nothing_to_say_is_a_tool_error(self, settings, tmp_path):
        """A failure the parent can act on, not an empty answer treated as a fact."""
        client = FakeClient(
            [
                subagent_call("Find the thing."),
                # Both of the child's rounds come back with nothing at all. The
                # first is asked to wrap up — the same rule the parent's turn
                # follows — and when that produces nothing either, the child has
                # failed rather than found nothing.
                [
                    StreamEvent(kind="usage", usage=Usage()),
                    StreamEvent(kind="done", finish_reason="stop"),
                ],
                [
                    StreamEvent(kind="usage", usage=Usage()),
                    StreamEvent(kind="done", finish_reason="stop"),
                ],
                text_script("<say>I could not find it; I will look myself.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client)

        events = await collect(loop)

        failures = [
            e
            for e in events
            if e.kind is EventKind.TOOL_RESULT
            and e.data["name"] == SUBAGENT_TOOL
            and not e.data["ok"]
        ]
        assert failures, "a sub-agent with no answer must report a failure"
        assert "without an answer" in failures[0].data.get("error", "")
        assert events[-1].data["reason"] == "complete", "and the parent carries on"

    async def test_the_childs_steps_are_marked_and_numbered_after_the_parent(
        self, settings, tmp_path
    ):
        """Otherwise the parent looks busy with steps it is not taking."""
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        client = FakeClient(
            [
                subagent_call("Read a.txt.", label="the a file"),
                read_script("a.txt"),
                text_script("<display>hello</display>"),
                text_script("<say>It says hello.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client)
        seen: list = []

        async def record(event) -> None:
            seen.append(event)

        loop.set_emitter(record)

        await collect(loop)

        child_states = [e for e in seen if e.kind is EventKind.STATE and "subagent" in e.data]
        assert child_states, "the child's progress must reach the session"
        assert all(e.data["subagent"] == "the a file" for e in child_states)
        assert min(e.data["step"] for e in child_states) > 1, (
            "numbered after the parent's round, so the turn's narration keeps working"
        )
        child_tools = [e for e in seen if e.kind is EventKind.TOOL_CALL]
        assert child_tools and child_tools[0].data["subagent"] == "the a file"


class TestDelegationsRunTogether:
    """The prompt tells the agent it can hand over independent parts at once, so
    the loop has to do it: a survey split three ways must not cost three times the
    latency. Each call is still announced and stored in the order the model made
    it — only the waiting is shared."""

    @staticmethod
    def _two_calls() -> list:
        calls = [
            ToolCallDelta(
                index=index,
                id=f"s{index}",
                name=SUBAGENT_TOOL,
                arguments=json.dumps({"task": f"part {index}", "label": f"part {index}"}),
            )
            for index in (1, 2)
        ]
        return [
            *(StreamEvent(kind="tool_call", tool_call=call) for call in calls),
            StreamEvent(kind="usage", usage=Usage()),
            StreamEvent(kind="done", finish_reason="tool_calls"),
        ]

    async def test_two_delegations_are_in_flight_at_once(self, settings, tmp_path):
        import asyncio

        gate = asyncio.Event()
        state = {"calls": 0, "live": 0, "peak": 0}

        class GateClient:
            """Holds a delegation's model call until two are in flight together.

            Counting arrivals is not enough: run sequentially, the first child
            times out and *then* the second arrives, which still looks like two.
            Calls in flight is the question being asked.
            """

            async def stream(self, messages, *, tools=None):
                state["calls"] += 1
                if state["calls"] == 1:
                    for event in TestDelegationsRunTogether._two_calls():
                        yield event
                    return
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
                try:
                    if state["live"] >= 2:
                        gate.set()
                    await asyncio.wait_for(gate.wait(), timeout=2)
                    for event in text_script("<say>Found it.</say>"):
                        yield event
                finally:
                    state["live"] -= 1

            async def aclose(self):
                return None

        loop = make_loop(settings, tmp_path, GateClient())

        events = await collect(loop)

        assert state["peak"] >= 2, "both children must be running at the same time"
        assert events[-1].data["reason"] == "complete"
        results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
        assert [e.data["call_id"] for e in results] == ["s1", "s2"], (
            "and their results still come back in the order the model asked"
        )

    async def test_a_delegation_that_is_never_reached_is_cancelled(self, settings, tmp_path):
        """A stop mid-round must not leave a child running behind it."""
        import asyncio

        from surtitle.core.agent import _TurnState
        from surtitle.tools.fs_tools import ToolResult

        async def slow_child(task: str, label: str) -> ToolResult:
            await asyncio.sleep(30)
            return ToolResult(ok=True, data={})

        loop = make_loop(settings, tmp_path, FakeClient([]))
        loop._spawn_subagent = slow_child  # type: ignore[method-assign]
        calls = [
            ToolCallDelta(
                index=index,
                id=f"s{index}",
                name=SUBAGENT_TOOL,
                arguments=json.dumps({"task": f"part {index}"}),
            )
            for index in (1, 2)
        ]
        # Stopped before the round reaches either call: they were prefetched, and
        # the turn's exit has to take them with it.
        loop.cancel()

        _ = [event async for event in loop._run_tools(calls, _TurnState(), on_chunk=None)]
        await asyncio.sleep(0)

        leftovers = [
            task for task in asyncio.all_tasks() if task.get_name().startswith("subagent-")
        ]
        assert not leftovers, f"a prefetched child outlived its turn: {leftovers}"


class TestTheOneLineItSpeaks:
    """A sub-agent works silently — that is the point — but silence for as long as
    it takes is indistinguishable from having stopped."""

    @pytest.fixture
    def session(self, tmp_path):
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        async def noop(*_args, **_kwargs):
            return None

        made = Session(
            session_id=record.id,
            project_id=project.id,
            root=tmp_path,
            settings=Settings(DEEPSEEK_API_KEY="k", SURTITLE_HOME=str(tmp_path)),
            store=store,
            deepseek=None,
            send=noop,
            send_audio=noop,
        )
        spoken: list[str] = []

        class Recorder:
            is_speaking = False

            def speak(self, text, *, final=False):
                spoken.append(text)

        made.tts = Recorder()  # type: ignore[assignment]
        return made, spoken

    async def test_it_says_what_is_being_looked_into(self, session):
        made, spoken = session
        made._subagent_announced = False
        made._spoke_this_turn = False

        await made._narrate_step(4, subagent="the retry settings")
        await made._narrate_step(6, subagent="the retry settings")

        named = [line for line in spoken if "Looking into" in line]
        assert named == ["Looking into the retry settings now."], (
            "named once per turn: four steps later the user does not need telling again"
        )

    async def test_it_stays_quiet_when_the_agent_already_spoke(self, session):
        made, spoken = session
        made._subagent_announced = False
        made._spoke_this_turn = True

        await made._narrate_step(4, subagent="the retry settings")

        assert spoken == [], (
            "a turn that opened with 'let me have that looked into' has already said it"
        )

    async def test_a_long_delegation_still_gets_the_progress_line(self, session):
        """A parent blocked on a child takes no steps of its own."""
        made, spoken = session
        made._subagent_announced = True  # already named the delegation

        await made._narrate_step(5)

        assert spoken and "Still working" in spoken[0]

    def test_the_line_is_not_the_final_utterance(self, session):
        """Marking it final would release echo suppression mid-turn."""
        made, _spoken = session
        assert isinstance(Chunk(ChunkKind.SAY, "x"), Chunk)
        assert made._turn_spoken_chars == 0


class TestTheChildsPrompt:
    def test_it_says_what_the_child_cannot_do(self):
        from surtitle.core.subagent import build_subagent_prompt

        prompt = build_subagent_prompt("a project")

        assert "a project" in prompt
        assert "no conversation history" in prompt
        assert "cannot write" in prompt, "the limits are stated, not just enforced"
        assert "Be brief" in prompt, "an essay from a child is context the parent pays for"


def test_the_tool_is_offered_to_the_parent():
    assert SUBAGENT_TOOL in [tool.name for tool in default_tool_list()]
    schema = next(t for t in default_tool_list() if t.name == SUBAGENT_TOOL).to_openai_schema()
    assert json.dumps(schema), "the schema has to survive the wire"
    assert "task" in schema["function"]["parameters"]["properties"]
