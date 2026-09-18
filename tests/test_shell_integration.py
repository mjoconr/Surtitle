"""Setting up the launcher and sign-in entries from inside the app.

Both are new ways in to something that used to need a terminal, so what matters is
that the right installer invocation is built, that removing an entry does not run
an installer at all, and that an unattended run never deletes a sign-in entry it
was not asked about.
"""

from __future__ import annotations

from surtitle import shell_integration as shell


class _Done:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestPaths:
    def test_windows_puts_the_entry_in_the_start_menu(self, monkeypatch, tmp_path):
        monkeypatch.setattr(shell, "platform", lambda: "win32")
        monkeypatch.setenv("APPDATA", str(tmp_path))
        assert (
            shell.menu_path()
            == tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Surtitle.lnk"
        )
        assert shell.startup_path().parent.name == "Startup"
        assert shell.startup_path().name == "Surtitle.lnk"

    def test_macos_uses_an_app_and_a_launch_agent(self, monkeypatch):
        monkeypatch.setattr(shell, "platform", lambda: "darwin")
        assert shell.menu_path().name == "Surtitle.app"
        assert shell.menu_path().parent.name == "Applications"
        assert shell.startup_path().name == "com.surtitle.launcher.plist"
        assert shell.startup_path().parent.name == "LaunchAgents"

    def test_linux_has_neither(self, monkeypatch):
        monkeypatch.setattr(shell, "platform", lambda: "linux")
        assert shell.menu_path() is None
        assert shell.startup_path() is None


class TestState:
    def _pin(self, monkeypatch, tmp_path):
        menu = tmp_path / "menu.lnk"
        startup = tmp_path / "startup.lnk"
        monkeypatch.setattr(shell, "menu_path", lambda: menu)
        monkeypatch.setattr(shell, "startup_path", lambda: startup)
        monkeypatch.setattr(shell, "supported", lambda: True)
        return menu, startup

    def test_reports_what_is_missing(self, monkeypatch, tmp_path):
        self._pin(monkeypatch, tmp_path)
        assert shell.state() == {"supported": True, "menu": False, "startup": False}

    def test_reports_what_is_there(self, monkeypatch, tmp_path):
        menu, startup = self._pin(monkeypatch, tmp_path)
        menu.write_text("x", encoding="utf-8")
        startup.write_text("x", encoding="utf-8")
        assert shell.state()["menu"] is True
        assert shell.state()["startup"] is True


class TestCommand:
    def test_windows_passes_shortcuts_only_and_the_startup_answer(self, monkeypatch):
        monkeypatch.setattr(shell, "platform", lambda: "win32")
        argv = shell.command(menu=True, startup=True)
        assert argv[0] == "powershell"
        assert "-ShortcutsOnly" in argv
        assert "-Startup" in argv

    def test_posix_passes_shortcuts_only_and_the_startup_answer(self, monkeypatch):
        monkeypatch.setattr(shell, "platform", lambda: "darwin")
        argv = shell.command(menu=True, startup=False)
        assert argv[0] == "/bin/sh"
        assert "--shortcuts-only" in argv
        assert "--no-startup" in argv

    def test_no_startup_flag_leaves_the_setting_alone(self, monkeypatch):
        """An unattended run must not delete a sign-in entry it was not asked about."""
        monkeypatch.setattr(shell, "platform", lambda: "darwin")
        argv = shell.command(menu=True, startup=None)
        assert "--startup" not in argv
        assert "--no-startup" not in argv


class TestApply:
    def _pin(self, monkeypatch, tmp_path):
        menu = tmp_path / "menu.lnk"
        startup = tmp_path / "startup.lnk"
        monkeypatch.setattr(shell, "menu_path", lambda: menu)
        monkeypatch.setattr(shell, "startup_path", lambda: startup)
        monkeypatch.setattr(shell, "supported", lambda: True)
        return menu, startup

    def test_adding_the_menu_entry_runs_the_installer(self, monkeypatch, tmp_path):
        self._pin(monkeypatch, tmp_path)
        calls: list[list[str]] = []

        ok, _ = shell.apply(
            menu=True, runner=lambda argv, **kwargs: calls.append(list(argv)) or _Done()
        )

        assert ok is True
        assert calls and "--shortcuts-only" in calls[0]

    def test_removing_the_menu_entry_needs_no_installer(self, monkeypatch, tmp_path):
        menu, _ = self._pin(monkeypatch, tmp_path)
        menu.write_text("x", encoding="utf-8")
        calls: list[list[str]] = []

        ok, _ = shell.apply(
            menu=False, runner=lambda argv, **kwargs: calls.append(list(argv)) or _Done()
        )

        assert ok is True
        assert not menu.exists()
        assert calls == [], "removing one shortcut is not an installer's job"

    def test_a_failed_run_reports_the_installer_reason(self, monkeypatch, tmp_path):
        self._pin(monkeypatch, tmp_path)

        ok, message = shell.apply(
            startup=True, runner=lambda argv, **kwargs: _Done(1, stderr="error: boom")
        )

        assert ok is False
        assert "boom" in message

    def test_an_unsupported_install_says_so(self, monkeypatch):
        monkeypatch.setattr(shell, "supported", lambda: False)
        ok, message = shell.apply(menu=True)
        assert ok is False
        assert "cannot manage" in message
