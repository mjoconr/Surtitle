"""Tests for what the agent is primed with, and what it remembers doing.

Both come from a real session. Across three turns the agent ran the same sequence
every time:

    list_dir -> search -> read AGENTS.md -> read README.md -> read plant.hosts
             -> read CURRENT_STATE.md -> read dsh-memory -> run the same shell commands

It was not being lazy; it had amnesia. Tool calls and their results were never
persisted, so history was rebuilt from prose alone and the model had no way to see
that it had already looked. And the project's own instructions -- AGENTS.md, a
current-state note -- were only seen if the model happened to read them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from surtitle.config import Settings
from surtitle.core.agent import _action_line, _with_actions, build_system_prompt
from surtitle.core.session import Session
from surtitle.store.db import Store
from surtitle.tools.fs_tools import ToolResult


class TestActionLines:
    """The transcript must record the work, not only the answer."""

    def test_a_read_is_summarised_with_its_path(self):
        line = _action_line(
            "read_file", {"path": "AGENTS.md"}, ToolResult(ok=True, display="Read 74 line(s)")
        )
        assert "AGENTS.md" in line
        assert "74" in line

    def test_a_command_is_summarised_with_the_command(self):
        line = _action_line(
            "run_shell",
            {"command": "./dsh-memory 2G-120"},
            ToolResult(ok=True, display="finished in 1377 ms"),
        )
        assert "./dsh-memory 2G-120" in line
        assert "1377" in line

    def test_a_failure_is_marked_as_such(self):
        line = _action_line("run_shell", {"command": "bad"}, ToolResult(ok=False, error="exit 1"))
        assert "FAILED" in line
        assert "exit 1" in line

    def test_a_search_records_its_pattern(self):
        line = _action_line(
            "search_files", {"pattern": "4C-120|2G-120"}, ToolResult(ok=True, display="2 matches")
        )
        assert "4C-120" in line

    def test_long_values_are_truncated(self):
        """These lines are replayed every turn, so they must stay cheap."""
        line = _action_line("run_shell", {"command": "x" * 500}, ToolResult(ok=True, display="ok"))
        assert len(line) < 200

    def test_newlines_in_a_command_do_not_break_the_listing(self):
        line = _action_line(
            "run_shell",
            {"command": "line one\nline two"},
            ToolResult(ok=True, display="ok"),
        )
        assert "\n" not in line

    def test_a_tool_without_arguments_still_records(self):
        line = _action_line("list_dir", {}, ToolResult(ok=True, display="Listed 31 item(s)"))
        assert "list_dir" in line
        assert "31" in line


class TestWorkIsAppended:
    def test_the_answer_comes_first_and_the_work_after(self):
        text = _with_actions("The machine is down.", ["read_file(plant.hosts) -> ok"])
        assert text.startswith("The machine is down.")
        assert "[work this turn]" in text
        assert "read_file(plant.hosts)" in text

    def test_a_turn_with_no_tools_is_unchanged(self):
        assert _with_actions("Just talking.", []) == "Just talking."

    def test_a_turn_with_work_but_no_answer_still_records_the_work(self):
        text = _with_actions("", ["list_dir -> Listed 3 item(s)"])
        assert "[work this turn]" in text
        assert "list_dir" in text


class TestProcessPrompting:
    """The prompt must ask for a process, not just for tags."""

    def test_it_forbids_repeating_work(self):
        prompt = build_system_prompt("p")
        assert "already did" in prompt or "already read" in prompt
        assert "[work this turn]" in prompt

    def test_it_asks_for_known_versus_assumed(self):
        prompt = build_system_prompt("p")
        assert "assuming" in prompt
        assert "inferring" in prompt or "inference" in prompt

    def test_it_asks_for_an_order_of_work(self):
        prompt = build_system_prompt("p")
        assert "Work in an order" in prompt

    def test_the_spoken_contract_survives(self):
        """The voice contract must not have been displaced by process guidance."""
        prompt = build_system_prompt("p")
        assert "<say>" in prompt
        assert "<display>" in prompt


@pytest.fixture
def project_session(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("P", tmp_path)
    record = store.create_session(project.id)
    settings = Settings(
        DEEPSEEK_API_KEY="sk-test", SURTITLE_HOME=str(tmp_path), voice_enabled=False
    )

    async def send(_payload):
        return None

    async def send_audio(_data):
        return None

    session = Session(
        session_id=record.id,
        project_id=project.id,
        root=tmp_path,
        settings=settings,
        store=store,
        deepseek=None,
        send=send,
        send_audio=send_audio,
    )
    from surtitle.tools.project_config import load_project_config

    session.project_config = load_project_config(tmp_path)
    return session, tmp_path


class TestProjectInstructionsAreInjected:
    """A project that documents how to work in it should not have to be asked."""

    def test_agents_md_is_loaded(self, project_session):
        session, root = project_session
        (root / "AGENTS.md").write_text("# House rules\nAlways check plant.hosts first.\n")
        prompt = session._system_prompt()
        assert "House rules" in prompt
        assert "check plant.hosts first" in prompt

    def test_contributing_is_loaded_as_a_convention_source(self, project_session):
        session, root = project_session
        (root / "CONTRIBUTING.md").write_text("Run the tests before committing.\n")
        assert "Run the tests before committing" in session._system_prompt()

    def test_config_instructions_are_loaded(self, project_session):
        session, _root = project_session
        session.project_config.instructions = "Machines are named <rack>-<slot>."
        assert "Machines are named" in session._system_prompt()

    def test_unconventional_files_are_not_slurped(self, project_session):
        """Only instruction files, not arbitrary documentation."""
        session, root = project_session
        (root / "random_notes.md").write_text("SECRET NOT AN INSTRUCTION")
        assert "SECRET NOT AN INSTRUCTION" not in session._system_prompt()

    def test_a_large_instruction_file_is_capped(self, project_session):
        session, root = project_session
        (root / "AGENTS.md").write_text("x" * 50000)
        prompt = session._system_prompt()
        assert len(prompt) < 20000, "an unbounded instruction file would crowd the conversation"

    def test_the_base_prompt_is_always_present(self, project_session):
        session, _root = project_session
        prompt = session._system_prompt()
        assert "<say>" in prompt
        assert "Check what you already did" in prompt

    def test_it_is_read_fresh_each_turn(self, project_session):
        """Editing AGENTS.md mid-session should take effect without a restart."""
        session, root = project_session
        (root / "AGENTS.md").write_text("first revision")
        assert "first revision" in session._system_prompt()
        (root / "AGENTS.md").write_text("second revision")
        assert "second revision" in session._system_prompt()


class TestProjectNotebook:
    """Knowledge must survive a session, or every chat re-derives everything."""

    def test_a_fresh_project_has_no_notes(self, project_session):
        from surtitle.tools.environment import project_notes

        _session, root = project_session
        assert project_notes(root) == ""

    def test_notes_are_appended_not_replaced(self, project_session):
        from surtitle.tools.environment import project_notes, write_notes

        _session, root = project_session
        write_notes(root, "4C-120 is down.")
        write_notes(root, "The report lives in docs/.")
        text = project_notes(root)
        assert "4C-120 is down." in text
        assert "report lives in docs/" in text

    def test_replacing_is_available_for_corrections(self, project_session):
        from surtitle.tools.environment import project_notes, write_notes

        _session, root = project_session
        write_notes(root, "4C-120 is down.")
        write_notes(root, "4C-120 is back up.", append=False)
        text = project_notes(root)
        assert "back up" in text
        assert "is down" not in text

    def test_empty_notes_are_refused(self, project_session):
        from surtitle.tools.environment import write_notes

        _session, root = project_session
        ok, message = write_notes(root, "   ")
        assert ok is False
        assert "nothing to record" in message.lower()

    def test_the_notebook_is_injected_into_the_prompt(self, project_session):
        from surtitle.tools.environment import write_notes

        session, root = project_session
        write_notes(root, "Machines are named <rack>-<slot>.")
        prompt = session._system_prompt()
        assert "## Project notebook" in prompt
        assert "Machines are named" in prompt

    def test_an_enormous_notebook_keeps_the_newest_notes(self, project_session):
        """It is injected every turn, so it must not be able to crowd the context."""
        from surtitle.tools.environment import project_notes, write_notes

        _session, root = project_session
        write_notes(root, "OLDEST MARKER")
        write_notes(root, "x" * 40000)
        write_notes(root, "NEWEST MARKER")
        text = project_notes(root)
        assert "NEWEST MARKER" in text, "the most recent note was dropped"
        assert len(text) < 10000, "the notebook was not capped"

    async def test_the_remember_tool_writes_the_notebook(self, project_session):
        from surtitle.tools.environment import project_notes
        from surtitle.tools.fs_tools import ToolContext
        from surtitle.tools.registry import default_registry

        _session, root = project_session
        registry = default_registry()
        result = await registry.dispatch(
            "remember", ToolContext(root=root), {"note": "2G-120 runs the sampling line."}
        )
        assert result.ok, result.error
        assert "2G-120 runs the sampling line" in project_notes(root)

    async def test_the_remember_tool_appends_by_default(self, project_session):
        from surtitle.tools.environment import project_notes
        from surtitle.tools.fs_tools import ToolContext
        from surtitle.tools.registry import default_registry

        _session, root = project_session
        registry = default_registry()
        ctx = ToolContext(root=root)
        await registry.dispatch("remember", ctx, {"note": "first fact"})
        await registry.dispatch("remember", ctx, {"note": "second fact"})
        text = project_notes(root)
        assert "first fact" in text and "second fact" in text

    async def test_an_empty_note_is_rejected(self, project_session):
        from surtitle.tools.fs_tools import ToolContext
        from surtitle.tools.registry import default_registry

        _session, root = project_session
        result = await default_registry().dispatch(
            "remember", ToolContext(root=root), {"note": "  "}
        )
        assert not result.ok

    async def test_read_notes_reports_an_empty_notebook(self, project_session):
        from surtitle.tools.fs_tools import ToolContext
        from surtitle.tools.registry import default_registry

        _session, root = project_session
        result = await default_registry().dispatch("read_notes", ToolContext(root=root), {})
        assert result.ok
        assert result.data["exists"] is False

    def test_the_notebook_needs_no_approval(self, project_session):
        """It is the agent's own working memory; prompting would defeat its purpose."""
        from surtitle.tools.registry import default_registry

        registry = default_registry()
        assert registry.requires_approval("remember", {"note": "x"}) is False
        assert registry.requires_approval("read_notes", {}) is False


class TestProjectBriefing:
    def test_the_working_directory_and_top_level_are_named(self, project_session):
        session, root = project_session
        (root / "plant.hosts").write_text("2G-120\n")
        (root / "docs").mkdir()
        prompt = session._system_prompt()
        assert "## Project briefing" in prompt
        assert str(root) in prompt
        assert "plant.hosts" in prompt

    def test_hidden_directories_are_not_advertised(self, project_session):
        session, root = project_session
        (root / ".surtitle").mkdir()
        (root / "visible.txt").write_text("x")
        prompt = session._system_prompt()
        assert "visible.txt" in prompt
        assert ".surtitle" not in prompt.split("## Project briefing")[1][:200]
