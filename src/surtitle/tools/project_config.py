"""Per-project configuration.

Global preferences live in the app data directory (see ``settings_store``), but
some settings are genuinely a property of *the project*: where LibreOffice is
installed, which MCP servers to talk to, which tools may run without asking.
Those belong next to the work, so a project can be handed to someone else and
still behave correctly.

The file is ``.surtitle.json`` at the project root. It is plain JSON, kept
small, and never contains a secret: MCP server definitions may reference an
environment variable name but must not embed a key, because this file is
expected to be committed alongside the project.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "CONFIG_FILENAME",
    "McpServerConfig",
    "ProjectConfig",
    "find_soffice",
    "load_project_config",
    "save_project_config",
]

log = logging.getLogger(__name__)

CONFIG_FILENAME = ".surtitle.json"
CONFIG_VERSION = 1

# Locations LibreOffice installs to when it is not on PATH. Checked in order so
# the most specific (per-user) install wins.
_SOFFICE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "~/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ),
    "win32": (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ),
}
_SOFFICE_LINUX = ("/usr/bin/soffice", "/usr/local/bin/soffice", "/snap/bin/libreoffice")


@dataclass(slots=True)
class McpServerConfig:
    """One MCP server this project talks to.

    ``command`` plus ``args`` describes a stdio server. ``env`` holds *names* of
    environment variables to pass through, not their values.
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    # Prefix applied to every tool this server exposes, so two servers cannot
    # collide on a name like "create".
    namespace: str | None = None
    # Tools from this server may run without an approval prompt.
    trusted_tools: list[str] = field(default_factory=list)
    cwd: str | None = None
    startup_timeout: float = 30.0
    call_timeout: float = 120.0

    @property
    def tool_prefix(self) -> str:
        """Namespace used when exposing this server's tools to the model."""
        if self.namespace:
            return _sanitise_namespace(self.namespace)
        return _sanitise_namespace(self.name)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in asdict(self).items()
            # `enabled` is excluded from the falsy filter below on purpose: a
            # disabled server must round-trip as disabled.
            if value not in (None, [], {})
        }
        return payload


def _sanitise_namespace(value: str) -> str:
    """Reduce a server name to a safe tool-name prefix."""
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value.strip().lower())
    return cleaned.strip("_") or "mcp"


@dataclass(slots=True)
class ProjectConfig:
    """Everything project-scoped."""

    version: int = CONFIG_VERSION
    # Explicit path to the LibreOffice binary, if auto-discovery is not enough.
    soffice_path: str | None = None
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    # Tools the user has chosen to trust inside this project.
    trusted_tools: list[str] = field(default_factory=list)
    # Extra instructions appended to the agent's system prompt for this project.
    instructions: str | None = None
    # Directories to keep out of reads and searches, relative to the root.
    ignore: list[str] = field(default_factory=list)
    # Python requirements approved for this project. Mirrored from the
    # environment's requirements.txt so the decision travels with the repo.
    requirements: list[str] = field(default_factory=list)

    @property
    def enabled_mcp_servers(self) -> list[McpServerConfig]:
        return [server for server in self.mcp_servers if server.enabled and server.command]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"version": self.version}
        if self.soffice_path:
            payload["soffice_path"] = self.soffice_path
        if self.mcp_servers:
            payload["mcp_servers"] = [server.to_dict() for server in self.mcp_servers]
        if self.trusted_tools:
            payload["trusted_tools"] = sorted(set(self.trusted_tools))
        if self.instructions:
            payload["instructions"] = self.instructions
        if self.ignore:
            payload["ignore"] = sorted(set(self.ignore))
        if self.requirements:
            payload["requirements"] = sorted(set(self.requirements))
        return payload


def load_project_config(root: Path) -> ProjectConfig:
    """Read ``.surtitle.json`` from a project root.

    A malformed or unreadable file is reported and then ignored, so a bad config
    degrades to defaults instead of making the project unusable.
    """
    path = root / CONFIG_FILENAME
    if not path.is_file():
        return ProjectConfig()

    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return ProjectConfig()

    if not isinstance(raw, dict):
        log.warning("ignoring %s: expected a JSON object", path)
        return ProjectConfig()

    servers: list[McpServerConfig] = []
    for entry in raw.get("mcp_servers") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        command = str(entry.get("command") or "").strip()
        if not name or not command:
            log.warning("skipping MCP server entry without a name or command: %s", entry)
            continue
        servers.append(
            McpServerConfig(
                name=name,
                command=command,
                args=[str(a) for a in (entry.get("args") or [])],
                env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                enabled=bool(entry.get("enabled", True)),
                namespace=entry.get("namespace"),
                trusted_tools=[str(t) for t in (entry.get("trusted_tools") or [])],
                cwd=entry.get("cwd"),
                startup_timeout=float(entry.get("startup_timeout", 30.0)),
                call_timeout=float(entry.get("call_timeout", 120.0)),
            )
        )

    return ProjectConfig(
        version=int(raw.get("version") or CONFIG_VERSION),
        soffice_path=raw.get("soffice_path") or None,
        mcp_servers=servers,
        trusted_tools=[str(t) for t in (raw.get("trusted_tools") or [])],
        instructions=raw.get("instructions") or None,
        ignore=[str(i) for i in (raw.get("ignore") or [])],
        requirements=[str(r) for r in (raw.get("requirements") or [])],
    )


def save_project_config(root: Path, config: ProjectConfig) -> Path:
    """Write ``.surtitle.json``, creating the project root if needed."""
    path = root / CONFIG_FILENAME
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def find_soffice(configured: str | None = None) -> Path | None:
    """Locate the LibreOffice binary.

    Checked in order: an explicit path, the ``SURTITLE_SOFFICE`` environment
    variable, ``PATH``, then the platform's usual install locations. Returning
    ``None`` rather than raising lets the caller explain what to install.
    """
    for candidate in (configured, os.environ.get("SURTITLE_SOFFICE")):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path

    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return Path(found)

    candidates = _SOFFICE_CANDIDATES.get(sys.platform)
    if candidates is None:
        candidates = _SOFFICE_LINUX
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def configured_soffice(root: Path) -> Path | None:
    """Convenience: discover LibreOffice using the project's own configuration."""
    with contextlib.suppress(Exception):
        return find_soffice(load_project_config(root).soffice_path)
    return find_soffice()
