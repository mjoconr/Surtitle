"""The conversation's objective, which is not the same thing as its plan.

The plan says what is being done now. The goal says what it is all for, and it is
the broader of the two: every plan item can be ticked while the thing the user
asked for is still not done, and that is the case a plan cannot catch. It is also
the thing that has to stop driving a turn once it is reached, or a finished
conversation gets nudged onward forever.
"""

from __future__ import annotations

import pytest
from tests.test_agent_loop import FakeClient, text_script, tool_script

from surtitle.config import Settings
from surtitle.core.agent import AgentLoop
from surtitle.core.events import EventKind
from surtitle.store.db import Store
from surtitle.tools.fs_tools import ToolContext
from surtitle.tools.registry import GOAL_TOOL, ToolRegistry, _goal_write_handler, default_tool_list


@pytest.fixture
def stored(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("P", tmp_path)
    record = store.create_session(project.id)
    return store, record.id


@pytest.fixture
def ctx(stored):
    store, session_id = stored
    return ToolContext(
        root=store.get_session(session_id).root and __import__("pathlib").Path("."),
        store=store,
        session_id=session_id,
    )


class TestTheRecord:
    def test_a_goal_is_set_and_read_back(self, stored):
        store, session_id = stored

        store.set_goal(session_id, "get the cap into 122 without losing the throw")

        record = store.get_session(session_id)
        assert record.goal == "get the cap into 122 without losing the throw"
        assert record.goal_achieved is False

    def test_reaching_it_keeps_the_text(self, stored):
        """What was wanted is worth keeping after it has been got."""
        store, session_id = stored
        store.set_goal(session_id, "ship 0.12")

        store.set_goal(session_id, None, achieved=True)

        record = store.get_session(session_id)
        assert record.goal == "ship 0.12"
        assert record.goal_achieved is True

    def test_clearing_it_removes_it(self, stored):
        store, session_id = stored
        store.set_goal(session_id, "something")

        store.set_goal(session_id, None)

        assert store.get_session(session_id).goal is None

    def test_whitespace_is_not_a_goal(self, stored):
        store, session_id = stored

        store.set_goal(session_id, "   ")

        assert store.get_session(session_id).goal is None

    def test_an_older_database_gains_the_columns(self, tmp_path):
        """The upgrade path, not the fresh schema: `CREATE TABLE IF NOT EXISTS`
        does nothing to a table that already exists."""
        import sqlite3

        path = tmp_path / "db.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL,
                created_at REAL NOT NULL, last_opened_at REAL NOT NULL,
                auto_approved TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'New conversation', created_at REAL NOT NULL,
                updated_at REAL NOT NULL);
            """
        )
        conn.execute(
            "INSERT INTO projects (id, name, root, created_at, last_opened_at)"
            " VALUES ('p1', 'P', ?, 1.0, 1.0)",
            (str(tmp_path),),
        )
        conn.execute(
            "INSERT INTO sessions (id, project_id, title, created_at, updated_at)"
            " VALUES ('s1', 'p1', 'Old', 1.0, 1.0)"
        )
        conn.commit()
        conn.close()

        store = Store(path)
        store.set_goal("s1", "recorded after the upgrade")

        assert store.get_session("s1").goal == "recorded after the upgrade"


class TestTheTool:
    def test_a_goal_is_recorded(self, stored):
        store, session_id = stored
        ctx = ToolContext(root=__import__("pathlib").Path("."), store=store, session_id=session_id)

        result = _goal_write_handler(ctx, goal="finish the sim")

        assert result.ok is True
        assert store.get_session(session_id).goal == "finish the sim"
        assert "finish the sim" in (result.display or "")

    def test_achieved_marks_it_without_a_new_text(self, stored):
        store, session_id = stored
        store.set_goal(session_id, "finish the sim")
        ctx = ToolContext(root=__import__("pathlib").Path("."), store=store, session_id=session_id)

        result = _goal_write_handler(ctx, achieved=True)

        assert (result.data or {})["goal_achieved"] is True
        assert store.get_session(session_id).goal == "finish the sim"

    def test_no_arguments_clears_it(self, stored):
        store, session_id = stored
        store.set_goal(session_id, "finish the sim")
        ctx = ToolContext(root=__import__("pathlib").Path("."), store=store, session_id=session_id)

        result = _goal_write_handler(ctx)

        assert result.ok is True
        assert store.get_session(session_id).goal is None

    def test_without_a_store_it_is_a_result_not_a_crash(self, tmp_path):
        result = _goal_write_handler(ToolContext(root=tmp_path), goal="x")

        assert result.ok is False
        assert "nowhere" in (result.error or "")

    def test_it_needs_no_approval_because_it_changes_nothing_outside_the_conversation(self):
        tool = next(t for t in default_tool_list() if t.name == GOAL_TOOL)

        assert tool.approval == "never"
        assert tool.mutating is False

    def test_a_sub_agent_cannot_set_the_parents_goal(self):
        """It has no conversation of its own for a goal to belong to."""
        assert GOAL_TOOL not in ToolRegistry(default_tool_list()).read_only().names()

    def test_it_is_offered_to_the_agent(self):
        assert GOAL_TOOL in [tool.name for tool in default_tool_list()]


def make_loop(settings, tmp_path, client, **kwargs):
    return AgentLoop(
        settings,
        root=tmp_path,
        session_id=kwargs.pop("session_id", "s1"),
        client=client,
        registry=ToolRegistry(default_tool_list()),
        **kwargs,
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DEEPSEEK_API_KEY="test-key",
        DEEPGRAM_API_KEY="test-key",
        max_steps=6,
        thinking_enabled=False,
    )


class TestItDrivesTheTurn:
    """The original complaint, one level up: it stopped, and needed prompting."""

    async def test_a_turn_that_stops_with_the_goal_standing_is_asked_to_carry_on(
        self, settings, tmp_path, stored
    ):
        store, session_id = stored
        store.set_goal(session_id, "get the whole suite green")
        client = FakeClient(
            [
                # It does some work, then stops with a summary instead of finishing.
                tool_script(name="list_dir", arguments={"path": "."}),
                text_script("<say>I have made a start.</say>"),
                text_script("<say>All done now.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        events = [event async for event in loop.run([], "get the suite green")]

        assert len(client.calls) == 3, "the goal standing is enough to ask once more"
        nudge = client.calls[2]["messages"][-1]
        assert nudge["role"] == "user"
        assert "get the whole suite green" in nudge["content"], (
            "the ask names the objective, so the agent does not have to guess it"
        )
        assert events[-1].kind is EventKind.DONE
        assert events[-1].data["reason"] == "complete"

    async def test_a_reached_goal_does_not_drive_anything(self, settings, tmp_path, stored):
        store, session_id = stored
        store.set_goal(session_id, "get the whole suite green")
        store.set_goal(session_id, None, achieved=True)
        client = FakeClient(
            [
                tool_script(name="list_dir", arguments={"path": "."}),
                text_script("<say>Done, and the suite is green.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await _drain(loop)

        assert len(client.calls) == 2, "a goal that is done is not a reason to keep going"

    async def test_the_plan_is_named_when_both_are_standing(self, settings, tmp_path, stored):
        """The more specific instruction is the better one."""
        store, session_id = stored
        store.set_goal(session_id, "get the whole suite green")
        store.set_todos(session_id, [{"content": "Fix the parser test", "status": "in_progress"}])
        client = FakeClient(
            [
                tool_script(name="list_dir", arguments={"path": "."}),
                text_script("<say>Started.</say>"),
                text_script("<say>Finished.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await _drain(loop)

        nudge = client.calls[2]["messages"][-1]["content"]
        assert "Fix the parser test" in nudge
        assert "plan on the user's screen" in nudge, "the plan is the specific one, so it is named"

    async def test_the_ask_happens_once(self, settings, tmp_path, stored):
        store, session_id = stored
        store.set_goal(session_id, "never finish")
        client = FakeClient(
            [
                tool_script(name="list_dir", arguments={"path": "."}),
                text_script("<say>Stopped.</say>"),
                text_script("<say>Still stopped.</say>"),
                text_script("<say>And again.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await _drain(loop)

        assert len(client.calls) == 3, "a model that answers twice is answering"

    async def test_a_question_with_no_tools_is_not_nudged(self, settings, tmp_path, stored):
        """Answering from what it knows is not a pause in the work."""
        store, session_id = stored
        store.set_goal(session_id, "get the whole suite green")
        client = FakeClient([text_script("<say>The goal is still green-suite.</say>")])
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        await _drain(loop)

        assert len(client.calls) == 1


class TestItReachesTheUi:
    async def test_the_goal_arrives_as_its_own_event(self, settings, tmp_path, stored):
        """The plan is echoed that way for the same reason: it is state the user
        watches, not something to be read out of a tool result."""
        store, session_id = stored
        client = FakeClient(
            [
                tool_script(name=GOAL_TOOL, arguments={"goal": "ship it"}),
                text_script("<say>Recorded.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, store=store, session_id=session_id)

        events = [event async for event in loop.run([], "set a goal")]

        goal_events = [event for event in events if event.kind is EventKind.GOAL]
        assert goal_events, "the UI is told rather than left to watch the tool result"
        assert goal_events[0].data["goal"] == "ship it"
        assert goal_events[0].data["achieved"] is False

    def test_a_reopened_conversation_carries_it(self, tmp_path):
        from starlette.testclient import TestClient

        from surtitle.server import create_app_for

        home = tmp_path / "home"
        home.mkdir()
        settings = Settings(
            DEEPSEEK_API_KEY="sk-test-deepseek-1234567890",
            SURTITLE_HOME=str(home),
            voice_enabled=False,
        )
        store = Store(settings.db_path)
        project = store.create_project("P", tmp_path / "work")
        record = store.create_session(project.id)
        store.set_goal(record.id, "keep the handoff working")
        store.close()

        app = create_app_for(settings)
        with TestClient(app) as client:
            payload = client.get(f"/api/sessions/{record.id}").json()

        assert payload["goal"] == "keep the handoff working"
        assert payload["goal_achieved"] is False


class TestItIsReplayed:
    async def test_the_goal_reaches_the_agent_every_turn(self, tmp_path, stored):
        """The transient notes are built per turn from the store, so losing the
        conversation cannot lose the objective with it."""
        from surtitle.core.session import Session

        store, session_id = stored
        store.set_goal(session_id, "keep the handoff working")

        async def noop(*_args, **_kwargs):
            return None

        session = Session(
            session_id=session_id,
            project_id=store.get_session(session_id).project_id,
            root=tmp_path,
            settings=Settings(DEEPSEEK_API_KEY="k", SURTITLE_HOME=str(tmp_path)),
            store=store,
            deepseek=None,
            send=noop,
            send_audio=noop,
        )

        note = session._context_note()

        assert "keep the handoff working" in note
        assert "What this conversation is for" in note

    async def test_a_reached_goal_is_replayed_as_reached(self, tmp_path, stored):
        from surtitle.core.session import Session

        store, session_id = stored
        store.set_goal(session_id, "keep the handoff working")
        store.set_goal(session_id, None, achieved=True)

        async def noop(*_args, **_kwargs):
            return None

        session = Session(
            session_id=session_id,
            project_id=store.get_session(session_id).project_id,
            root=tmp_path,
            settings=Settings(DEEPSEEK_API_KEY="k", SURTITLE_HOME=str(tmp_path)),
            store=store,
            deepseek=None,
            send=noop,
            send_audio=noop,
        )

        assert "achieved" in session._context_note()


async def _drain(loop) -> None:
    async for _event in loop.run([], "go"):
        pass
