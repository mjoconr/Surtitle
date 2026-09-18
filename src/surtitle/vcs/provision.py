"""Portable git and svn, fetched into the app data directory on demand.

Surtitle's agent works on the user's files, and version control is how a change
becomes recoverable. A fresh Windows machine has neither ``git`` nor ``svn`` on
``PATH``, and installing either normally needs an administrator — which is the
dead end this module removes. Both are unpacked into the app's own data directory
(``%LOCALAPPDATA%\\Surtitle\\tools``), need no installer and no elevation, and are
never written into a project the user can see.

**Windows only, deliberately.** Portable Windows builds of both tools are
published by their maintainers; on macOS and Linux a package manager is the right
answer and a private copy in the data directory would fight the system one. On
those platforms this module locates what is already installed and says so —
:func:`status` reports "not installed, install it with your package manager"
rather than silently offering a download it cannot honour.

Everything here is verified twice: the archive must match the SHA-256 pinned in
:data:`CATALOG`, and the extracted binary must run and report the version it is
supposed to be. A download that is merely corrupt is common; a download that
unpacks into something that runs is the thing worth checking.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from surtitle.config import Settings, get_settings
from surtitle.jobs import BackgroundJob

__all__ = [
    "CATALOG",
    "TOOLS",
    "InstallJob",
    "InstallResult",
    "PortableTool",
    "ToolLocation",
    "ToolStatus",
    "activate",
    "child_env",
    "executable",
    "install",
    "locate",
    "path_entries",
    "status",
    "tools_dir",
]

TOOLS = ("git", "svn")


@dataclass(frozen=True, slots=True)
class PortableTool:
    """One downloadable build, pinned to a version and a digest."""

    name: str
    version: str
    url: str
    sha256: str
    size: int
    # Path of the program inside the archive, POSIX-joined.
    executable: str
    # Directories inside the archive that have to be on PATH for it to work.
    path_entries: tuple[str, ...]
    # How to make it state its version, and what that output must contain.
    version_args: tuple[str, ...]
    version_marker: str
    homepage: str


# Pinned by hand, because a "latest" URL would make a download unpredictable and
# a checksum impossible. Updating a version means updating all four facts.
#
# MinGit rather than PortableGit: it is the smaller build that still includes the
# whole command-line client, and `cmd/git.exe` is the wrapper that sets up
# GIT_EXEC_PATH for the rest of the tree. It carries no installer.
CATALOG: dict[str, PortableTool] = {
    "git": PortableTool(
        name="git",
        version="2.51.0",
        url=(
            "https://github.com/git-for-windows/git/releases/download/"
            "v2.51.0.windows.1/MinGit-2.51.0-64-bit.zip"
        ),
        # Published by the release itself, as the asset's sha256 digest.
        sha256="c2c955a21fa99889d83f485f24fa5d9a38fffc2d509d4022385510e11c26b250",
        size=41_268_410,
        executable="cmd/git.exe",
        path_entries=("cmd",),
        version_args=("--version",),
        version_marker="2.51.0",
        homepage="https://gitforwindows.org/",
    ),
    "svn": PortableTool(
        name="svn",
        version="1.14.5",
        url="https://www.visualsvn.com/files/Apache-Subversion-1.14.5.zip",
        # VisualSVN publishes no checksum file, so this was computed from the
        # archive itself and belongs here as the record of what was verified.
        sha256="23d393431b0aeec67490669b7d3b4a1a83332fdc184055fef48ea9123eaf7e0a",
        size=4_227_878,
        executable="bin/svn.exe",
        path_entries=("bin",),
        version_args=("--version", "--quiet"),
        version_marker="1.14.5",
        homepage="https://subversion.apache.org/",
    ),
}


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def tools_dir(settings: Settings) -> Path:
    """Where portable tools live: beside the models, outside any project."""
    return settings.data_dir / "tools"


def _portable_root(settings: Settings, name: str) -> Path:
    return tools_dir(settings) / name


def path_entries(settings: Settings | None = None) -> list[str]:
    """Directories to prepend to ``PATH`` so the portable tools are found.

    Only tools that are actually unpacked contribute: a stale entry pointing at a
    half-removed tree would shadow a working system install.
    """
    settings = settings or get_settings()
    entries: list[str] = []
    for name, tool in CATALOG.items():
        root = _portable_root(settings, name)
        if not (root / tool.executable).is_file():
            continue
        entries.extend(str(root / part) for part in tool.path_entries)
    return entries


def activate(settings: Settings | None = None) -> list[str]:
    """Put the portable tools on this process's ``PATH``. Idempotent.

    Done once for the whole process rather than per subprocess, because the agent
    reaches the filesystem through several paths — ``run_shell``, ``run_python``,
    a project's own environment — and a tool that only works in one of them is
    worse than one that is plainly missing.
    """
    entries = path_entries(settings)
    if not entries:
        return []
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    added = [entry for entry in entries if entry not in parts]
    if added:
        os.environ["PATH"] = os.pathsep.join([*added, *parts])
    return added


def child_env(
    settings: Settings | None = None, base: dict[str, str] | None = None
) -> dict[str, str]:
    """An environment for a subprocess that must find the portable tools.

    Used by every subprocess this app starts on the agent's behalf, so a tool
    installed from the tray works in all of them — and so it works even before
    :func:`activate` has run in this process.
    """
    env = dict(base if base is not None else os.environ)
    entries = path_entries(settings)
    if entries:
        current = env.get("PATH", "")
        parts = current.split(os.pathsep) if current else []
        env["PATH"] = os.pathsep.join([*entries, *parts])
    return env


@dataclass(slots=True)
class ToolLocation:
    """Where a tool was found, and how."""

    name: str
    path: str
    source: str  # "portable" | "system"
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "source": self.source,
            "version": self.version,
        }


def executable(settings: Settings | None = None, name: str = "git") -> Path | None:
    """The program to run for ``name``: the portable copy first, then ``PATH``.

    Portable first on purpose. If the user installed the tools from the tray, that
    is the copy the app promised to use, and it is the one whose version the
    update notices and the guide refer to.
    """
    settings = settings or get_settings()
    tool = CATALOG.get(name)
    if tool is not None:
        candidate = _portable_root(settings, name) / tool.executable
        if candidate.is_file():
            return candidate
    found = shutil.which(_exe(name)) or shutil.which(name)
    return Path(found) if found else None


def _reported_version(found: Path, name: str, *, timeout: float = 30.0) -> str:
    tool = CATALOG.get(name)
    args = list(tool.version_args) if tool else ["--version"]
    try:
        result = subprocess.run(
            [str(found), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (result.stdout or "") + (result.stderr or "")
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def locate(
    name: str = "git", settings: Settings | None = None, *, verify: bool = True
) -> ToolLocation | None:
    """Find ``name`` and report which copy it is.

    ``verify`` runs the program to read its version. That is a subprocess, so the
    callers that ask on every agent turn pass ``verify=False`` and take the pinned
    version for a portable copy instead; the ones a user is looking at — the tray,
    ``surtitle tools status`` — ask for the real answer.
    """
    settings = settings or get_settings()
    found = executable(settings, name)
    if found is None:
        return None
    tool = CATALOG.get(name)
    portable = tool is not None and found == _portable_root(settings, name) / tool.executable
    version = ""
    if verify:
        version = _reported_version(found, name)
    elif portable:
        version = tool.version
    return ToolLocation(
        name=name,
        path=str(found),
        source="portable" if portable else "system",
        version=version,
    )


@dataclass(slots=True)
class ToolStatus:
    """One row of ``surtitle tools status`` and the tray's menu."""

    name: str
    available: bool
    source: str = ""
    path: str = ""
    version: str = ""
    # What to do about it, when it is missing or could be better.
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "source": self.source,
            "path": self.path,
            "version": self.version,
            "hint": self.hint,
        }


def status(
    settings: Settings | None = None,
    *,
    platform: str | None = None,
    verify: bool = True,
) -> list[ToolStatus]:
    """What is available, and what the user could do about what is not."""
    settings = settings or get_settings()
    current = platform or sys.platform
    rows: list[ToolStatus] = []
    for name in TOOLS:
        location = locate(name, settings, verify=verify)
        if location is not None:
            rows.append(
                ToolStatus(
                    name=name,
                    available=True,
                    source=location.source,
                    path=location.path,
                    version=location.version,
                )
            )
            continue
        if current == "win32":
            hint = "Not installed. Download it from the tray, or run `surtitle tools install`."
        else:
            hint = (
                f"Not installed. Install {name} with your package manager "
                f"(macOS: `brew install {name}`, Debian/Ubuntu: `sudo apt install {name}`)."
            )
        rows.append(ToolStatus(name=name, available=False, hint=hint))
    return rows


@dataclass(slots=True)
class InstallResult:
    """What an install did, tool by tool."""

    installed: list[str]
    skipped: list[str]
    failed: list[str]
    detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": list(self.installed),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "detail": self.detail,
        }


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, expected: int, report: Callable[[float], None]) -> None:
    """Stream ``url`` to ``destination``, reporting how much has arrived."""
    import httpx

    destination.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or expected or 0)
        seen = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
                seen += len(chunk)
                if total:
                    report(min(99.0, seen * 100.0 / total))


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Every member, refusing one that would escape the destination.

    ``ZipFile.extractall`` already normalises these away in current Python, but a
    silent normalisation would turn a malicious archive into a *partial* tree
    rather than an error, and this is a download that gets unpacked and then run.
    """
    members: list[zipfile.ZipInfo] = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        parts = [part for part in name.split("/") if part not in {"", "."}]
        if name.startswith("/") or ".." in parts or (len(name) > 1 and name[1] == ":"):
            raise ValueError(f"the archive contains an unsafe path: {info.filename}")
        members.append(info)
    return members


def _unpack(archive: Path, destination: Path) -> None:
    staging = destination.with_name(destination.name + ".part")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging, members=_safe_members(bundle))
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def install(
    settings: Settings | None = None,
    names: Iterable[str] = TOOLS,
    *,
    progress: Callable[[float, str], None] | None = None,
    platform: str | None = None,
) -> InstallResult:
    """Download, verify and unpack the portable tools.

    A tool already present is skipped rather than re-fetched, so this is safe to
    run again after a partial failure.
    """
    settings = settings or get_settings()
    current = platform or sys.platform
    report = progress or (lambda _percent, _message: None)
    wanted = list(names)

    if current != "win32":
        return InstallResult(
            installed=[],
            skipped=[],
            failed=[],
            detail=(
                "Portable copies are only published for Windows. On this machine, "
                "install git and svn with the system package manager."
            ),
        )

    installed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    downloads = tools_dir(settings) / "downloads"

    for index, name in enumerate(wanted):
        tool = CATALOG.get(name)
        if tool is None:
            failed.append(name)
            continue
        target = _portable_root(settings, name)
        if (target / tool.executable).is_file():
            skipped.append(name)
            continue

        base = index * 100.0 / max(1, len(wanted))
        span = 100.0 / max(1, len(wanted))
        report(base, f"Downloading {name} {tool.version}…")

        archive = downloads / f"{name}-{tool.version}.zip"
        try:
            _download(
                tool.url,
                archive,
                tool.size,
                lambda percent, base=base, span=span, name=name: report(
                    base + percent * span / 100.0, f"Downloading {name}… {percent:.0f}%"
                ),
            )
            digest = _sha256_of(archive)
            if digest != tool.sha256:
                raise ValueError(
                    f"checksum mismatch for {name}: expected "
                    f"{tool.sha256[:12]}…, got {digest[:12]}…"
                )
            report(base + span * 0.8, f"Unpacking {name}…")
            _unpack(archive, target)
            program = target / tool.executable
            if not program.is_file():
                raise ValueError(f"{name} did not unpack to {tool.executable}")
            reported = _reported_version(program, name)
            if tool.version_marker not in reported:
                raise ValueError(f"{name} does not run: {reported or 'no version output'}")
        except Exception as exc:  # noqa: BLE001 - a failed tool is reported, not raised
            failed.append(name)
            report(base + span, f"{name} failed: {exc}")
            shutil.rmtree(target, ignore_errors=True)
            continue
        finally:
            archive.unlink(missing_ok=True)

        installed.append(name)
        report(base + span, f"{name} {tool.version} installed")

    activate(settings)
    detail = _summarise(installed, skipped, failed)
    return InstallResult(installed=installed, skipped=skipped, failed=failed, detail=detail)


def _summarise(installed: list[str], skipped: list[str], failed: list[str]) -> str:
    parts: list[str] = []
    if installed:
        parts.append(f"installed {', '.join(installed)}")
    if skipped:
        parts.append(f"already present: {', '.join(skipped)}")
    if failed:
        parts.append(f"failed: {', '.join(failed)}")
    return "; ".join(parts) or "nothing to do"


class InstallJob(BackgroundJob):
    """The download as a background job, so the tray and Settings can watch it.

    It unpacks tens of megabytes from the network; doing it on the request thread
    would block the server and the tray menu for as long as the connection lasts.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        super().__init__(name="vcs tools")
        self._settings = settings

    def _work(self, names: Iterable[str] | None = None) -> tuple[bool, str]:
        result = install(
            self._settings,
            names or TOOLS,
            progress=self.set_progress,
        )
        return result.ok, result.detail
