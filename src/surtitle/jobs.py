"""One long-running background task, with a state anything can read.

Installing the speech engines and pulling an update are the same shape: a
blocking subprocess and a network transfer that must not run on the server's
event loop, a result nobody waits for synchronously, and a progress line the tray
menu and the Settings screen both want. Owning that shape once keeps the two from
growing different ideas of what "running" means — and keeps the failure path
(the one that must never raise into a request handler) in a single place.
"""

from __future__ import annotations

import threading
from typing import Any

__all__ = ["BackgroundJob"]


class BackgroundJob:
    """A thread that runs :meth:`_work` and publishes how it is going.

    Subclasses implement :meth:`_work`, returning ``(ok, message)``, and call
    :meth:`set_progress` while it runs.
    """

    def __init__(self, *, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._ok: bool | None = None
        self._message = "not started"
        self._percent = 0.0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "ok": self._ok,
                "message": self._message,
                "percent": round(self._percent, 1),
            }

    def start(self, *args: Any, **kwargs: Any) -> bool:
        """Start the work. False when it is already running."""
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._ok = None
            self._message = "starting…"
            self._percent = 0.0

        thread = threading.Thread(
            target=self._run,
            args=args,
            kwargs=kwargs,
            name=f"surtitle-{self._name}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the work finishes. True when it is not running."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.running

    def set_progress(self, percent: float, message: str) -> None:
        with self._lock:
            self._percent = percent
            self._message = message

    def _run(self, *args: Any, **kwargs: Any) -> None:
        try:
            ok, message = self._work(*args, **kwargs)
        except Exception as exc:  # a failure is reported, never raised into a request
            ok, message = False, f"{self._name} failed: {exc}"
        with self._lock:
            self._running = False
            self._ok = ok
            self._message = message
            if ok:
                self._percent = 100.0

    def _work(self, *args: Any, **kwargs: Any) -> tuple[bool, str]:
        raise NotImplementedError
