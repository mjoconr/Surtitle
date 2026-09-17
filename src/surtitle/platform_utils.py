"""Small cross-platform helpers with no third-party dependencies."""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

__all__ = ["find_free_port", "human_bytes", "is_windows", "open_browser", "port_is_free"]


def is_windows() -> bool:
    """True when running on Windows."""
    return sys.platform == "win32"


def human_bytes(count: float) -> str:
    """Compact size rendering, e.g. ``812 B``, ``4.1 KB``, ``1.4 GB``."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def port_is_free(host: str, port: int) -> bool:
    """Return ``True`` when ``host:port`` can be bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(host: str, start: int, *, attempts: int = 25) -> int:
    """Return the first free port at or after ``start``.

    Raises :class:`OSError` when nothing is free in the probed range, so failures
    name the range instead of silently binding something unexpected.
    """
    for candidate in range(start, start + attempts):
        if port_is_free(host, candidate):
            return candidate
    raise OSError(f"no free port found in range {start}-{start + attempts - 1} on {host}")


def open_browser(url: str, *, delay: float = 0.8) -> None:
    """Open ``url`` in the default browser after a short delay.

    Runs on a daemon thread so it never blocks the server's startup path.
    """

    def _open() -> None:
        # Opening a browser is best-effort: a failure must never affect the server.
        with contextlib.suppress(Exception):  # pragma: no cover - browser quirks
            webbrowser.open(url, new=2)

    timer = threading.Timer(delay, _open)
    timer.daemon = True
    timer.start()


def readable_path(path: Path) -> str:
    """Render a path compactly, using ``~`` for the home directory."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def env_flag(name: str, *, default: bool = False) -> bool:
    """Parse a boolean environment variable leniently."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
