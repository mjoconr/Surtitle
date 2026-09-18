"""Choosing a folder on the machine the agent actually runs on.

The browser cannot answer this. The File System Access API hands back an opaque
handle, not a path the server could hand to ``resolve_in_root`` and read — and
the whole point of the project folder is that the agent can work inside it. So the
dialog opens here, in the process that owns the files, and returns an ordinary
absolute path.

Three implementations behind one function, because there is no portable one:
``SHBrowseForFolderW`` through ctypes on Windows, ``choose folder`` through
osascript on macOS, and zenity or kdialog on Linux. A machine with no desktop (a
headless server, an SSH session) answers ``None`` by being *unavailable* rather
than by hanging on a window nobody can see.

The Windows one runs in a child process rather than in this one, which is
:mod:`surtitle.win32_folder_dialog` and explains why. The short version is that
the server can only offer the dialog a worker thread and was started detached, so
the window it produced could not be found on screen; a fresh process gets a main
thread, per-monitor DPI awareness and a synthesized Alt press, which is what it
takes to put the dialog in front of the user.

This is the *second* answer to "where is the project folder" and never the only
one: :mod:`surtitle.folder_browse` lists the filesystem over HTTP, so a machine
this cannot serve — a remote browser, a headless host, a dialog that refuses to
appear — still has a working picker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_TITLE", "available", "choose_folder"]

DEFAULT_TITLE = "Choose a folder for this project"

# The child process's contract; see surtitle/win32_folder_dialog.py. Exit 3 is a
# cancel, which is an answer rather than a failure.
_WIN32_CANCELLED = 3
# CREATE_NO_WINDOW. The child is a console program only because the release ships
# the console interpreter; without this the user sees a black window flash before
# the dialog, which reads as a bug.
_CREATE_NO_WINDOW = 0x08000000


def available(
    *, which: Callable[[str], str | None] = shutil.which, platform: str | None = None
) -> bool:
    """True when this machine can show a folder chooser at all."""
    current = platform or sys.platform
    if current == "win32":
        return True
    if current == "darwin":
        return which("osascript") is not None
    return which("zenity") is not None or which("kdialog") is not None


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run(command: list[str], runner: Callable[..., Any]) -> str | None:
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    # A cancelled dialog exits non-zero on every one of these tools, which is a
    # normal answer rather than a failure to report.
    if getattr(result, "returncode", 1) != 0:
        return None
    path = (getattr(result, "stdout", "") or "").strip()
    return path or None


def _macos_choose(title: str, initial: str | None, runner: Callable[..., Any]) -> str | None:
    location = ""
    if initial:
        location = f' default location POSIX file "{_escape(initial)}"'
    script = f'POSIX path of (choose folder with prompt "{_escape(title)}"{location})'
    return _run(["osascript", "-e", script], runner)


def _linux_choose(
    title: str,
    initial: str | None,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
) -> str | None:
    zenity = which("zenity")
    if zenity:
        return _run(
            [zenity, "--file-selection", "--directory", f"--title={title}"],
            runner,
        )
    kdialog = which("kdialog")
    if kdialog:
        start = initial or str(Path.home())
        return _run([kdialog, "--getexistingdirectory", start, f"--title={title}"], runner)
    return None


def _windows_command(title: str, initial: str | None) -> list[str]:
    """The argv that runs the chooser in its own process."""
    command = [sys.executable, "-m", "surtitle.win32_folder_dialog", title]
    if initial:
        command.append(initial)
    return command


def _child_choice(result: Any) -> str | None:
    """Read a chooser child's answer: a path, or nothing for cancel or failure."""
    if getattr(result, "returncode", 1) != 0:
        return None
    return (getattr(result, "stdout", "") or "").strip() or None


def _windows_choose(title: str, initial: str | None, runner: Callable[..., Any]) -> str | None:
    """Show the Windows chooser in a child process. ``None`` means no path.

    A cancel and a crash are deliberately the same answer here. Both leave the
    user with no folder chosen, and the picker's own in-app browser is still
    available, so there is nothing the caller could do differently with the
    distinction.
    """
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "check": False}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        result = runner(_windows_command(title, initial), encoding="utf-8", **kwargs)
    except OSError:
        return None
    return _child_choice(result)


def choose_folder(
    *,
    title: str = DEFAULT_TITLE,
    initial: str | None = None,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform: str | None = None,
) -> str | None:
    """Show a folder chooser and return the chosen absolute path.

    ``None`` means the user cancelled, or that this machine has no chooser.
    :func:`available` tells those apart when the caller needs to.
    """
    run = runner or subprocess.run
    current = platform or sys.platform
    if current == "win32":
        return _windows_choose(title, initial, run)
    if current == "darwin":
        if which("osascript") is None:
            return None
        return _macos_choose(title, initial, run)
    return _linux_choose(title, initial, run, which)
