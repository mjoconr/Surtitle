"""Noticing that a newer Surtitle has been published, and saying so a few times.

An update nobody hears about is an update nobody installs, and a notice that
repeats forever is a notice people learn to dismiss. So this is deliberately
bounded: the user is told at most :data:`MAX_ANNOUNCEMENTS` times about a given
version, never twice within :data:`COOLDOWN_SECONDS`, and the count resets when a
*newer* version appears — which is a genuinely new thing to say.

Two pieces:

* :func:`is_newer` and :class:`ReleaseWatcher`, which compare the running version
  against the newest published release and cache the answer, because the caller is
  a tray polling every couple of seconds and GitHub's API is not free;
* :class:`ReleaseNotices`, which persists how often the user has been told, in the
  app data directory, so closing the app does not restart the nagging.

Prereleases are not announced to a user running a release: "0.6.0-rc1 is available"
is noise unless they asked to follow the development line, and they can still see
it on the releases page.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from surtitle import __version__
from surtitle.config import Settings, get_settings

__all__ = [
    "COOLDOWN_SECONDS",
    "MAX_ANNOUNCEMENTS",
    "ReleaseNotices",
    "ReleaseWatcher",
    "is_newer",
    "parse_version",
]

log = logging.getLogger(__name__)

# How many times one version is announced before it stops being mentioned.
MAX_ANNOUNCEMENTS = 3
# ...and how long to wait between two of those mentions. Twelve hours means the
# three land over about a day, which is long enough to be useful and short enough
# not to be nagging.
COOLDOWN_SECONDS = 12 * 60 * 60
# How long a checked answer is trusted. The tray polls the server, not GitHub, so
# this is what keeps a two-second poll from becoming 43,200 requests a day.
CHECK_INTERVAL_SECONDS = 6 * 60 * 60


def parse_version(text: str) -> tuple[tuple[int, ...], bool]:
    """``"v1.2.3-rc1"`` -> ``((1, 2, 3), True)``. The flag marks a prerelease.

    Deliberately not a dependency: the only versions this has to compare are the
    project's own, which are three dotted numbers with an optional ``-suffix``.
    Anything unrecognised compares as zeroes rather than raising, because a
    version string that cannot be parsed must not break an update check.
    """
    core = (text or "").strip().lstrip("vV").split("+")[0]
    main, _, prerelease = core.partition("-")
    numbers = tuple(int(part) for part in re.findall(r"\d+", main))
    return numbers, bool(prerelease.strip())


def _padded(numbers: tuple[int, ...], width: int) -> tuple[int, ...]:
    return numbers + (0,) * max(0, width - len(numbers))


def is_newer(current: str, candidate: str) -> bool:
    """True when ``candidate`` is a version worth telling the user about."""
    mine, mine_is_pre = parse_version(current)
    theirs, theirs_is_pre = parse_version(candidate)
    if not theirs:
        return False
    if theirs_is_pre and not mine_is_pre:
        # A prerelease of a later version is still a prerelease: announcing it to
        # somebody running a released build is noise they did not ask for.
        return False
    width = max(len(mine), len(theirs))
    left, right = _padded(mine, width), _padded(theirs, width)
    if left != right:
        return right > left
    # Same numbers: a final release supersedes its own prereleases, and a
    # prerelease is not news to somebody already running one.
    return mine_is_pre and not theirs_is_pre


@dataclass(slots=True)
class LatestRelease:
    """The published release, as much of it as a notification needs."""

    version: str = ""
    tag: str = ""
    name: str = ""
    page: str = ""
    published_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tag": self.tag,
            "name": self.name,
            "page": self.page,
            "published_at": self.published_at,
        }


def _from_payload(payload: dict[str, Any]) -> LatestRelease:
    tag = str(payload.get("tag_name") or "").strip()
    return LatestRelease(
        version=tag.lstrip("vV"),
        tag=tag,
        name=str(payload.get("name") or "").strip(),
        page=str(payload.get("html_url") or "").strip(),
        published_at=str(payload.get("published_at") or "").strip(),
    )


class ReleaseNotices:
    """How many times the user has been told about a version, on disk.

    Kept in the data directory rather than in memory: an app restarted three times
    in an afternoon would otherwise announce the same release three times, which
    is exactly the behaviour the cap exists to prevent.
    """

    def __init__(self, path: Path, *, max_announcements: int = MAX_ANNOUNCEMENTS) -> None:
        self.path = path
        self.max_announcements = max_announcements
        self._lock = threading.Lock()

    # --- storage ---------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self, payload: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Written beside and renamed, so a crash mid-write cannot leave a
            # truncated file that reads as "never announced anything" — which
            # would restart the whole sequence.
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            log.debug("could not record the release notice", exc_info=True)

    # --- policy ----------------------------------------------------------
    def _load(self, version: str) -> dict[str, Any]:
        stored = self._read()
        if stored.get("version") != version:
            # A different release is a different thing to say.
            return {"version": version, "announcements": 0, "last_announced": 0.0}
        return {
            "version": version,
            "announcements": int(stored.get("announcements") or 0),
            "last_announced": float(stored.get("last_announced") or 0.0),
        }

    def state(self, version: str, *, now: float | None = None) -> dict[str, Any]:
        """What has been said about ``version``, and whether more may be said."""
        if not version:
            return {"version": "", "announcements": 0, "can_announce": False}
        moment = time.time() if now is None else now
        with self._lock:
            stored = self._load(version)
        announced = stored["announcements"]
        waited = moment - stored["last_announced"]
        can = announced < self.max_announcements and (announced == 0 or waited >= COOLDOWN_SECONDS)
        return {
            "version": version,
            "announcements": announced,
            "remaining": max(0, self.max_announcements - announced),
            "can_announce": can,
            "next_in_seconds": 0 if can else max(0, int(COOLDOWN_SECONDS - waited)),
        }

    def record(self, version: str, *, now: float | None = None) -> dict[str, Any]:
        """Count one announcement of ``version`` and return the new state."""
        if not version:
            return {"version": "", "announcements": 0, "can_announce": False}
        moment = time.time() if now is None else now
        with self._lock:
            stored = self._load(version)
            stored["announcements"] = min(stored["announcements"] + 1, self.max_announcements)
            stored["last_announced"] = moment
            self._write(stored)
        return self.state(version, now=moment)

    def forget(self) -> None:
        """Drop the record, so a version can be announced again. Used by tests."""
        self.path.unlink(missing_ok=True)


@dataclass(slots=True)
class _Cache:
    checked_at: float = 0.0
    latest: LatestRelease | None = None
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class ReleaseWatcher:
    """Checks GitHub for a newer release, and remembers the answer for a while.

    The network call is on a method rather than a background thread on purpose:
    the app already has a request that can carry it (``GET /api/release``), and a
    thread started at import time would make every test that builds an app talk to
    GitHub.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        fetcher: Callable[[str], Any] | None = None,
        current: str | None = None,
        interval: float = CHECK_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings or get_settings()
        self._fetcher = fetcher
        self.current = current or __version__
        self.interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._cache = _Cache()
        self.notices = ReleaseNotices(self.settings.data_dir / "release-notice.json")

    # --- checking --------------------------------------------------------
    def check(
        self, *, force: bool = False, fetcher: Callable[[str], Any] | None = None
    ) -> dict[str, Any]:
        """Look up the newest release, at most once per :attr:`interval`."""
        from surtitle import selfupdate

        moment = self._clock()
        with self._lock:
            fresh = self._cache.checked_at and (moment - self._cache.checked_at) < self.interval
            if fresh and not force:
                return self._describe()
            # Mark the attempt before making it, so a slow or hanging request
            # cannot be retried by every poll that arrives while it runs.
            self._cache.checked_at = moment

        # Not selfupdate.release(): that swallows a failed lookup into an empty
        # payload, which is right for an update the user asked for and wrong here —
        # "could not reach GitHub" and "no release found" are different things to
        # put in front of somebody wondering why they were not told.
        fetch = fetcher or self._fetcher or selfupdate._get_json
        try:
            payload = fetch(f"{selfupdate.RELEASES_API}/latest")
        except Exception as exc:  # noqa: BLE001 - a failed check is information, not a crash
            payload, error = {}, f"{type(exc).__name__}: {exc}"
        else:
            error = ""

        with self._lock:
            self._cache.payload = payload if isinstance(payload, dict) else {}
            self._cache.latest = _from_payload(self._cache.payload) if self._cache.payload else None
            self._cache.error = error or (
                "" if self._cache.latest else "no published release found"
            )
            self._cache.checked_at = moment
            return self._describe()

    def snapshot(self) -> dict[str, Any]:
        """The cached answer, without touching the network."""
        with self._lock:
            return self._describe()

    def record_notice(self) -> dict[str, Any]:
        """Count one announcement of the available version."""
        snapshot = self.snapshot()
        version = (snapshot.get("latest") or {}).get("version") or ""
        state = self.notices.record(version)
        snapshot.update(
            {
                "announcements": state["announcements"],
                "can_announce": state["can_announce"],
                "remaining": state.get("remaining", 0),
            }
        )
        return snapshot

    def _describe(self) -> dict[str, Any]:
        """Build the payload from the cache. Caller holds the lock."""
        latest = self._cache.latest
        version = latest.version if latest else ""
        available = bool(version) and is_newer(self.current, version)
        quiet: dict[str, Any] = {"announcements": 0, "can_announce": False}
        notice = self.notices.state(version) if available else quiet
        return {
            "current": self.current,
            "latest": latest.to_dict() if latest else None,
            "available": available,
            "checked_at": self._cache.checked_at,
            "error": self._cache.error,
            "can_announce": bool(notice.get("can_announce")),
            "announcements": int(notice.get("announcements") or 0),
            "remaining": int(notice.get("remaining") or 0),
        }
