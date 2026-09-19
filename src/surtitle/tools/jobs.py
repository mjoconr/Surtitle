"""Commands that outlive the call that started them.

A shell command that takes four minutes does not belong inside a turn. The turn is
blocked for those four minutes, the user hears nothing, and the model has to decide
whether an error means "not finished yet" or "broken". So the command is started,
the call returns a name for it, and the agent gets on with something else — or asks
for it again in a moment, or waits a bounded amount of time for it.

Three rules hold this together:

* **A job belongs to its conversation.** The registry is owned by the session, not
  the turn: a build started in one turn is still there three turns later. And when
  the session closes, its jobs are killed — a background process that outlives the
  window that started it is a process nobody can see or stop.
* **Output goes to a file, not a pipe.** Nothing has to be drained while the command
  runs, so a job that prints for an hour cannot fill a pipe buffer and deadlock on a
  reader that stopped reading.
* **The file is read from the end.** What a reader wants from a long-running job is
  the newest lines, and what they get says how much came before it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from surtitle.tools.fs_tools import MAX_OUTPUT_CHARS, ToolContext, ToolResult

__all__ = ["Job", "JobRegistry", "job_kill", "job_output", "run_background"]

log = logging.getLogger(__name__)

# How long `job_output` may be asked to wait for a job to finish. Bounded because
# the alternative — a tool call that blocks for as long as the command takes — is
# the problem this feature exists to solve.
MAX_WAIT_SECONDS = 120.0
_POLL_SECONDS = 0.25
# Jobs are held for the life of the session, but a loop that starts hundreds of
# them is a loop that has stopped making sense. The oldest finished one is dropped.
MAX_JOBS = 20


@dataclass(slots=True)
class Job:
    """One command running (or finished) in the background."""

    id: str
    command: str
    log_path: Path
    process: asyncio.subprocess.Process
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    @property
    def seconds(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


class JobRegistry:
    """The background commands of one conversation."""

    def __init__(self, *, directory: Path | None = None, limit: int = MAX_JOBS) -> None:
        # No directory is made until a job exists. Every conversation constructs one
        # of these, and almost none of them run a background command — a temp
        # directory per session is a leak measured in the hundreds after a test run,
        # and an empty one per conversation in the wild.
        self._directory = directory
        self._jobs: dict[str, Job] = {}
        self._next = 1
        self._limit = limit

    @property
    def directory(self) -> Path | None:
        """Where job logs live, or ``None`` while no job has been started."""
        return self._directory

    def _root(self) -> Path:
        if self._directory is None:
            self._directory = Path(tempfile.mkdtemp(prefix="surtitle-jobs-"))
        self._directory.mkdir(parents=True, exist_ok=True)
        return self._directory

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id.strip())

    def jobs(self) -> list[Job]:
        return list(self._jobs.values())

    async def start(self, command: str, ctx: ToolContext) -> Job:
        """Start ``command`` in the project root and return the job for it."""
        if os.name == "nt":
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
        else:
            argv = ["/bin/sh", "-c", command]

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # Put the child in its own process group so the whole tree can be killed,
        # exactly as the foreground shell tool does: a build is rarely one process.
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        job_id = f"job-{self._next}"
        self._next += 1
        log_path = self._root() / f"{job_id}.log"

        self._make_room()
        # The file is opened for the child rather than piped to us: nothing has to
        # read it for the command to make progress.
        handle = log_path.open("wb")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(ctx.root),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                **kwargs,
            )
        finally:
            handle.close()

        job = Job(id=job_id, command=command, log_path=log_path, process=process)
        self._jobs[job_id] = job
        log.info("background job %s started: %r", job_id, command[:80])
        return job

    def _make_room(self) -> None:
        """Forget the oldest finished job once the registry is full."""
        if len(self._jobs) < self._limit:
            return
        for job in sorted(self._jobs.values(), key=lambda item: item.started_at):
            if not job.running:
                self._forget(job)
                return

    def _forget(self, job: Job) -> None:
        self._jobs.pop(job.id, None)
        with contextlib.suppress(OSError):
            job.log_path.unlink()

    def read(self, job: Job, *, max_chars: int | None = None) -> tuple[str, bool]:
        """The end of a job's output, and whether anything was left out.

        The tail rather than the head: for a command that has been running for a
        while, the last thing it printed is the part that says where it is.

        ``max_chars`` is resolved here rather than defaulted in the signature: a
        default argument is captured when the module is imported, which makes the
        cap untestable and unconfigurable at run time.
        """
        limit = max_chars or MAX_OUTPUT_CHARS
        try:
            size = job.log_path.stat().st_size
        except OSError:
            return "", False
        with contextlib.suppress(OSError):
            with job.log_path.open("rb") as handle:
                if size > limit:
                    handle.seek(size - limit)
                text = handle.read().decode("utf-8", errors="replace")
            cut = size > limit or len(text) > limit
            return text[-limit:], cut
        return "", False

    async def kill(self, job: Job) -> None:
        """Stop a job and everything it started."""
        if not job.running:
            return
        job.process.terminate()
        for _ in range(40):
            if not job.running:
                break
            await asyncio.sleep(0.05)
        if job.running:
            with contextlib.suppress(Exception):
                if os.name != "nt":
                    os.killpg(os.getpgid(job.process.pid), 9)
                job.process.kill()
        job.finished_at = job.finished_at or time.time()
        log.info("background job %s stopped", job.id)

    async def kill_all(self) -> None:
        """Stop every job, and remove their logs. For a session that is closing."""
        for job in list(self._jobs.values()):
            with contextlib.suppress(Exception):
                await self.kill(job)
        self._jobs.clear()
        if self._directory is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None

    async def wait(self, job: Job, seconds: float) -> None:
        """Wait for a job to finish, or for the clock to run out — whichever first."""
        deadline = time.monotonic() + seconds
        while job.running and time.monotonic() < deadline:
            await asyncio.sleep(_POLL_SECONDS)
        if not job.running:
            job.finished_at = job.finished_at or time.time()


# --- the tools -----------------------------------------------------------


async def run_background(ctx: ToolContext, command: str = "") -> ToolResult:
    """Start a command and return at once, naming the job."""
    if not command.strip():
        return ToolResult(ok=False, error="command must not be empty.")
    if ctx.jobs is None:
        return ToolResult(ok=False, error="Background jobs are not available in this session.")
    try:
        job = await ctx.jobs.start(command, ctx)
    except FileNotFoundError:
        return ToolResult(ok=False, error="The shell to run that command was not found.")
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not start that command: {exc}")

    return ToolResult(
        ok=True,
        data={
            "job": job.id,
            "command": command,
            "running": True,
            "note": (
                "Started. Read it with job_output, or carry on and read it later; "
                "it keeps running either way."
            ),
        },
        display=f"{job.id} started: {command[:60]}",
    )


async def job_output(ctx: ToolContext, job: str = "", *, wait_seconds: float = 0.0) -> ToolResult:
    """The end of a job's output, waiting up to ``wait_seconds`` for it to finish."""
    registry = ctx.jobs
    if registry is None:
        return ToolResult(ok=False, error="Background jobs are not available in this session.")
    found = registry.get(str(job))
    if found is None:
        known = ", ".join(item.id for item in registry.jobs()) or "none"
        return ToolResult(ok=False, error=f"No job called {job!r}. Known: {known}.")

    waiting = max(0.0, min(float(wait_seconds or 0.0), MAX_WAIT_SECONDS))
    if waiting and found.running:
        await registry.wait(found, waiting)

    text, cut = registry.read(found)
    if cut:
        text = f"[earlier output elided]\n{text}"
    state = "running" if found.running else f"finished, exit code {found.returncode}"
    if not text:
        text = "(nothing printed yet)"
    return ToolResult(
        ok=True,
        data={
            "job": found.id,
            "command": found.command,
            "state": state,
            "running": found.running,
            "exit_code": found.returncode,
            "seconds": round(found.seconds, 1),
            "output": text,
        },
        display=f"{found.id}: {state}",
    )


async def job_kill(ctx: ToolContext, job: str = "") -> ToolResult:
    """Stop a job that is still running."""
    registry = ctx.jobs
    if registry is None:
        return ToolResult(ok=False, error="Background jobs are not available in this session.")
    found = registry.get(str(job))
    if found is None:
        known = ", ".join(item.id for item in registry.jobs()) or "none"
        return ToolResult(ok=False, error=f"No job called {job!r}. Known: {known}.")
    if not found.running:
        return ToolResult(
            ok=True,
            data={"job": found.id, "running": False, "exit_code": found.returncode},
            display=f"{found.id} had already finished",
        )
    await registry.kill(found)
    return ToolResult(
        ok=True,
        data={"job": found.id, "running": False, "exit_code": found.returncode},
        display=f"{found.id} stopped",
    )
