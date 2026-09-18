"""Making the commit, once the user has said yes.

Two decisions are enforced here rather than left to the caller's care:

* **Nothing is committed by accident.** An empty change is a refusal, not an
  empty commit, because an empty commit is the kind of thing that looks like
  success in a tool result and leaves a puzzling entry in somebody's history.
* **Surtitle's own state is never committed.** ``.surtitle/`` holds the project
  notebook, the isolated environment and uploads — the agent's working files, not
  the user's. It is excluded from the commit and reported as excluded, rather than
  quietly included or made into an error the agent has to interpret.

Everything else is the user's call: which changes, what the message says, and
whether to publish. The approval on the ``vcs_commit`` tool is where that is asked.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from surtitle.vcs import provision

__all__ = ["CommitResult", "commit", "state_dir_name"]

# Matches both separators so a Windows path in git's output is recognised.
_OWN_STATE = ".surtitle"
_TIMEOUT = 120.0

Runner = Callable[[Sequence[str], Path], tuple[int, str, str]]


def state_dir_name() -> str:
    """The per-project state directory, as it appears in version-control output."""
    return _OWN_STATE


def _run(argv: Sequence[str], cwd: Path) -> tuple[int, str, str]:
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
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout or "", result.stderr or ""


@dataclass(slots=True)
class CommitResult:
    """What happened, in terms the agent can report without inventing anything."""

    ok: bool
    system: str = ""
    revision: str = ""
    pushed: bool = False
    excluded: list[str] = field(default_factory=list)
    output: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "system": self.system,
            "revision": self.revision,
            "pushed": self.pushed,
            "excluded": list(self.excluded),
            "output": self.output,
            "error": self.error,
        }


def _tail(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _is_own_state(path: str) -> bool:
    """True when a version-control path is inside Surtitle's own state directory.

    ``lstrip("./")`` would be wrong here and was: it strips *characters*, not a
    prefix, so ``.surtitle/notes.md`` became ``surtitle/notes.md`` and never
    matched — the one path this exists to keep out of a commit was the one path
    it let through.
    """
    cleaned = path.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    parts = [part for part in cleaned.split("/") if part]
    return bool(parts) and parts[0] == _OWN_STATE


def _git_commit(
    root: Path,
    run: Runner,
    *,
    message: str,
    paths: list[str],
    include_all: bool,
    push: bool,
) -> CommitResult:
    result = CommitResult(ok=False, system="git")

    if include_all:
        code, _out, err = run(["git", "add", "-A"], root)
    elif paths:
        code, _out, err = run(["git", "add", "--", *paths], root)
    else:
        return CommitResult(
            ok=False,
            system="git",
            error="Say which paths to stage, or ask for everything.",
        )
    if code != 0:
        return CommitResult(ok=False, system="git", error=_tail(err) or "git add failed")

    # The project's own state is not the user's work. Unstage it before the
    # commit rather than leaving it in, and say so afterwards.
    code, staged, _err = run(["git", "diff", "--cached", "--name-only"], root)
    if code == 0:
        unwanted = [name for name in staged.splitlines() if name.strip() and _is_own_state(name)]
        if unwanted:
            run(["git", "restore", "--staged", "--", *unwanted], root)
            result.excluded = sorted(unwanted)

    code, staged, _err = run(["git", "diff", "--cached", "--name-only"], root)
    if code == 0 and not staged.strip():
        return CommitResult(
            ok=False,
            system="git",
            error="There is nothing to commit — the working tree matches the last commit.",
            excluded=result.excluded,
        )

    code, out, err = run(["git", "commit", "-m", message], root)
    if code != 0:
        return CommitResult(
            ok=False, system="git", error=_tail(err) or _tail(out) or "git commit failed"
        )
    result.ok = True
    result.output = _tail(f"{out}\n{err}")

    code, revision, _err = run(["git", "rev-parse", "--short", "HEAD"], root)
    if code == 0:
        result.revision = revision.strip()

    if push:
        code, out, err = run(["git", "push"], root)
        if code != 0:
            result.ok = False
            result.error = f"committed, but the push failed: {_tail(err) or _tail(out)}"
        else:
            result.pushed = True
            result.output += "\n" + _tail(f"{out}\n{err}")
    return result


def _svn_unversioned(root: Path, run: Runner) -> list[str]:
    """Every path svn does not track yet, which a commit would silently omit."""
    code, out, _err = run(["svn", "status"], root)
    if code != 0:
        return []
    found: list[str] = []
    for line in out.splitlines():
        if line.startswith("?") and line[1:].strip():
            found.append(line[1:].strip())
    return found


def _svn_commit(
    root: Path,
    run: Runner,
    *,
    message: str,
    paths: list[str],
    include_all: bool,
    push: bool,
) -> CommitResult:
    """``svn commit``: no staging area, and publishing is the commit itself."""
    targets = list(paths)
    excluded: list[str] = []
    if include_all and not targets:
        # "Everything" in svn means versioning new files first: a commit silently
        # omits anything unversioned, which is the classic way work looks saved
        # and is not. Surtitle's own state is left unversioned on purpose.
        for name in _svn_unversioned(root, run):
            if _is_own_state(name):
                excluded.append(name)
                continue
            run(["svn", "add", "--parents", name], root)

    code, out, err = run(["svn", "commit", "-m", message, *targets], root)
    if code != 0:
        text = _tail(err) or _tail(out)
        if "nothing to commit" in text.lower() or "E200009" in text:
            return CommitResult(
                ok=False,
                system="svn",
                error="There is nothing to commit in this working copy.",
                excluded=excluded,
            )
        return CommitResult(ok=False, system="svn", error=text or "svn commit failed")

    result = CommitResult(ok=True, system="svn", output=_tail(out), excluded=excluded)
    match = re.search(r"Committed revision (\d+)", out)
    if match:
        result.revision = match.group(1)
    if push:
        # There is no separate publish step in svn; saying so beats implying one
        # happened.
        result.output += "\n(Subversion publishes on commit; nothing further to push.)"
    return result


def commit(
    root: Path,
    *,
    system: str = "",
    message: str,
    paths: Sequence[str] | None = None,
    include_all: bool = True,
    push: bool = False,
    settings: Any = None,
    run: Runner | None = None,
) -> CommitResult:
    """Stage and commit ``root``, optionally publishing it.

    ``system`` selects the tool; when it is empty the choice follows what is
    installed, preferring git exactly as :mod:`surtitle.vcs.repo` does.
    """
    runner = run or _run
    if not (message or "").strip():
        return CommitResult(ok=False, error="A commit needs a message.")
    wanted = (system or "").strip().lower()
    if not wanted:
        wanted = "git" if provision.executable(settings, "git") is not None else "svn"
    if provision.executable(settings, wanted) is None:
        return CommitResult(
            ok=False,
            system=wanted,
            error=(
                f"{wanted} is not installed here. Install it from the tray "
                f"(or `surtitle tools install`) before committing."
            ),
        )

    chosen = list(paths or [])
    if wanted == "git":
        return _git_commit(
            root,
            runner,
            message=message.strip(),
            paths=chosen,
            include_all=include_all,
            push=push,
        )
    return _svn_commit(
        root,
        runner,
        message=message.strip(),
        paths=chosen,
        include_all=include_all,
        push=push,
    )
