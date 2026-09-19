"""End-to-end tests for the WebSocket conversation channel.

These drive a real server over a real socket, so they cover the handshake, the
binary audio framing, and — importantly — that barge-in cancels an in-flight
model call rather than leaving it running.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from surtitle.config import Settings
from surtitle.llm.deepseek import StreamEvent, Usage
from surtitle.server import create_app_for


def make_settings(tmp_path, **overrides) -> Settings:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    values = {
        "DEEPSEEK_API_KEY": "sk-test",
        "DEEPGRAM_API_KEY": "dg-test",
        "SURTITLE_HOME": str(home),
        "voice_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


class ScriptedClient:
    """A model that replays a script, with a gate so a turn can be paused."""

    def __init__(self, scripts: list[list[StreamEvent]], *, block: bool = False) -> None:
        self.scripts = scripts
        self.calls = 0
        self.started = False
        self.release = None
        self._block = block

    async def stream(self, messages, *, tools=None) -> AsyncIterator[StreamEvent]:
        index = min(self.calls, len(self.scripts) - 1)
        self.calls += 1
        self.started = True
        for event in self.scripts[index]:
            yield event
        if self._block:
            import asyncio

            # Simulate a slow generation so cancellation can be observed.
            self.release = asyncio.get_running_loop().create_future()
            await self.release

    async def aclose(self) -> None:
        return None


def text_script(*deltas: str) -> list[StreamEvent]:
    return [StreamEvent(kind="text", text=d) for d in deltas] + [
        StreamEvent(kind="usage", usage=Usage()),
        StreamEvent(kind="done"),
    ]


@pytest.fixture
def setup(tmp_path):
    """A running app plus a project and session to talk into."""
    settings = make_settings(tmp_path)
    app = create_app_for(settings)
    with TestClient(app) as client:
        state = app.state.app_state
        root = tmp_path / "proj"
        root.mkdir()
        (root / "notes.txt").write_text("hello world", encoding="utf-8")
        project = state.store.create_project("Test", root)
        session = state.store.create_session(project.id)
        yield client, app, project, session


def hello(project_id: str, session_id: str) -> str:
    return json.dumps(
        {"kind": "hello", "data": {"project_id": project_id, "session_id": session_id}}
    )


def receive_until(socket, kinds: set[str], limit: int = 60) -> list[dict]:
    """Collect events until one of ``kinds`` arrives."""
    seen: list[dict] = []
    for _ in range(limit):
        payload = json.loads(socket.receive_text())
        seen.append(payload)
        if payload.get("kind") in kinds:
            break
    return seen


class TestHandshake:
    def test_hello_yields_ready(self, setup):
        client, _app, project, session = setup
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            events = receive_until(socket, {"ready"}, limit=5)
            ready = next(e for e in events if e["kind"] == "ready")
            assert ready["data"]["project_id"] == project.id
            assert ready["data"]["session_id"] == session.id

    def test_unknown_project_is_reported(self, setup):
        client, _app, _project, session = setup
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello("missing", session.id))
            events = receive_until(socket, {"error"}, limit=5)
            assert any(e["kind"] == "error" for e in events)

    def test_non_hello_first_message_is_rejected(self, setup):
        client, _app, _project, _session = setup
        with client.websocket_connect("/ws") as socket:
            socket.send_text(json.dumps({"kind": "text", "data": {"text": "hi"}}))
            events = receive_until(socket, {"error"}, limit=5)
            assert any("hello" in (e["data"].get("message") or "").lower() for e in events)

    def test_unknown_command_kind_is_reported(self, setup):
        client, _app, project, session = setup
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "nonsense", "data": {}}))
            events = receive_until(socket, {"error"}, limit=5)
            assert any("unknown command" in (e["data"].get("message") or "") for e in events)

    def test_ping_answers(self, setup):
        client, _app, project, session = setup
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "ping", "data": {}}))
            events = receive_until(socket, {"ready"}, limit=5)
            assert any(e["data"].get("pong") for e in events)

    def test_audio_before_hello_is_ignored(self, setup):
        client, _app, project, session = setup
        with client.websocket_connect("/ws") as socket:
            # 0x01 opcode + PCM; must not raise on the server.
            socket.send_bytes(bytes([0x01]) + b"\x00" * 320)
            socket.send_text(hello(project.id, session.id))
            events = receive_until(socket, {"ready"}, limit=5)
            assert any(e["kind"] == "ready" for e in events)


class TestConversation:
    def test_typed_turn_streams_say_and_display(self, setup):
        client, app, project, session = setup
        app.state.app_state.deepseek = ScriptedClient(
            [text_script("<say>Hello there.</say><display>on screen only</display>")]
        )

        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "text", "data": {"text": "hi"}}))

            events = receive_until(socket, {"done"}, limit=80)

        # Display text streams in chunks, so reassemble before asserting.
        said = "".join(e["data"]["text"] for e in events if e["kind"] == "say")
        shown = "".join(e["data"]["text"] for e in events if e["kind"] == "agent_text")

        assert "Hello there." in said
        assert "on screen only" in shown
        # The whole point of the speak layer: display text is never spoken.
        assert "on screen only" not in said
        assert "Hello there." not in shown

    def test_transcript_is_persisted(self, setup):
        client, app, project, session = setup
        app.state.app_state.deepseek = ScriptedClient([text_script("<say>Stored reply.</say>")])

        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "text", "data": {"text": "remember this"}}))
            receive_until(socket, {"done"}, limit=80)

        messages = app.state.app_state.store.list_messages(session.id)
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[0].content == "remember this"
        assert messages[1].spoken and "Stored reply." in messages[1].spoken

    def test_the_run_counters_follow_a_real_turn(self, setup):
        """The tray icon's numbers come from here, so they must be wired up.

        The counters are fed from the two emit paths in the session — the turn
        stream and the side channel — and a test that only called the counter
        directly would not notice either path being missed.
        """
        client, app, project, session = setup
        app.state.app_state.deepseek = ScriptedClient(
            [
                [
                    StreamEvent(kind="text", text="<say>Counted.</say>"),
                    StreamEvent(
                        kind="usage",
                        usage=Usage(prompt_tokens=1200, completion_tokens=300, cached_tokens=800),
                    ),
                    StreamEvent(kind="done"),
                ]
            ]
        )

        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "text", "data": {"text": "hello"}}))
            receive_until(socket, {"done"}, limit=80)

        usage = app.state.app_state.stats.snapshot()
        assert usage["turns"] == 1
        assert usage["model_calls"] == 1
        assert usage["prompt_tokens"] == 1200
        assert usage["completion_tokens"] == 300
        assert usage["cached_tokens"] == 800
        assert usage["total_tokens"] == 1500
        # The model is priced, so a cost must have been accumulated rather than
        # the run being flagged as unpriced.
        assert usage["priced"] is True
        assert usage["cost_usd"] > 0

    def test_first_message_titles_the_session(self, setup):
        client, app, project, session = setup
        app.state.app_state.deepseek = ScriptedClient([text_script("<say>ok</say>")])

        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(
                json.dumps({"kind": "text", "data": {"text": "Summarise the Q3 report"}})
            )
            receive_until(socket, {"done"}, limit=80)

        stored = app.state.app_state.store.get_session(session.id)
        assert stored.title == "Summarise the Q3 report"

    def test_model_error_is_surfaced(self, setup):
        client, app, project, session = setup

        class Broken:
            async def stream(self, messages, *, tools=None):
                from surtitle.llm.deepseek import DeepSeekError

                raise DeepSeekError("The DeepSeek API rejected the key.")
                yield  # pragma: no cover

            async def aclose(self):
                return None

        app.state.app_state.deepseek = Broken()

        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "text", "data": {"text": "hi"}}))
            events = receive_until(socket, {"done"}, limit=40)

        assert any(e["kind"] == "error" for e in events)
        assert any(e["kind"] == "done" for e in events)

    def test_empty_text_is_ignored(self, setup):
        client, app, project, session = setup
        app.state.app_state.deepseek = ScriptedClient([text_script("<say>hi</say>")])

        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "text", "data": {"text": "   "}}))
            # A ping response proves the loop is still healthy and nothing ran.
            socket.send_text(json.dumps({"kind": "ping", "data": {}}))
            events = receive_until(socket, {"ready"}, limit=10)

        assert not any(e["kind"] == "user_text" for e in events)
        assert any(e["data"].get("pong") for e in events)


class SlowClient:
    """Emits one spoken sentence, then pauses before finishing the turn.

    The pause is what makes the reconnect happen *during* the answer, which is the
    situation that used to lose it.
    """

    def __init__(self, *, pause: float = 2.0) -> None:
        self.pause = pause
        self.started = False

    async def stream(self, messages, *, tools=None):
        import asyncio

        self.started = True
        yield StreamEvent(kind="text", text="<say>The iodine service is up.</say>")
        await asyncio.sleep(self.pause)
        yield StreamEvent(kind="text", text="<say> All four machines report it.</say>")
        yield StreamEvent(kind="usage", usage=Usage())
        yield StreamEvent(kind="done")

    async def aclose(self) -> None:
        return None


class TestReconnectDuringATurn:
    """A second connection mid-answer must not cost the user the answer.

    This is the "voice is converting but no action" report. A page open in a
    second tab (or any reconnect) opened another WebSocket for a conversation
    that was already live. Registering it replaced — and closed — the session
    running the turn, so the reply was generated and then delivered to a socket
    that no longer existed. The browser showed the question with no answer.

    The live session is now reused and its transport rebound to the new socket,
    so the answer finishes where the user is now looking.
    """

    def test_the_rest_of_the_answer_arrives_on_the_new_connection(self, setup):
        client, app, project, session = setup
        slow = SlowClient(pause=2.0)
        app.state.app_state.deepseek = slow

        with client.websocket_connect("/ws") as first:
            first.send_text(hello(project.id, session.id))
            receive_until(first, {"ready"}, limit=5)
            first.send_text(json.dumps({"kind": "text", "data": {"text": "uptime of IOD?"}}))
            # The turn is genuinely in flight once the first sentence appears.
            opening = receive_until(first, {"say"}, limit=40)
            assert any(e["kind"] == "say" for e in opening)
            live = app.state.app_state.sessions.get(session.id)
            assert live is not None

            with client.websocket_connect("/ws") as second:
                second.send_text(hello(project.id, session.id))
                ready = receive_until(second, {"ready"}, limit=10)
                assert any(e["kind"] == "ready" for e in ready)
                # Same session, still running the same turn.
                assert app.state.app_state.sessions.get(session.id) is live

                events = receive_until(second, {"done"}, limit=120)

        said = "".join(e["data"]["text"] for e in events if e["kind"] == "say")
        assert "All four machines report it." in said, (
            "the rest of the answer was thrown away with the old connection"
        )
        assert any(e["kind"] == "done" for e in events)

    def test_a_reconnect_reuses_the_session_rather_than_starting_another(self, setup):
        client, app, project, session = setup
        app.state.app_state.deepseek = SlowClient(pause=1.0)

        with client.websocket_connect("/ws") as first:
            first.send_text(hello(project.id, session.id))
            receive_until(first, {"ready"}, limit=5)
            first.send_text(json.dumps({"kind": "text", "data": {"text": "hi"}}))
            receive_until(first, {"say"}, limit=40)
            manager = app.state.app_state.sessions
            live = manager.get(session.id)
            assert live is not None

            with client.websocket_connect("/ws") as second:
                second.send_text(hello(project.id, session.id))
                receive_until(second, {"ready"}, limit=10)
                assert manager.get(session.id) is live, "a second session was created"
                assert manager.count == 1

    def test_the_reused_session_reports_itself_as_resumed(self, setup):
        client, _app, project, session = setup
        with client.websocket_connect("/ws") as first:
            first.send_text(hello(project.id, session.id))
            receive_until(first, {"ready"}, limit=5)

            with client.websocket_connect("/ws") as second:
                second.send_text(hello(project.id, session.id))
                events = receive_until(second, {"ready"}, limit=10)

        ready = next(e for e in events if e["kind"] == "ready")
        # The browser was away and missed events, so the session restates itself.
        assert ready["data"]["resumed"] is True
        assert ready["data"]["state"]

    def test_a_superseded_connection_does_not_close_the_live_session(self, setup):
        """The stale tab's handler runs last and must leave the session alone."""
        client, app, project, session = setup
        app.state.app_state.deepseek = ScriptedClient([text_script("<say>ok</say>")])
        manager = app.state.app_state.sessions

        # Managed by hand so the *first* connection can be closed while the second
        # is still open — which is the ordering that used to kill the session.
        first = client.websocket_connect("/ws")
        first.__enter__()
        second = client.websocket_connect("/ws")
        second.__enter__()
        try:
            first.send_text(hello(project.id, session.id))
            receive_until(first, {"ready"}, limit=5)
            live = manager.get(session.id)
            assert live is not None

            second.send_text(hello(project.id, session.id))
            receive_until(second, {"ready"}, limit=10)
            assert manager.get(session.id) is live

            # The superseded connection now disconnects and runs its cleanup.
            first.__exit__(None, None, None)

            assert manager.get(session.id) is live, "the live session was unregistered"
        finally:
            second.__exit__(None, None, None)


class TestBargeIn:
    def test_barge_in_cancels_an_in_flight_turn(self, setup):
        """A cancelled turn must stop generating, not run to completion."""
        client, app, project, session = setup
        scripted = ScriptedClient([text_script("<say>This is a long reply.</say>")], block=True)
        app.state.app_state.deepseek = scripted

        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "text", "data": {"text": "go"}}))

            # Wait until the model has begun, then interrupt. A real interruption
            # carries transcribed speech; that is what the server now requires.
            receive_until(socket, {"say"}, limit=40)
            live = app.state.app_state.sessions.get(session.id)
            assert live is not None
            live._speaking = True
            live._speech_since_playback = True
            socket.send_text(json.dumps({"kind": "barge_in", "data": {}}))

            # Drain until the turn admits it was stopped. Reading a fixed
            # number of frames would block once the socket goes quiet.
            events = receive_until(socket, {"state"}, limit=40)

        stopped = [
            e for e in events if e["kind"] == "state" and e["data"].get("reason") == "stopped"
        ]
        assert stopped, f"barge-in did not stop the turn: {events}"
        # And the turn must genuinely end, not linger awaiting the model.
        assert not any(e["kind"] == "done" for e in events), "cancelled turn still completed"

    def test_cancel_with_no_active_turn_is_harmless(self, setup):
        client, _app, project, session = setup
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "barge_in", "data": {}}))
            socket.send_text(json.dumps({"kind": "ping", "data": {}}))
            events = receive_until(socket, {"ready"}, limit=20)
            assert any(e["data"].get("pong") for e in events)


class TestApprovalOverTheWire:
    def test_tool_marked_read_only_never_prompts(self, setup):
        client, app, project, session = setup
        app.state.app_state.deepseek = ScriptedClient([text_script("<say>Done.</say>")])

        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text(json.dumps({"kind": "text", "data": {"text": "hi"}}))
            events = receive_until(socket, {"done"}, limit=60)

        assert not any(e["kind"] == "approval_request" for e in events)

    def test_trusted_tools_are_reported_in_ready(self, setup):
        client, app, project, session = setup
        app.state.app_state.store.set_auto_approved(project.id, ["write_file"])

        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            events = receive_until(socket, {"ready"}, limit=5)

        ready = next(e for e in events if e["kind"] == "ready")
        assert "write_file" in ready["data"]["trusted_tools"]


class TestAudioFraming:
    def test_binary_audio_frames_are_accepted(self, setup):
        client, _app, project, session = setup
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            # 20 ms of silence at 16 kHz mono PCM16.
            socket.send_bytes(bytes([0x01]) + b"\x00" * 640)
            socket.send_text(json.dumps({"kind": "ping", "data": {}}))
            events = receive_until(socket, {"ready"}, limit=10)
            assert any(e["data"].get("pong") for e in events)

    def test_unknown_frame_opcode_is_ignored(self, setup):
        client, _app, project, session = setup
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_bytes(bytes([0x7F]) + b"garbage")
            socket.send_text(json.dumps({"kind": "ping", "data": {}}))
            events = receive_until(socket, {"ready"}, limit=10)
            assert any(e["data"].get("pong") for e in events)

    def test_malformed_json_is_ignored(self, setup):
        client, _app, project, session = setup
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            receive_until(socket, {"ready"}, limit=5)
            socket.send_text("{not json")
            socket.send_text(json.dumps({"kind": "ping", "data": {}}))
            events = receive_until(socket, {"ready"}, limit=10)
            assert any(e["data"].get("pong") for e in events)


class TestUnconfiguredStartup:
    """The app must be usable enough to fix its own configuration.

    Credentials are entered in the app's own Settings screen, so refusing to
    serve when they are missing would be a deadlock.
    """

    @pytest.fixture
    def bare(self, tmp_path):
        settings = Settings(
            SURTITLE_HOME=str(tmp_path / "home"),
            voice_enabled=False,
        )
        app = create_app_for(settings)
        with TestClient(app) as client:
            state = app.state.app_state
            root = tmp_path / "proj"
            root.mkdir()
            project = state.store.create_project("Unconfigured", root)
            session = state.store.create_session(project.id)
            yield client, app, project, session

    def test_serves_the_ui_without_any_credentials(self, bare):
        client, _app, _project, _session = bare
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/settings").status_code == 200

    def test_health_reports_both_keys_missing(self, bare):
        client, _app, _project, _session = bare
        body = client.get("/api/health").json()
        assert body["deepseek_configured"] is False
        assert body["deepgram_configured"] is False

    def test_a_session_can_still_be_opened(self, bare):
        """The socket must connect, because it is how the user reaches Settings."""
        client, _app, project, session = bare
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            events = receive_until(socket, {"ready"}, limit=10)
            assert any(e["kind"] == "ready" for e in events)

    def test_the_missing_key_is_reported_as_recoverable(self, bare):
        client, _app, project, session = bare
        with client.websocket_connect("/ws") as socket:
            socket.send_text(hello(project.id, session.id))
            events = receive_until(socket, {"error"}, limit=10)
            error = next((e for e in events if e["kind"] == "error"), None)
            assert error is not None, f"no notice was sent: {events}"
            assert error["data"]["kind_detail"] == "not_configured"
            assert error["data"]["recoverable"] is True
            assert "Settings" in error["data"]["message"]

    def test_settings_can_be_saved_while_unconfigured(self, bare):
        client, _app, _project, _session = bare
        response = client.put("/api/settings", json={"reasoning_effort": "high"})
        assert response.status_code == 200
        assert response.json()["sections"]["llm"]


class TestReadyReportsTheVoiceEngines:
    """The `ready` payload is how the UI explains a voice configuration.

    A configured engine that cannot start is the hardest voice failure to
    diagnose from the outside, so the reason and the fix travel with the session
    rather than only appearing in a log the user will not read.
    """

    def test_ready_names_both_engines(self, tmp_path):
        settings = make_settings(tmp_path, voice_enabled=True)
        app = create_app_for(settings)
        with TestClient(app) as client:
            state = app.state.app_state
            root = tmp_path / "proj-engines"
            root.mkdir()
            project = state.store.create_project("Engines", root)
            session = state.store.create_session(project.id)
            with client.websocket_connect("/ws") as socket:
                socket.send_text(hello(project.id, session.id))
                events = receive_until(socket, {"ready"}, limit=5)
            ready = next(e for e in events if e["kind"] == "ready")["data"]
            assert ready["voice_backends"] == {"stt": "deepgram", "tts": "deepgram"}

    def test_a_local_model_that_is_missing_explains_itself(self, tmp_path):
        settings = make_settings(
            tmp_path,
            voice_enabled=True,
            SURTITLE_STT_BACKEND="local",
            SURTITLE_TTS_BACKEND="local",
            SURTITLE_MODELS_DIR=str(tmp_path / "no-models-here"),
        )
        app = create_app_for(settings)
        with TestClient(app) as client:
            state = app.state.app_state
            root = tmp_path / "proj-local"
            root.mkdir()
            project = state.store.create_project("Local", root)
            session = state.store.create_session(project.id)
            with client.websocket_connect("/ws") as socket:
                socket.send_text(hello(project.id, session.id))
                events = receive_until(socket, {"ready"}, limit=5)
            ready = next(e for e in events if e["kind"] == "ready")["data"]

        assert ready["voice_enabled"] is False, "voice must not claim to work"
        assert ready["voice_problem"], "the reason must travel with the session"
        assert "local stt model" in ready["voice_problem"]
        assert ready["voice_fix"] and "models download" in ready["voice_fix"]

    def test_a_missing_local_voice_does_not_remove_hosted_recognition(self, tmp_path):
        """Half a pipeline is strictly better than none."""
        settings = make_settings(
            tmp_path,
            voice_enabled=True,
            SURTITLE_TTS_BACKEND="local",
            SURTITLE_MODELS_DIR=str(tmp_path / "no-models-here"),
        )
        app = create_app_for(settings)
        with TestClient(app) as client:
            state = app.state.app_state
            root = tmp_path / "proj-half"
            root.mkdir()
            project = state.store.create_project("Half", root)
            session = state.store.create_session(project.id)
            with client.websocket_connect("/ws") as socket:
                socket.send_text(hello(project.id, session.id))
                events = receive_until(socket, {"ready"}, limit=5)
            ready = next(e for e in events if e["kind"] == "ready")["data"]

        # Recognition is hosted and has a key, so it is present; only the spoken
        # reply is missing, and the session says so rather than going silent.
        assert ready["voice_problem"]
        assert ready["voice_backends"]["stt"] == "deepgram"


class TestAClosedSessionDoesNotKeepItsSocket:
    """A session that has been closed must not go on being talked to.

    Reported as "I just restarted but it seems broken, text and voice", where a
    page refresh fixed it. The session underneath had been torn down — its engines
    stopped, and `emit` drops everything — but this connection kept dispatching
    into the dead object, so a turn ran, stored its work, and reached neither the
    screen nor the speaker. Nothing was logged and nothing was said.

    Ending the connection is what turns that into a reconnect.
    """

    async def test_the_pump_stops_for_a_closed_session(self):
        from surtitle.server import _pump

        class Socket:
            def __init__(self):
                self.reads = 0

            async def receive(self):
                self.reads += 1
                return {"type": "websocket.receive", "text": '{"kind": "ping"}'}

        class Closed:
            closed = True

        socket = Socket()
        await _pump(socket, Closed())  # type: ignore[arg-type]

        assert socket.reads == 0, "a closed session must not be handed more work"

    async def test_the_pump_still_runs_until_the_client_leaves(self):
        from surtitle.server import _pump

        class Socket:
            def __init__(self):
                self.reads = 0

            async def receive(self):
                self.reads += 1
                return {"type": "websocket.disconnect"}

        class Live:
            closed = False

        socket = Socket()
        await _pump(socket, Live())  # type: ignore[arg-type]

        assert socket.reads == 1, "an open session is served until the socket closes"

    def test_closing_a_session_is_recorded(self):
        """Two silent closes made this take reading the log by hand to place."""
        import inspect

        from surtitle.core.session import Session, SessionManager

        assert "self._closed = True" in inspect.getsource(Session.close)
        assert "session closed" in inspect.getsource(Session.close)
        assert "closing session" in inspect.getsource(SessionManager.release)
