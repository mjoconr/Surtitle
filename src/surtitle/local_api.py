"""Finding the local server, and talking to it.

``surtitle tray`` runs as its own process, and it has to answer two questions
before it can draw anything: *is a server running on this machine*, and *on which
port*. Probing forward from the default port would be a guess — the server itself
probes for a free port at startup and may be on any of twenty-five of them — so
the server writes down where it landed in the data directory when it starts, and
deletes the note when it stops.

The note is a hint, never the truth. A hint can be stale (a crashed process, a
restored backup, two installs sharing a data directory) and acting on a stale
hint means the Stop menu item targets nothing. So every read is confirmed
against the running server, and a note that fails to answer is treated as absent.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from surtitle.config import Settings

__all__ = [
    "INSTANCE_FILENAME",
    "ServerInstance",
    "clear_instance",
    "fetch_status",
    "find_instance",
    "instance_path",
    "read_instance",
    "request_shutdown",
    "request_update",
    "request_voice_install",
    "write_instance",
]

INSTANCE_FILENAME = "server.json"

# Short on purpose. Every caller of these helpers is either drawing a tray menu
# or making a tray menu responsive, and a menu that hangs for the length of a
# TCP timeout reads as a broken app. A loopback server answers in microseconds.
_PROBE_TIMEOUT = 1.5
_STOP_TIMEOUT = 3.0


@dataclass(frozen=True, slots=True)
class ServerInstance:
    """Where a running server is, and which process it is."""

    pid: int
    host: str
    port: int
    version: str = ""
    started_at: float = 0.0

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "version": self.version,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ServerInstance:
        return cls(
            pid=int(payload.get("pid") or 0),
            host=str(payload.get("host") or "127.0.0.1"),
            port=int(payload.get("port") or 0),
            version=str(payload.get("version") or ""),
            started_at=float(payload.get("started_at") or 0.0),
        )


def instance_path(settings: Settings) -> Path:
    """Where the running-server note lives, inside the app data directory."""
    return settings.data_dir / INSTANCE_FILENAME


def write_instance(
    settings: Settings,
    *,
    host: str,
    port: int,
    version: str,
    pid: int | None = None,
) -> ServerInstance:
    """Record where this process is serving, for ``surtitle tray`` to find.

    Best-effort: a data directory that cannot be written is not a reason to
    refuse to serve, and the tray still finds a server on the default port.
    """
    import time as _time

    instance = ServerInstance(
        pid=pid if pid is not None else os.getpid(),
        host=host,
        port=port,
        version=version,
        started_at=_time.time(),
    )
    target = instance_path(settings)
    with contextlib.suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-replace so a reader never sees half a file: the tray may
        # read this the instant it is created.
        scratch = target.with_suffix(".tmp")
        scratch.write_text(json.dumps(instance.to_dict()), encoding="utf-8")
        os.replace(scratch, target)
    return instance


def clear_instance(settings: Settings) -> None:
    """Remove the note. Leaves another process's note alone."""
    target = instance_path(settings)
    with contextlib.suppress(OSError):
        if target.is_file() and _is_ours(target):
            target.unlink()


def _is_ours(target: Path) -> bool:
    """True when the note names this process, so we do not delete a live peer's."""
    instance = _read_path(target)
    return instance is None or instance.pid == os.getpid()


def _read_path(target: Path) -> ServerInstance | None:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    instance = ServerInstance.from_dict(payload)
    return instance if instance.port else None


def read_instance(settings: Settings) -> ServerInstance | None:
    """The recorded instance, without checking that it is still alive."""
    return _read_path(instance_path(settings))


def fetch_status(url: str, *, timeout: float = _PROBE_TIMEOUT) -> dict[str, Any] | None:
    """``GET /api/status``, or ``None`` when nothing answers there.

    Never raises. "Nothing is listening" is the normal state of affairs for a
    stopped server, not an error the caller has to handle.
    """
    try:
        with httpx.Client(base_url=url, timeout=timeout) as client:
            response = client.get("/api/status")
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def request_shutdown(url: str, *, timeout: float = _STOP_TIMEOUT) -> bool:
    """Ask the server at ``url`` to stop. True when it accepted the request."""
    try:
        with httpx.Client(base_url=url, timeout=timeout) as client:
            response = client.post("/api/shutdown")
    except (httpx.HTTPError, ValueError, OSError):
        return False
    return response.status_code == 200


def request_voice_install(url: str, *, timeout: float = _PROBE_TIMEOUT) -> dict[str, Any] | None:
    """Ask the server at ``url`` to install the local engines and their models.

    The server starts the work in the background, so this returns as soon as it
    has accepted the request rather than when the download has finished. ``None``
    means nothing answered; a 409 (already running) is a normal answer, not an
    error, and is passed through with ``started`` false.
    """
    try:
        with httpx.Client(base_url=url, timeout=timeout) as client:
            response = client.post("/api/voice/install")
    except (httpx.HTTPError, ValueError, OSError):
        return None
    if response.status_code not in (200, 202, 409):
        return None
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def request_update(
    url: str, target: str = "release", *, timeout: float = _PROBE_TIMEOUT
) -> dict[str, Any] | None:
    """Ask the server at ``url`` to pull ``target`` (``"release"`` or ``"main"``).

    Like the voice install, the server starts the work in the background, so this
    returns as soon as the request is accepted. ``None`` means nothing answered.
    """
    try:
        with httpx.Client(base_url=url, timeout=timeout) as client:
            response = client.post("/api/update", json={"target": target})
    except (httpx.HTTPError, ValueError, OSError):
        return None
    if response.status_code not in (200, 202, 409):
        return None
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def find_instance(
    settings: Settings,
    *,
    url: str | None = None,
    timeout: float = _PROBE_TIMEOUT,
) -> ServerInstance | None:
    """Locate a live server: an explicit URL, the recorded one, or the default.

    Returns ``None`` when nothing answers, and clears the note in that case so
    the next attempt does not read it again.
    """
    candidates: list[str] = []
    if url:
        candidates.append(url)
    else:
        recorded = read_instance(settings)
        if recorded is not None:
            candidates.append(recorded.url)
        candidates.append(f"http://{settings.host}:{settings.port}")

    for candidate in dict.fromkeys(candidates):
        status = fetch_status(candidate, timeout=timeout)
        if status is None:
            continue
        # Prefer what the server says about itself over what the file claimed:
        # the file is a hint, and this is the process that will answer Stop.
        host, port = _split_url(candidate, settings)
        return ServerInstance(
            pid=int(status.get("pid") or 0),
            host=host,
            port=port,
            version=str(status.get("version") or ""),
            started_at=float(status.get("started_at") or 0.0),
        )

    if url is None:
        with contextlib.suppress(OSError):
            instance_path(settings).unlink(missing_ok=True)
    return None


def _split_url(url: str, settings: Settings) -> tuple[str, int]:
    """Host and port from a URL, falling back to the configured ones."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return settings.host, settings.port
    return parsed.host or settings.host, parsed.port or settings.port
