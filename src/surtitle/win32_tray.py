"""A Windows notification-area icon, built directly on the Win32 shell API.

The app ships a released archive with no installer and no administrator rights,
and the whole point of that archive is that it works offline and cannot be
broken by a dependency swap. A tray icon is therefore not worth a new runtime
dependency — particularly a GUI toolkit that would drag in its own event loop
and its own threading rules next to an asyncio server. ``Shell_NotifyIcon`` and
a message loop are a few hundred lines of ctypes and nothing else.

Everything here is Windows-only and is imported lazily by
:mod:`surtitle.tray`; ``ctypes.WINFUNCTYPE`` and ``ctypes.wintypes.HICON`` do
not exist on other platforms, so importing this module elsewhere fails at import
time by design rather than silently misbehaving.

Two details are easy to get wrong and are handled deliberately:

* Every function used is given explicit ``argtypes``/``restype``. Without them
  ctypes assumes ``int``, which truncates 64-bit handles and pointers to 32 bits
  and produces a crash a long way from the cause.
* The ``WNDPROC`` callback is held on the instance for the lifetime of the
  window. Windows keeps the raw function pointer, so letting the Python object
  be collected calls into freed memory the next time the user clicks the icon.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable, Sequence
from ctypes import wintypes
from pathlib import Path

from surtitle.tray import MenuEntry

__all__ = ["MenuEntry", "TrayIcon"]

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_APP = 0x8000
WM_USER = 0x0400

# With NOTIFYICON_VERSION_4 the shell reports a keyboard selection as
# NIN_SELECT/NIN_KEYSELECT rather than a mouse message.
NIN_SELECT = WM_USER
NIN_KEYSELECT = WM_USER + 1

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NOTIFYICON_VERSION_4 = 4

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002

TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

SM_CXSMICON = 49
SM_CYSMICON = 50

MB_OK = 0x00000000
MB_YESNO = 0x00000004
MB_ICONQUESTION = 0x00000020
MB_ICONINFORMATION = 0x00000040
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000

# What MessageBoxW returns for the Yes button.
IDYES = 6

ERROR_CLASS_ALREADY_EXISTS = 1410
IDI_APPLICATION = 32512

# The tooltip field is a fixed 128-wide character array; the shell rejects the
# whole update if it does not fit, so every assignment is clipped first.
_TIP_CHARS = 127

_TRAY_MESSAGE = WM_APP + 1
_TIMER_ID = 1
# How often the shell is handed a fresh tooltip. The tooltip text itself comes
# from a cache that a separate thread refreshes, so this is only a repaint.
_REFRESH_MS = 3000


class GUID(ctypes.Structure):
    """Only present to get ``NOTIFYICONDATAW``'s 64-bit layout right."""

    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """``NOTIFYICONDATAW``, at its largest (Vista and later) size.

    ``cbSize`` must be the full structure size for ``NOTIFYICON_VERSION_4``;
    the shorter historical layouts exist only for compatibility with Windows
    2000 and are not worth supporting here.
    """

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _declare() -> None:
    """Pin every prototype. Called once, at import."""
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM

    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND

    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t

    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.GetMessageW.restype = ctypes.c_int

    _user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL

    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t

    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.PostQuitMessage.restype = None

    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostMessageW.restype = wintypes.BOOL

    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL

    _user32.CreatePopupMenu.argtypes = []
    _user32.CreatePopupMenu.restype = wintypes.HMENU

    _user32.AppendMenuW.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_size_t,
        wintypes.LPCWSTR,
    ]
    _user32.AppendMenuW.restype = wintypes.BOOL

    _user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        ctypes.c_void_p,
    ]
    # TPM_RETURNCMD makes the return value the chosen item's identifier rather
    # than a BOOL, so the declared type has to be wide enough to hold it.
    _user32.TrackPopupMenu.restype = wintypes.UINT

    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.DestroyMenu.restype = wintypes.BOOL

    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL

    _user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL

    _user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    _user32.SetTimer.restype = ctypes.c_size_t

    _user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.LoadImageW.restype = wintypes.HANDLE

    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    _user32.LoadIconW.restype = wintypes.HICON

    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _user32.DestroyIcon.restype = wintypes.BOOL

    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int

    _user32.MessageBoxW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    ]
    _user32.MessageBoxW.restype = ctypes.c_int

    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT

    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE


_declare()


def _loword(value: int) -> int:
    return value & 0xFFFF


def _int_resource(value: int) -> wintypes.LPCWSTR:
    """Wrap a small integer as the ``MAKEINTRESOURCE`` pseudo-pointer.

    ``LoadIconW`` takes either a string or a resource ordinal encoded as a
    pointer-sized integer, which ctypes cannot express by passing an ``int``
    where it expects a wide string.
    """
    return ctypes.cast(ctypes.c_void_p(value), wintypes.LPCWSTR)


class TrayIcon:
    """A notification-area icon with its own message loop on its own thread.

    The server runs ``uvicorn`` on the main thread, so the icon cannot own it.
    A Win32 window is bound to the thread that created it, which is why the
    whole thing — window, loop, menu — lives in one dedicated thread and talks
    to the rest of the process only through the callables it is given.
    """

    _CLASS_NAME = "SurtitleTrayWindow"

    def __init__(
        self,
        *,
        tooltip: Callable[[], str],
        menu: Callable[[], Sequence[MenuEntry]],
        on_select: Callable[[str], None],
        title: str = "Surtitle",
        icon_path: Path | None = None,
    ) -> None:
        self._tooltip = tooltip
        self._menu = menu
        self._on_select = on_select
        self._title = title
        self._icon_path = str(icon_path) if icon_path else None

        self._hwnd: int | None = None
        self._icon: int | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._error: BaseException | None = None
        self._stopping = False
        # Held for the lifetime of the window: Windows stores the bare pointer.
        self._wndproc = WNDPROC(self._handle_message)
        self._taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")

    # --- public API ------------------------------------------------------
    @property
    def error(self) -> BaseException | None:
        """Why the icon failed to appear, if it did."""
        return self._error

    @property
    def running(self) -> bool:
        return self._hwnd is not None and not self._stopping

    def start(self, *, timeout: float = 5.0) -> bool:
        """Create the icon. Returns False when the shell refused it.

        Blocks until the window exists so a caller that wants to report failure
        can do it in the same breath, rather than discovering it later.
        """
        self._thread = threading.Thread(target=self._pump, name="surtitle-tray", daemon=True)
        self._thread.start()
        self._started.wait(timeout=timeout)
        if self._error is not None:
            log.warning("tray icon unavailable: %s", self._error)
            return False
        return self._hwnd is not None

    def stop(self, *, timeout: float = 2.0) -> None:
        """Remove the icon and end the message loop."""
        self._stopping = True
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def message(self, text: str, *, title: str | None = None) -> None:
        """Show a modal information box owned by the icon.

        Owned, and forced to the front: the owner window is deliberately
        invisible, and an unowned box raised from a background thread can open
        behind the browser with nothing to click.
        """
        _user32.MessageBoxW(
            self._hwnd or None,
            text,
            title or self._title,
            MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST,
        )

    def confirm(self, text: str, *, title: str | None = None) -> bool:
        """Ask a yes/no question owned by the icon. True when the user says yes.

        Reserved for the choices that cost the user something — a ~100 MB
        download, say. Same ownership and topmost flags as :meth:`message`, for
        the same reason: an unowned box raised from a background thread can open
        behind the browser with nothing to click.
        """
        answer = _user32.MessageBoxW(
            self._hwnd or None,
            text,
            title or self._title,
            MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST,
        )
        return answer == IDYES

    # --- thread ----------------------------------------------------------
    def _pump(self) -> None:
        try:
            instance = _kernel32.GetModuleHandleW(None)
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = self._wndproc
            window_class.hInstance = instance
            window_class.lpszClassName = self._CLASS_NAME
            if not _user32.RegisterClassW(ctypes.byref(window_class)) and (
                # Registering the same class twice within one process is normal
                # — the class is per process, not per icon — and is not a
                # failure.
                ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            # An ordinary top-level window, created without WS_VISIBLE: it is
            # never shown, but it can own dialogs and receive the shell's
            # callback message, which a message-only window cannot do as well.
            self._hwnd = _user32.CreateWindowExW(
                0,
                self._CLASS_NAME,
                self._title,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not self._hwnd:
                raise ctypes.WinError(ctypes.get_last_error())

            self._icon = self._load_icon()
            if not self._add():
                raise OSError("the shell refused to add the notification icon")

            _user32.SetTimer(self._hwnd, _TIMER_ID, _REFRESH_MS, None)
            self._started.set()

            message = MSG()
            while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(message))
                _user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:  # reported, never raised into a dead thread
            self._error = exc
            self._started.set()
        finally:
            self._delete()
            if self._icon:
                _user32.DestroyIcon(self._icon)
                self._icon = None

    def _load_icon(self) -> int:
        """The app icon, from the shipped ``.ico``, or the generic one."""
        small = _user32.GetSystemMetrics(SM_CXSMICON) or 16
        if self._icon_path and Path(self._icon_path).is_file():
            handle = _user32.LoadImageW(
                None, self._icon_path, IMAGE_ICON, small, small, LR_LOADFROMFILE
            )
            if handle:
                return handle
            log.warning("could not load tray icon from %s; using the default", self._icon_path)
        return _user32.LoadIconW(None, _int_resource(IDI_APPLICATION))

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = _TRAY_MESSAGE
        data.hIcon = self._icon
        data.szTip = self._tip()
        return data

    def _tip(self) -> str:
        try:
            text = self._tooltip() or self._title
        except Exception:  # a broken tooltip must not take the icon down
            log.exception("tray tooltip failed")
            text = self._title
        return text[:_TIP_CHARS]

    def _add(self) -> bool:
        if not _shell32.Shell_NotifyIconW(
            NIM_ADD, ctypes.byref(self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP))
        ):
            return False
        # Version 4 is what makes the shell report clicks with usable
        # coordinates and deliver WM_CONTEXTMENU for a keyboard invocation.
        data = self._data(0)
        data.uVersion = NOTIFYICON_VERSION_4
        _shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
        return True

    def _refresh(self) -> None:
        if not self._hwnd:
            return
        data = self._data(0)
        data.uFlags = NIF_TIP
        data.szTip = self._tip()
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def _delete(self) -> None:
        if not self._hwnd:
            return
        try:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
        except Exception:  # the shell is going away; nothing useful to do
            log.debug("could not remove the tray icon", exc_info=True)

    # --- window procedure ------------------------------------------------
    def _handle_message(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Never let a Python exception escape into the Win32 message pump."""
        try:
            self._dispatch(hwnd, message, wparam, lparam)
        except Exception:
            log.exception("tray window message %s failed", message)
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _dispatch(self, hwnd: int, message: int, wparam: int, lparam: int) -> None:
        if message == _TRAY_MESSAGE:
            # With NOTIFYICON_VERSION_4 the mouse message arrives in the low
            # word of lParam, not in wParam as it did in earlier versions.
            event = _loword(lparam)
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_SELECT, NIN_KEYSELECT):
                self._select("open")
            elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
        elif message == WM_TIMER and wparam == _TIMER_ID:
            self._refresh()
        elif self._taskbar_created and message == self._taskbar_created:
            # Explorer restarted and every notification icon with it.
            log.info("the taskbar was recreated; restoring the tray icon")
            self._add()
        elif message == WM_CLOSE:
            _user32.DestroyWindow(hwnd)
        elif message == WM_DESTROY:
            self._delete()
            _user32.PostQuitMessage(0)

    def _show_menu(self) -> None:
        entries = list(self._menu())
        menu = _user32.CreatePopupMenu()
        if not menu:
            return
        chosen = 0
        try:
            for index, entry in enumerate(entries, start=1):
                if entry.separator:
                    _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    flags = MF_STRING
                    if not entry.enabled:
                        flags |= MF_GRAYED | MF_DISABLED
                    _user32.AppendMenuW(menu, flags, index, entry.label)

            point = POINT()
            _user32.GetCursorPos(ctypes.byref(point))
            # The documented dance for a tray menu: without foregrounding the
            # window first, the menu refuses to close when the user clicks
            # somewhere else.
            _user32.SetForegroundWindow(self._hwnd)
            chosen = _user32.TrackPopupMenu(
                menu,
                TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                point.x,
                point.y,
                0,
                self._hwnd,
                None,
            )
            _user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            _user32.DestroyMenu(menu)

        if 1 <= chosen <= len(entries):
            entry = entries[chosen - 1]
            if entry.action and entry.enabled:
                self._select(entry.action)

    def _select(self, action: str) -> None:
        try:
            self._on_select(action)
        except Exception:
            log.exception("tray action %r failed", action)
