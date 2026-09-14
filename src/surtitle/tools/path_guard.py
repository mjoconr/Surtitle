"""Project-root confinement.

Every filesystem tool in this package resolves paths through :func:`resolve_in_root`.
Nothing else should build a path by hand: a single bypass is a full escape from
the user's chosen directory, and the agent is a language model that can be
steered by the contents of the documents it reads.

The guard defends against four distinct attacks:

1. **Traversal** — ``../../../etc/passwd``.
2. **Absolute escape** — ``/etc/passwd`` or ``C:\\Windows\\System32``.
3. **Symlink escape** — a link inside the project pointing outside it, or a
   project subdirectory that is itself a symlink out of the tree.
4. **Case and separator tricks on Windows** — ``..\\``, mixed separators, and
   drive-relative paths.

Resolution strategy: normalize separators and strip Windows extended-length
prefixes, join onto the resolved root, then resolve symlinks *before*
containment is tested, so the comparison is always between real paths.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PathEscapeError",
    "ResolvedPath",
    "is_probably_binary",
    "resolve_in_root",
]


class PathEscapeError(ValueError):
    """Raised when a requested path would leave the project root."""

    def __init__(self, requested: str) -> None:
        super().__init__(
            f"Refusing to access {requested!r}: it is outside the project directory. "
            "The agent may only read and write inside the project it is attached to."
        )
        self.requested = requested


@dataclass(slots=True, frozen=True)
class ResolvedPath:
    """A validated path inside the project root.

    ``relative`` is always POSIX-style so it can be shown to the model and used
    in messages identically on macOS and Windows.
    """

    absolute: Path
    relative: str
    root: Path

    def __str__(self) -> str:
        return self.relative


# Windows extended-length and device prefixes that bypass normal parsing.
_WINDOWS_PREFIX = re.compile(r"^\\\\[?.]\\")
_DRIVE = re.compile(r"^[A-Za-z]:")


def _normalise_separators(raw: str) -> str:
    """Convert backslashes to forward slashes for uniform handling."""
    return raw.replace("\\", "/")


def _reject_if_absolute(raw: str) -> None:
    """Reject paths that name a location rather than a position in the project.

    Rejection is deliberate rather than stripping the leading slash: silently
    rewriting ``/etc/passwd`` into ``<root>/etc/passwd`` would make the agent
    appear to succeed at reading a file it never read.
    """
    candidate = raw.strip()
    if not candidate:
        raise PathEscapeError(raw)

    normalised = _normalise_separators(candidate)
    if normalised.startswith("//"):
        raise PathEscapeError(raw)  # UNC share
    if normalised.startswith("/"):
        raise PathEscapeError(raw)
    if _DRIVE.match(normalised):
        raise PathEscapeError(raw)
    if _WINDOWS_PREFIX.match(candidate):
        raise PathEscapeError(raw)

    # A leading ~ would expand to the user's home, outside the project.
    if normalised.startswith("~"):
        raise PathEscapeError(raw)


def resolve_in_root(root: Path, requested: str | Path) -> ResolvedPath:
    """Resolve ``requested`` and prove it is inside ``root``.

    Returns a :class:`ResolvedPath`; raises :class:`PathEscapeError` otherwise.
    The target does not need to exist, so this works for writes as well as reads.
    """
    root_resolved = root.resolve()
    raw = str(requested)

    _reject_if_absolute(raw)
    normalised = _normalise_separators(raw)

    joined = root_resolved / normalised
    try:
        # resolve() collapses symlinks and ".."; strict=False allows new files.
        # Containment is judged on the *resolved* result, so a path that climbs
        # out and back in (``sub/../notes.txt``) is allowed, while one that ends
        # up outside is refused below.
        resolved = joined.resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loops
        raise PathEscapeError(raw) from exc

    if not _is_within(root_resolved, resolved):
        raise PathEscapeError(raw)

    relative = resolved.relative_to(root_resolved).as_posix()
    if relative == ".":
        relative = ""
    return ResolvedPath(absolute=resolved, relative=relative, root=root_resolved)


def _is_within(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` is ``root`` itself or below it.

    Compares resolved paths only. On Windows the comparison is case-insensitive
    because the filesystem is, so ``C:\\Proj`` and ``c:\\proj`` are the same
    directory and must not be treated as an escape.
    """
    if os.name == "nt":
        root_parts = [p.lower() for p in root.parts]
        candidate_parts = [p.lower() for p in candidate.parts]
    else:
        root_parts = list(root.parts)
        candidate_parts = list(candidate.parts)

    if len(candidate_parts) < len(root_parts):
        return False
    return candidate_parts[: len(root_parts)] == root_parts


def is_probably_binary(path: Path, *, sample_size: int = 8192) -> bool:
    """Heuristic binary sniff, so tools never dump bytes into the model context."""
    try:
        with path.open("rb") as handle:
            sample = handle.read(sample_size)
    except OSError:
        return True
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Decode as UTF-8 and look for a high proportion of control characters.
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    if not text:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t")
    return (printable / len(text)) < 0.85
