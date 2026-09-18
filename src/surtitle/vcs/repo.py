"""What version control says about a project, without changing anything.

The agent should not have to guess whether a folder is a working copy, what
branch it is on, or whether there is uncommitted work in it — and it should not
have to run three commands to find out. This reads that state through the tools
themselves, so the answer is the working copy's own, not an inference from
directory names.

Read-only by construction: every command here is a query. Staging, committing and
pushing live in the ``vcs_commit`` tool, behind an approval.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from surtitle.vcs import provision

__all__ = ["RepoState", "detect"]

_TIMEOUT = 20.0

Runner = Callable[[Sequence[str], Path], tuple[int, str]]


def _run(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    """Run a query command, returning ``(returncode, stdout)``.

    Errors are folded into the return code rather than raised: "is this a git
    checkout" is a question whose false answer is an exception from git, and a
    caller that had to catch it would be catching the normal case.
    """
    try:
        result = subprocess.run(
            [str(part) for part in argv],
            cwd=str(cwd),
            env=provision.child_env(),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return result.returncode, result.stdout or ""


@dataclass(slots=True)
class RepoState:
    """One project's version-control situation."""

    system: str = "none"  # "git" | "svn" | "none"
    root: str = ""
    branch: str = ""
    revision: str = ""
    changed: int = 0
    untracked: int = 0
    ahead: int = 0
    behind: int = 0
    remote: str = ""
    error: str = ""

    @property
    def dirty(self) -> bool:
        return bool(self.changed or self.untracked)

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "root": self.root,
            "branch": self.branch,
            "revision": self.revision,
            "changed": self.changed,
            "untracked": self.untracked,
            "ahead": self.ahead,
            "behind": self.behind,
            "remote": self.remote,
            "dirty": self.dirty,
            "error": self.error,
        }

    def describe(self) -> str:
        """One line for the prompt or a tool result."""
        if self.system == "none":
            return self.error or "This project is not under version control."
        where = f" on branch {self.branch}" if self.branch else ""
        revision = f" at r{self.revision}" if self.revision else ""
        bits: list[str] = []
        if self.changed:
            bits.append(f"{self.changed} changed")
        if self.untracked:
            bits.append(f"{self.untracked} untracked")
        if self.ahead:
            bits.append(f"{self.ahead} to push")
        if self.behind:
            bits.append(f"{self.behind} to pull")
        state = ", ".join(bits) if bits else "nothing uncommitted"
        remote = f", remote {self.remote}" if self.remote else ""
        return f"A {self.system} working copy{where}{revision}: {state}{remote}."


def _git(root: Path, run: Runner, state: RepoState) -> RepoState:
    code, top = run(["git", "rev-parse", "--show-toplevel"], root)
    if code != 0:
        return state
    state.system = "git"
    state.root = top.strip()

    code, branch_line = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    if code == 0:
        state.branch = branch_line.strip()
    code, revision = run(["git", "rev-parse", "--short", "HEAD"], root)
    if code == 0:
        state.revision = revision.strip()
    code, remote = run(["git", "config", "--get", "remote.origin.url"], root)
    if code == 0:
        state.remote = remote.strip()

    code, porcelain = run(["git", "status", "--porcelain=v1", "--branch"], root)
    if code == 0:
        for line in porcelain.splitlines():
            if line.startswith("##"):
                state.ahead, state.behind = _ahead_behind(line)
            elif line.startswith("??"):
                state.untracked += 1
            elif line.strip():
                state.changed += 1
    elif porcelain.strip():
        state.error = porcelain.strip().splitlines()[0]
    return state


def _ahead_behind(header: str) -> tuple[int, int]:
    """Read ``[ahead 1, behind 2]`` out of a ``git status --branch`` header."""
    if "[" not in header:
        return 0, 0
    inside = header[header.index("[") + 1 : header.rindex("]")]
    ahead = behind = 0
    for part in inside.split(","):
        part = part.strip()
        if part.startswith("ahead "):
            ahead = int(part.split()[1] or 0)
        elif part.startswith("behind "):
            behind = int(part.split()[1] or 0)
    return ahead, behind


def _svn(root: Path, run: Runner, state: RepoState) -> RepoState:
    code, info = run(["svn", "info"], root)
    if code != 0:
        if "E155007" in info or "not a working copy" in info.lower():
            return state
        state.system = "svn"
        state.error = (
            info.strip().splitlines()[0] if info.strip() else "svn could not read this folder."
        )
        return state
    state.system = "svn"
    for line in info.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "Working Copy Root Path":
            state.root = value
        elif key == "Relative URL":
            state.branch = value
        elif key == "Revision":
            state.revision = value
        elif key == "URL":
            state.remote = value
    code, status = run(["svn", "status"], root)
    if code == 0:
        for line in status.splitlines():
            if not line.strip():
                continue
            if line.startswith("?"):
                state.untracked += 1
            else:
                state.changed += 1
    return state


def detect(
    root: Path,
    *,
    run: Runner | None = None,
    settings: Any = None,
    check_svn: bool | None = None,
) -> RepoState:
    """Describe the working copy ``root`` sits in, if any.

    Git is asked first because a git repository inside an svn checkout is
    vanishingly rare and the reverse is not, so the order costs nothing and saves
    one subprocess in the common case. ``svn`` is only asked when it is actually
    available, since a missing binary produces a failure that looks like "not a
    working copy".
    """
    runner = run or _run
    state = RepoState()
    if provision.executable(settings, "git") is not None:
        state = _git(root, runner, state)
        if state.system == "git":
            return state
    wanted = check_svn
    if wanted is None:
        wanted = provision.executable(settings, "svn") is not None
    if wanted:
        state = _svn(root, runner, state)
    return state
