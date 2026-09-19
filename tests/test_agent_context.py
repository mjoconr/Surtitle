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

    def test_a_command_keeps_the_output_it_produced(self):
        """A command's stdout is the one thing a later turn cannot look up again.

        Observed: the agent checked `import sherpa_onnx`, got ModuleNotFoundError,
        and on the next turn re-ran the same check because the transcript recorded
        only "finished in 146 ms". It then doubted its own finding and asked the
        user to confirm a fact it had already established.
        """
        line = _action_line(
            "run_shell",
            {"command": "python -c 'import sherpa_onnx'"},
            ToolResult(
                ok=False,
                data={"stdout": "", "stderr": "ModuleNotFoundError: No module named 'sherpa_onnx'"},
                error="`python -c` failed (exit 1)",
            ),
        )
        assert "ModuleNotFoundError" in line, f"the output was dropped: {line!r}"
        assert "sherpa_onnx" in line
        assert "failed" in line.lower(), "the exit status must survive too"

    def test_a_successful_command_keeps_its_output(self):
        line = _action_line(
            "run_shell",
            {"command": "uname -a"},
            ToolResult(ok=True, data={"stdout": "Darwin MacBookPro 24.6.0 arm64\n" * 4}),
        )
        assert "Darwin MacBookPro" in line

    def test_a_noisy_output_is_bounded(self):
        """These lines are replayed every turn, so output must not be unbounded."""
        line = _action_line(
            "run_shell",
            {"command": "cat big.log"},
            ToolResult(ok=True, data={"stdout": "y" * 50000}),
        )
        assert len(line) < 600, f"an unbounded output reached the transcript ({len(line)} chars)"

    def test_output_stays_on_one_line(self):
        line = _action_line(
            "run_shell",
            {"command": "ls"},
            ToolResult(ok=True, data={"stdout": "a\nb\nc\n"}),
        )
        assert "\n" not in line

    def test_a_truncated_result_says_so(self):
        line = _action_line(
            "run_shell",
            {"command": "find ."},
            ToolResult(ok=True, data={"stdout": "a\nb"}, truncated=True),
        )
        assert "truncated" in line


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
        assert "Assumed" in prompt
        assert "Checked" in prompt and "Told" in prompt

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
        """An oversized file is shown in part, and declared as partial.

        It used to be skipped entirely, on the grounds that a fragment reads as the
        whole document. For the project's *primary* instruction file that trade is
        the wrong way round: conventions and accumulated learnings live there, so
        dropping it means starting the session knowing nothing about the project.
        The fragment-is-not-the-whole concern is met by saying so explicitly.
        """
        session, root = project_session
        (root / "AGENTS.md").write_text("x" * 50000)
        prompt = session._system_prompt()
        assert "x" * 5000 in prompt, "the primary file must be present, if only in part"
        assert "AGENTS.md" in prompt
        assert "truncated; read the file for the rest" in prompt
        assert "Instructions shown in part" in prompt, "a fragment must be declared partial"
        # The file is capped at 20,000 characters, and the prompt is that plus the
        # standing instructions and a little project context — so the total tracks
        # the cap. The bound is loose on purpose: what it catches is an unbounded
        # file, not a prompt that grew by a paragraph (the version-control primer
        # added about 1,700 characters when it was introduced, and the section
        # describing the interface about 1,200).
        assert len(prompt) < 36_000, "an unbounded instruction file crowded the conversation"

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


class TestNeverAssume:
    """The project's standing rule: an assumption is never stated as fact.

    This is a design commitment, not a style preference. A guess presented as fact
    is the most damaging output this agent can produce, because the user acts on it.
    These tests keep the commitment from quietly weakening.
    """

    def test_assumption_is_named_as_the_standing_rule(self):
        prompt = build_system_prompt("p")
        assert "Never state an assumption as fact" in prompt

    def test_the_three_epistemic_states_are_defined(self):
        prompt = build_system_prompt("p")
        for state in ("Checked", "Told", "Assumed"):
            assert state in prompt, f"{state!r} must be defined as a category"

    def test_only_checked_and_told_may_be_stated_plainly(self):
        prompt = build_system_prompt("p")
        assert "Only the first two may be stated plainly" in prompt

    def test_filling_gaps_with_plausible_values_is_forbidden(self):
        prompt = build_system_prompt("p")
        assert "Do not fill a gap" in prompt

    def test_describing_unopened_files_is_forbidden(self):
        prompt = build_system_prompt("p")
        assert "before you have opened or run it" in " ".join(prompt.split())

    def test_ambiguity_requires_a_question_not_a_guess(self):
        prompt = build_system_prompt("p")
        assert "ask which one is meant" in " ".join(prompt.split())

    def test_not_knowing_is_explicitly_acceptable(self):
        prompt = build_system_prompt("p")
        # The phrasing wraps, so match on a stable fragment.
        assert "acceptable answer" in prompt

    def test_volume_is_not_a_substitute_for_knowing(self):
        # The prompt is wrapped, so compare on normalised whitespace.
        assert "not a substitute for knowing" in " ".join(build_system_prompt("p").split())

    def test_it_asks_for_evidence_to_be_named(self):
        """An answer should say what it is based on, so the user can judge it."""
        prompt = build_system_prompt("p")
        assert "what you based it on" in " ".join(prompt.split()) or "name what you based" in prompt

    def test_the_rule_coexists_with_the_voice_contract(self):
        """Process guidance must not displace the spoken/displayed split."""
        prompt = build_system_prompt("p")
        assert "<say>" in prompt
        assert "<display>" in prompt
        assert "Never state an assumption as fact" in prompt

    def test_a_checkable_assumption_is_checked_rather_than_offered(self):
        """The rule must not teach the agent to announce its uncertainty.

        It used to demonstrate the labelling with "I have not confirmed this, but
        … Shall I check?", which is exactly the behaviour the user objected to:
        checking is the standing expectation, so saying that you are about to do
        it is noise on every turn, and it was crowding out the answer. An
        assumption worth stating is one looking cannot settle.
        """
        prompt = " ".join(build_system_prompt("p").split())
        assert "Shall I check?" not in prompt, (
            "offering to check something checkable is the same sentence every turn"
        )
        assert "is not a disclaimer to hand the user" in prompt, (
            "the rule must say that a checkable assumption is the next thing to do"
        )

    def test_announcing_a_check_is_forbidden(self):
        prompt = " ".join(build_system_prompt("p").split())
        assert "Checking is the default, so do not announce it" in prompt, (
            "without this the model narrates 'I'll need to look at that' instead of looking"
        )


class TestRepeatCallGuard:
    """A repeated identical call cannot make progress, so it is refused.

    DSH guards this mechanically with a counter and an injected reminder rather
    than with prompt advice, which is the right shape: advice can be ignored, and a
    refusal cannot. Observed here, the same files were read and the same commands
    re-run on every turn with nothing stopping it.
    """

    def test_the_first_calls_are_allowed(self):
        from surtitle.core.agent import RepeatCallGuard

        guard = RepeatCallGuard()
        for _ in range(2):
            guard.check("read_file", {"path": "AGENTS.md"})
            assert guard.reminder() is None

    def test_the_third_identical_call_is_refused(self):
        from surtitle.core.agent import RepeatCallGuard

        guard = RepeatCallGuard()
        for _ in range(3):
            guard.check("read_file", {"path": "AGENTS.md"})
        assert guard.reminder() is not None

    def test_every_repeat_past_the_threshold_is_refused(self):
        """Letting the calls in between through would defeat the guard."""
        from surtitle.core.agent import RepeatCallGuard

        guard = RepeatCallGuard()
        refusals = 0
        for _ in range(8):
            guard.check("read_file", {"path": "AGENTS.md"})
            if guard.reminder() is not None:
                refusals += 1
        assert refusals >= 6, f"only {refusals} of 8 repeats were refused"

    def test_different_arguments_reset_the_count(self):
        from surtitle.core.agent import RepeatCallGuard

        guard = RepeatCallGuard()
        guard.check("read_file", {"path": "a.txt"})
        guard.check("read_file", {"path": "a.txt"})
        guard.check("read_file", {"path": "b.txt"})
        guard.check("read_file", {"path": "b.txt"})
        assert guard.reminder() is None, "reading a different file must be allowed"

    def test_argument_order_does_not_hide_a_repeat(self):
        from surtitle.core.agent import RepeatCallGuard

        guard = RepeatCallGuard()
        guard.check("search_files", {"pattern": "x", "glob": "*.md"})
        guard.check("search_files", {"glob": "*.md", "pattern": "x"})
        guard.check("search_files", {"pattern": "x", "glob": "*.md"})
        assert guard.reminder() is not None, "key order must not defeat the guard"

    def test_the_reminder_carries_the_result_already_obtained(self):
        from surtitle.core.agent import RepeatCallGuard

        guard = RepeatCallGuard()
        for _ in range(2):
            guard.check("read_file", {"path": "AGENTS.md"})
        guard.observe_result("Read 74 line(s) from AGENTS.md: house rules follow")
        guard.check("read_file", {"path": "AGENTS.md"})
        reminder = guard.reminder()
        assert reminder is not None
        assert "house rules follow" in reminder, "the model must be given what it already has"
        assert "cannot change" in reminder

    def test_it_tells_the_model_to_do_something_different(self):
        from surtitle.core.agent import RepeatCallGuard

        guard = RepeatCallGuard()
        for _ in range(3):
            guard.check("run_shell", {"command": "./dsh-memory 2G-120"})
        reminder = guard.reminder() or ""
        assert "different" in reminder or "Stop repeating" in reminder


class TestGuardIsWiredIntoTheLoop:
    """The guard must actually intervene, not merely exist."""

    async def test_a_repeating_model_is_refused_rather_than_looping(self, tmp_path):
        import json as jsonlib

        from surtitle.config import Settings
        from surtitle.core.agent import AgentLoop
        from surtitle.core.events import EventKind
        from surtitle.llm.deepseek import StreamEvent, ToolCallDelta
        from surtitle.tools.registry import Tool, ToolRegistry

        calls: list[str] = []

        def handler(ctx, path="x"):
            from surtitle.tools.fs_tools import ToolResult

            calls.append(path)
            return ToolResult(ok=True, display=f"read {path}")

        registry = ToolRegistry(
            [
                Tool(
                    name="read_file",
                    description="read",
                    parameters={"type": "object", "properties": {}},
                    handler=handler,
                    approval="never",
                )
            ]
        )

        class RepeatingModel:
            """Always asks for the same call, never concludes."""

            async def stream(self, messages, *, tools=None):
                for _ in range(2):
                    call = ToolCallDelta(
                        index=0,
                        id="c1",
                        name="read_file",
                        arguments=jsonlib.dumps({"path": "AGENTS.md"}),
                    )
                    yield StreamEvent(kind="tool_call", tool_call=call)
                yield StreamEvent(kind="done", finish_reason="tool_calls")

            async def aclose(self):
                return None

        settings = Settings(
            DEEPSEEK_API_KEY="sk",
            SURTITLE_HOME=str(tmp_path),
            max_steps=8,
            thinking_enabled=False,
        )
        loop = AgentLoop(settings, root=tmp_path, client=RepeatingModel(), registry=registry)

        outcomes = []
        async for event in loop.run([], "read it"):
            if event.kind is EventKind.TOOL_RESULT:
                outcomes.append(event.data)

        refused = [item for item in outcomes if "refused" in (item.get("display") or "")]
        assert refused, f"a repeating model was never refused: {outcomes}"
        # The tool must not have been executed on the refused attempts.
        assert len(calls) < 8, f"the tool ran {len(calls)} times despite the guard"


class TestGuardSpansTurns:
    """A repeat on the *next* turn is still a repeat.

    Observed: the agent verified `import sherpa_onnx` on one turn and re-ran the
    same check on the next. A guard rebuilt per turn could never see that, which is
    why it is owned by the session and handed to each turn.
    """

    def test_a_loop_uses_the_guard_it_is_given(self, tmp_path):
        from surtitle.core.agent import AgentLoop, RepeatCallGuard

        guard = RepeatCallGuard()
        loop = AgentLoop(
            Settings(DEEPSEEK_API_KEY="sk", SURTITLE_HOME=str(tmp_path)),
            root=tmp_path,
            repeat_guard=guard,
        )
        assert loop._repeat_guard is guard, "the turn made its own guard, so repeats span nothing"

    def test_a_loop_without_one_still_gets_a_guard(self, tmp_path):
        from surtitle.core.agent import AgentLoop, RepeatCallGuard

        loop = AgentLoop(
            Settings(DEEPSEEK_API_KEY="sk", SURTITLE_HOME=str(tmp_path)),
            root=tmp_path,
        )
        assert isinstance(loop._repeat_guard, RepeatCallGuard)

    async def test_each_turn_is_given_the_sessions_guard(self, project_session, monkeypatch):
        session, _root = project_session
        handed: list[object] = []

        class RecordingLoop:
            def __init__(self, *args, **kwargs):
                handed.append(kwargs.get("repeat_guard"))

            def set_emitter(self, emitter):
                return None

            async def run(self, history, user_text, *, on_chunk=None):
                if False:  # pragma: no cover - makes this an async generator
                    yield None

            async def aclose(self):
                return None

        monkeypatch.setattr("surtitle.core.session.AgentLoop", RecordingLoop)

        await session._run_turn("first")
        await session._run_turn("second")

        assert len(handed) == 2
        assert handed[0] is session._repeat_guard
        assert handed[0] is handed[1], "the guard was rebuilt between turns"


class TestStoredToolCallKeepsItsOutput:
    """The durable record is evidence, not a status line.

    `tool_calls.result` used to hold "`cmd` finished in 146 ms" for every shell
    call, which is why a past session's work could not be audited after the fact.
    """

    async def test_a_shell_call_stores_what_it_printed(self, tmp_path):
        import json as jsonlib

        from surtitle.core.agent import AgentLoop
        from surtitle.llm.deepseek import StreamEvent, ToolCallDelta
        from surtitle.tools.registry import Tool, ToolRegistry

        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        def handler(ctx, command=""):
            return ToolResult(
                ok=False,
                data={"stdout": "", "stderr": "ModuleNotFoundError: No module named 'sherpa_onnx'"},
                error="failed (exit 1)",
            )

        registry = ToolRegistry(
            [
                Tool(
                    name="run_shell",
                    description="run",
                    parameters={"type": "object", "properties": {}},
                    handler=handler,
                    approval="never",
                )
            ]
        )

        class OneShotModel:
            def __init__(self):
                self._calls = 0

            async def stream(self, messages, *, tools=None):
                self._calls += 1
                if self._calls == 1:
                    yield StreamEvent(
                        kind="tool_call",
                        tool_call=ToolCallDelta(
                            index=0,
                            id="c1",
                            name="run_shell",
                            arguments=jsonlib.dumps({"command": "python -c 'import sherpa_onnx'"}),
                        ),
                    )
                    yield StreamEvent(kind="done", finish_reason="tool_calls")
                    return
                yield StreamEvent(kind="text", text="It is missing.")
                yield StreamEvent(kind="done", finish_reason="stop")

            async def aclose(self):
                return None

        loop = AgentLoop(
            Settings(DEEPSEEK_API_KEY="sk", SURTITLE_HOME=str(tmp_path), thinking_enabled=False),
            root=tmp_path,
            store=store,
            session_id=record.id,
            client=OneShotModel(),
            registry=registry,
        )
        async for _event in loop.run([], "why is local voice dead"):
            pass

        stored = store.list_tool_calls(record.id)
        assert stored, "the tool call was not recorded"
        assert "ModuleNotFoundError" in (stored[0].result or ""), (
            f"the stored result is a status line, not evidence: {stored[0].result!r}"
        )

    async def test_the_transcript_handed_to_the_next_turn_keeps_the_output(self, tmp_path):
        """The end-to-end guarantee: turn two can read what turn one ran."""
        import json as jsonlib

        from surtitle.core.agent import AgentLoop
        from surtitle.llm.deepseek import StreamEvent, ToolCallDelta
        from surtitle.tools.registry import Tool, ToolRegistry

        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        registry = ToolRegistry(
            [
                Tool(
                    name="run_shell",
                    description="run",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda ctx, command="": ToolResult(
                        ok=True, data={"stdout": "sherpa_onnx is not installed"}
                    ),
                    approval="never",
                )
            ]
        )

        class OneShotModel:
            def __init__(self):
                self._calls = 0

            async def stream(self, messages, *, tools=None):
                self._calls += 1
                if self._calls == 1:
                    yield StreamEvent(
                        kind="tool_call",
                        tool_call=ToolCallDelta(
                            index=0,
                            id="c1",
                            name="run_shell",
                            arguments=jsonlib.dumps({"command": "pip list"}),
                        ),
                    )
                    yield StreamEvent(kind="done", finish_reason="tool_calls")
                    return
                yield StreamEvent(kind="text", text="Checked.")
                yield StreamEvent(kind="done", finish_reason="stop")

            async def aclose(self):
                return None

        loop = AgentLoop(
            Settings(DEEPSEEK_API_KEY="sk", SURTITLE_HOME=str(tmp_path), thinking_enabled=False),
            root=tmp_path,
            store=store,
            session_id=record.id,
            client=OneShotModel(),
            registry=registry,
        )
        async for _event in loop.run([], "is sherpa installed"):
            pass

        assistant = [m for m in store.list_messages(record.id) if m.role == "assistant"]
        assert assistant, "no assistant message was stored"
        assert "sherpa_onnx is not installed" in assistant[-1].content, (
            "the next turn cannot see what the command said, so it will re-run it"
        )


class TestDocumentationAwareness:
    """The agent must know what a project documents, including docs/ subdirectories.

    From a real project: the file explaining how to reach the machine fleet lived
    at `docs/ACCESS_METHOD.md`, and priming that only looked at the project root
    never read it. The agent was then asked how to connect and did not know —
    correctly, because it had never been shown.
    """

    def _prompt(self, session):
        return session._system_prompt()

    def test_a_docs_subdirectory_instruction_file_is_read(self, project_session):
        """With budget available, a docs/ instruction file is loaded outright."""
        session, root = project_session
        (root / "docs").mkdir()
        (root / "docs" / "ACCESS_METHOD.md").write_text(
            "Use ./plant-mcp-cli -m <SITE> call to reach a machine.\\n"
        )
        prompt = self._prompt(session)
        assert "plant-mcp-cli" in prompt

    def test_the_connection_guide_is_at_least_named_when_budget_is_tight(self, project_session):
        """Naming is sufficient: the agent is told to read what it needs.

        This is the arrangement for a documentation-heavy project - the routing
        layer stays resident and the detail is one read away, rather than trying to
        keep 100 KB of guides inside the window.
        """
        session, root = project_session
        (root / "AGENTS.md").write_text("Routing: read docs/ACCESS_METHOD.md.\\n" * 200)
        (root / "docs").mkdir()
        (root / "docs" / "ACCESS_METHOD.md").write_text("plant-mcp-cli detail\\n" * 900)
        prompt = self._prompt(session)
        # Either loaded or named in the index - never absent.
        assert "ACCESS_METHOD" in prompt, "the connection guide vanished entirely"

    def test_a_skipped_document_is_still_named(self, project_session):
        """Skipping for size must not make a document invisible."""
        session, root = project_session
        (root / "docs").mkdir()
        # Too large for the budget, so it will be skipped rather than shredded.
        (root / "docs" / "AGENT_INTERFACE.md").write_text("x" * 90000)
        prompt = self._prompt(session)
        assert "docs/AGENT_INTERFACE.md" in prompt, "a skipped doc must be named"

    def test_injected_documents_are_not_listed_as_unread(self, project_session):
        session, root = project_session
        (root / "AGENTS.md").write_text("# Rules\\nDo the thing.\\n")
        prompt = self._prompt(session)
        index = prompt[prompt.find("## Other documentation") :]
        assert "- AGENTS.md" not in index, "an injected file was listed as unread"

    def test_readme_is_not_listed(self, project_session):
        """Conventional entry points need no advertising."""
        session, root = project_session
        (root / "README.md").write_text("# Read me\\n")
        prompt = self._prompt(session)
        index = prompt[prompt.find("## Other documentation") :]
        assert "README.md" not in index

    def test_an_oversized_document_is_not_shredded(self, project_session):
        """A *secondary* file cut to a fraction of itself reads as the whole.

        The primary instruction file is the documented exception: it is shown in
        part and declared partial, because losing it entirely costs the session all
        knowledge of the project. Everything after it is still named rather than
        shredded.
        """
        session, root = project_session
        (root / "AGENTS.md").write_text("# Rules\n")
        (root / "CLAUDE.md").write_text("x" * 90000)
        prompt = self._prompt(session)
        # Skipped, so its body is absent and it is named instead.
        assert "x" * 2000 not in prompt
        assert "CLAUDE.md" in prompt

    def test_no_duplicate_sections_are_emitted(self, project_session):
        session, root = project_session
        (root / "AGENTS.md").write_text("# Rules\\n")
        prompt = self._prompt(session)
        assert prompt.count("## Project instructions") == 1
        assert prompt.count("## Other documentation") <= 1

    def test_a_user_global_instruction_file_is_read(self, project_session):
        """Conventions that are not per-project still reach the agent."""
        session, _root = project_session
        global_file = session.settings.data_dir / "AGENTS.md"
        global_file.parent.mkdir(parents=True, exist_ok=True)
        global_file.write_text("House rule: always cite the file you read.\\n")
        assert "always cite the file you read" in self._prompt(session)

    def test_the_index_lists_nested_docs(self, project_session):
        session, root = project_session
        (root / "docs").mkdir()
        (root / "docs" / "GIT_POLICY.md").write_text("branch naming rules\\n")
        prompt = self._prompt(session)
        assert "docs/GIT_POLICY.md" in prompt

    def test_the_budget_is_respected(self, project_session):
        session, root = project_session
        for name in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"):
            (root / name).write_text("y" * 40000)
        prompt = self._prompt(session)
        assert len(prompt) < 60000, "the resident set is unbounded"
