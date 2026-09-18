"""Updating Surtitle from GitHub, without a terminal.

There are two shapes of installation, and they get two honest answers.

A **git checkout** can move itself. ``main`` is a fast-forward pull; a **release**
is a tag to check out. Either way the dependencies are re-synced afterwards with
uv, and the running process keeps the code it started with until it is restarted
— so the result says so rather than pretending the new version is already live.

An **archive or wheel** cannot replace itself. On Windows the running
interpreter's files are locked, and swapping a directory that is mid-execution is
how a working installation becomes a broken one. Those installs are told which
release exists and where to get it instead of being promised something the updater
will not do.

Both targets are deliberate: ``main`` is what a contributor wants to follow, a
tag is what everybody else wants. Neither is done silently — the caller confirms
first.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from surtitle.jobs import BackgroundJob

__all__ = [
    "MAIN_BRANCH",
    "RELEASES_PAGE",
    "TARGETS",
    "UpdateJob",
    "UpdateResult",
    "UpdateStatus",
    "apply",
    "check",
    "git_checkout",
    "kind",
    "latest_release",
]

REPO_SLUG = "mjoconr/Surtitle"
RELEASES_PAGE = f"https://github.com/{REPO_SLUG}/releases"
RELEASES_API = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
MAIN_BRANCH = "main"

# "release" is a checkout of the newest tag; "main" follows the development
# branch. Anything else is refused rather than guessed at.
TARGETS = ("release", "main")

_API_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """What an update would do, without doing it."""

    kind: str  # "git" | "archive"
    version: str
    branch: str = ""
    latest_release: str = ""
    release_available: bool = False
    main_available: bool = False
    behind: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "branch": self.branch,
            "latest_release": self.latest_release,
            "release_available": self.release_available,
            "main_available": self.main_available,
            "behind": self.behind,
            "detail": self.detail,
        }


@dataclass(slots=True)
class UpdateResult:
    ok: bool
    message: str
    steps: list[str] = field(default_factory=list)


def git_checkout() -> Path | None:
    """The checkout this installation runs from, if it is one.

    A release archive and a wheel both lack ``.git``; only a clone can pull.
    """
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return root
    return None


def kind() -> str:
    """``"git"`` when this installation can update itself, else ``"archive"``."""
    if git_checkout() is None or shutil.which("git") is None:
        return "archive"
    return "git"


def _git(args: list[str], root: Path, run: Callable[..., Any]) -> Any:
    return run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def _text(completed: Any) -> str:
    return (getattr(completed, "stdout", "") or "").strip()


def _last_line(completed: Any) -> str:
    for stream in (getattr(completed, "stderr", ""), getattr(completed, "stdout", "")):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return ""


def _ok(completed: Any) -> bool:
    return getattr(completed, "returncode", 1) == 0


def _is_newer(candidate: str, current: str) -> bool:
    """True when ``candidate`` is a later version than ``current``.

    Deliberately tolerant: a tag that is not numeric at all (``v2.0.0-rc1`` shaped
    oddly, or a branch-like name) is compared as equal rather than crashing a
    check that only exists to be helpful.
    """

    def parts(text: str) -> tuple[int, ...]:
        cleaned = text.strip().lstrip("vV")
        numbers = []
        for chunk in cleaned.split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            numbers.append(int(digits) if digits else 0)
        return tuple(numbers)

    return parts(candidate) > parts(current)


def _github_latest() -> dict[str, Any]:
    import httpx

    response = httpx.get(
        RELEASES_API,
        timeout=_API_TIMEOUT,
        headers={"Accept": "application/vnd.github+json"},
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def latest_release(*, fetcher: Callable[[], Any] | None = None) -> str:
    """The newest published tag name, or ``""`` when GitHub cannot be reached."""
    import httpx

    fetch = fetcher or _github_latest
    try:
        payload = fetch()
    except (httpx.HTTPError, ValueError, OSError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("tag_name") or "").strip()


def _newest_tag(root: Path, run: Callable[..., Any]) -> str:
    """The newest local tag, for when the API is unreachable but git is not."""
    lines = [
        line.strip()
        for line in _text(
            _git(["tag", "--list", "v*", "--sort=-v:refname"], root, run)
        ).splitlines()
    ]
    return lines[0] if lines else ""


def check(
    *, runner: Callable[..., Any] | None = None, fetcher: Callable[[], Any] | None = None
) -> UpdateStatus:
    """Report what an update would do, touching the network at most once."""
    from surtitle import __version__

    run = runner or subprocess.run
    root = git_checkout() if shutil.which("git") else None
    latest = latest_release(fetcher=fetcher)
    release_available = bool(latest) and _is_newer(latest, __version__)

    if root is None:
        detail = (
            f"{latest} is available to download"
            if release_available
            else f"Surtitle {__version__} is the newest release"
        )
        return UpdateStatus(
            kind="archive",
            version=__version__,
            latest_release=latest,
            release_available=release_available,
            detail=detail,
        )

    branch = _text(_git(["rev-parse", "--abbrev-ref", "HEAD"], root, run))
    behind = 0
    if _ok(_git(["fetch", "--quiet", "origin"], root, run)):
        behind = int(
            _text(_git(["rev-list", "--count", f"HEAD..origin/{MAIN_BRANCH}"], root, run)) or 0
        )
    main_available = behind > 0

    if main_available and release_available:
        detail = f"{behind} commit(s) behind main; {latest} is also available"
    elif main_available:
        detail = f"{behind} commit(s) behind origin/{MAIN_BRANCH}"
    elif release_available:
        detail = f"{latest} is available"
    else:
        detail = f"up to date on {branch or 'main'}"
    return UpdateStatus(
        kind="git",
        version=__version__,
        branch=branch,
        latest_release=latest,
        release_available=release_available,
        main_available=main_available,
        behind=behind,
        detail=detail,
    )


def _sync_dependencies(root: Path, run: Callable[..., Any]) -> str:
    """Re-install after new code lands. Returns a note, or ``""`` when skipped."""
    uv = shutil.which("uv")
    if not uv or not (root / "uv.lock").is_file():
        return "run your installer again to refresh the dependencies"
    # --inexact so an installed voice-local extra is not pruned by the update.
    result = run(
        [uv, "sync", "--inexact", "--quiet"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if _ok(result):
        return "uv sync --inexact"
    return "uv sync --inexact (failed — run it yourself)"


def apply(
    target: str = "release",
    *,
    runner: Callable[..., Any] | None = None,
    fetcher: Callable[[], Any] | None = None,
) -> UpdateResult:
    """Move a git checkout to ``target`` and refresh its dependencies.

    Refuses anything that is not a checkout, rather than half-updating a release
    archive. Never rebases or discards local work: a fast-forward or nothing.
    """
    from surtitle import __version__

    if target not in TARGETS:
        return UpdateResult(False, f"unknown update target {target!r}", [])

    run = runner or subprocess.run
    root = git_checkout()
    if root is None or not shutil.which("git"):
        return UpdateResult(
            False,
            "This installation cannot update itself — it was not installed from a "
            f"git checkout. Download the newest release from {RELEASES_PAGE}",
            [],
        )

    steps: list[str] = []
    if target == "main":
        steps.append(f"git fetch origin {MAIN_BRANCH}")
        if not _ok(_git(["fetch", "--quiet", "origin", MAIN_BRANCH], root, run)):
            return UpdateResult(False, "could not reach GitHub; check the network", steps)
        steps.append(f"git checkout {MAIN_BRANCH}")
        if not _ok(_git(["checkout", "--quiet", MAIN_BRANCH], root, run)):
            return UpdateResult(
                False, f"could not switch to {MAIN_BRANCH}; there may be local changes", steps
            )
        steps.append(f"git merge --ff-only origin/{MAIN_BRANCH}")
        merged = _git(["merge", "--ff-only", f"origin/{MAIN_BRANCH}"], root, run)
        if not _ok(merged):
            return UpdateResult(
                False, _last_line(merged) or "the update was not a fast-forward", steps
            )
    else:
        tag = latest_release(fetcher=fetcher) or _newest_tag(root, run)
        if not tag:
            return UpdateResult(False, "could not work out which release to use", steps)
        steps.append(f"git check out {tag}")
        if not _ok(_git(["fetch", "--quiet", "--tags", "origin"], root, run)):
            return UpdateResult(False, "could not reach GitHub; check the network", steps)
        if not _ok(_git(["checkout", "--quiet", tag], root, run)):
            return UpdateResult(
                False, f"could not check out {tag}; there may be local changes", steps
            )
        if not _is_newer(tag, __version__):
            return UpdateResult(True, f"already on {tag}", steps)

    note = _sync_dependencies(root, run)
    if note:
        steps.append(note)
    return UpdateResult(True, "updated; restart Surtitle to run the new version", steps)


class UpdateJob(BackgroundJob):
    """One background update, with a state the tray and the UI can read."""

    def __init__(self) -> None:
        super().__init__(name="update")

    def _work(self, target: str) -> tuple[bool, str]:
        self.set_progress(0.0, f"updating from {target}…")
        result = apply(target)
        return result.ok, result.message
