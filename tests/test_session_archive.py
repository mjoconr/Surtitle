"""Archiving and deleting conversations.

The whole point of these features is what they *do not* touch. A project is a
directory of real work; conversations are the chat about it. Filing a chat away
or deleting it must leave every file on disk exactly where it was, so most of
what follows asserts on the project folder as much as on the database.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from surtitle.config import Settings
from surtitle.server import create_app_for
from surtitle.store.db import SCHEMA_VERSION, Store

# --------------------------------------------------------------------- store


@pytest.fixture
def project(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "report.md").write_text("# the work\n", encoding="utf-8")
    return store, store.create_project("plant", root), root


class TestArchiveState:
    def test_a_new_conversation_is_not_archived(self, project):
        store, proj, _ = project
        session = store.create_session(proj.id, "fresh")
        assert session.archived is False
        assert session.archived_at is None
        assert session.to_dict()["archived"] is False

    def test_archiving_sets_a_timestamp_and_keeps_the_session(self, project):
        store, proj, _ = project
        session = store.create_session(proj.id, "old chat")

        updated = store.set_session_archived(session.id, True)

        assert updated is not None
        assert updated.archived is True
        assert updated.archived_at is not None
        # Still there: archiving is not deleting.
        assert store.get_session(session.id) is not None

    def test_archiving_an_unknown_session_reports_that(self, project):
        store, _, _ = project
        assert store.set_session_archived("nope", True) is None

    def test_unarchiving_clears_the_timestamp(self, project):
        store, proj, _ = project
        session = store.create_session(proj.id, "old chat")
        store.set_session_archived(session.id, True)

        restored = store.set_session_archived(session.id, False)

        assert restored is not None
        assert restored.archived is False
        assert restored.archived_at is None


class TestArchiveListing:
    def test_archived_conversations_leave_the_default_list(self, project):
        store, proj, _ = project
        live = store.create_session(proj.id, "live")
        filed = store.create_session(proj.id, "filed")
        store.set_session_archived(filed.id, True)

        ids = [s.id for s in store.list_sessions(proj.id)]

        assert ids == [live.id]

    def test_include_archived_brings_them_back(self, project):
        store, proj, _ = project
        live = store.create_session(proj.id, "live")
        filed = store.create_session(proj.id, "filed")
        store.set_session_archived(filed.id, True)

        ids = {s.id for s in store.list_sessions(proj.id, include_archived=True)}

        assert ids == {live.id, filed.id}

    def test_archiving_does_not_disturb_the_live_conversations(self, project):
        store, proj, _ = project
        first = store.create_session(proj.id, "one")
        second = store.create_session(proj.id, "two")
        store.set_session_archived(first.id, True)

        remaining = [s.title for s in store.list_sessions(proj.id)]

        assert remaining == ["two"]
        assert store.get_session(second.id).title == "two"


class TestArchivedConversationsLeaveSearch:
    def test_archived_chats_stop_being_retrieved(self, project):
        store, proj, _ = project
        session = store.create_session(proj.id, "4C-120 investigation")
        store.add_message(session.id, "user", "why is 4C-120 down?")
        assert store.search_conversations("4C-120")

        store.set_session_archived(session.id, True)

        assert store.search_conversations("4C-120") == []

    def test_restoring_makes_them_searchable_again(self, project):
        store, proj, _ = project
        session = store.create_session(proj.id, "4C-120 investigation")
        store.add_message(session.id, "user", "why is 4C-120 down?")
        store.set_session_archived(session.id, True)

        store.set_session_archived(session.id, False)

        hits = store.search_conversations("4C-120")
        assert [hit["session_id"] for hit in hits] == [session.id]

    def test_the_substring_fallback_also_respects_the_archive(self, project):
        # Phrase queries with punctuation exercise the non-FTS path.
        store, proj, _ = project
        session = store.create_session(proj.id, "quoted")
        store.add_message(session.id, "user", 'the "cold start" fault on 4C-117')
        store.set_session_archived(session.id, True)

        assert store.search_conversations('"cold start"') == []


class TestDeleteConversation:
    def test_deleting_removes_the_transcript(self, project):
        store, proj, _ = project
        session = store.create_session(proj.id, "doomed")
        store.add_message(session.id, "user", "hello")

        assert store.delete_session(session.id) is True

        assert store.get_session(session.id) is None
        assert store.list_messages(session.id) == []

    def test_deleting_removes_the_search_index_rows(self, project):
        store, proj, _ = project
        session = store.create_session(proj.id, "doomed")
        store.add_message(session.id, "user", "a distinctive phrase about wool")

        store.delete_session(session.id)

        # Without explicit cleanup the virtual table would keep the excerpt and
        # search would report conversations that no longer exist.
        assert store.search_conversations("distinctive phrase") == []

    def test_deleting_an_unknown_session_reports_that(self, project):
        store, _, _ = project
        assert store.delete_session("nope") is False


class TestPurgeArchive:
    def test_purging_removes_only_archived_conversations(self, project):
        store, proj, _ = project
        live = store.create_session(proj.id, "live")
        gone_a = store.create_session(proj.id, "filed a")
        gone_b = store.create_session(proj.id, "filed b")
        for session in (gone_a, gone_b):
            store.set_session_archived(session.id, True)

        removed = store.purge_archived_sessions(proj.id)

        assert removed == 2
        assert store.get_session(live.id) is not None
        assert store.get_session(gone_a.id) is None
        assert store.get_session(gone_b.id) is None

    def test_purging_reports_zero_when_there_is_no_archive(self, project):
        store, proj, _ = project
        store.create_session(proj.id, "live")
        assert store.purge_archived_sessions(proj.id) == 0

    def test_purging_leaves_the_project_directory_alone(self, project):
        store, proj, root = project
        session = store.create_session(proj.id, "filed")
        store.set_session_archived(session.id, True)

        store.purge_archived_sessions(proj.id)

        assert root.is_dir()
        assert (root / "report.md").read_text(encoding="utf-8") == "# the work\n"


class TestMigration:
    def test_a_database_from_before_archiving_gains_the_column(self, tmp_path):
        """Upgrading in place must not require the user to abandon history."""
        path = tmp_path / "old.sqlite"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL,
                created_at REAL NOT NULL, last_opened_at REAL NOT NULL,
                auto_approved TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'New conversation',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, spoken TEXT,
                created_at REAL NOT NULL
            );
            INSERT INTO projects VALUES ('p1', 'plant', '/tmp/plant', 1.0, 1.0, '[]');
            INSERT INTO sessions VALUES ('s1', 'p1', 'before the upgrade', 1.0, 1.0);
            INSERT INTO messages (session_id, role, content, created_at)
                VALUES ('s1', 'user', 'kept across the upgrade', 1.0);
            """
        )
        conn.commit()
        conn.close()

        store = Store(path)

        sessions = store.list_sessions("p1", include_archived=True)
        assert [s.title for s in sessions] == ["before the upgrade"]
        assert sessions[0].archived is False
        # The old transcript survived, and archiving now works on it.
        assert store.list_messages("s1")[0].content == "kept across the upgrade"
        assert store.set_session_archived("s1", True).archived is True

    def test_the_schema_version_is_recorded(self, project):
        store, _, _ = project
        row = store._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        assert row["value"] == str(SCHEMA_VERSION)

    def test_migrating_twice_is_harmless(self, tmp_path):
        path = tmp_path / "db.sqlite"
        first = Store(path)
        first.close()
        second = Store(path)  # must not raise on the already-current schema
        assert second.list_sessions("missing") == []


# ----------------------------------------------------------------------- api


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Settings(
        DEEPSEEK_API_KEY="sk-test-deepseek-1234567890",
        DEEPGRAM_API_KEY="dg-test-deepgram-0987654321",
        SURTITLE_HOME=str(home),
        voice_enabled=False,
    )


@pytest.fixture
async def client(settings):
    app = create_app_for(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    await app.state.app_state.aclose()


class TestSessionApi:
    @pytest.fixture
    async def project(self, client, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "keep.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        body = (await client.post("/api/projects", json={"name": "P", "root": str(root)})).json()
        return body, root

    async def test_the_default_list_is_live_conversations_only(self, client, project):
        body, _ = project
        live = (await client.post(f"/api/projects/{body['id']}/sessions")).json()
        filed = (await client.post(f"/api/projects/{body['id']}/sessions")).json()
        await client.post(f"/api/sessions/{filed['id']}/archive")

        listed = (await client.get(f"/api/projects/{body['id']}/sessions")).json()

        assert [s["id"] for s in listed["sessions"]] == [live["id"]]
        assert listed["counts"] == {"active": 1, "archived": 1}

    async def test_the_archived_view_lists_the_archive(self, client, project):
        body, _ = project
        await client.post(f"/api/projects/{body['id']}/sessions")
        filed = (await client.post(f"/api/projects/{body['id']}/sessions")).json()
        await client.post(f"/api/sessions/{filed['id']}/archive")

        listed = (
            await client.get(
                f"/api/projects/{body['id']}/sessions", params={"archived": "archived"}
            )
        ).json()

        assert [s["id"] for s in listed["sessions"]] == [filed["id"]]
        assert listed["sessions"][0]["archived"] is True

    async def test_all_returns_both_sides(self, client, project):
        body, _ = project
        await client.post(f"/api/projects/{body['id']}/sessions")
        filed = (await client.post(f"/api/projects/{body['id']}/sessions")).json()
        await client.post(f"/api/sessions/{filed['id']}/archive")

        listed = (
            await client.get(f"/api/projects/{body['id']}/sessions", params={"archived": "all"})
        ).json()

        assert len(listed["sessions"]) == 2

    async def test_an_unknown_view_falls_back_to_active(self, client, project):
        body, _ = project
        await client.post(f"/api/projects/{body['id']}/sessions")
        filed = (await client.post(f"/api/projects/{body['id']}/sessions")).json()
        await client.post(f"/api/sessions/{filed['id']}/archive")

        listed = (
            await client.get(f"/api/projects/{body['id']}/sessions", params={"archived": "junk"})
        ).json()

        assert listed["counts"]["active"] == 1

    async def test_archive_then_unarchive_round_trips(self, client, project):
        body, _ = project
        session = (await client.post(f"/api/projects/{body['id']}/sessions")).json()

        archived = (await client.post(f"/api/sessions/{session['id']}/archive")).json()
        assert archived["session"]["archived"] is True
        assert (await client.get(f"/api/sessions/{session['id']}")).json()["archived"] is True

        restored = (await client.post(f"/api/sessions/{session['id']}/unarchive")).json()
        assert restored["session"]["archived"] is False

    async def test_archiving_an_unknown_session_is_404(self, client):
        response = await client.post("/api/sessions/nope/archive")
        assert response.status_code == 404

    async def test_deleting_one_conversation_leaves_the_folder_alone(self, client, project):
        body, root = project
        session = (await client.post(f"/api/projects/{body['id']}/sessions")).json()

        assert (await client.delete(f"/api/sessions/{session['id']}")).status_code == 200

        assert (await client.get(f"/api/sessions/{session['id']}")).status_code == 404
        assert (root / "keep.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"

    async def test_emptying_the_archive_needs_confirmation(self, client, project):
        body, _ = project
        session = (await client.post(f"/api/projects/{body['id']}/sessions")).json()
        await client.post(f"/api/sessions/{session['id']}/archive")

        refused = await client.delete(f"/api/projects/{body['id']}/sessions/archived")

        assert refused.status_code == 400
        assert refused.json()["field"] == "confirm"
        # Refusing must not have deleted anything.
        assert (await client.get(f"/api/sessions/{session['id']}")).status_code == 200

    async def test_emptying_the_archive_deletes_every_archived_conversation(self, client, project):
        body, root = project
        live = (await client.post(f"/api/projects/{body['id']}/sessions")).json()
        filed = (await client.post(f"/api/projects/{body['id']}/sessions")).json()
        await client.post(f"/api/sessions/{filed['id']}/archive")

        response = await client.delete(
            f"/api/projects/{body['id']}/sessions/archived", params={"confirm": "true"}
        )

        assert response.status_code == 200
        assert response.json()["purged"] == 1
        assert (await client.get(f"/api/sessions/{filed['id']}")).status_code == 404
        assert (await client.get(f"/api/sessions/{live['id']}")).status_code == 200
        assert root.is_dir()

    async def test_the_project_payload_keeps_archived_chats_out_of_the_sidebar(
        self, client, project
    ):
        body, _ = project
        await client.post(f"/api/projects/{body['id']}/sessions")
        filed = (await client.post(f"/api/projects/{body['id']}/sessions")).json()
        await client.post(f"/api/sessions/{filed['id']}/archive")

        payload = (await client.get(f"/api/projects/{body['id']}")).json()

        assert [s["id"] for s in payload["sessions"]] != [filed["id"]]
        assert payload["session_counts"]["archived"] == 1
