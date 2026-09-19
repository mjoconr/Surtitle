"""Background jobs: commands that outlive the call that started them.

These run real subprocesses — a job that does not start is not worth testing — but
short ones, and the commands are chosen to work under both `sh` and `cmd` so the
same file means the same thing on CI.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from surtitle.config import Settings
from surtitle.core.session import Session
from surtitle.store.db import Store
from surtitle.tools.fs_tools import ToolContext
from surtitle.tools.jobs import MAX_WAIT_SECONDS, JobRegistry, job_kill, job_output, run_background
from surtitle.tools.registry import ToolRegistry, default_tool_list


def python(script: str) -> str:
    """A command string that runs ``script``, quoted for either shell."""
    return f'"{sys.executable}" -c "{script}"'


@pytest.fixture
def registry(tmp_path):
    made = JobRegistry(directory=tmp_path / "jobs")
    yield made
    asyncio.get_event_loop_policy().new_event_loop()  # no-op: keeps the fixture honest


@pytest.fixture
def ctx(tmp_path, registry):
    return ToolContext(root=tmp_path, jobs=registry)


@pytest.fixture
def wired(tmp_path):
    """A real session, because jobs belong to one and closing it must stop them."""
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("P", tmp_path)
    record = store.create_session(project.id)

    async def noop(*_args, **_kwargs):
        return None

    return Session(
        session_id=record.id,
        project_id=project.id,
        root=tmp_path,
        settings=Settings(DEEPSEEK_API_KEY="k", SURTITLE_HOME=str(tmp_path)),
        store=store,
        deepseek=None,
        send=noop,
        send_audio=noop,
        registry=ToolRegistry(default_tool_list()),
    )


class TestStartingAJob:
    async def test_it_returns_at_once_and_names_the_job(self, ctx):
        result = await run_background(ctx, command=python("import time; time.sleep(5)"))

        assert result.ok is True
        assert (result.data or {})["job"] == "job-1"
        assert (result.data or {})["running"] is True, "the turn is not held while it runs"

        await ctx.jobs.kill_all()

    async def test_an_empty_command_is_refused(self, ctx):
        result = await run_background(ctx, command="   ")

        assert result.ok is False
        assert "empty" in (result.error or "")

    async def test_without_a_registry_it_is_a_result_not_a_crash(self, tmp_path):
        """A tool failure is data; an exception would end the turn."""
        result = await run_background(ToolContext(root=tmp_path), command="echo hi")

        assert result.ok is False
        assert "not available" in (result.error or "")

    async def test_a_job_runs_in_the_project_directory(self, ctx, tmp_path):
        result = await run_background(ctx, command=python("import os; print(os.getcwd())"))
        found = ctx.jobs.get((result.data or {})["job"])

        text, _cut = await _finished_output(ctx, found)

        assert str(tmp_path) in text, "the project root, like the foreground shell tool"


async def _finished_output(ctx, job) -> tuple[str, bool]:
    await ctx.jobs.wait(job, 10)
    return ctx.jobs.read(job)


class TestReadingAJob:
    async def test_output_and_exit_code_come_back(self, ctx):
        result = await run_background(ctx, command="echo hello-from-a-job")

        read = await job_output(ctx, job=(result.data or {})["job"], wait_seconds=10)

        assert read.ok is True
        assert "hello-from-a-job" in (read.data or {})["output"]
        assert (read.data or {})["running"] is False
        assert (read.data or {})["exit_code"] == 0

    async def test_waiting_reports_a_job_that_has_not_finished(self, ctx):
        """Bounded: the whole point is not to hold a turn open."""
        result = await run_background(ctx, command=python("import time; time.sleep(30)"))

        read = await job_output(ctx, job=(result.data or {})["job"], wait_seconds=0.3)

        assert (read.data or {})["running"] is True
        assert "running" in (read.data or {})["state"]

        await ctx.jobs.kill_all()

    async def test_waiting_is_capped(self, ctx, monkeypatch):
        """Asked for more than the cap, it waits the cap and reports the job running."""
        import surtitle.tools.jobs as jobs_module

        monkeypatch.setattr(jobs_module, "MAX_WAIT_SECONDS", 0.2)
        result = await run_background(ctx, command=python("import time; time.sleep(30)"))

        read = await job_output(ctx, job=(result.data or {})["job"], wait_seconds=10_000)

        assert (read.data or {})["running"] is True, "it must not wait for the command"

        await ctx.jobs.kill_all()

    def test_the_cap_is_finite(self):
        assert 0 < MAX_WAIT_SECONDS <= 300

    async def test_an_unknown_job_names_the_ones_that_exist(self, ctx):
        await run_background(ctx, command="echo one")

        read = await job_output(ctx, job="job-99")

        assert read.ok is False
        assert "job-1" in (read.error or ""), "the answer says what it could have meant"

    async def test_a_failing_command_is_reported_not_hidden(self, ctx):
        result = await run_background(ctx, command=python("import sys; sys.exit(3)"))

        read = await job_output(ctx, job=(result.data or {})["job"], wait_seconds=10)

        assert (read.data or {})["exit_code"] == 3
        assert "exit code 3" in (read.data or {})["state"]

    async def test_a_job_that_printed_a_lot_gives_its_tail_and_says_it_was_cut(
        self, ctx, registry, monkeypatch
    ):
        import surtitle.tools.jobs as jobs_module

        # Patched at the module, which the cap resolves at call time: a default
        # argument would have captured the real one when the module was imported.
        monkeypatch.setattr(jobs_module, "MAX_OUTPUT_CHARS", 200)
        result = await run_background(ctx, command=python("print('x' * 5000)"))
        found = ctx.jobs.get((result.data or {})["job"])
        await ctx.jobs.wait(found, 10)

        text, cut = registry.read(found)

        assert cut is True, "a reader is told that what it has is not all of it"
        assert len(text) <= 200
        assert "x" * 100 in text, "the end of the output is the part that says where it got to"


class TestStoppingJobs:
    async def test_kill_stops_it(self, ctx):
        result = await run_background(ctx, command=python("import time; time.sleep(30)"))

        stopped = await job_kill(ctx, job=(result.data or {})["job"])

        assert stopped.ok is True
        assert (stopped.data or {})["running"] is False

    async def test_killing_something_already_finished_is_not_an_error(self, ctx):
        result = await run_background(ctx, command="echo done")
        read = await job_output(ctx, job=(result.data or {})["job"], wait_seconds=10)
        assert (read.data or {})["running"] is False

        stopped = await job_kill(ctx, job=(result.data or {})["job"])

        assert stopped.ok is True
        assert "already finished" in (stopped.display or "")

    async def test_kill_all_stops_everything_and_clears_up(self, ctx, registry):
        for _ in range(3):
            await run_background(ctx, command=python("import time; time.sleep(30)"))
        assert len(registry.jobs()) == 3

        await registry.kill_all()

        assert registry.jobs() == []
        assert registry.directory is None, "a job's log does not outlive its session"


class TestItLeavesNothingBehindWhenUnused:
    """Every conversation builds one of these; almost none of them run a job.

    Found by counting temp directories after a test run: 169 of them, one per
    `Session` ever constructed, because the registry made its directory in the
    constructor and most sessions are never closed.
    """

    def test_building_one_makes_no_directory(self):
        registry = JobRegistry()

        assert registry.directory is None

    async def test_a_started_job_makes_exactly_one(self, tmp_path):
        registry = JobRegistry(directory=tmp_path / "jobs")
        ctx = ToolContext(root=tmp_path, jobs=registry)

        await run_background(ctx, command="echo hi")

        assert registry.directory == tmp_path / "jobs"
        assert len(list(registry.directory.glob("*.log"))) == 1
        await registry.kill_all()

    async def test_a_session_that_never_runs_a_job_removes_nothing(self, wired):
        """Closing it must not fail on a directory that was never made."""
        assert wired.jobs.directory is None

        await wired.close()

        assert wired.jobs.directory is None


class TestTheRegistryIsNotUnbounded:
    async def test_the_oldest_finished_job_is_dropped(self, tmp_path):
        registry = JobRegistry(directory=tmp_path / "jobs", limit=2)
        ctx = ToolContext(root=tmp_path, jobs=registry)

        for _ in range(2):
            result = await run_background(ctx, command="echo old")
            await job_output(ctx, job=(result.data or {})["job"], wait_seconds=10)
        third = await run_background(ctx, command="echo new")

        assert [job.id for job in registry.jobs()] == ["job-2", (third.data or {})["job"]], (
            "a loop that starts jobs forever must not grow a list forever"
        )


class TestTheyBelongToTheSession:
    """Started in one turn, still there in the next; killed when the window closes."""

    async def test_the_registry_outlives_a_turn(self, wired):
        """The loop is built per turn; the registry is built with the session."""
        assert wired.jobs is not None

        started = await wired.jobs.start(
            python("import time; time.sleep(30)"), ToolContext(root=wired.root)
        )

        assert wired.jobs.get(started.id) is not None
        await wired.jobs.kill_all()

    async def test_closing_the_session_stops_its_jobs(self, wired):
        started = await wired.jobs.start(
            python("import time; time.sleep(30)"), ToolContext(root=wired.root)
        )
        assert started.running is True

        await wired.close()

        assert started.running is False, (
            "a background command that outlives the window that started it is a "
            "process the user cannot see or stop"
        )

    def test_a_sub_agent_is_not_offered_them(self):
        """The jobs belong to the parent conversation."""
        reach = ToolRegistry(default_tool_list()).read_only().names()

        assert "run_background" not in reach
        assert "job_output" not in reach
        assert "job_kill" not in reach


class TestTheToolsAreDeclared:
    def test_they_are_in_the_default_set_with_the_right_policies(self):
        tools = {tool.name: tool for tool in default_tool_list()}

        assert tools["run_background"].approval == "ask", "starting a process changes things"
        assert tools["run_background"].mutating is True
        assert tools["job_output"].approval == "never", "reading what ran is not a new act"
        assert tools["job_kill"].approval == "never", "and neither is stopping the agent's own job"

    def test_the_paths_and_descriptions_survive_the_wire(self):
        import json

        for name in ("run_background", "job_output", "job_kill"):
            tool = next(tool for tool in default_tool_list() if tool.name == name)
            assert json.dumps(tool.to_openai_schema())
            assert tool.parameters["required"], f"{name} must require its argument"
