"""Running a command through a shell, and the quoting that has to survive it.

Most of what a shell tool does is covered where it is used. What is tested here is
the one thing that is invisible on the machine this is usually developed on: the
command string has to reach the platform's shell intact, and on Windows that is not
what a hand-built ``cmd.exe`` argv does.
"""

from __future__ import annotations

import asyncio

from surtitle.tools import shell_tools
from surtitle.tools.fs_tools import ToolContext
from surtitle.tools.shell_tools import run_shell

# What an agent writes on Windows the moment it needs a specific interpreter, and
# the shape that a hand-built `cmd.exe /s /c` argv destroys. Not a Windows path
# that has to exist: the point is the quoting, and this runs everywhere.
QUOTED_EXECUTABLE = '"C:\\Program Files\\Python\\python.exe" -c "print(1)"'


class TestTheCommandReachesTheShell:
    async def test_the_command_string_is_handed_over_whole(self, tmp_path, monkeypatch):
        """`cmd /s /c <command>` strips the first quote of the command and the last
        quote anywhere on the line, so the quoted path above arrives at the shell
        with a stray quote at the end of the executable and fails as "not recognized
        as an internal or external command". Handing the string to the platform's
        shell lets CPython wrap it in the extra pair of quotes that survives."""
        seen: dict[str, str] = {}

        async def fake(command, **_kwargs):
            seen["command"] = command
            raise FileNotFoundError("no shell here")

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake)

        result = await run_shell(ToolContext(root=tmp_path), QUOTED_EXECUTABLE)

        assert seen["command"] == QUOTED_EXECUTABLE, "not rewritten into a cmd.exe argv"
        assert result.ok is False, "a shell that cannot be started is a result, not a crash"

    async def test_the_argv_form_is_still_executed_directly(self, tmp_path, monkeypatch):
        """A list is a program and its arguments, and must not go near a shell:
        every argument would be re-parsed."""
        called: dict[str, object] = {}

        async def fake(*target, **_kwargs):
            called["target"] = target
            raise FileNotFoundError("no program here")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

        result = await shell_tools._execute(
            ["/nonexistent/program", "arg"], ToolContext(root=tmp_path), timeout=5, label="x"
        )

        assert called["target"] == ("/nonexistent/program", "arg")
        assert result.ok is False
        assert "not found" in (result.error or "")


class TestItStillRuns:
    async def test_a_command_produces_its_output(self, tmp_path):
        result = await run_shell(ToolContext(root=tmp_path), "echo hello")

        assert result.ok is True
        assert "hello" in (result.data or {}).get("stdout", "")

    async def test_a_shell_builtin_works(self, tmp_path):
        """The reason it is a shell: `cd` is not a program on any platform."""
        result = await run_shell(ToolContext(root=tmp_path), "cd . && echo here")

        assert result.ok is True
        assert "here" in (result.data or {}).get("stdout", "")

    async def test_a_failing_command_reports_its_code(self, tmp_path):
        result = await run_shell(ToolContext(root=tmp_path), "exit 3")

        assert result.ok is False
        assert (result.data or {}).get("exit_code") == 3

    async def test_an_empty_command_is_refused(self, tmp_path):
        result = await run_shell(ToolContext(root=tmp_path), "   ")

        assert result.ok is False
        assert "empty" in (result.error or "")
