"""Skills: procedures the project teaches the agent, read when they are wanted.

A skill is a directory with a ``SKILL.md`` in it — one per procedure — living in the
project's ``skills/`` or in the user's own ``$SURTITLE_HOME/skills/``. The agent is
told each skill's **name and one line about it** in the notes it gets every turn, and
calls the ``skill`` tool to read the whole thing when the job in front of it matches.

That split is the point. A project's conventions have to reach the agent, and the
alternatives are worse: putting every procedure in the system prompt spends the
context of every turn on the ones that are not being used, and *naming* them without
a way to read them leaves the agent guessing at what it cannot see. So the catalogue
is short, and the body is one call away.

Two rules:

* **A name is a name.** It is checked against a pattern rather than being treated as
  a path, and the directory it resolves to is then proved to be inside the skills
  root. A skill called ``../../.ssh`` is not a skill.
* **Project first, then personal.** A project can pin its own version of a procedure
  without the user's copy shadowing it, which is the direction that surprises nobody.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from surtitle.tools.path_guard import PathEscapeError, resolve_in_root

__all__ = ["Skill", "catalogue", "discover", "load_body", "personal_dir", "read_skill"]

# What a skill is called. Kept strict because the name is used to build a path.
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.IGNORECASE)
# A skill is instructions, and instructions that do not fit are instructions the
# agent will skim. Cut, and say it was cut — the file is on disk and readable.
_MAX_BODY_CHARS = 20_000
_SKILL_FILE = "SKILL.md"


@dataclass(slots=True)
class Skill:
    """One procedure, as discovered — before its body is read."""

    name: str
    description: str
    path: Path
    source: str  # "project" or "personal"

    @property
    def directory(self) -> Path:
        return self.path.parent


def _parse(text: str, fallback_name: str) -> tuple[str, str]:
    """``(name, description)`` from a skill's front matter, with fallbacks.

    Deliberately not a YAML parser: the fields that matter are two strings, and a
    project should not need a dependency to describe one. A file with no front
    matter still works — the directory names it and its first real line describes
    it, which is what somebody writing one quickly will produce.
    """
    name = fallback_name
    description = ""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, _, value = line.partition(":")
            if key.strip().lower() == "name" and value.strip():
                name = value.strip().strip("\"'")
            elif key.strip().lower() == "description" and value.strip():
                description = value.strip().strip("\"'")
    if not description:
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped == "---" or stripped.startswith("#"):
                continue
            if stripped.startswith(("name:", "description:")):
                continue
            description = stripped[:200]
            break
    return name, description


def discover(root: Path, *, personal_dir: Path | None = None) -> list[Skill]:
    """Every skill this conversation can load, project first.

    A project skill and a personal one with the same name: the project's wins. It is
    the more specific statement of how work is done *here*, and a personal copy that
    quietly overrode it would be a mystery to everybody but the person who wrote it.
    """
    found: dict[str, Skill] = {}
    for source, base in (("personal", personal_dir), ("project", root / "skills")):
        if base is None or not base.is_dir():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:  # pragma: no cover - an unreadable directory is not fatal
            continue
        for entry in entries:
            if not entry.is_dir() or not _NAME.match(entry.name):
                continue
            # Confinement goes through the same guard as every other project path,
            # so a symlinked skill directory pointing out of the project is refused
            # rather than read. The name is already pattern-checked; this is the
            # second, independent check that the rule asks for.
            try:
                manifest = resolve_in_root(base, f"{entry.name}/{_SKILL_FILE}").absolute
            except PathEscapeError:
                continue
            if not manifest.is_file():
                continue
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - unreadable file
                continue
            name, description = _parse(text, entry.name)
            # An invalid name is not a skill: it cannot be asked for, and listing it
            # would be listing something the agent can never load.
            if not _NAME.match(name):
                continue
            found[name] = Skill(
                name=name,
                description=description or "(no description)",
                path=manifest,
                source=source,
            )
    return list(found.values())


def personal_dir(ctx: Any) -> Path | None:
    """Where the *user's* own skills live, if this context knows the settings."""
    settings = getattr(ctx, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    return Path(data_dir) / "skills" if data_dir is not None else None


def catalogue(skills: list[Skill]) -> str:
    """The section naming every skill, for the notes the agent gets each turn."""
    if not skills:
        return ""
    lines = [f"- `{skill.name}` — {skill.description}" for skill in skills]
    return (
        "## Skills this project teaches\n"
        "Procedures written down for you, each one a file you can read in full. They "
        "are not loaded automatically: when a task matches one, read it with the "
        "`skill` tool before you start, and follow it rather than improvising.\n\n"
        + "\n".join(lines)
    )


def read_skill(
    name: str, root: Path, *, personal_dir: Path | None = None, limit: int = _MAX_BODY_CHARS
) -> tuple[Skill, str, list[str]] | None:
    """The skill called ``name``, its body, and the files beside it.

    ``None`` when there is no such skill. The body is the file without its front
    matter, cut to ``limit`` — and the caller says when it was cut, because a skill
    silently halved reads as the whole procedure.
    """
    wanted = (name or "").strip()
    if not _NAME.match(wanted):
        return None
    skill = next(
        (item for item in discover(root, personal_dir=personal_dir) if item.name == wanted), None
    )
    if skill is None:
        return None

    companions = sorted(
        item.relative_to(skill.directory).as_posix()
        for item in skill.directory.rglob("*")
        if item.is_file() and item.name != _SKILL_FILE
    )
    return skill, load_body(skill, limit=limit), companions[:40]


def load_body(skill: Skill, *, limit: int = _MAX_BODY_CHARS) -> str:
    """The instructions themselves: the file without its front matter.

    Read from the skill's own path rather than by looking its name up again — a
    project skill and a personal one can share a name, and asking for "the skill
    called x" a second time is how the wrong one gets returned.
    """
    try:
        text = skill.path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - it was readable a moment ago
        return ""
    body = _strip_front_matter(text).strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n\n[the rest of this skill was not loaded]"
    return body


def _strip_front_matter(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[index + 1 :])
    return text
