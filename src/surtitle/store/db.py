"""SQLite persistence for projects, sessions and transcripts.

Deliberately stdlib-only: ``sqlite3`` ships with Python on every platform, so
there is nothing extra to bundle into the Windows release and no server process
to manage. WAL mode is enabled so a long-running agent turn does not block UI
reads from another request thread.
"""

from __future__ import annotations

import contextlib
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

SCHEMA_VERSION = 2

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
    updated_at  REAL NOT NULL,
    -- NULL while the conversation is live. A timestamp once the user filed it
    -- away: archived conversations keep their messages and can be restored, but
    -- drop out of the sidebar and out of the agent's history search.
    archived_at REAL
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
    archived_at: float | None = None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "archived": self.archived,
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
        # Full-text search over past conversations, when the SQLite build has FTS5.
        # Created here rather than in the schema script because an unsupported
        # module would make the whole script fail, taking the tables with it.
        self.fts5 = self._enable_search()
        self._migrate()

    # --- lifecycle -------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._add_missing_columns()
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                self._conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )

    def _add_missing_columns(self) -> None:
        """Bring an existing database up to the current schema.

        ``CREATE TABLE IF NOT EXISTS`` silently does nothing to a table that
        already exists, so a column added to ``_SCHEMA`` would never reach a
        database created by an older build. Each entry here is additive and
        idempotent, which keeps upgrades safe to repeat and safe to interrupt.
        """
        additions = {"sessions": {"archived_at": "REAL"}}
        for table, columns in additions.items():
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not existing:  # table absent entirely; the schema script will make it
                continue
            for name, sql_type in columns.items():
                if name not in existing:
                    log.info("adding %s.%s", table, name)
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    def _enable_search(self) -> bool:
        """Create the conversation search index if FTS5 is available."""
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5("
                "content, session_id UNINDEXED, message_id UNINDEXED, role UNINDEXED)"
            )
            return True
        except sqlite3.OperationalError as exc:  # pragma: no cover - build dependent
            log.info("conversation search unavailable (FTS5 missing): %s", exc)
            return False

    def _index_message(self, message_id: int, session_id: str, role: str, content: str) -> None:
        """Add a message to the search index. Best-effort by design: failing to
        index must never fail the conversation."""
        if not self.fts5 or not content.strip():
            return
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute(
                "INSERT INTO message_search (content, session_id, message_id, role)"
                " VALUES (?, ?, ?, ?)",
                (content, session_id, str(message_id), role),
            )

    def search_conversations(
        self, query: str, *, limit: int = 5, exclude_session: str | None = None
    ) -> list[dict[str, Any]]:
        """Find past messages matching ``query``.

        This is the retrieval half of durable memory: the notebook holds what the
        agent chose to record, and this finds what it did not. Results are ranked,
        short, and meant to be *offered* - the caller decides whether they belong in
        the model's context.
        """
        needle = (query or "").strip()
        if not needle:
            return []

        rows: list[sqlite3.Row] = []
        with self._lock:
            if self.fts5:
                # Quote the query so punctuation cannot be read as FTS syntax; a
                # stray quote in a machine name would otherwise raise.
                match = '"' + needle.replace('"', '""') + '"'
                with contextlib.suppress(sqlite3.Error):
                    rows = self._conn.execute(
                        "SELECT m.session_id, m.role, m.content,"
                        " snippet(message_search, 0, '', '', '...', 12) AS excerpt,"
                        " bm25(message_search) AS score"
                        " FROM message_search"
                        " JOIN messages m ON m.id = CAST(message_search.message_id AS INTEGER)"
                        " JOIN sessions s ON s.id = m.session_id"
                        " WHERE message_search MATCH ? AND s.archived_at IS NULL"
                        " ORDER BY score LIMIT ?",
                        (match, limit + 5),
                    ).fetchall()
            if not rows:
                # Fallback, and also the path taken when a quoted phrase finds
                # nothing: a plain substring scan is slower but never misses.
                rows = self._conn.execute(
                    "SELECT m.session_id, m.role, m.content, m.content AS excerpt, 0 AS score"
                    " FROM messages m JOIN sessions s ON s.id = m.session_id"
                    " WHERE m.content LIKE ? AND s.archived_at IS NULL"
                    " ORDER BY m.id DESC LIMIT ?",
                    (f"%{needle}%", limit + 5),
                ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            if exclude_session and row["session_id"] == exclude_session:
                continue
            session = self.get_session(row["session_id"])
            results.append(
                {
                    "session_id": row["session_id"],
                    "session_title": session.title if session else "(deleted)",
                    "role": row["role"],
                    "excerpt": " ".join((row["excerpt"] or "").split())[:300],
                }
            )
            if len(results) >= limit:
                break
        return results

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

    def list_sessions(
        self, project_id: str, *, limit: int = 50, include_archived: bool = False
    ) -> list[Session]:
        """Conversations for a project, most recently used first.

        Archived conversations are excluded unless ``include_archived`` is set,
        which is what makes archiving feel like filing something away rather
        than deleting it.
        """
        sql = "SELECT * FROM sessions WHERE project_id = ?"
        if not include_archived:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY updated_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (project_id, limit)).fetchall()
        return [self._row_to_session(r) for r in rows]

    def set_session_archived(self, session_id: str, archived: bool = True) -> Session | None:
        """File a conversation away, or bring it back. Never touches the files.

        Returns the updated session, or ``None`` when the id is unknown.
        """
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET archived_at = ? WHERE id = ?",
                (now if archived else None, session_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_session(session_id)

    def purge_archived_sessions(self, project_id: str) -> int:
        """Permanently delete every archived conversation in a project.

        Returns how many were removed. The project directory itself is never
        touched - this only clears chat history.
        """
        with self._lock, self._conn:
            ids = [
                row["id"]
                for row in self._conn.execute(
                    "SELECT id FROM sessions WHERE project_id = ? AND archived_at IS NOT NULL",
                    (project_id,),
                ).fetchall()
            ]
            for session_id in ids:
                self.delete_session(session_id)
        return len(ids)

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
        """Delete one conversation and its transcript. Files are not affected."""
        with self._lock, self._conn:
            # The search index is a virtual table with no foreign key, so the
            # cascade below does not reach it. Leave the rows and search would
            # keep returning excerpts from conversations that no longer exist.
            if self.fts5:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute(
                        "DELETE FROM message_search WHERE session_id = ?", (session_id,)
                    )
            cursor = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        keys = row.keys()
        return Session(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"] if "archived_at" in keys else None,
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
            self._index_message(message_id, session_id, role, content)
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
