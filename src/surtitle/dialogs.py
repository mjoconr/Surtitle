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
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_TITLE", "available", "choose_folder"]

DEFAULT_TITLE = "Choose a folder for this project"

# Windows: BIF_RETURNONLYFSDIRS hides files; BIF_NEWDIALOGSTYLE gives the resizable
# dialog with a "New folder" button; BIF_EDITBOX lets a path be typed.
_BIF_RETURNONLYFSDIRS = 0x00000001
_BIF_EDITBOX = 0x00000010
_BIF_NEWDIALOGSTYLE = 0x00000040
_MAX_PATH = 260


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


def _windows_choose(
    title: str, initial: str | None
) -> str | None:  # pragma: no cover - Windows only
    """The Win32 folder picker, through ctypes so it adds no dependency.

    ``SHBrowseForFolderW`` is older than the shell's newer ``IFileDialog``, but it
    is a single call with a plain struct rather than a COM interface that has to be
    vtable-walked by hand — and the extra work buys a dialog the user cannot tell
    apart for this purpose.
    """
    import ctypes
    from ctypes import wintypes

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32

    # The dialog runs on whichever thread called this; COM has to be initialized
    # there first. A refusal (already initialized with another threading model) is
    # not fatal, so the corresponding CoUninitialize is skipped.
    initialized = ole32.CoInitialize(None) == 0
    try:
        display = ctypes.create_unicode_buffer(_MAX_PATH)
        info = BROWSEINFOW()
        info.pidlRoot = None
        info.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
        info.lpszTitle = title
        info.ulFlags = _BIF_RETURNONLYFSDIRS | _BIF_EDITBOX | _BIF_NEWDIALOGSTYLE

        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return None

        path = ctypes.create_unicode_buffer(_MAX_PATH)
        shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
        try:
            found = shell32.SHGetPathFromIDListW(pidl, path)
        finally:
            # The shell allocated the item id list; leaking one per dialog would
            # be small but permanent.
            ole32.CoTaskMemFree(pidl)
        return path.value if found and path.value else None
    finally:
        if initialized:
            ole32.CoUninitialize()


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
        return _windows_choose(title, initial)
    if current == "darwin":
        if which("osascript") is None:
            return None
        return _macos_choose(title, initial, run)
    return _linux_choose(title, initial, run, which)
