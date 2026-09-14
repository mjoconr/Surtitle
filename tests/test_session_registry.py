"""The live-session registry.

A reconnecting browser opens a second WebSocket for the same conversation before
the first has finished tearing down. Two things have to hold for that to be safe,
and neither did:

* registering the new session must retire the old one, or the old session's
  Deepgram socket and outbox task keep running — two speech pipelines for one
  conversation, so a reply can be cut off or spoken twice;
* the superseded connection's cleanup must not unregister the session that
  replaced it, or the live conversation disappears from the registry and leaks
  its voice sockets.

The user's own log showed six WebSocket connections in six seconds and pairs of
Deepgram STT connections for a single conversation, which is what this prevents.
"""

from __future__ import annotations

import pytest

from surtitle.core.session import Session, SessionManager


class FakeSession:
    """Just enough Session to observe lifecycle calls."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class TestAdd:
    async def test_a_single_session_is_registered(self):
        manager = SessionManager()
        session = FakeSession("s1")
        await manager.add(session)  # type: ignore[arg-type]
        assert manager.get("s1") is session
        assert manager.count == 1

    async def test_registering_a_reconnect_retires_the_previous_one(self):
        manager = SessionManager()
        first, second = FakeSession("s1"), FakeSession("s1")

        await manager.add(first)  # type: ignore[arg-type]
        await manager.add(second)  # type: ignore[arg-type]

        assert first.closed == 1, "the orphaned session kept its voice sockets open"
        assert second.closed == 0
        assert manager.get("s1") is second
        assert manager.count == 1

    async def test_two_conversations_coexist(self):
        manager = SessionManager()
        a, b = FakeSession("s1"), FakeSession("s2")
        await manager.add(a)  # type: ignore[arg-type]
        await manager.add(b)  # type: ignore[arg-type]
        assert manager.count == 2
        assert a.closed == 0 and b.closed == 0

    async def test_re_adding_the_same_instance_is_harmless(self):
        manager = SessionManager()
        session = FakeSession("s1")
        await manager.add(session)  # type: ignore[arg-type]
        await manager.add(session)  # type: ignore[arg-type]
        assert session.closed == 0, "a session must not retire itself"
        assert manager.get("s1") is session


class TestRemove:
    async def test_it_closes_and_forgets(self):
        manager = SessionManager()
        session = FakeSession("s1")
        await manager.add(session)  # type: ignore[arg-type]

        await manager.remove("s1")

        assert session.closed == 1
        assert manager.get("s1") is None

    async def test_removing_an_unknown_id_is_harmless(self):
        manager = SessionManager()
        await manager.remove("nope")
        assert manager.count == 0

    async def test_a_superseded_handler_cannot_evict_the_new_session(self):
        """The old connection's cleanup must leave the new one alone."""
        manager = SessionManager()
        old, new = FakeSession("s1"), FakeSession("s1")
        await manager.add(old)  # type: ignore[arg-type]
        await manager.add(new)  # type: ignore[arg-type]

        # The old WebSocket handler now finishes and runs its cleanup.
        await manager.remove("s1", session=old)  # type: ignore[arg-type]

        assert manager.get("s1") is new, "the live conversation was unregistered"
        assert new.closed == 0
        assert manager.count == 1

    async def test_the_owning_handler_still_removes_its_session(self):
        manager = SessionManager()
        session = FakeSession("s1")
        await manager.add(session)  # type: ignore[arg-type]

        await manager.remove("s1", session=session)  # type: ignore[arg-type]

        assert manager.get("s1") is None
        assert session.closed == 1

    async def test_pinned_removal_of_an_already_removed_session_is_harmless(self):
        manager = SessionManager()
        session = FakeSession("s1")
        await manager.add(session)  # type: ignore[arg-type]
        await manager.remove("s1", session=session)  # type: ignore[arg-type]

        await manager.remove("s1", session=session)  # type: ignore[arg-type]

        assert session.closed == 1, "it must not be closed twice"
        assert manager.count == 0

    async def test_close_all_empties_the_registry(self):
        manager = SessionManager()
        for index in range(3):
            await manager.add(FakeSession(f"s{index}"))  # type: ignore[arg-type]

        await manager.close_all()

        assert manager.count == 0


class TestRealSessionCleanup:
    """`close()` on a real Session must be safe to call twice."""

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

    async def test_closing_twice_does_not_raise(self, session):
        await session.close()
        await session.close()

    async def test_the_registry_closes_a_real_session(self, session):
        manager = SessionManager()
        await manager.add(session)
        await manager.remove(session.session_id)
        assert manager.get(session.session_id) is None
