"""Shell and Python execution.

These are the most powerful and most dangerous tools, so they are always
approval-gated and always bounded:

* every invocation runs with ``cwd`` inside the project root;
* a timeout is enforced, with the whole *process group* killed on expiry so a
  spawned child cannot outlive the call;
* stdout and stderr are captured and truncated, and the truncation is stated
  explicitly so the model does not reason from a partial log as if it were whole.

Python runs in a fresh interpreter as a subprocess rather than in-process. That
costs a little startup time and buys three things: a crash cannot take down the
server, module-level state cannot leak between calls, and cancellation is a real
kill rather than best-effort.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from surtitle.tools.environment import project_env_python
from surtitle.tools.fs_tools import MAX_OUTPUT_CHARS, ToolContext, ToolResult

__all__ = ["run_python", "run_shell"]

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 300.0
_PYTHON_TIMEOUT = 120.0


def _truncate_output(text: str, *, label: str) -> tuple[str, bool]:
    """Cap captured output, keeping the head and tail of the log.

    The tail is kept because tracebacks and error messages live there; the head
    is kept because it usually holds the first failure.
    """
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    half = MAX_OUTPUT_CHARS // 2
    kept = (
        text[:half]
        + f"\n... [{label} truncated: {len(text) - MAX_OUTPUT_CHARS} characters elided] ...\n"
        + text[-half:]
    )
    return kept, True


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a process and its children, escalating to SIGKILL if needed."""
    if process.returncode is not None:
        return
    if os.name == "nt":
        # taskkill /T walks the child tree; /F is needed because a bare
        # terminate() only kills the immediate process on Windows.
        with contextlib.suppress(Exception):
            import subprocess

            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), 15)  # SIGTERM
    with contextlib.suppress(Exception):
        process.terminate()
    try:
        # Deliberately not using asyncio.wait_for: Python 3.11 cancels the task
        # awaiting wait_for, which would re-raise CancelledError here.
        for _ in range(50):
            if process.returncode is not None:
                return
            time.sleep(0.1)
    except Exception:  # pragma: no cover
        pass
    with contextlib.suppress(Exception):
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), 9)  # SIGKILL
        process.kill()


async def _execute(
    target: str | list[str],
    ctx: ToolContext,
    *,
    timeout: float,
    stdin_data: str | None = None,
    label: str,
) -> ToolResult:
    """Run ``target`` in the project root with a hard timeout.

    A list is an argv and is executed directly. A string is a shell command and is
    handed to the platform's own shell — ``cmd.exe`` on Windows, ``/bin/sh``
    elsewhere — through ``create_subprocess_shell`` rather than a hand-built
    ``cmd.exe /s /c <command>`` argv.

    That is not a style preference. With ``/s``, ``cmd.exe`` strips the first quote
    of the command *and the last quote anywhere on the line*, so
    ``"C:\\Program Files\\Python\\python.exe" script.py`` — what an agent writes
    constantly on Windows — reaches the shell as ``C:\\Program Files\\Python\\python.exe"
    script.py`` and fails with `is not recognized as an internal or external
    command`. CPython's shell handling wraps the whole command in one more pair of
    quotes, which is exactly what makes that case survive.
    """
    cwd = ctx.root
    if not cwd.is_dir():
        return ToolResult(
            ok=False,
            error=f"The project directory no longer exists: {cwd}",
        )

    env = os.environ.copy()
    # Keep subprocess output unbuffered and deterministic across platforms.
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")

    # Put the child in its own process group so we can kill the whole tree.
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    shown = target if isinstance(target, str) else target[0]
    common: dict[str, object] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        **kwargs,
    }
    started = time.monotonic()
    try:
        if isinstance(target, str):
            process = await asyncio.create_subprocess_shell(target, **common)
        else:
            process = await asyncio.create_subprocess_exec(*target, **common)
    except FileNotFoundError:
        return ToolResult(ok=False, error=f"Command not found: {shown}")
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not start {shown}: {exc}")

    timed_out = False
    try:
        # This is deliberately NOT wrapped in asyncio.wait_for: on Python 3.11
        # wait_for cancels the task it wraps, which would re-raise
        # CancelledError and lose this traceback. asyncio.wait leaves the
        # coroutine alone so the timeout path can report properly.
        communicate = asyncio.create_task(
            process.communicate(input=stdin_data.encode("utf-8") if stdin_data else None)
        )
        done, _pending = await asyncio.wait({communicate}, timeout=timeout)
        if not done:
            timed_out = True
            _kill_process_tree(process)
            with contextlib.suppress(Exception):
                await asyncio.wait({communicate}, timeout=5)
            if not communicate.done():
                communicate.cancel()
            stdout_bytes, stderr_bytes = b"", b""
        else:
            stdout_bytes, stderr_bytes = communicate.result()
    except asyncio.CancelledError:
        # The user pressed stop. Kill the tree, then report the cancellation
        # rather than swallowing it, so the agent loop can unwind.
        _kill_process_tree(process)
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    stdout, stdout_cut = _truncate_output(stdout, label="stdout")
    stderr, stderr_cut = _truncate_output(stderr, label="stderr")
    truncated = stdout_cut or stderr_cut

    if timed_out:
        return ToolResult(
            ok=False,
            error=(
                f"{label} exceeded the {timeout:.0f}s timeout and was stopped. "
                "Make the task smaller, or raise the timeout for this call."
            ),
            data={
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": duration_ms,
                "exit_code": None,
                "timed_out": True,
            },
            display=f"{label} timed out after {timeout:.0f}s",
            truncated=truncated,
        )

    exit_code = process.returncode
    ok = exit_code == 0
    return ToolResult(
        ok=ok,
        error=None if ok else f"{label} exited with code {exit_code}",
        data={
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "timed_out": False,
        },
        display=(
            f"{label} finished in {duration_ms} ms" if ok else f"{label} failed (exit {exit_code})"
        ),
        truncated=truncated,
    )


async def run_shell(
    ctx: ToolContext,
    command: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> ToolResult:
    """Run a shell command inside the project directory.

    Uses ``cmd.exe`` on Windows and ``/bin/sh`` elsewhere, through the platform's
    own shell rather than a hand-split argv: shell built-ins and pipes work, because
    this is genuinely a shell. ``_execute`` explains why the command string is not
    wrapped in a ``cmd.exe /c`` argv by hand.
    """
    if not command.strip():
        return ToolResult(ok=False, error="command must not be empty.")

    bounded = max(1.0, min(float(timeout), MAX_TIMEOUT))
    return await _execute(command, ctx, timeout=bounded, label=f"`{command[:60]}`")


async def run_python(
    ctx: ToolContext,
    code: str,
    *,
    timeout: float = _PYTHON_TIMEOUT,
) -> ToolResult:
    """Execute Python code in the project directory and return its output.

    The script is written to a temporary file *inside the project* so relative
    paths in the code behave the way the user expects, then removed. This is the
    general-purpose escape hatch: it is how the agent produces a PDF, a
    spreadsheet, or an analysis the built-in artifact tools do not cover.
    """
    if not code.strip():
        return ToolResult(ok=False, error="code must not be empty.")

    bounded = max(1.0, min(float(timeout), MAX_TIMEOUT))
    script_path: Path | None = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix=".surtitle_", suffix=".py", dir=str(ctx.root))
        os.close(fd)
        script_path = Path(raw_path)
        script_path.write_text(code, encoding="utf-8")

        # Prefer the project's own interpreter when it exists. That is what makes
        # `install_packages` useful: the agent installs into its environment and
        # then actually imports from it here.
        python, isolated = _interpreter_for(ctx.root)

        result = await _execute(
            [str(python), "-u", str(script_path.name)],
            ctx,
            timeout=bounded,
            label="python",
        )
        # Tell the model which environment ran, so a missing import is
        # diagnosable without another round trip.
        if result.data is not None:
            result.data["script"] = script_path.name
            result.data["isolated"] = isolated
            result.data["interpreter"] = str(python)
        return result
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not prepare the Python script: {exc}")
    finally:
        if script_path is not None:
            with contextlib.suppress(OSError):
                script_path.unlink()


def _interpreter_for(root: Path) -> tuple[Path, bool]:
    """Choose the interpreter for agent code.

    Returns ``(python, isolated)``. The project environment is preferred when it
    exists, so agent code sees the packages it installed; otherwise the
    application interpreter is used, so a project that never installs anything
    still runs code immediately.
    """
    project_python = project_env_python(root)
    if project_python is not None:
        return project_python, True
    return Path(sys.executable), False
