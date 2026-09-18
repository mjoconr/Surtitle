"""The launcher entry and the sign-in entry, set up from inside the app.

Both are ordinary per-user shortcuts, and both were reachable only by re-running
Setup. Someone who extracted a release archive and never ran it had no Start Menu
entry and no way to start at sign-in without first discovering a script. The app
can offer both from its own menu instead.

The work stays in the installers (``scripts/install.ps1`` and
``scripts/install.sh``): they already resolve the launcher, the icon and the
per-user locations, and doing it again here would give two definitions of where a
shortcut belongs. They are run in their "shortcuts only" mode, which touches
nothing else — which matters, because the app is running out of the very
environment a full install would rebuild.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["apply", "menu_path", "platform", "project_root", "startup_path", "state", "supported"]

APP_NAME = "Surtitle"


def platform() -> str:
    """The platform, through one name.

    Tests need to answer for another platform without changing ``sys.platform``,
    which the standard library itself reads (``sysconfig`` builds a module name out
    of it and stops importing).
    """
    return sys.platform


AGENT_LABEL = f"com.{APP_NAME.lower()}.launcher"


def project_root() -> Path | None:
    """The installation this code belongs to, however it was installed."""
    from surtitle import selfupdate, update

    for root in (update.git_checkout(), selfupdate.install_root()):
        if root is not None:
            return root
    candidate = Path(__file__).resolve().parents[2]
    return candidate if (candidate / "pyproject.toml").is_file() else None


def menu_path() -> Path | None:
    """Where the launcher entry lives, or ``None`` where there is no such idea."""
    if platform() == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return (
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"
        )
    if platform() == "darwin":
        return Path.home() / "Applications" / f"{APP_NAME}.app"
    return None


def startup_path() -> Path | None:
    """Where the start-at-sign-in entry lives, or ``None`` on a platform without one."""
    if platform() == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
            / f"{APP_NAME}.lnk"
        )
    if platform() == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
    return None


def supported() -> bool:
    """True when this platform has entries to manage and an installer to run them."""
    if menu_path() is None or startup_path() is None:
        return False
    root = project_root()
    if root is None:
        return False
    return (root / "scripts" / "install.ps1").is_file() or (
        root / "scripts" / "install.sh"
    ).is_file()


def state() -> dict[str, Any]:
    """What is set up right now, for the tray menu and the UI."""
    menu = menu_path()
    startup = startup_path()
    return {
        "supported": supported(),
        "menu": bool(menu and menu.exists()),
        "startup": bool(startup and startup.exists()),
    }


def command(*, menu: bool, startup: bool | None) -> list[str]:
    """The installer invocation for these choices.

    ``-NonInteractive`` on Windows and omitting the startup flag on POSIX both mean
    "do not ask": the installer then leaves the sign-in setting exactly as it is,
    which is what a menu item about something else must do.
    """
    root = project_root()
    assert root is not None
    if platform() == "win32":
        args = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "scripts" / "install.ps1"),
            "-ShortcutsOnly",
        ]
        if startup is True:
            args.append("-Startup")
        elif startup is False:
            args.append("-NoStartup")
        return args
    args = ["/bin/sh", str(root / "scripts" / "install.sh"), "--shortcuts-only"]
    if startup is True:
        args.append("--startup")
    elif startup is False:
        args.append("--no-startup")
    return args


def apply(
    *,
    menu: bool | None = None,
    startup: bool | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    """Create or remove the launcher entry, and/or set start-at-sign-in.

    ``menu=True`` reconciles the entry (which also repairs a stale one, such as the
    iconless shortcut an earlier release created). ``menu=False`` deletes it, which
    needs no installer because it is one file. ``startup`` is passed through.
    """
    if not supported():
        return False, "this installation cannot manage launcher entries"

    if menu is False:
        path = menu_path()
        if path is not None and path.exists():
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    path.unlink()
        menu = None

    if menu is None and startup is None:
        return True, "nothing to change"

    run = runner or subprocess.run
    result = run(
        command(menu=bool(menu), startup=startup), capture_output=True, text=True, check=False
    )
    if getattr(result, "returncode", 1) != 0:
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
        lines = [line for line in detail.splitlines() if line.strip()]
        return False, lines[-1] if lines else "the installer failed"
    return True, "launcher settings updated"
