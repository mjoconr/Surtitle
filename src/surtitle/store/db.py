"""SQLite persistence for projects, sessions and transcripts.

Deliberately stdlib-only: ``sqlite3`` ships with Python on every platform, so
there is nothing extra to bundle into the Windows release and no server process
to manage. WAL mode is enabled so a long-running agent turn does not block UI
reads from another request thread.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Message", "Project", "Session", "Store", "ToolCallRecord"]

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    root              TEXT NOT NULL,
    created_at        REAL NOT NULL,
    last_opened_at    REAL NOT NULL,
    -- JSON array of tool names the user chose not to be asked about again.
    auto_approved     TEXT NOT NULL DEFAULT '[]'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_root ON projects(root);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT 'New conversation',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    -- Spoken subset of `content`, when the model used <say> tags.
    spoken      TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    step          INTEGER NOT NULL DEFAULT 0,
    name          TEXT NOT NULL,
    arguments     TEXT NOT NULL,
    result        TEXT,
    ok            INTEGER,
    approved      INTEGER,
    duration_ms   INTEGER,
    created_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, id);
"""


@dataclass(slots=True)
class Project:
    """A directory the agent is attached to."""

    id: str
    name: str
    root: str
    created_at: float
    last_opened_at: float
    auto_approved: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return Path(self.root)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "root": self.root,
            "created_at": self.created_at,
            "last_opened_at": self.last_opened_at,
            "auto_approved": list(self.auto_approved),
            "exists": self.path.is_dir(),
        }


@dataclass(slots=True)
class Session:
    """One conversation within a project."""

    id: str
    project_id: str
    title: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class Message:
    """A stored transcript entry."""

    id: int
    session_id: str
    role: str
    content: str
    spoken: str | None
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "spoken": self.spoken,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class ToolCallRecord:
    """A stored tool invocation and its outcome."""

    id: int
    session_id: str
    step: int
    name: str
    arguments: dict[str, Any]
    result: str | None
    ok: bool | None
    approved: bool | None
    duration_ms: int | None
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "step": self.step,
            "name": self.name,
            "arguments": self.arguments,
            "result": self.result,
            "ok": self.ok,
            "approved": self.approved,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at,
        }


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


class Store:
    """Thread-safe SQLite access layer.

    A single connection is shared and guarded by a lock. SQLite handles
    concurrent readers well, but this app's write volume is tiny and a lock
    removes an entire class of "database is locked" failures on Windows.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    # --- lifecycle -------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- projects --------------------------------------------------------
    def create_project(
        self, name: str, root: Path, *, auto_approved: list[str] | None = None
    ) -> Project:
        """Create a project, or return the existing one for the same directory."""
        resolved = root.expanduser().resolve()
        now = time.time()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM projects WHERE root = ?", (str(resolved),)
            ).fetchone()
            if existing is not None:
                return self._row_to_project(existing)

            project_id = _new_id()
            self._conn.execute(
                "INSERT INTO projects (id, name, root, created_at, last_opened_at, auto_approved)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, name, str(resolved), now, now, json.dumps(auto_approved or [])),
            )
        return Project(
            id=project_id,
            name=name,
            root=str(resolved),
            created_at=now,
            last_opened_at=now,
            auto_approved=list(auto_approved or []),
        )

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._row_to_project(row) if row else None

    def list_projects(self) -> list[Project]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projects ORDER BY last_opened_at DESC"
            ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def touch_project(self, project_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE projects SET last_opened_at = ? WHERE id = ?",
                (time.time(), project_id),
            )

    def rename_project(self, project_id: str, name: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE projects SET name = ? WHERE id = ?", (name, project_id)
            )
        return cursor.rowcount > 0

    def delete_project(self, project_id: str) -> bool:
        """Forget a project. The user's files on disk are never touched."""
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0

    def set_auto_approved(self, project_id: str, tools: list[str]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE projects SET auto_approved = ? WHERE id = ?",
                (json.dumps(sorted(set(tools))), project_id),
            )

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> Project:
        try:
            approved = json.loads(row["auto_approved"] or "[]")
        except json.JSONDecodeError:
            approved = []
        return Project(
            id=row["id"],
            name=row["name"],
            root=row["root"],
            created_at=row["created_at"],
            last_opened_at=row["last_opened_at"],
            auto_approved=[str(x) for x in approved] if isinstance(approved, list) else [],
        )

    # --- sessions --------------------------------------------------------
    def create_session(self, project_id: str, title: str = "New conversation") -> Session:
        now = time.time()
        session_id = _new_id()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (id, project_id, title, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, project_id, title, now, now),
            )
        return Session(
            id=session_id,
            project_id=project_id,
            title=title,
            created_at=now,
            updated_at=now,
        )

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(self, project_id: str, *, limit: int = 50) -> list[Session]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def touch_session(self, session_id: str, *, title: str | None = None) -> None:
        with self._lock, self._conn:
            if title is None:
                self._conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (time.time(), session_id),
                )
            else:
                self._conn.execute(
                    "UPDATE sessions SET updated_at = ?, title = ? WHERE id = ?",
                    (time.time(), title, session_id),
                )

    def delete_session(self, session_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # --- messages --------------------------------------------------------
    def add_message(
        self, session_id: str, role: str, content: str, *, spoken: str | None = None
    ) -> Message:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO messages (session_id, role, content, spoken, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, spoken, now),
            )
            self._conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            message_id = int(cursor.lastrowid or 0)
        return Message(
            id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            spoken=spoken,
            created_at=now,
        )

    def list_messages(self, session_id: str, *, limit: int = 500) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            Message(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                spoken=r["spoken"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # --- tool calls ------------------------------------------------------
    def add_tool_call(
        self,
        session_id: str,
        name: str,
        arguments: dict[str, Any],
        *,
        step: int = 0,
        result: str | None = None,
        ok: bool | None = None,
        approved: bool | None = None,
        duration_ms: int | None = None,
    ) -> ToolCallRecord:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO tool_calls"
                " (session_id, step, name, arguments, result, ok, approved,"
                " duration_ms, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    step,
                    name,
                    json.dumps(arguments, ensure_ascii=False),
                    result,
                    None if ok is None else int(ok),
                    None if approved is None else int(approved),
                    duration_ms,
                    now,
                ),
            )
            record_id = int(cursor.lastrowid or 0)
        return ToolCallRecord(
            id=record_id,
            session_id=session_id,
            step=step,
            name=name,
            arguments=arguments,
            result=result,
            ok=ok,
            approved=approved,
            duration_ms=duration_ms,
            created_at=now,
        )

    def list_tool_calls(self, session_id: str, *, limit: int = 200) -> list[ToolCallRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        records = []
        for row in rows:
            try:
                arguments = json.loads(row["arguments"])
            except json.JSONDecodeError:
                arguments = {}
            records.append(
                ToolCallRecord(
                    id=row["id"],
                    session_id=row["session_id"],
                    step=row["step"],
                    name=row["name"],
                    arguments=arguments if isinstance(arguments, dict) else {},
                    result=row["result"],
                    ok=None if row["ok"] is None else bool(row["ok"]),
                    approved=None if row["approved"] is None else bool(row["approved"]),
                    duration_ms=row["duration_ms"],
                    created_at=row["created_at"],
                )
            )
        return records
