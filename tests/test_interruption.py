"""Tests for interruption handling and interrupted-turn recording.

Both behaviours come from a real session:

* Replies were cut off mid-sentence and two user utterances were stored with no
  answer. The client detects loudness for barge-in, and loudness cannot tell the
  user from the agent's own speakers — so the agent's voice was interrupting it.
* The conversation then appeared to reset. Interrupted turns were never recorded
  at all: the user's message was stored while the assistant's reply was not, so
  every interrupted exchange vanished from the model's history.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from surtitle.config import Settings
from surtitle.core.session import Session
from surtitle.store.db import Store


class FakeTts:
    """Records barge-in without touching the network."""

    def __init__(self) -> None:
        self.barged = 0
        self.is_speaking = True

    async def barge_in(self) -> None:
        self.barged += 1
        self.is_speaking = False


class FakeStt:
    def __init__(self) -> None:
        self.suppressed = None

    def set_suppression(self, value: bool) -> None:
        self.suppressed = value

    async def finish_utterance(self) -> bool:
        return False

    def stop(self) -> None:  # pragma: no cover - not exercised here
        return None


@pytest.fixture
def live_session(tmp_path: Path):
    """A real Session with fake voice clients and a real store."""
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("P", tmp_path)
    record = store.create_session(project.id)
    settings = Settings(
        DEEPSEEK_API_KEY="sk-test", SURTITLE_HOME=str(tmp_path), voice_enabled=False
    )
    sent: list[dict] = []

    async def send(payload):
        sent.append(payload)

    async def send_audio(_data):
        return None

    session = Session(
        session_id=record.id,
        project_id=project.id,
        root=tmp_path,
        settings=settings,
        store=store,
        deepseek=None,
        send=send,
        send_audio=send_audio,
    )
    session.tts = FakeTts()
    session.stt = FakeStt()
    return session, store, record, sent


class TestBargeInGuard:
    async def test_loudness_alone_does_not_interrupt(self, live_session):
        """The agent hearing itself is the common case and must be ignored."""
        session, _store, _record, _sent = live_session
        session._speaking = True
        session._speech_since_playback = False

        await session.cancel_turn(require_speech=True)

        # The meaningful side effects: nothing was stopped, and nothing was sent
        # to the browser to say the turn had ended.
        assert session.tts.barged == 0, "the agent interrupted itself"
        assert session._speaking is True, "the speaking flag was cleared"
        assert not any(payload.get("data", {}).get("reason") == "stopped" for payload in _sent)

    async def test_transcribed_speech_does_interrupt(self, live_session):
        session, _store, _record, _sent = live_session
        session._speaking = True
        session._speech_since_playback = True

        await session.cancel_turn(require_speech=True)

        assert session.tts.barged == 1, "a genuine interruption was ignored"

    async def test_an_explicit_stop_always_interrupts(self, live_session):
        """A user pressing stop must never be filtered by the speech guard."""
        session, _store, _record, _sent = live_session
        session._speaking = True
        session._speech_since_playback = False

        await session.cancel_turn()

        assert session.tts.barged == 1, "an explicit stop was ignored"

    async def test_speech_during_playback_is_recorded_as_the_trigger(self, live_session):
        """A transcript arriving while speaking is what authorises interruption."""
        from surtitle.voice.stt import TranscriptEvent

        session, _store, _record, _sent = live_session
        session._speaking = True
        assert session._speech_since_playback is False

        await session._on_transcript(TranscriptEvent(text="excuse me", final=False))
        assert session._speech_since_playback is True

    async def test_speech_while_silent_does_not_arm_the_guard(self, live_session):
        """Ordinary listening must not be mistaken for talking over the agent."""
        from surtitle.voice.stt import TranscriptEvent

        session, _store, _record, _sent = live_session
        session._speaking = False

        await session._on_transcript(TranscriptEvent(text="a normal question", final=False))
        assert session._speech_since_playback is False

    async def test_the_trigger_resets_when_playback_starts(self, live_session):
        session, _store, _record, _sent = live_session
        session._speech_since_playback = True
        session.stt.set_suppression(False)

        await session._on_speaking_started()

        assert session._speech_since_playback is False, (
            "a previous interruption would authorise the next one for free"
        )
        assert session._speaking is True

    async def test_playback_end_clears_the_speaking_flag(self, live_session):
        session, _store, _record, _sent = live_session
        session._speaking = True
        await session._on_speaking_finished()
        assert session._speaking is False


class TestInterruptedTurnRecording:
    """An interrupted exchange must reach the transcript."""

    async def test_the_partial_reply_is_stored(self, live_session, monkeypatch):
        session, store, record, _sent = live_session

        # A turn that produces output and then hangs, so it can be cancelled.
        class HangingLoop:
            partial_text = "I started answering and then"
            partial_spoken = "I started answering"

            async def run(self, history, user_text, *, on_chunk=None):
                # Store the user message as the real loop does, then block.
                store.add_message(session.session_id, "user", user_text)
                yield _state_event()
                await asyncio.sleep(30)

            async def aclose(self):
                return None

            def set_emitter(self, emitter):
                return None

        monkeypatch.setattr(
            "surtitle.core.session.AgentLoop",
            lambda *args, **kwargs: HangingLoop(),
        )

        task = asyncio.create_task(session._run_turn("a question"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        messages = store.list_messages(record.id)
        roles = [message.role for message in messages]
        assert "assistant" in roles, f"the interrupted turn was never recorded: {roles}"
        assistant = [m for m in messages if m.role == "assistant"][-1]
        assert assistant.content == "I started answering and then"
        assert assistant.spoken == "I started answering"

    async def test_history_does_not_duplicate_the_user_message(self, live_session, monkeypatch):
        """The stored exchange already holds it, so it must not be appended again."""
        session, store, record, _sent = live_session
        store.add_message(record.id, "user", "already stored")
        store.add_message(record.id, "assistant", "already answered")
        session._rolled_back = True

        history = session._build_history()
        assert [message["content"] for message in history] == [
            "already stored",
            "already answered",
        ]
        # The flag is consumed, so later turns take the normal path.
        assert session._rolled_back is False


def _queued(session) -> list[dict]:
    """Drain what the session has queued for the browser."""
    events = []
    while not session._outbox.empty():
        events.append(session._outbox.get_nowait().to_dict())
    return events


class TestApprovalSurvivesTheReplyFinishing:
    """A prompt must not be wiped by the turn's own voice finishing.

    Observed: the agent narrated its plan, then asked to edit a file. The request
    was announced while the reply was still being spoken and, 637 ms later,
    `speaking -> idle` was broadcast — which cleared the prompt off the screen
    while the turn went on waiting for an answer. The session still believed it
    was SPEAKING, because the agent's own states had never been mirrored into it.
    """

    async def test_the_session_tracks_the_state_the_turn_reports(self, live_session):
        from surtitle.core.events import Event, EventKind, SessionState

        session, _store, _record, _sent = live_session
        session._set_state(SessionState.SPEAKING)

        await session._emit_or_queue(
            Event(kind=EventKind.STATE, data={"state": SessionState.AWAITING_APPROVAL.value})
        )

        assert session._state.state is SessionState.AWAITING_APPROVAL, (
            "the session ignored the state the turn reported, so it stayed on SPEAKING"
        )

    async def test_finishing_the_reply_does_not_broadcast_over_an_approval(self, live_session):
        from surtitle.core.events import Event, EventKind, SessionState

        session, _store, _record, _sent = live_session
        session._set_state(SessionState.SPEAKING)
        await session._emit_or_queue(
            Event(kind=EventKind.STATE, data={"state": SessionState.AWAITING_APPROVAL.value})
        )
        session._speaking = True
        _queued(session)

        await session._on_speaking_finished()

        states = [
            e["data"]["state"]
            for e in _queued(session)
            if e["kind"] == "state" and "state" in e["data"]
        ]
        assert states == [], (
            f"finishing the reply broadcast {states}, which is what dismissed the prompt"
        )
        assert session._state.state is SessionState.AWAITING_APPROVAL

    async def test_an_ordinary_reply_finish_still_reports_idle(self, live_session):
        """The fix must not leave the indicator stuck on SPEAKING forever."""
        from surtitle.core.events import SessionState

        session, _store, _record, _sent = live_session
        session._set_state(SessionState.SPEAKING)
        session._speaking = True
        _queued(session)

        await session._on_speaking_finished()

        states = [
            e["data"]["state"]
            for e in _queued(session)
            if e["kind"] == "state" and "state" in e["data"]
        ]
        assert states == [SessionState.IDLE.value]


class TestAPendingApprovalIsReasked:
    """The prompt lives on screen, so a reconnect has to be asked again."""

    async def test_the_payload_is_kept_while_the_question_is_open(self):
        from surtitle.core.agent import ApprovalBroker

        broker = ApprovalBroker()
        payload = {
            "call_id": "c1",
            "name": "edit_file",
            "summary": "Edit a file",
            "arguments": {"path": "a.md"},
            "mutating": True,
        }
        broker.register("c1", "edit_file", {"path": "a.md"}, announcement=payload)

        assert broker.pending_announcements() == [payload]

    async def test_nothing_is_reasked_once_it_is_answered(self):
        from surtitle.core.agent import ApprovalBroker

        broker = ApprovalBroker()
        broker.register("c1", "edit_file", {}, announcement={"call_id": "c1"})
        broker.resolve("c1", allowed=True)

        assert broker.pending_announcements() == []

    async def test_a_reconnect_restates_the_open_question(self, live_session):
        from surtitle.core.events import EventKind

        session, _store, _record, _sent = live_session
        payload = {"call_id": "c1", "name": "edit_file", "arguments": {}}
        session.approvals.register("c1", "edit_file", {}, announcement=payload)

        await session.announce()

        kinds = [event["kind"] for event in _queued(session)]
        assert EventKind.READY.value in kinds
        assert EventKind.APPROVAL_REQUEST.value in kinds, (
            "a reconnecting browser was told the state but never asked the question"
        )

    async def test_a_reconnect_with_nothing_pending_asks_nothing(self, live_session):
        from surtitle.core.events import EventKind

        session, _store, _record, _sent = live_session

        await session.announce()

        kinds = [event["kind"] for event in _queued(session)]
        assert kinds == [EventKind.READY.value]


def _state_event():
    from surtitle.core.events import Event, EventKind

    return Event(kind=EventKind.STATE, data={"state": "thinking"})
