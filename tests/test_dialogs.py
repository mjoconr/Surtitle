"""Choosing a folder on the machine running the server.

The Windows chooser is ctypes inside a child process and is verified by hand on a
real desktop; what is tested here is the dispatch, the command each platform is
given, and the child's exit-code protocol, because getting those wrong is silent —
the dialog simply never appears.

That silence is the bug this file exists for. The Windows chooser used to be
called in-process on an ``asyncio`` worker thread of a detached server, where the
window could not be found on screen, and a user who clicks Browse and sees
nothing has no way to tell that apart from a slow machine.
"""

from __future__ import annotations

from surtitle import dialogs


class _Done:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class TestAvailability:
    def test_windows_always_has_one(self):
        assert dialogs.available(which=lambda name: None, platform="win32") is True

    def test_macos_needs_osascript(self):
        def which(name):
            return "/usr/bin/osascript" if name == "osascript" else None

        assert dialogs.available(which=which, platform="darwin") is True
        assert dialogs.available(which=lambda name: None, platform="darwin") is False

    def test_linux_accepts_either_tool(self):
        def only(name):
            return f"/usr/bin/{name}" if name == "kdialog" else None

        assert dialogs.available(which=only, platform="linux") is True
        assert dialogs.available(which=lambda name: None, platform="linux") is False


class TestMacOS:
    def _which(self, name):
        return "/usr/bin/osascript" if name == "osascript" else None

    def test_osascript_is_asked_for_a_posix_path(self):
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            return _Done(stdout="/Users/mike/Projects\n")

        path = dialogs.choose_folder(runner=runner, which=self._which, platform="darwin")

        assert path == "/Users/mike/Projects"
        assert calls[0][0] == "osascript"
        assert "choose folder" in calls[0][2]

    def test_the_title_is_escaped_into_the_script(self):
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            return _Done(stdout="/x")

        dialogs.choose_folder(
            title='Pick "one"', runner=runner, which=self._which, platform="darwin"
        )

        assert 'Pick \\"one\\"' in calls[0][2]

    def test_cancelling_is_not_an_error(self):
        path = dialogs.choose_folder(
            runner=lambda command, **kwargs: _Done(returncode=1),
            which=self._which,
            platform="darwin",
        )
        assert path is None

    def test_without_osascript_nothing_is_launched(self):
        calls: list[list[str]] = []
        path = dialogs.choose_folder(
            runner=lambda command, **kwargs: calls.append(command) or _Done(),
            which=lambda name: None,
            platform="darwin",
        )
        assert path is None
        assert calls == []


class TestLinux:
    def test_zenity_is_preferred(self):
        calls: list[list[str]] = []

        def which(name):
            return {"zenity": "/usr/bin/zenity", "kdialog": "/usr/bin/kdialog"}.get(name)

        path = dialogs.choose_folder(
            runner=lambda command, **kwargs: calls.append(command) or _Done(stdout="/home/mike\n"),
            which=which,
            platform="linux",
        )

        assert path == "/home/mike"
        assert calls[0][0] == "/usr/bin/zenity"
        assert "--directory" in calls[0]

    def test_kdialog_is_the_fallback(self):
        calls: list[list[str]] = []

        def which(name):
            return "/usr/bin/kdialog" if name == "kdialog" else None

        dialogs.choose_folder(
            runner=lambda command, **kwargs: calls.append(command) or _Done(stdout="/home/mike"),
            which=which,
            platform="linux",
        )

        assert calls[0][0] == "/usr/bin/kdialog"
        assert "--getexistingdirectory" in calls[0]

    def test_neither_installed_means_no_dialog(self):
        calls: list[list[str]] = []
        path = dialogs.choose_folder(
            runner=lambda command, **kwargs: calls.append(command) or _Done(),
            which=lambda name: None,
            platform="linux",
        )
        assert path is None
        assert calls == []

    def test_an_empty_answer_is_a_cancel(self):
        path = dialogs.choose_folder(
            runner=lambda command, **kwargs: _Done(stdout="  \n"),
            which=lambda name: "/usr/bin/zenity",
            platform="linux",
        )
        assert path is None


class _DoneWithCode:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class TestWindows:
    """The chooser runs in its own process; these are the contract with it."""

    def _run(self, result):
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            return result

        path = dialogs.choose_folder(runner=runner, platform="win32")
        return path, calls

    def test_the_chooser_runs_as_a_separate_process(self):
        """In-process, it got a worker thread of a detached server and no window."""
        import sys

        path, calls = self._run(_DoneWithCode(stdout="C:\\Projects\n"))

        assert path == "C:\\Projects"
        assert calls[0][0] == sys.executable
        assert calls[0][1:3] == ["-m", "surtitle.win32_folder_dialog"]

    def test_the_initial_folder_is_passed_through(self):
        calls: list[list[str]] = []

        dialogs.choose_folder(
            initial="C:\\Wooltech",
            runner=lambda command, **kwargs: calls.append(command) or _DoneWithCode(),
            platform="win32",
        )

        assert calls[0][-1] == "C:\\Wooltech"

    def test_the_title_becomes_an_argument_not_quoted_shell_text(self):
        """Shell quoting of this path was unreliable; an argv entry is not."""
        calls: list[list[str]] = []

        dialogs.choose_folder(
            title="Choose a folder for this project",
            runner=lambda command, **kwargs: calls.append(command) or _DoneWithCode(),
            platform="win32",
        )

        assert "Choose a folder for this project" in calls[0]

    def test_exit_three_is_a_cancel(self):
        """The child reports a cancel with its own exit code, not an empty path."""
        path, _ = self._run(_DoneWithCode(returncode=3))
        assert path is None

    def test_a_failure_is_also_no_path(self):
        """Exit 4 covers a COM refusal or a crash; the browser is still there."""
        path, _ = self._run(_DoneWithCode(returncode=4, stdout="C:\\half"))
        assert path is None

    def test_a_path_is_taken_from_stdout_only(self):
        path, _ = self._run(_DoneWithCode(stdout="  C:\\Wooltech  \n"))
        assert path == "C:\\Wooltech"

    def test_a_child_that_cannot_start_is_no_path(self):
        def runner(command, **kwargs):
            raise OSError("no interpreter")

        assert dialogs.choose_folder(runner=runner, platform="win32") is None

    def test_the_console_window_is_suppressed_only_on_windows(self):
        """A black window flashing before the dialog reads as a bug."""
        seen: list[dict] = []

        def runner(command, **kwargs):
            seen.append(kwargs)
            return _DoneWithCode()

        dialogs.choose_folder(runner=runner, platform="win32")
        assert seen[0]["capture_output"] is True
        assert seen[0]["encoding"] == "utf-8"


class TestWindowsChildProcess:
    """The other half of the contract: what the child reports."""

    def test_main_refuses_to_run_off_windows(self):
        """One guard, so importing it on a build machine is still harmless."""
        from surtitle import win32_folder_dialog

        assert win32_folder_dialog.main(["Pick a folder"]) == win32_folder_dialog.EXIT_FAILED

    def test_the_exit_codes_are_distinct(self):
        from surtitle import win32_folder_dialog

        assert win32_folder_dialog.EXIT_CANCELLED != 0
        assert win32_folder_dialog.EXIT_FAILED not in {0, win32_folder_dialog.EXIT_CANCELLED}

    def test_the_child_prints_a_unicode_path(self):
        """A path with a non-ASCII name must survive the pipe."""
        import inspect

        from surtitle import win32_folder_dialog

        source = inspect.getsource(win32_folder_dialog.main)
        assert 'encoding="utf-8"' in source
