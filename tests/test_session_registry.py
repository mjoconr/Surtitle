"""The live-session registry.

A page open in two tabs, or a browser that reconnects, opens a second WebSocket
for a conversation that is already live. Getting this wrong is expensive, and it
was wrong twice over:

* the first version simply overwrote the registry entry, orphaning the previous
  session with its Deepgram socket and outbox task still running;
* the second version retired the previous session, which is worse when a turn is
  in flight — the turn was cancelled and its answer went to a socket that had
  just been closed. Two tabs then destroyed each other in a loop, each retirement
  provoking a reconnect. The user's log showed six connections in six seconds,
  and the symptom was a spoken question being transcribed, sent, and answered
  into nothing: "voice is converting but no action".

The registry now reuses the live session and rebinds its transport, so a
reconnect costs nothing and the running turn survives.
"""

from __future__ import annotations

import asyncio

import pytest

from surtitle.core.session import Session, SessionManager


class FakeSession:
    """Just enough Session to observe lifecycle and rebinding."""

    def __init__(self, session_id: str, *, label: str = "") -> None:
        self.session_id = session_id
        self.label = label
        self.closed = 0
        self.rebound = 0
        # Whether a turn is running. A session with work in flight must survive its
        # connection leaving, so the registry asks before closing it.
        self.in_flight = False
        # The transport the registry hands over on rebind.
        self.send = f"{label or session_id}-transport"
        self.send_audio = f"{label or session_id}-audio"

    def _turn_in_flight(self) -> bool:
        return self.in_flight

    def rebind(self, *, send, send_audio) -> None:
        self.rebound += 1
        self.send = send
        self.send_audio = send_audio

    async def close(self) -> None:
        self.closed += 1


class TestAcquire:
    async def test_a_new_conversation_starts_its_session(self):
        manager = SessionManager()
        session = FakeSession("s1")
        chosen, started = await manager.acquire(session, "conn-1")
        assert chosen is session
        assert started is True
        assert manager.get("s1") is session
        assert manager.count == 1

    async def test_a_reconnect_reuses_the_live_session(self):
        manager = SessionManager()
        live = FakeSession("s1", label="live")
        await manager.acquire(live, "conn-1")

        reconnecting = FakeSession("s1", label="new-connection")
        chosen, started = await manager.acquire(reconnecting, "conn-2")

        assert chosen is live, "the in-flight turn's session must be kept"
        assert started is False, "it must not be started twice"
        assert live.closed == 0, "closing it here is what destroyed the answer"

    async def test_a_reconnect_moves_the_transport_to_the_new_connection(self):
        manager = SessionManager()
        live = FakeSession("s1")
        await manager.acquire(live, "conn-1")

        reconnecting = FakeSession("s1")
        await manager.acquire(reconnecting, "conn-2")

        assert live.rebound == 1
        # The session now speaks to the new connection, not the dead one.
        assert live.send == reconnecting.send
        assert live.send_audio == reconnecting.send_audio

    async def test_two_conversations_coexist(self):
        manager = SessionManager()
        a, b = FakeSession("s1"), FakeSession("s2")
        await manager.acquire(a, "c1")
        await manager.acquire(b, "c2")
        assert manager.count == 2
        assert a.closed == 0 and b.closed == 0

    async def test_two_tabs_do_not_destroy_each_other(self):
        """The loop that made the same voice appear to stutter and stop."""
        manager = SessionManager()
        tab_a = FakeSession("s1")
        tab_b = FakeSession("s1")

        await manager.acquire(tab_a, "tab-a")
        _chosen_b, started_b = await manager.acquire(tab_b, "tab-b")
        # Tab A's old socket closing must not tear the shared session down.
        await manager.release("s1", "tab-a")

        assert tab_a.closed == 0, "one tab's disconnect must not kill the session"
        assert started_b is False
        assert manager.get("s1") is tab_a
        assert manager.count == 1


class TestRelease:
    async def test_the_owner_releases_and_closes(self):
        manager = SessionManager()
        session = FakeSession("s1")
        await manager.acquire(session, "conn-1")

        await manager.release("s1", "conn-1")

        assert session.closed == 1
        assert manager.get("s1") is None

    async def test_a_superseded_connection_cannot_close_the_session(self):
        manager = SessionManager()
        live = FakeSession("s1")
        await manager.acquire(live, "conn-1")
        await manager.acquire(FakeSession("s1"), "conn-2")

        # The stale tab's handler finally runs.
        await manager.release("s1", "conn-1")

        assert live.closed == 0
        assert manager.get("s1") is live, "the live conversation was unregistered"

    async def test_releasing_twice_is_harmless(self):
        manager = SessionManager()
        session = FakeSession("s1")
        await manager.acquire(session, "conn-1")

        await manager.release("s1", "conn-1")
        await manager.release("s1", "conn-1")

        assert session.closed == 1

    async def test_releasing_an_unknown_session_is_harmless(self):
        manager = SessionManager()
        await manager.release("nope", "conn-1")
        assert manager.count == 0


class TestWorkOutlivesTheConnection:
    """A disconnect must not destroy a turn that is still running.

    Observed: a browser reload disconnects before it reconnects, and the release
    path closed the session, cancelling the turn. The reloaded page then found
    nothing to reclaim and started an empty session — so reloading while the agent
    was working threw the work away, and a prompt waiting for an answer became
    unanswerable because the question only ever existed on the screen that went
    away.
    """

    async def test_an_in_flight_turn_survives_the_socket_closing(self):
        manager = SessionManager()
        session = FakeSession("s1")
        session.in_flight = True
        await manager.acquire(session, "conn-1")

        await manager.release("s1", "conn-1")

        assert session.closed == 0, "the running turn was cancelled by a disconnect"
        assert manager.get("s1") is session, "the work was forgotten, so it cannot be reclaimed"
        await manager.close_all()

    async def test_a_reload_reclaims_the_running_session(self):
        manager = SessionManager()
        live = FakeSession("s1", label="live")
        live.in_flight = True
        await manager.acquire(live, "conn-1")
        await manager.release("s1", "conn-1")

        chosen, started = await manager.acquire(FakeSession("s1"), "conn-2")

        assert chosen is live, "the reload was handed a fresh session and lost the turn"
        assert started is False
        assert live.rebound == 1
        await manager.close_all()

    async def test_an_idle_session_is_still_closed_immediately(self):
        manager = SessionManager()
        session = FakeSession("s1")
        await manager.acquire(session, "conn-1")

        await manager.release("s1", "conn-1")

        assert session.closed == 1, "an idle session must not be kept alive forever"
        assert manager.count == 0

    async def test_an_orphan_is_swept_once_it_stops_working(self, monkeypatch):
        monkeypatch.setattr("surtitle.core.session._ORPHAN_SWEEP_SECONDS", 0.01)
        monkeypatch.setattr("surtitle.core.session._ORPHAN_GRACE_SECONDS", 5.0)
        manager = SessionManager()
        session = FakeSession("s1")
        session.in_flight = True
        await manager.acquire(session, "conn-1")
        await manager.release("s1", "conn-1")
        assert session.closed == 0

        session.in_flight = False
        for _ in range(50):
            await asyncio.sleep(0.01)
            if session.closed:
                break

        assert session.closed == 1, "an orphan that finished its work was never reclaimed"
        assert manager.count == 0
        await manager.close_all()

    async def test_a_hung_turn_is_not_kept_forever(self, monkeypatch):
        """The grace period bounds how long a turn that never ends holds sockets."""
        monkeypatch.setattr("surtitle.core.session._ORPHAN_SWEEP_SECONDS", 0.01)
        monkeypatch.setattr("surtitle.core.session._ORPHAN_GRACE_SECONDS", 0.0)
        manager = SessionManager()
        session = FakeSession("s1")
        session.in_flight = True
        await manager.acquire(session, "conn-1")
        await manager.release("s1", "conn-1")

        for _ in range(50):
            await asyncio.sleep(0.01)
            if session.closed:
                break

        assert session.closed == 1, "a session that never goes idle was leaked"
        await manager.close_all()


class TestForceRemove:
    """Archiving or deleting a conversation must stop it listening."""

    async def test_remove_closes_whoever_owns_it(self):
        manager = SessionManager()
        live = FakeSession("s1")
        await manager.acquire(live, "conn-1")
        await manager.acquire(FakeSession("s1"), "conn-2")

        await manager.remove("s1")

        assert live.closed == 1
        assert manager.get("s1") is None

    async def test_close_all_empties_the_registry(self):
        manager = SessionManager()
        for index in range(3):
            await manager.acquire(FakeSession(f"s{index}"), f"c{index}")

        await manager.close_all()

        assert manager.count == 0


class TestRealSession:
    @pytest.fixture
    def session(self, tmp_path):
        from surtitle.config import Settings
        from surtitle.store.db import Store

        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        async def send(_payload):
            return None

        async def send_audio(_data):
            return None

        return Session(
            session_id=record.id,
            project_id=project.id,
            root=tmp_path,
            settings=Settings(
                DEEPSEEK_API_KEY="sk-test",
                SURTITLE_HOME=str(tmp_path),
                voice_enabled=False,
            ),
            store=store,
            deepseek=None,
            send=send,
            send_audio=send_audio,
        )

    async def test_rebinding_replaces_the_transport(self, session):
        calls: list[bytes] = []

        async def new_audio(data: bytes) -> None:
            calls.append(data)

        async def new_send(_payload) -> None:
            return None

        session.rebind(send=new_send, send_audio=new_audio)
        await session.send_audio(b"pcm")

        assert calls == [b"pcm"]

    async def test_closing_twice_does_not_raise(self, session):
        await session.close()
        await session.close()

    async def test_the_registry_closes_a_real_session(self, session):
        manager = SessionManager()
        await manager.acquire(session, "conn-1")
        await manager.remove(session.session_id)
        assert manager.get(session.session_id) is None
