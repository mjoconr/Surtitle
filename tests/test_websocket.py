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

            # Wait until the model has begun, then interrupt.
            receive_until(socket, {"say"}, limit=40)
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
        assert response.json()["sections"]["model"]
