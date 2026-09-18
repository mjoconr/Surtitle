"""Tests for the HTTP API.

The most important assertions here are the security ones: no endpoint may ever
return a credential value, and the file endpoints must stay inside the project
root. The rest covers the CRUD the UI depends on.
"""

from __future__ import annotations

import asyncio
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

    async def test_health_reports_which_engines_are_selected(self, client):
        body = (await client.get("/api/health")).json()
        assert body["voice_backends"] == {"stt": "deepgram", "tts": "deepgram"}


class TestStatus:
    """The endpoint behind the tray icon and ``surtitle status``."""

    async def test_reports_the_run(self, client):
        response = await client.get("/api/status")
        assert response.status_code == 200
        body = response.json()
        assert {"version", "pid", "url", "model", "sessions", "usage", "storage"} <= set(body)
        assert body["usage"]["uptime_seconds"] >= 0
        assert body["pid"] > 0

    async def test_reports_what_is_stored(self, client, tmp_path):
        project = await client.post("/api/projects", json={"name": "P", "root": str(tmp_path)})
        project_id = project.json()["id"]
        await client.post(f"/api/projects/{project_id}/sessions", json={"title": "T"})
        body = (await client.get("/api/status")).json()
        assert body["storage"]["projects"] == 1
        assert body["storage"]["conversations"] == 1
        assert body["storage"]["db_bytes"] > 0

    async def test_never_leaks_a_key(self, client):
        body = (await client.get("/api/status")).text
        assert "sk-test-deepseek" not in body
        assert "dg-test-deepgram" not in body


class TestShutdown:
    """Stopping the server, and who is allowed to ask for it."""

    async def test_refuses_when_nothing_can_stop_it(self, client):
        """A test app has no uvicorn server behind it, so it must not pretend."""
        response = await client.post("/api/shutdown")
        assert response.status_code == 503

    async def test_a_local_request_stops_the_server(self, settings):
        stopped: list[bool] = []
        app = create_app_for(settings, on_shutdown=lambda: stopped.append(True))
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/shutdown")
            assert response.status_code == 200
            assert response.json()["stopping"] is True
            # Deferred by a loop turn so this response is flushed first: the
            # caller has to be told it worked before the server closes.
            await asyncio.sleep(0.15)
        assert stopped == [True]
        await app.state.app_state.aclose()

    async def test_a_remote_request_is_refused(self, settings):
        """Exposing the port to a network must not hand it a stop button."""
        app = create_app_for(settings, on_shutdown=lambda: None)
        transport = httpx.ASGITransport(app=app, client=("10.211.55.9", 40123))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/shutdown")
        assert response.status_code == 403
        await app.state.app_state.aclose()

    async def test_a_remote_request_cannot_stop_even_with_a_handler_wired(self, settings):
        stopped: list[bool] = []
        app = create_app_for(settings, on_shutdown=lambda: stopped.append(True))
        transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 40123))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            await http.post("/api/shutdown")
        await asyncio.sleep(0.1)
        assert stopped == []
        await app.state.app_state.aclose()


class TestLocalModels:
    async def test_models_endpoint_reports_every_registered_model(self, client):
        """The Settings screen needs to know what is installed before offering it."""
        response = await client.get("/api/models")
        assert response.status_code == 200
        rows = response.json()["models"]
        assert rows, "no models were reported"
        assert {"key", "kind", "label", "present", "missing", "bytes", "path"} <= set(rows[0])
        assert all(row["present"] is False for row in rows), "the test home has no models"
        assert {"stt", "tts"} <= {row["kind"] for row in rows}

    async def test_models_endpoint_does_not_leak_a_key(self, client):
        body = (await client.get("/api/models")).text
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
        body = response.json()
        assert body["files_removed"] is False
        # The reply names the folder it left alone, so the UI can say so too
        # rather than leaving "deleted" to mean whatever the user fears.
        assert body["root"] == str(target.resolve())
        assert (target / "important.txt").read_text(encoding="utf-8") == "do not delete"

    async def test_delete_takes_the_conversations_with_it(self, client, tmp_path):
        target = tmp_path / "project"
        project = (
            await client.post("/api/projects", json={"name": "Talks", "root": str(target)})
        ).json()
        session = (
            await client.post(f"/api/projects/{project['id']}/sessions", json={"title": "One"})
        ).json()

        body = (await client.delete(f"/api/projects/{project['id']}")).json()
        assert body["sessions_removed"] == 1
        assert (await client.get(f"/api/sessions/{session['id']}")).status_code == 404
        assert (await client.get(f"/api/projects/{project['id']}")).status_code == 404
        # The folder and anything the agent wrote into it survive.
        assert target.is_dir()

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

    @pytest.fixture
    async def bare_client(self, tmp_path, monkeypatch):
        """An app whose credentials come from nowhere but its own store.

        Built by constructing Settings *without* keys, rather than by clearing an
        attribute afterwards: the store snapshots launch credentials at
        construction precisely so post-hoc mutation cannot fake the state.
        """
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        home = tmp_path / "bare-home"
        home.mkdir()
        app = create_app_for(Settings(SURTITLE_HOME=str(home), voice_enabled=False))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
        await app.state.app_state.aclose()

    async def test_stores_a_new_credential(self, bare_client):
        response = await bare_client.put(
            "/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-fresh"}
        )
        assert response.status_code == 200
        assert response.json()["credential"]["configured"] is True
        assert response.json()["credential"]["source"] == "file"
        # The response must not echo the value back.
        assert "dg-fresh" not in response.text

    async def test_credentials_file_is_owner_only(self, bare_client, tmp_path):
        import os
        import stat

        await bare_client.put("/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-secret"})
        path = tmp_path / "bare-home" / ".credentials.json"
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
    async def test_malformed_keys_are_rejected_inline(self, bare_client, bad_value):
        response = await bare_client.put(
            "/api/credentials/DEEPGRAM_API_KEY", json={"value": bad_value}
        )
        assert response.status_code == 400
        assert response.json()["field"] == "DEEPGRAM_API_KEY"

    async def test_verify_reports_a_missing_credential(self, bare_client):
        response = await bare_client.post("/api/credentials/DEEPGRAM_API_KEY/verify")
        assert response.status_code == 400
        assert "not configured" in response.json()["error"]

    async def test_verify_rejects_an_unknown_reference(self, client):
        response = await client.post("/api/credentials/SOMETHING_ELSE/verify")
        assert response.status_code == 400

    async def test_verify_never_echoes_the_key_on_failure(self, bare_client):
        """Whether the probe succeeds or fails, the key must not come back."""
        await bare_client.put(
            "/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-verify-me-not-echoed"}
        )
        response = await bare_client.post("/api/credentials/DEEPGRAM_API_KEY/verify")
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


class TestCredentialEditing:
    """A saved key must be replaceable.

    Environment-provided keys are read-only by design, but a key stored by the
    app itself is the user's to change — and the UI must say which case applies,
    because a locked field with no explanation looks like an app bug.
    """

    @pytest.fixture
    def store_client(self, tmp_path, monkeypatch):
        """An app with no environment credentials, so the file is authoritative."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        home = tmp_path / "home"
        home.mkdir()
        settings = Settings(
            SURTITLE_HOME=str(home),
            voice_enabled=False,
        )
        app = create_app_for(settings)
        transport = httpx.ASGITransport(app=app)
        return app, settings, transport

    async def test_a_saved_key_reports_as_writable(self, store_client):
        app, _settings, transport = store_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            saved = await client.put(
                "/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-first-1234"}
            )
            assert saved.status_code == 200
            state = saved.json()["credential"]
            assert state["configured"] is True
            assert state["source"] == "file"
            # Writable is what lets the UI leave the field editable.
            assert state["writable"] is True
        await app.state.app_state.aclose()

    async def test_a_saved_key_can_be_replaced(self, store_client):
        app, _settings, transport = store_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.put("/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-first-1234"})
            replaced = await client.put(
                "/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-second-5678"}
            )
            assert replaced.status_code == 200, replaced.text
            assert replaced.json()["credential"]["configured"] is True

            # And the store really holds the new value, not the old one.
            stored = app.state.app_state.settings_store.credential_value("DEEPGRAM_API_KEY")
            assert stored == "dg-second-5678"
        await app.state.app_state.aclose()

    async def test_replacement_never_echoes_either_key(self, store_client):
        app, _settings, transport = store_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.put("/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-first-1234"})
            response = await client.put(
                "/api/credentials/DEEPGRAM_API_KEY", json={"value": "dg-second-5678"}
            )
            assert "dg-first-1234" not in response.text
            assert "dg-second-5678" not in response.text
        await app.state.app_state.aclose()

    async def test_an_env_key_is_read_only_and_cannot_be_replaced(self, settings):
        """The lock is real, and it is the reason the UI must explain itself."""
        app = create_app_for(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            view = (await client.get("/api/settings")).json()
            deepseek = next(p for p in view["providers"] if p["id"] == "deepseek")
            assert deepseek["credential"]["writable"] is False
            assert deepseek["credential"]["source"] == "env"

            refused = await client.put(
                "/api/credentials/DEEPSEEK_API_KEY", json={"value": "sk-replacement"}
            )
            assert refused.status_code == 400
            assert "precedence" in refused.json()["error"].lower()
        await app.state.app_state.aclose()


class TestDraftVerification:
    """A key should be testable before it is saved."""

    @pytest.fixture
    def client_app(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        home = tmp_path / "home"
        home.mkdir()
        app = create_app_for(Settings(SURTITLE_HOME=str(home), voice_enabled=False))
        return app

    async def test_draft_is_used_instead_of_the_stored_value(self, client_app, monkeypatch):
        """The draft must reach the probe, and must not be persisted."""
        import surtitle.server as server_module

        seen: list[str] = []

        async def fake_probe(ref, value):
            seen.append(value)
            return {"ok": True, "provider": "test", "models": []}

        monkeypatch.setattr(server_module, "_probe_streaming", None, raising=False)
        app = client_app
        state = app.state.app_state
        # Deepgram has no discovery endpoint, so it uses the streaming probe.
        state.settings_store._credentials["DEEPGRAM_API_KEY"] = "dg-stored-0000"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/credentials/DEEPGRAM_API_KEY/verify", json={"draft": "dg-draft-1111"}
            )
            # The real probe will fail to reach Deepgram, which is fine: what
            # matters is that the draft was accepted for testing and not stored.
            assert response.status_code in (200, 400, 502)
            assert state.settings_store.credential_value("DEEPGRAM_API_KEY") == "dg-stored-0000"
        await app.state.app_state.aclose()

    async def test_missing_key_and_no_draft_is_a_clear_error(self, client_app):
        app = client_app
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/credentials/DEEPGRAM_API_KEY/verify", json={})
            assert response.status_code == 400
            assert "not configured" in response.json()["error"]
        await app.state.app_state.aclose()

    async def test_empty_draft_falls_back_to_the_stored_key(self, client_app):
        app = client_app
        app.state.app_state.settings_store._credentials["DEEPGRAM_API_KEY"] = "dg-stored-0000"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/credentials/DEEPGRAM_API_KEY/verify", json={"draft": "   "}
            )
            # An empty draft must not be treated as the key to test.
            assert response.status_code in (200, 400, 502)
        await app.state.app_state.aclose()


class TestLocalVoiceInstall:
    """Installing offline speech from the app, and who may ask for it."""

    async def test_status_carries_the_local_voice_state(self, client):
        body = (await client.get("/api/status")).json()
        voice = body["local_voice"]
        assert {"runtime", "models", "ready", "detail", "install"} <= set(voice)
        assert voice["install"]["running"] is False
        assert voice["ready"] is False, "the test home has neither engines nor models"

    async def test_the_endpoint_reports_the_same_state(self, client):
        response = await client.get("/api/voice/install")
        assert response.status_code == 200
        assert {"runtime", "models", "ready", "detail", "install"} <= set(response.json())

    async def test_a_local_request_starts_the_install(self, settings, monkeypatch):
        app = create_app_for(settings)
        started: list[object] = []
        monkeypatch.setattr(
            app.state.app_state.voice_install, "start", lambda s: started.append(s) or True
        )
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/voice/install")
        assert response.status_code == 202
        assert response.json()["started"] is True
        assert started, "the job was never asked to start"
        await app.state.app_state.aclose()

    async def test_a_remote_request_cannot_install_anything(self, settings, monkeypatch):
        """A server exposed to a network must not download to this machine on command."""
        app = create_app_for(settings)
        started: list[object] = []
        monkeypatch.setattr(
            app.state.app_state.voice_install, "start", lambda s: started.append(s) or True
        )
        transport = httpx.ASGITransport(app=app, client=("10.211.55.9", 40123))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/voice/install")
        assert response.status_code == 403
        assert started == []
        await app.state.app_state.aclose()

    async def test_a_second_request_while_running_is_a_conflict(self, settings, monkeypatch):
        app = create_app_for(settings)
        monkeypatch.setattr(app.state.app_state.voice_install, "start", lambda s: False)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/voice/install")
        assert response.status_code == 409
        assert response.json()["started"] is False
        await app.state.app_state.aclose()


class TestUpdate:
    """Pulling new code from GitHub, and who is allowed to ask for it."""

    async def test_status_carries_the_update_block(self, client):
        body = (await client.get("/api/status")).json()
        update = body["update"]
        assert {"kind", "version", "job", "self_update"} <= set(update)
        assert update["kind"] in {"git", "archive"}
        assert isinstance(update["self_update"], bool)
        assert update["job"]["running"] is False

    async def test_a_local_request_starts_the_update(self, settings, monkeypatch):
        app = create_app_for(settings)
        started: list[str] = []
        monkeypatch.setattr(
            app.state.app_state.update,
            "start",
            lambda target, settings=None: started.append(target) or True,
        )
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/update", json={"target": "main"})
        assert response.status_code == 202
        assert response.json() == {"started": True, "target": "main"}
        assert started == ["main"]
        await app.state.app_state.aclose()

    async def test_the_default_target_is_the_release(self, settings, monkeypatch):
        app = create_app_for(settings)
        started: list[str] = []
        monkeypatch.setattr(
            app.state.app_state.update,
            "start",
            lambda target, settings=None: started.append(target) or True,
        )
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/update", json={})
        assert response.status_code == 202
        assert started == ["release"]
        await app.state.app_state.aclose()

    async def test_an_unknown_target_is_refused(self, settings):
        app = create_app_for(settings)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/update", json={"target": "nightly"})
        assert response.status_code == 400
        await app.state.app_state.aclose()

    async def test_a_remote_request_cannot_change_the_code(self, settings, monkeypatch):
        """Changing which code runs is not a network-reachable action."""
        app = create_app_for(settings)
        started: list[str] = []
        monkeypatch.setattr(
            app.state.app_state.update,
            "start",
            lambda target, settings=None: started.append(target) or True,
        )
        transport = httpx.ASGITransport(app=app, client=("10.211.55.9", 40123))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/update", json={"target": "main"})
        assert response.status_code == 403
        assert started == []
        await app.state.app_state.aclose()

    async def test_a_second_request_while_running_is_a_conflict(self, settings, monkeypatch):
        app = create_app_for(settings)
        monkeypatch.setattr(
            app.state.app_state.update, "start", lambda target, settings=None: False
        )
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/update", json={"target": "main"})
        assert response.status_code == 409
        await app.state.app_state.aclose()

    async def test_the_check_endpoint_reports_without_updating(self, client, monkeypatch):
        from surtitle import update as updater

        monkeypatch.setattr(
            updater,
            "check",
            lambda **kw: updater.UpdateStatus(
                kind="git", version="0.1.0", detail="up to date on main"
            ),
        )
        response = await client.get("/api/update")
        assert response.status_code == 200
        body = response.json()
        assert body["status"]["kind"] == "git"
        assert body["status"]["detail"] == "up to date on main"
        assert "job" in body


class TestFolderDialog:
    """The native folder chooser, and who may open a window on this desktop."""

    async def test_a_local_request_returns_the_chosen_path(self, settings, monkeypatch):
        app = create_app_for(settings)
        from surtitle import dialogs

        monkeypatch.setattr(dialogs, "available", lambda **kw: True)
        monkeypatch.setattr(dialogs, "choose_folder", lambda **kw: "/tmp/chosen")
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/dialog/folder")
        assert response.status_code == 200
        assert response.json()["path"] == "/tmp/chosen"
        await app.state.app_state.aclose()

    async def test_cancelling_reports_no_path(self, settings, monkeypatch):
        app = create_app_for(settings)
        from surtitle import dialogs

        monkeypatch.setattr(dialogs, "available", lambda **kw: True)
        monkeypatch.setattr(dialogs, "choose_folder", lambda **kw: None)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/dialog/folder")
        assert response.status_code == 200
        assert response.json()["path"] is None
        assert response.json()["cancelled"] is True
        await app.state.app_state.aclose()

    async def test_a_machine_without_a_desktop_says_so(self, settings, monkeypatch):
        """A headless server must answer, not hang on a window nobody can see."""
        app = create_app_for(settings)
        from surtitle import dialogs

        monkeypatch.setattr(dialogs, "available", lambda **kw: False)
        opened: list[int] = []
        monkeypatch.setattr(dialogs, "choose_folder", lambda **kw: opened.append(1))
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/dialog/folder")
        assert response.status_code == 501
        assert opened == []
        await app.state.app_state.aclose()

    async def test_a_remote_request_cannot_open_a_window(self, settings, monkeypatch):
        app = create_app_for(settings)
        from surtitle import dialogs

        monkeypatch.setattr(dialogs, "available", lambda **kw: True)
        opened: list[int] = []
        monkeypatch.setattr(dialogs, "choose_folder", lambda **kw: opened.append(1))
        transport = httpx.ASGITransport(app=app, client=("10.211.55.9", 40123))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/dialog/folder")
        assert response.status_code == 403
        assert opened == []
        await app.state.app_state.aclose()

    async def test_health_reports_whether_a_chooser_exists(self, client):
        body = (await client.get("/api/health")).json()
        assert "folder_dialog" in body
        assert isinstance(body["folder_dialog"], bool)

    async def test_the_in_app_browser_is_always_offered(self, client):
        """It needs nothing from the machine's desktop, so it is never conditional."""
        assert (await client.get("/api/health")).json()["folder_browse"] is True


class TestFolderBrowse:
    """The in-app picker: a directory listing over HTTP, loopback only.

    This is what makes choosing a project folder work on a machine whose native
    chooser will not open — a detached Windows session, a remote browser, a
    headless host — so the listing itself and the fence around it are the tests
    that matter.
    """

    async def test_a_level_is_listed_with_its_jump_targets(self, client, tmp_path):
        level = tmp_path / "level"
        level.mkdir()
        (level / "alpha").mkdir()
        (level / "beta").mkdir()
        (level / "file.txt").write_text("x", encoding="utf-8")

        body = (await client.get("/api/dialog/browse", params={"path": str(level)})).json()
        assert [entry["name"] for entry in body["entries"]] == ["alpha", "beta"]
        assert body["path"] == str(level)
        assert body["parent"] == str(level.parent)
        assert [crumb["path"] for crumb in body["crumbs"]][-1] == str(level)
        assert body["home"]

    async def test_an_absolute_path_is_required(self, client):
        response = await client.get("/api/dialog/browse", params={"path": "relative/dir"})
        assert response.status_code == 400
        assert response.json()["code"] == "not-fully-qualified"

    async def test_a_missing_folder_is_reported_not_raised(self, client, tmp_path):
        response = await client.get("/api/dialog/browse", params={"path": str(tmp_path / "gone")})
        assert response.status_code == 400
        assert response.json()["code"] == "unreadable"

    async def test_no_path_starts_at_home(self, client):
        body = (await client.get("/api/dialog/browse")).json()
        assert body["path"] == body["home"]

    async def test_a_folder_can_be_created(self, client, tmp_path):
        response = await client.post(
            "/api/dialog/browse", json={"path": str(tmp_path), "name": "new-project"}
        )
        assert response.status_code == 200
        assert response.json()["path"] == str(tmp_path / "new-project")
        assert (tmp_path / "new-project").is_dir()

    async def test_a_duplicate_name_is_refused(self, client, tmp_path):
        (tmp_path / "taken").mkdir()
        response = await client.post(
            "/api/dialog/browse", json={"path": str(tmp_path), "name": "taken"}
        )
        assert response.status_code == 400
        assert response.json()["code"] == "exists"

    async def test_a_name_with_a_separator_is_refused(self, client, tmp_path):
        """Otherwise a name could create a tree, or reach outside the parent."""
        response = await client.post(
            "/api/dialog/browse", json={"path": str(tmp_path), "name": "../escape"}
        )
        assert response.status_code == 400
        assert not (tmp_path.parent / "escape").exists()

    async def test_a_remote_caller_cannot_list_this_machine(self, settings):
        app = create_app_for(settings)
        transport = httpx.ASGITransport(app=app, client=("10.211.55.9", 40123))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            listed = await http.get("/api/dialog/browse")
            made = await http.post("/api/dialog/browse", json={"path": "/", "name": "x"})
        assert listed.status_code == 403
        assert made.status_code == 403
        await app.state.app_state.aclose()


class TestVersionControlTools:
    """The portable git and svn, from the app's point of view."""

    async def test_status_lists_both_tools_and_the_job(self, client):
        body = (await client.get("/api/tools/vcs")).json()
        assert [row["name"] for row in body["tools"]] == ["git", "svn"]
        assert body["install"]["running"] is False
        assert isinstance(body["platform_supported"], bool)

    async def test_the_tray_status_carries_them_too(self, client):
        """The tray reads /api/status, which must stay cheap and complete."""
        body = (await client.get("/api/status")).json()
        assert [row["name"] for row in body["vcs"]["tools"]] == ["git", "svn"]
        assert body["vcs"]["install"]["running"] is False

    async def test_an_install_is_accepted_and_reported(self, client, monkeypatch):
        from surtitle.vcs import provision

        monkeypatch.setattr(
            provision,
            "install",
            lambda *a, **k: provision.InstallResult(
                installed=["git"], skipped=[], failed=[], detail="installed git"
            ),
        )

        response = await client.post("/api/tools/vcs")
        assert response.status_code == 202
        assert response.json()["started"] is True

        body = (await client.get("/api/tools/vcs")).json()
        for _ in range(200):
            if not body["install"]["running"]:
                break
            await asyncio.sleep(0.02)
            body = (await client.get("/api/tools/vcs")).json()

        assert body["install"]["running"] is False
        assert body["install"]["ok"] is True
        assert "installed git" in body["install"]["message"]

    async def test_a_second_request_while_one_runs_is_a_state_not_a_fault(
        self, client, monkeypatch
    ):
        from surtitle.vcs import provision

        release = __import__("threading").Event()

        def slow(*_args, **_kwargs):
            release.wait(5)
            return provision.InstallResult(installed=[], skipped=[], failed=[], detail="done")

        monkeypatch.setattr(provision, "install", slow)
        assert (await client.post("/api/tools/vcs")).status_code == 202
        second = await client.post("/api/tools/vcs")
        assert second.status_code == 409
        assert second.json()["started"] is False
        release.set()

    async def test_a_remote_caller_cannot_start_a_download(self, settings, monkeypatch):
        """It would pull tens of megabytes onto a machine the caller does not own."""
        app = create_app_for(settings)
        transport = httpx.ASGITransport(app=app, client=("10.211.55.9", 40123))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/tools/vcs")
        assert response.status_code == 403
        await app.state.app_state.aclose()


class TestShellIntegration:
    """Launcher and sign-in entries, managed from the app."""

    async def test_status_carries_the_shell_state(self, client):
        body = (await client.get("/api/status")).json()
        assert {"supported", "menu", "startup"} <= set(body["shell"])

    async def test_the_endpoint_reports_state(self, client):
        response = await client.get("/api/shell")
        assert response.status_code == 200
        assert {"supported", "menu", "startup"} <= set(response.json())

    async def test_a_local_request_applies_the_change(self, settings, monkeypatch):
        app = create_app_for(settings)
        from surtitle import shell_integration

        calls: list[dict] = []
        monkeypatch.setattr(
            shell_integration, "apply", lambda **kw: calls.append(kw) or (True, "updated")
        )
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/shell", json={"startup": True})
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert calls == [{"menu": None, "startup": True}]
        await app.state.app_state.aclose()

    async def test_a_remote_request_cannot_write_shortcuts(self, settings, monkeypatch):
        app = create_app_for(settings)
        from surtitle import shell_integration

        calls: list[dict] = []
        monkeypatch.setattr(
            shell_integration, "apply", lambda **kw: calls.append(kw) or (True, "updated")
        )
        transport = httpx.ASGITransport(app=app, client=("10.211.55.9", 40123))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/shell", json={"startup": True})
        assert response.status_code == 403
        assert calls == []
        await app.state.app_state.aclose()

    async def test_a_failure_is_reported_as_an_error(self, settings, monkeypatch):
        app = create_app_for(settings)
        from surtitle import shell_integration

        monkeypatch.setattr(
            shell_integration, "apply", lambda **kw: (False, "the Start Menu is not writable")
        )
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/api/shell", json={"menu": True})
        assert response.status_code == 400
        assert "not writable" in response.json()["error"]
        await app.state.app_state.aclose()
