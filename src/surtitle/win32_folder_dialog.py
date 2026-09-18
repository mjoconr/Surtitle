"""The Windows folder chooser, run as its own short-lived process.

This exists as a separate process for reasons that are all failure modes of doing
it in the server:

* **Thread.** The shell dialog runs a modal message loop on the thread that calls
  it. The server can only offer it a worker thread, and a dialog on a worker
  thread of a process started detached from any console is exactly the situation
  where the window never appears or cannot be found on screen.
* **Foreground.** A process started in the background cannot simply put a window
  in front of what the user is doing. This one synthesizes a single Alt press
  immediately before showing the dialog, which is what actually lets the window
  take the foreground — the same technique the DeepSeek harness uses for its
  workspace picker.
* **DPI.** One process can only choose its DPI awareness once, at startup. Here
  that is free to be per-monitor-v2, so the dialog is not a blurry scaled bitmap
  on a high-DPI screen.

The protocol is deliberately small: the chosen path on stdout, exit 0; exit 3 for
a cancel; exit 4 with a reason on stderr for anything else. A caller that gets
anything unexpected treats it as "no path", because a picker that fails must cost
the user the dialog, not the request.

Run as ``python -m surtitle.win32_folder_dialog <title> [initial-directory]``.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys

__all__ = ["BFFM_INITIALIZED", "BFFM_SETSELECTIONW", "choose", "main"]

_BIF_RETURNONLYFSDIRS = 0x00000001
_BIF_EDITBOX = 0x00000010
_BIF_NEWDIALOGSTYLE = 0x00000040
_MAX_PATH = 32768

BFFM_INITIALIZED = 1
# SendMessageW(hwnd, BFFM_SETSELECTIONW, TRUE, (LPARAM)path) — the wide variant
# is the one that takes a Unicode string.
BFFM_SETSELECTIONW = 0x0467

_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002
# PER_MONITOR_AWARE_V2, then the older PROCESS_PER_MONITOR_DPI_AWARE.
_DPI_CONTEXT_PER_MONITOR_V2 = -4
_PROCESS_PER_MONITOR_DPI_AWARE = 2

EXIT_CANCELLED = 3
EXIT_FAILED = 4


def _make_dpi_aware() -> None:  # pragma: no cover - Windows only
    """Best effort: a refusal here costs sharpness, not the dialog."""
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return
    with contextlib.suppress(OSError):
        with_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if with_context is not None and with_context(ctypes.c_void_p(_DPI_CONTEXT_PER_MONITOR_V2)):
            return
    # The fallback for a Windows older than the per-monitor-v2 API.
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)


def _grant_foreground() -> None:  # pragma: no cover - Windows only
    """One synthesized Alt press, so the dialog may activate in front.

    Windows only lets a process move a window to the foreground if it is itself in
    the foreground, or if it received input recently. Synthesizing one keystroke
    counts as the latter and costs nothing visible: the dialog opens from the
    press, not from the release.
    """
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(_VK_MENU, 0, 0, 0)
        user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    except (AttributeError, OSError):
        pass


def choose(title: str, initial: str | None = None) -> str | None:  # pragma: no cover
    """Show the folder chooser and return the chosen path, or None on cancel."""
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

    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, wintypes.HWND, wintypes.UINT, wintypes.LPARAM, wintypes.LPARAM
    )

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32

    # The dialog needs an apartment-threaded COM apartment on this thread. A
    # refusal means somebody already chose a different model, which is not fatal;
    # the matching CoUninitialize is then skipped.
    initialized = ole32.CoInitialize(None) == 0
    keep_alive = None
    try:
        display = ctypes.create_unicode_buffer(_MAX_PATH)
        info = BROWSEINFOW()
        info.pidlRoot = None
        info.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
        info.lpszTitle = title
        info.ulFlags = _BIF_RETURNONLYFSDIRS | _BIF_EDITBOX | _BIF_NEWDIALOGSTYLE

        if initial:
            shell32.SendMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            shell32.SendMessageW.restype = wintypes.LPARAM
            wanted = str(initial)

            def _preselect(hwnd, message, _lparam, _data):
                if message == BFFM_INITIALIZED:
                    shell32.SendMessageW(
                        hwnd,
                        BFFM_SETSELECTIONW,
                        1,
                        ctypes.cast(ctypes.c_wchar_p(wanted), ctypes.c_void_p).value,
                    )
                return 0

            # The callback must outlive the call, or Windows calls through a
            # collected pointer.
            keep_alive = callback_type(_preselect)
            info.lpfn = ctypes.cast(keep_alive, ctypes.c_void_p)
            info.lParam = 0

        _grant_foreground()
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
            # The shell owns this item id list; leaking one per dialog is small
            # but permanent.
            ole32.CoTaskMemFree(pidl)
        del keep_alive
        return path.value if found and path.value else None
    finally:
        if initialized:
            ole32.CoUninitialize()


def main(argv: list[str] | None = None) -> int:
    """Print the chosen folder and return a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    title = args[0] if args else "Choose a folder"
    initial = args[1] if len(args) > 1 and args[1] else None

    # The path is handed back over a pipe, so it must not be mangled by a legacy
    # code page on the way out.
    with_error = getattr(sys.stdout, "reconfigure", None)
    if with_error is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if sys.platform != "win32":  # pragma: no cover - guarded in tests
        print("the folder chooser needs Windows", file=sys.stderr)
        return EXIT_FAILED

    _make_dpi_aware()
    try:
        chosen = choose(title, initial)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never a traceback
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if not chosen:
        return EXIT_CANCELLED
    print(chosen)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
