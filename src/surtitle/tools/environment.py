"""Per-project Python environments, so the agent can acquire capability on demand.

The agent's real power is that it can write code for *anything*. That only works
if it can also obtain the libraries that code needs. Letting it install into the
application's own environment would be the wrong answer twice over: it would
corrupt the shipped runtime, and on Windows it would need admin rights to write
there at all.

So each project gets its own virtual environment under
``<project>/.surtitle/venv``. The agent installs into that, ``run_python``
executes against it, and uninstalling a project is deleting one directory.

Approval model (chosen deliberately): installing a package is arbitrary
third-party code execution, because a package's build backend runs at install
time. Therefore every install is approval-gated, but an approved *set* of
requirements is remembered per project and skipped next time — the same way a
lockfile records a decision once instead of prompting forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from surtitle.tools.project_config import (
    ProjectConfig,
    load_project_config,
    save_project_config,
)

__all__ = [
    "ENV_DIR_NAME",
    "NOTES_FILENAME",
    "EnvironmentError_",
    "EnvironmentStatus",
    "PackageSearchResult",
    "environment_summary",
    "install_packages",
    "list_installed",
    "project_env_python",
    "project_env_status",
    "project_notes",
    "read_notes",
    "search_pypi",
    "write_notes",
]

log = logging.getLogger(__name__)

# Everything environment-related lives under one hidden directory in the project.
ENV_DIR_NAME = ".surtitle"
VENV_DIR_NAME = "venv"
REQUIREMENTS_NAME = "requirements.txt"

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
PYPI_SEARCH_URL = "https://pypi.org/search/"
_INSTALL_TIMEOUT = 600.0

# A requirement specifier we are willing to pass to a package manager. Anything
# that is not a plain distribution name (with an optional version specifier or
# extras) is refused, because a leading "-" turns into a command-line flag and
# requirements files can also carry VCS URLs and direct paths.
_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"  # distribution name
    r"(?:\[[A-Za-z0-9._,-]+\])?"  # optional extras
    r"(?:\s*(?:==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9._*+!-]+(?:\s*,\s*(?:==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9._*+!-]+)*)?"
    r"$"
)


class EnvironmentError_(RuntimeError):
    """Raised when a project environment cannot be created or used."""


@dataclass(slots=True)
class EnvironmentStatus:
    """What the agent's execution environment looks like right now."""

    exists: bool
    path: Path
    python: Path | None
    package_count: int
    approved: list[str]
    installed: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "isolated": self.exists,
            "path": str(self.path),
            "package_count": self.package_count,
            "approved": self.approved,
            "installed": self.installed,
        }


@dataclass(slots=True)
class PackageSearchResult:
    """One package found on PyPI."""

    name: str
    version: str
    summary: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "url": self.url,
        }


def env_dir(root: Path) -> Path:
    """The environment directory for a project."""
    return root / ENV_DIR_NAME


def venv_dir(root: Path) -> Path:
    """The virtual environment directory for a project."""
    return env_dir(root) / VENV_DIR_NAME


def project_env_python(root: Path) -> Path | None:
    """The project interpreter, or ``None`` when no environment exists yet.

    Several names are probed on purpose: ``python -m venv`` creates ``bin/python3``
    on most Unix systems while some tools create ``bin/python``, and checking
    only one of them silently falls back to the application interpreter.
    """
    base = venv_dir(root)
    if os.name == "nt":
        candidates = (base / "Scripts" / "python.exe",)
    else:
        candidates = (base / "bin" / "python", base / "bin" / "python3")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def requirements_path(root: Path) -> Path:
    """Where the recorded, approved requirements live."""
    return env_dir(root) / REQUIREMENTS_NAME


def _read_requirements(path: Path) -> list[str]:
    """Read a requirements file, ignoring comments and blank lines."""
    if not path.is_file():
        return []
    requirements: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            requirements.append(stripped)
    return requirements


def _write_requirements(path: Path, requirements: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# Packages approved for this project by Surtitle.\n"
    body += "# Every entry here has been explicitly approved by the user.\n"
    body += "".join(f"{req}\n" for req in requirements)
    path.write_text(body, encoding="utf-8")


def validate_requirements(requirements: list[str]) -> tuple[list[str], list[str]]:
    """Split requested requirements into (valid, refused) with reasons.

    Refusing anything that is not a plain distribution name is what stops a
    crafted "requirement" from becoming a package-manager flag such as
    ``--index-url`` or a direct URL install from an untrusted host.
    """
    valid: list[str] = []
    refused: list[str] = []
    for raw in requirements:
        candidate = str(raw).strip()
        if not candidate or candidate.startswith("-") or not _REQUIREMENT.match(candidate):
            refused.append(
                f"{raw!r} is not a plain package requirement. Use a name like "
                "'requests' or 'pandas>=2.2'. Flags, URLs and file paths are not accepted."
            )
            continue
        valid.append(candidate)
    return valid, refused


def approved_requirements(root: Path) -> list[str]:
    """Requirements already approved for this project."""
    return _read_requirements(requirements_path(root))


def remember_requirements(root: Path, requirements: list[str]) -> list[str]:
    """Record newly approved requirements in the project's requirements file."""
    existing = _read_requirements(requirements_path(root))
    seen = {_canonical(req) for req in existing}
    for requirement in requirements:
        key = _canonical(requirement)
        if key not in seen:
            existing.append(requirement)
            seen.add(key)
    _write_requirements(requirements_path(root), existing)

    # Mirror into the project config so the decision travels with the repo.
    with contextlib.suppress(Exception):
        config = load_project_config(root)
        config.requirements = sorted(existing)
        save_project_config(root, config)

    return existing


def _canonical(requirement: str) -> str:
    """Normalise a requirement for set comparison (name plus specifier)."""
    return re.sub(r"\s+", "", requirement).lower()


def new_requirements(root: Path, requirements: list[str]) -> list[str]:
    """Which of ``requirements`` have not been approved for this project yet.

    Approval is per exact requirement, so a new package always asks but an
    already-approved one does not.
    """
    approved = {_canonical(req) for req in approved_requirements(root)}
    return [req for req in requirements if _canonical(req) not in approved]


async def ensure_venv(root: Path, *, timeout: float = 180.0) -> Path:
    """Create the project environment if it does not exist, returning its python.

    Uses the standard library's ``venv`` rather than depending on ``uv`` being
    installed: the application already ships a working interpreter, and this
    keeps the Windows release free of an extra external tool.
    """
    existing = project_env_python(root)
    if existing is not None:
        return existing

    target = venv_dir(root)
    target.parent.mkdir(parents=True, exist_ok=True)

    argv = [sys.executable, "-m", "venv", str(target)]
    code, stdout, stderr = await _run(argv, cwd=root, timeout=timeout)

    if code != 0 or project_env_python(root) is None:
        detail = (stderr or stdout or "").strip().splitlines()
        raise EnvironmentError_(
            "Could not create a project environment. "
            + (detail[-1] if detail else f"`python -m venv` exited with {code}.")
        )
    log.info("created project environment at %s", target)
    return project_env_python(root)  # type: ignore[return-value]


async def install_packages(
    root: Path,
    requirements: list[str],
    *,
    timeout: float = _INSTALL_TIMEOUT,
    upgrade: bool = False,
) -> tuple[bool, str, list[str]]:
    """Install requirements into the project environment.

    Returns ``(ok, output, installed_names)``. ``uv`` is used for installation
    when available because it is markedly faster and gives cleaner errors, with
    a fallback to the environment's own pip so the feature still works without it.
    """
    valid, refused = validate_requirements(requirements)
    if refused:
        return False, " ".join(refused), []
    if not valid:
        return False, "No package names were supplied.", []

    try:
        python = await ensure_venv(root, timeout=min(timeout, 300.0))
    except EnvironmentError_ as exc:
        return False, str(exc), []

    uv = shutil.which("uv")
    attempts: list[list[str]] = []
    if uv:
        attempts.append([uv, "pip", "install", "--python", str(python)])
    # Fallback: some environments have no uv on PATH.
    attempts.append([str(python), "-m", "pip", "install", "--disable-pip-version-check"])
    if upgrade:
        for attempt in attempts:
            attempt.append("--upgrade")

    last_output = ""
    for attempt in attempts:
        argv = [*attempt, *valid]
        code, stdout, stderr = await _run(argv, cwd=root, timeout=timeout)
        last_output = (stdout + "\n" + stderr).strip()

        if code == 0:
            remember_requirements(root, valid)
            installed = _names_from_requirements(valid)
            return True, _tail(last_output), installed

        if "No module named pip" in last_output or ("pip" in last_output and not uv):
            continue

    # Report the most useful line rather than the whole log.
    lines = [
        line
        for line in last_output.splitlines()
        if line.strip() and not line.startswith("Requirement already satisfied")
    ]
    detail = lines[-1] if lines else "the installer produced no output"
    return False, f"Installation failed: {detail}", []


def _names_from_requirements(requirements: list[str]) -> list[str]:
    """Extract bare distribution names from requirement strings."""
    names = []
    for requirement in requirements:
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if match:
            names.append(match.group(1))
    return names


async def list_installed(root: Path, *, timeout: float = 60.0) -> list[str]:
    """List distributions installed in the project environment."""
    python = project_env_python(root)
    if python is None:
        return []
    code, stdout, _stderr = await _run(
        [str(python), "-m", "pip", "list", "--format", "json", "--disable-pip-version-check"],
        cwd=root,
        timeout=timeout,
    )
    if code != 0:
        return []
    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(stdout or "[]")
        if isinstance(payload, list):
            return sorted(
                str(entry.get("name"))
                for entry in payload
                if isinstance(entry, dict) and entry.get("name")
            )
    return []


def environment_summary(root: Path) -> dict[str, object]:
    """Cheap, synchronous environment facts for the session handshake.

    Deliberately avoids ``pip list``: this runs while a socket is connecting, and
    shelling out would add seconds to every session start. Counts are read from
    the environment directory instead.
    """
    python = project_env_python(root)
    if python is None:
        return {"isolated": False, "approved": approved_requirements(root), "package_count": 0}

    packages = 0
    site_packages = venv_dir(root) / ("Lib" if os.name == "nt" else "lib")
    with contextlib.suppress(OSError):
        for candidate in site_packages.rglob("site-packages"):
            packages += sum(
                1
                for entry in candidate.iterdir()
                if entry.is_dir() and entry.suffix == ".dist-info"
            )
    return {
        "isolated": True,
        "approved": approved_requirements(root),
        "package_count": packages,
    }


# A notebook the agent keeps about the project, and that is injected into its
# system prompt on every turn. Without somewhere durable to write, every session
# starts from zero and knowledge like "this machine is down" cannot accumulate.
NOTES_FILENAME = "notes.md"
NOTES_MAX_CHARS = 8000


def notes_path(root: Path) -> Path:
    """Where the project notebook lives."""
    return env_dir(root) / NOTES_FILENAME


def read_notes(root: Path) -> str:
    """Current notebook contents, or an empty string."""
    path = notes_path(root)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def project_notes(root: Path) -> str:
    """The notebook, capped so it cannot crowd out the conversation.

    Truncation keeps the *newest* content, because a notebook grows by appending
    and the most recent findings are the ones most likely to matter.
    """
    text = read_notes(root)
    if len(text) <= NOTES_MAX_CHARS:
        return text
    return "... [earlier notes elided]\n" + text[-NOTES_MAX_CHARS:]


def write_notes(root: Path, text: str, *, append: bool = True) -> tuple[bool, str]:
    """Write or append to the notebook.

    Returns ``(ok, message)``. Appending is the default because the value of a
    notebook is accumulation; replacing it is a deliberate act.
    """
    path = notes_path(root)
    cleaned = (text or "").strip()
    if not cleaned:
        return False, "There is nothing to record."
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if append and path.is_file():
            existing = path.read_text(encoding="utf-8", errors="replace").rstrip()
            payload = f"{existing}\n{cleaned}\n" if existing else f"{cleaned}\n"
        else:
            payload = f"{cleaned}\n"
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        return False, f"Could not write the project notebook: {exc}"
    return True, ""


async def project_env_status(root: Path) -> EnvironmentStatus:
    """Describe the project environment for the UI and the model."""
    python = project_env_python(root)
    installed = await list_installed(root) if python is not None else []
    return EnvironmentStatus(
        exists=python is not None,
        path=venv_dir(root),
        python=python,
        package_count=len(installed),
        approved=approved_requirements(root),
        installed=installed[:200],
    )


async def search_pypi(
    query: str, *, limit: int = 8, timeout: float = 20.0
) -> list[PackageSearchResult]:
    """Search PyPI for packages matching ``query``.

    PyPI's JSON API has no search endpoint, so a bare name is looked up exactly
    and anything else is resolved through the search page. Both paths are
    best-effort: an unreachable index returns no results rather than an error,
    because the agent can still try an install directly.
    """
    cleaned = query.strip()
    if not cleaned:
        return []

    results: list[PackageSearchResult] = []

    # An exact name is by far the most common case and is cheap and reliable.
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(PYPI_JSON_URL.format(name=cleaned))
            if response.status_code == 200:
                payload = response.json()
                info = payload.get("info") or {}
                name = str(info.get("name") or cleaned)
                results.append(
                    PackageSearchResult(
                        name=name,
                        version=str(info.get("version") or ""),
                        summary=str(info.get("summary") or ""),
                        url=str(info.get("project_url") or f"https://pypi.org/project/{name}/"),
                    )
                )
                return results

    # Otherwise fall back to the HTML search page.
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(PYPI_SEARCH_URL, params={"q": cleaned})
            if response.status_code == 200:
                results.extend(_parse_search_page(response.text, limit))

    return results[:limit]


def _parse_search_page(html: str, limit: int) -> list[PackageSearchResult]:
    """Extract package names from PyPI's search results page.

    Deliberately tolerant: if the markup changes the regex simply matches less,
    and the caller falls back to attempting the install by name.
    """
    seen: set[str] = set()
    results: list[PackageSearchResult] = []
    for match in re.finditer(r'<a[^>]+class="package-snippet"[^>]*href="([^"]+)"', html):
        href = match.group(1)
        parts = [part for part in href.strip("/").split("/") if part]
        if len(parts) < 2 or parts[0] != "project":
            continue
        name = parts[1]
        if name in seen:
            continue
        seen.add(name)
        results.append(
            PackageSearchResult(
                name=name,
                version="",
                summary="",
                url=f"https://pypi.org/project/{name}/",
            )
        )
        if len(results) >= limit:
            break
    return results


async def _run(argv: list[str], *, cwd: Path, timeout: float) -> tuple[int, str, str]:
    """Run a subprocess, killing its whole process group on timeout."""
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000200
    else:
        kwargs["start_new_session"] = True

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            **kwargs,
        )
    except FileNotFoundError:
        return 127, "", f"Command not found: {argv[0]}"
    except OSError as exc:
        return 1, "", f"Could not start {argv[0]}: {exc}"

    communicate = asyncio.create_task(process.communicate())
    done, _pending = await asyncio.wait({communicate}, timeout=timeout)

    if not done:
        _kill_tree(process)
        with contextlib.suppress(Exception):
            await asyncio.wait({communicate}, timeout=5)
        if not communicate.done():
            communicate.cancel()
        return 124, "", f"Timed out after {timeout:.0f}s"

    stdout_bytes, stderr_bytes = communicate.result()
    return (
        int(process.returncode or 0),
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a process and its children."""
    if process.returncode is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import subprocess

            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), 15)
    with contextlib.suppress(Exception):
        process.terminate()
    for _ in range(50):
        if process.returncode is not None:
            return
        time.sleep(0.1)
    with contextlib.suppress(Exception):
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), 9)
        process.kill()


def _tail(text: str, limit: int = 2000) -> str:
    """Keep the end of a long log, where the outcome is stated."""
    if len(text) <= limit:
        return text
    return f"... [{len(text) - limit} characters omitted]\n{text[-limit:]}"


def load_config(root: Path) -> ProjectConfig:
    """Convenience wrapper so callers do not import project_config directly."""
    return load_project_config(root)
