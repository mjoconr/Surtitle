"""Choosing a folder on the machine running the server.

The Windows branch is ctypes and is verified by hand on a real desktop; what is
tested here is the dispatch and the command each platform is given, because
getting those wrong is silent — the dialog simply never appears.
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
