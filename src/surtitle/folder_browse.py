"""Browsing this machine's filesystem to choose a project folder.

The browser cannot answer where a project lives: the File System Access API
returns an opaque handle rather than a path, and the agent has to be confined to
a real directory on the machine running the server. The native chooser answers
the question when it can be shown — but it is a window on somebody's desktop. It
cannot serve a remote browser, it can fail to take the foreground when the server
was started detached, and on a machine with no desktop it does not exist at all.

So the server answers the question itself as well, one directory level at a time:
list the child directories of a path, hand back the breadcrumb chain that leads
to it, and create a child directory. Everything the picker needs is then a plain
JSON round trip, which works in any browser on any machine — including the
headless one the native chooser cannot reach.

Only directories are listed. A project folder is a directory, files are noise,
and returning them would be a directory-listing service for the whole filesystem
for no benefit. Paths must be fully qualified: a relative path would silently
resolve against the server process's working directory, which is not a location
the user chose.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_ENTRIES",
    "BrowserError",
    "Listing",
    "create_directory",
    "fully_qualified",
    "listing",
    "roots",
]

# One level is bounded the way a directory listing in a web UI has to be: a
# folder holding tens of thousands of children should cost a truncated answer
# rather than a stalled request.
MAX_ENTRIES = 1000

# Only meaningful on Windows, where these cannot appear in a name at all.
_WINDOWS_ILLEGAL = set('<>:"|?*')
_FILE_ATTRIBUTE_HIDDEN = 0x2


class BrowserError(Exception):
    """A refusal the caller can act on, in the picker's own vocabulary."""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.path = path

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": str(self), "path": self.path}


@dataclass(slots=True, frozen=True)
class Entry:
    """One child directory. ``path`` is absolute, so no client joins segments."""

    name: str
    path: str
    hidden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "hidden": self.hidden}


@dataclass(slots=True)
class Listing:
    """One directory level, plus every jump target the picker offers."""

    path: str
    parent: str | None = None
    crumbs: list[dict[str, str]] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    home: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "parent": self.parent,
            "crumbs": list(self.crumbs),
            "entries": [entry.to_dict() for entry in self.entries],
            "roots": list(self.roots),
            "home": self.home,
            "truncated": self.truncated,
        }


def fully_qualified(path: str | Path, *, platform: str | None = None) -> bool:
    """True when ``path`` names one fixed location regardless of process state.

    On Windows that means a drive-qualified path (``C:\\Users``) or a complete UNC
    path (``\\\\server\\share``). A rooted but drive-less form such as ``\\Users``
    is *not* qualified: Windows resolves it against the process's current drive,
    so accepting it would let the caller name a different folder than the one
    they meant.
    """
    text = str(path)
    if (platform or sys.platform) != "win32":
        return text.startswith("/")
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    return bool(re.match(r"^\\\\[^\\/]+[\\/][^\\/]+", text))


def _letters_from_mask(mask: int) -> list[str]:
    """Drive roots named by a ``GetLogicalDrives`` bitmask."""
    return [f"{chr(ord('A') + index)}:\\" for index in range(26) if mask & (1 << index)]


def _windows_roots() -> list[str]:  # pragma: no cover - Windows only
    if hasattr(os, "listdrives"):  # Python 3.12+
        return [str(drive).rstrip("\\/") + "\\" for drive in os.listdrives()]
    import ctypes

    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return []
    return _letters_from_mask(int(mask))


def roots(*, platform: str | None = None) -> list[str]:
    """Every filesystem root the picker offers as a jump target."""
    if (platform or sys.platform) != "win32":
        return [os.sep]
    return _windows_roots()


def _is_hidden(name: str, attributes: int | None) -> bool:
    """Hidden means dot-prefixed, plus the Windows attribute where it is readable.

    ``os.stat_result`` exposes ``st_file_attributes`` only on Windows, so this is
    the one place the platform's own idea of "hidden" can be honoured instead of
    guessing from the name.
    """
    if name.startswith("."):
        return True
    return attributes is not None and bool(attributes & _FILE_ATTRIBUTE_HIDDEN)


def crumbs_for(target: Path) -> list[dict[str, str]]:
    """The chain from the filesystem root down to ``target``.

    The root crumb is labelled by its full path — ``/`` or ``C:\\`` says more
    than an empty name would — and every other crumb is a jump target.
    """
    chain: list[Path] = []
    current = target
    while True:
        chain.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    result: list[dict[str, str]] = []
    for entry in reversed(chain):
        at_root = entry.parent == entry
        result.append({"name": str(entry) if at_root else entry.name, "path": str(entry)})
    return result


def _collect(target: Path, *, max_entries: int, platform: str | None) -> tuple[list[Entry], bool]:
    on_windows = (platform or sys.platform) == "win32"
    found: list[tuple[str, Entry]] = []
    try:
        with os.scandir(target) as scan:
            for item in scan:
                try:
                    if not item.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    # A snapshot of the level is fine; one unreadable entry must
                    # not cost the whole listing. A broken symlink lands here.
                    continue
                attributes: int | None = None
                if on_windows:
                    try:
                        attributes = item.stat(follow_symlinks=False).st_file_attributes
                    except OSError:
                        attributes = None
                found.append(
                    (
                        item.name.casefold(),
                        Entry(
                            name=item.name,
                            path=str(target / item.name),
                            hidden=_is_hidden(item.name, attributes),
                        ),
                    )
                )
    except OSError as exc:
        raise BrowserError(
            "unreadable", f"Cannot read that folder: {exc.strerror or exc}", str(target)
        ) from exc

    # Case-insensitive ordering, because that is the order the folders appear in
    # Explorer and Finder; a plain sort puts every capitalised name first.
    found.sort(key=lambda pair: (pair[0], pair[1].name))
    truncated = len(found) > max_entries
    return [entry for _, entry in found[:max_entries]], truncated


def listing(
    path: str | Path | None = None,
    *,
    max_entries: int = MAX_ENTRIES,
    platform: str | None = None,
    home: Path | None = None,
) -> Listing:
    """List one directory level, defaulting to the server account's home."""
    starting = home if home is not None else Path.home()
    raw = str(path) if path is not None else str(starting)
    if not fully_qualified(raw, platform=platform):
        raise BrowserError(
            "not-fully-qualified",
            "Give an absolute path, for example C:\\Projects or /home/me/projects.",
            raw,
        )

    target = Path(raw).expanduser()
    if not target.is_dir():
        raise BrowserError("unreadable", "That folder does not exist.", str(target))

    entries, truncated = _collect(target, max_entries=max(1, int(max_entries)), platform=platform)
    parent = target.parent
    return Listing(
        path=str(target),
        parent=None if parent == target else str(parent),
        crumbs=crumbs_for(target),
        entries=entries,
        roots=roots(platform=platform),
        home=str(starting),
        truncated=truncated,
    )


def validate_name(name: str, *, platform: str | None = None) -> str:
    """Check a proposed child directory name, returning it trimmed."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise BrowserError("bad-name", "Give the new folder a name.")
    if cleaned in {".", ".."}:
        raise BrowserError("bad-name", "That name is not available.")
    if "/" in cleaned or "\\" in cleaned:
        raise BrowserError("bad-name", "A folder name cannot contain a path separator.")
    if "\x00" in cleaned:
        raise BrowserError("bad-name", "That name contains an invalid character.")
    if (platform or sys.platform) == "win32":
        illegal = sorted(set(cleaned) & _WINDOWS_ILLEGAL)
        if illegal:
            raise BrowserError("bad-name", f"A folder name cannot contain {', '.join(illegal)}.")
        if cleaned.endswith("."):
            raise BrowserError("bad-name", "A Windows folder name cannot end with a full stop.")
    return cleaned


def create_directory(parent: str | Path, name: str, *, platform: str | None = None) -> Path:
    """Create one child directory under an existing parent.

    Deliberately non-recursive: a missing parent is a real failure rather than a
    level to invent, so the caller cannot create a tree by mistyping a path.
    """
    cleaned = validate_name(name, platform=platform)
    if not fully_qualified(parent, platform=platform):
        raise BrowserError("not-fully-qualified", "Give an absolute parent path.", str(parent))
    home = Path(str(parent)).expanduser()
    if not home.is_dir():
        raise BrowserError("unreadable", "That folder does not exist.", str(home))
    target = home / cleaned
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise BrowserError(
            "exists", "A folder with that name is already there.", str(target)
        ) from exc
    except OSError as exc:
        raise BrowserError(
            "create-failed", f"Cannot create that folder: {exc.strerror or exc}", str(target)
        ) from exc
    return target
