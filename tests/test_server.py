"""Tests for the HTTP API.

The most important assertions here are the security ones: no endpoint may ever
return a credential value, and the file endpoints must stay inside the project
root. The rest covers the CRUD the UI depends on.
"""

from __future__ import annotations

import json

import httpx
import pytest

from surtitle.config import Settings
from surtitle.server import create_app_for
from surtitle.store.settings_store import SETTINGS_FILENAME


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Settings(
        DEEPSEEK_API_KEY="sk-test-deepseek-1234567890",
        DEEPGRAM_API_KEY="dg-test-deepgram-0987654321",
        SURTITLE_HOME=str(home),
        voice_enabled=True,
    )


@pytest.fixture
async def client(settings):
    app = create_app_for(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        http.app = app  # type: ignore[attr-defined]
        yield http
    await app.state.app_state.aclose()


class TestHealth:
    async def test_reports_configuration_state(self, client):
        response = await client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["deepseek_configured"] is True
        assert body["deepgram_configured"] is True
        assert body["version"]

    async def test_health_never_leaks_a_key(self, client):
        body = (await client.get("/api/health")).text
        assert "sk-test-deepseek" not in body
        assert "dg-test-deepgram" not in body


class TestProjects:
    async def test_create_and_list(self, client, tmp_path):
        target = tmp_path / "work"
        response = await client.post(
            "/api/projects", json={"name": "Sampling line", "root": str(target)}
        )
        assert response.status_code == 200
        project = response.json()
        assert project["name"] == "Sampling line"
        assert project["exists"] is True
        assert target.is_dir()

        listed = (await client.get("/api/projects")).json()["projects"]
        assert [p["id"] for p in listed] == [project["id"]]

    async def test_create_without_root_uses_managed_folder(self, client, settings):
        response = await client.post("/api/projects", json={"name": "Managed One"})
        assert response.status_code == 200
        project = response.json()
        assert project["root"].startswith(str(settings.default_workspace_dir))
        assert "managed-one" in project["root"]

    async def test_name_is_required(self, client):
        response = await client.post("/api/projects", json={"name": "   "})
        assert response.status_code == 400
        assert response.json()["field"] == "name"

    async def test_relative_root_is_rejected(self, client):
        response = await client.post("/api/projects", json={"name": "x", "root": "relative/path"})
        assert response.status_code == 400
        assert response.json()["field"] == "root"

    async def test_missing_project_is_404(self, client):
        assert (await client.get("/api/projects/nope")).status_code == 404

    async def test_rename(self, client, tmp_path):
        project = (
            await client.post("/api/projects", json={"name": "Old", "root": str(tmp_path / "p")})
        ).json()
        renamed = await client.patch(f"/api/projects/{project['id']}", json={"name": "New"})
        assert renamed.json()["name"] == "New"

    async def test_delete_does_not_remove_files(self, client, tmp_path):
        target = tmp_path / "keepme"
        project = (
            await client.post("/api/projects", json={"name": "Keep", "root": str(target)})
        ).json()
        (target / "important.txt").write_text("do not delete", encoding="utf-8")

        response = await client.delete(f"/api/projects/{project['id']}")
        assert response.status_code == 200
        assert response.json()["files_removed"] is False
        assert (target / "important.txt").read_text(encoding="utf-8") == "do not delete"

    async def test_duplicate_root_reuses_the_project(self, client, tmp_path):
        payload = {"name": "First", "root": str(tmp_path / "same")}
        first = (await client.post("/api/projects", json=payload)).json()
        second = (
            await client.post("/api/projects", json={"name": "Second", "root": payload["root"]})
        ).json()
        assert first["id"] == second["id"]


class TestProjectFiles:
    @pytest.fixture
    async def project(self, client, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "notes.md").write_text("# Notes\n", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "deep.txt").write_text("deep", encoding="utf-8")
        return (await client.post("/api/projects", json={"name": "P", "root": str(root)})).json()

    async def test_lists_files(self, client, project):
        body = (await client.get(f"/api/projects/{project['id']}/files")).json()
        names = {entry["name"] for entry in body["entries"]}
        assert {"notes.md", "sub"} <= names

    async def test_serves_a_file_inline(self, client, project):
        response = await client.get(
            f"/api/projects/{project['id']}/file", params={"path": "notes.md"}
        )
        assert response.status_code == 200
        assert "# Notes" in response.text

    async def test_blocks_path_escape(self, client, project):
        response = await client.get(
            f"/api/projects/{project['id']}/file", params={"path": "../../../etc/passwd"}
        )
        assert response.status_code in (403, 404)

    async def test_blocks_absolute_path(self, client, project):
        response = await client.get(
            f"/api/projects/{project['id']}/file", params={"path": "/etc/passwd"}
        )
        assert response.status_code in (403, 404)

    async def test_missing_file_is_404(self, client, project):
        response = await client.get(
            f"/api/projects/{project['id']}/file", params={"path": "nope.txt"}
        )
        assert response.status_code == 404

    async def test_listing_a_missing_folder_is_an_error(self, client, project):
        response = await client.get(f"/api/projects/{project['id']}/files", params={"path": "nope"})
        assert response.status_code == 400


class TestSessions:
    @pytest.fixture
    async def project(self, client, tmp_path):
        root = tmp_path / "sp"
        root.mkdir()
        return (await client.post("/api/projects", json={"name": "S", "root": str(root)})).json()

    async def test_create_and_fetch(self, client, project):
        created = (
            await client.post(f"/api/projects/{project['id']}/sessions", json={"title": "Chat one"})
        ).json()
        assert created["title"] == "Chat one"

        fetched = (await client.get(f"/api/sessions/{created['id']}")).json()
        assert fetched["messages"] == []
        assert fetched["tool_calls"] == []

    async def test_sessions_appear_under_the_project(self, client, project):
        await client.post(f"/api/projects/{project['id']}/sessions", json={})
        body = (await client.get(f"/api/projects/{project['id']}/sessions")).json()
        assert len(body["sessions"]) == 1

    async def test_delete(self, client, project):
        created = (await client.post(f"/api/projects/{project['id']}/sessions", json={})).json()
        assert (await client.delete(f"/api/sessions/{created['id']}")).status_code == 200
        assert (await client.get(f"/api/sessions/{created['id']}")).status_code == 404

    async def test_transcript_is_returned_in_order(self, client, project):
        created = (await client.post(f"/api/projects/{project['id']}/sessions", json={})).json()
        state = client.app.state.app_state
        state.store.add_message(created["id"], "user", "first")
        state.store.add_message(created["id"], "assistant", "second", spoken="second")

        messages = (await client.get(f"/api/sessions/{created['id']}")).json()["messages"]
        assert [m["content"] for m in messages] == ["first", "second"]
        assert messages[1]["spoken"] == "second"


class TestSettingsApi:
    async def test_describe_includes_preferences(self, client):
        body = (await client.get("/api/settings")).json()
        assert "sections" in body
        names = {field["name"] for fields in body["sections"].values() for field in fields}
        assert {"deepseek_model", "tts_model", "voice_enabled"} <= names

    async def test_describe_never_returns_a_secret(self, client, settings):
        text = (await client.get("/api/settings")).text
        assert "sk-test-deepseek" not in text
        assert "dg-test-deepgram" not in text
        # Not even a masked or truncated form.
        assert "1234567890" not in text

    async def test_credentials_report_status_without_values(self, client):
        providers = (await client.get("/api/settings")).json()["providers"]
        deepseek = next(p for p in providers if p["id"] == "deepseek")
        assert deepseek["credential"]["configured"] is True
        assert deepseek["credential"]["source"] == "env"
        assert deepseek["credential"]["writable"] is False
        assert "value" not in deepseek["credential"]

    async def test_save_a_preference(self, client):
        response = await client.put("/api/settings", json={"reasoning_effort": "high"})
        assert response.status_code == 200
        body = response.json()
        effort = next(f for f in body["sections"]["model"] if f["name"] == "reasoning_effort")
        assert effort["value"] == "high"
        assert effort["stored"] is True

    async def test_preference_is_persisted_to_disk(self, client, settings):
        await client.put("/api/settings", json={"max_steps": 7})
        saved = json.loads((settings.data_dir / SETTINGS_FILENAME).read_text(encoding="utf-8"))
        assert saved["max_steps"] == 7

    async def test_unknown_preference_is_rejected(self, client):
        response = await client.put("/api/settings", json={"nonsense": 1})
        assert response.status_code == 400
        assert response.json()["field"] == "nonsense"

    async def test_invalid_choice_is_rejected_with_the_field(self, client):
        response = await client.put("/api/settings", json={"reasoning_effort": "extreme"})
        assert response.status_code == 400
        assert response.json()["field"] == "reasoning_effort"

    async def test_out_of_range_number_is_rejected(self, client):
        assert (await client.put("/api/settings", json={"max_steps": 99999})).status_code == 400

    async def test_reset_clears_stored_preferences(self, client):
        await client.put("/api/settings", json={"max_steps": 5})
        await client.delete("/api/settings")
        body = (await client.get("/api/settings")).json()
        stored = [f for fields in body["sections"].values() for f in fields if f["stored"]]
        assert stored == []


class TestCredentialsApi:
    async def test_env_credential_cannot_be_overwritten(self, client):
        """A key from the environment wins, so saving one here would be a lie."""
        response = await client.put("/api/credentials/DEEPSEEK_API_KEY", json={"value": "sk-new"})
        assert response.status_code == 400
        assert "environment" in response.json()["error"].lower()

    async def test_env_credential_cannot_be_deleted(self, client):
        response = await client.delete("/api/credentials/DEEPSEEK_API_KEY")
        assert response.status_code == 400

    async def test_stores_a_new_credential(self, client, settings, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        # The Settings object captured the key at construction, so clearing the
        # environment is enough for the store to treat the file as authoritative.
        settings.deepgram_api_key = None

        response = await client.put("/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-fresh"})
        assert response.status_code == 200
        assert response.json()["credential"]["configured"] is True
        assert response.json()["credential"]["source"] == "file"

        # Response must not echo the value back.
        assert "dg-fresh" not in response.text

    async def test_credentials_file_is_owner_only(self, client, settings, monkeypatch):
        import os
        import stat

        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        settings.deepgram_api_key = None
        await client.put("/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-secret"})

        path = settings.data_dir / ".credentials.json"
        assert path.exists()
        if os.name == "posix":
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600, f"credentials file mode is {oct(mode)}, expected 0o600"

    @pytest.mark.parametrize(
        "bad_value",
        [
            "DEEPGRAM_API_KEY=abc",  # a whole env line
            '"quoted-key"',  # wrapped in quotes
            "has space",  # contains whitespace
            "",  # empty
        ],
    )
    async def test_malformed_keys_are_rejected_inline(
        self, client, settings, monkeypatch, bad_value
    ):
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        settings.deepgram_api_key = None
        response = await client.put("/api/credentials/DEEPGRAM_API_KEY", json={"value": bad_value})
        assert response.status_code == 400
        assert response.json()["field"] == "DEEPGRAM_API_KEY"

    async def test_verify_reports_a_missing_credential(self, client, settings, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        settings.deepgram_api_key = None
        response = await client.post("/api/credentials/DEEPGRAM_API_KEY/verify")
        assert response.status_code == 400
        assert "not configured" in response.json()["error"]

    async def test_verify_rejects_an_unknown_reference(self, client):
        response = await client.post("/api/credentials/SOMETHING_ELSE/verify")
        assert response.status_code == 400

    async def test_verify_never_echoes_the_key_on_failure(self, client, monkeypatch):
        from surtitle.store.settings_store import SettingsStore

        store = SettingsStore(client.app.state.app_state.settings)
        store._credentials["DEEPGRAM_API_KEY"] = "dg-verify-me-not-echoed"
        client.app.state.app_state.settings_store = store

        response = await client.post("/api/credentials/DEEPGRAM_API_KEY/verify")
        assert "dg-verify-me-not-echoed" not in response.text


class TestTools:
    async def test_lists_tool_policies(self, client):
        tools = (await client.get("/api/tools")).json()["tools"]
        by_name = {t["name"]: t for t in tools}
        assert by_name["read_file"]["approval"] == "never"
        assert by_name["write_file"]["approval"] == "ask"
        assert by_name["write_file"]["mutating"] is True
        assert by_name["make_pdf"]["mutating"] is True
        assert "list_dir" in by_name
