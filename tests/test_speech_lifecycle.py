"""The synthesiser must say when it has finished speaking.

This is the root cause of a long-running report: "the microphone only works for
the first exchange". The full chain was

1. `Session._on_speaking_started` turns on echo suppression, so the agent does not
   transcribe its own voice.
2. Nothing ever turned it off. `on_finished` was reachable only through
   `TextToSpeech.stop()`, so after the agent's very first reply `is_speaking` stayed
   true for the rest of the session.
3. `Session.handle_mic(open=True)` sets suppression to `is_speaking()`, so every
   later press of the microphone button **re-armed** suppression rather than
   clearing it.
4. Every transcript from then on was discarded as the agent's own voice, at debug
   level, so the log showed nothing at all.

The symptoms were therefore: the first question works; the microphone then looks
broken; toggling it does not help; real, loud audio is demonstrably arriving
(peak amplitude 0.996 in the log); and it happens in every browser, because none
of it is browser-specific.
"""

from __future__ import annotations

import asyncio

import pytest

from surtitle.config import Settings
from surtitle.voice.tts import TextToSpeech


def make_settings(**overrides) -> Settings:
    values = {"DEEPSEEK_API_KEY": "k", "DEEPGRAM_API_KEY": "k"}
    values.update(overrides)
    return Settings(**values)


class FakeTtsSocket:
    """A speech socket that acknowledges every utterance immediately."""

    def __init__(self) -> None:
        import json

        self.sent: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._json = json

    async def send(self, payload) -> None:
        self.sent.append(payload)
        try:
            message = self._json.loads(payload)
        except (TypeError, ValueError):
            return
        if message.get("type") == "Flush":
            # Answering the flush is what ends an utterance's synthesis.
            await self._queue.put(self._json.dumps({"type": "Flushed"}))

    async def recv(self):
        return await self._queue.get()

    state = None


class TestEndOfTurnIsReported:
    @pytest.fixture
    def tts(self):
        started: list[bool] = []
        finished: list[bool] = []

        async def on_started():
            started.append(True)

        async def on_finished():
            finished.append(True)

        service = TextToSpeech(
            make_settings(),
            on_audio=lambda audio, sequence: _noop(),
            on_started=on_started,
            on_finished=on_finished,
        )
        return service, started, finished

    async def test_speaking_ends_when_the_turn_ends(self, tts, monkeypatch):
        service, started, finished = tts
        monkeypatch.setattr(service, "_ensure_socket", lambda: _socket(FakeTtsSocket()))
        await service.start()

        service.speak("The iodine service is up.", final=True)
        service.end_of_turn()
        await asyncio.sleep(0.2)

        assert started == [True]
        assert finished == [True], "nothing reported the end of speaking"
        assert service.is_speaking is False

    async def test_the_report_comes_after_the_queued_sentences(self, tts, monkeypatch):
        """Suppression must not lift while sentences are still being synthesised."""
        service, _started, finished = tts
        socket = FakeTtsSocket()
        monkeypatch.setattr(service, "_ensure_socket", lambda: _socket(socket))
        await service.start()

        service.speak("First sentence.", final=False)
        service.speak("Second sentence.", final=False)
        service.end_of_turn()
        await asyncio.sleep(0.2)

        assert finished == [True]
        spoken = [json_type(item) for item in socket.sent]
        assert spoken.count("Speak") == 2, "both sentences should have been spoken"

    async def test_an_empty_turn_does_not_announce_speaking(self, tts, monkeypatch):
        """A turn with nothing to say must not claim the agent is talking.

        Ending a silent turn used to flip the speaking flag on and off, which
        briefly enabled echo suppression and told the UI the agent was speaking.
        """
        service, started, finished = tts
        monkeypatch.setattr(service, "_ensure_socket", lambda: _socket(FakeTtsSocket()))
        await service.start()

        service.end_of_turn()
        await asyncio.sleep(0.15)

        assert started == [], "nothing was spoken, so speaking never started"
        assert finished == []
        assert service.is_speaking is False

    async def test_stopping_is_still_reported(self, tts, monkeypatch):
        service, _started, _finished = tts
        monkeypatch.setattr(service, "_ensure_socket", lambda: _socket(FakeTtsSocket()))
        await service.start()
        service.speak("Something.", final=True)
        await asyncio.sleep(0.1)

        await service.stop()

        assert service.is_speaking is False

    async def test_barge_in_does_not_announce_a_completed_reply(self, tts, monkeypatch):
        """An interruption is not a finished reply and must not be announced."""
        service, _started, finished = tts
        monkeypatch.setattr(service, "_abandon_socket", lambda: _noop())
        # A reply is mid-synthesis; the user talks over it.
        service._active = True

        await service.barge_in()

        assert service.is_speaking is False
        assert finished == [], "an interruption must not be reported as finishing"


class RecordingStt:
    """Stands in for the recogniser, recording suppression changes."""

    def __init__(self) -> None:
        self.suppression: list[bool] = []

    def set_suppression(self, suppressed: bool) -> None:
        self.suppression.append(suppressed)


@pytest.fixture
def wired(tmp_path):
    """A real Session driving a real TextToSpeech and a recording recogniser."""
    from surtitle.core.session import Session
    from surtitle.store.db import Store

    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("P", tmp_path)
    record = store.create_session(project.id)

    async def send(_payload):
        return None

    async def send_audio(_data):
        return None

    session = Session(
        session_id=record.id,
        project_id=project.id,
        root=tmp_path,
        settings=make_settings(SURTITLE_HOME=str(tmp_path), voice_enabled=True),
        store=store,
        deepseek=None,
        send=send,
        send_audio=send_audio,
    )
    stt = RecordingStt()
    session.stt = stt  # type: ignore[assignment]
    tts = TextToSpeech(
        session.settings,
        on_audio=lambda audio, sequence: _noop(),
        on_started=session._on_speaking_started,
        on_finished=session._on_speaking_finished,
    )
    session.tts = tts
    return session, tts, stt


class TestSuppressionIsReleasedAfterATurn:
    """The exact chain that made the microphone stop working after one exchange.

    `_run_turn` finishing must reach `stt.set_suppression(False)`. Before, nothing
    called `end_of_turn`, so `on_finished` never fired and suppression stayed on for
    the rest of the session.
    """

    async def test_a_completed_reply_releases_suppression(self, wired, monkeypatch):
        _session, tts, stt = wired
        monkeypatch.setattr(tts, "_ensure_socket", lambda: _socket(FakeTtsSocket()))
        await tts.start()

        # The agent replies, then `_run_turn` reaches its end and says so.
        tts.speak("The iodine service is up.", final=True)
        tts.end_of_turn()
        await asyncio.sleep(0.2)

        assert stt.suppression == [True, False], (
            "a finished reply must release echo suppression, or every later "
            "transcript is discarded as the agent's own voice"
        )
        assert tts.is_speaking is False

    async def test_reopening_the_mic_does_not_leave_suppression_on(self, wired, monkeypatch):
        """`handle_mic` derives suppression from `is_speaking`."""
        session, tts, stt = wired
        monkeypatch.setattr(tts, "_ensure_socket", lambda: _socket(FakeTtsSocket()))
        await tts.start()

        tts.speak("A reply.", final=True)
        tts.end_of_turn()
        await asyncio.sleep(0.2)
        stt.suppression.clear()

        # The user presses the mic button again.
        await session.handle_mic(True)

        assert stt.suppression == [False], (
            "pressing the microphone must not re-arm suppression once the agent "
            "has stopped speaking"
        )


class TestFailuresAreSpoken:
    """A voice-first user is listening, not reading.

    A turn that exhausted its step budget ended in silence: the error was on
    screen, the agent said nothing, and the turn wrote no assistant message. It was
    reported as "it seems to have stopped" — which is exactly what silence means to
    someone who is listening rather than reading.
    """

    @staticmethod
    def _recorder(spoken: list[str]):
        class Recorder:
            is_speaking = False

            def speak(self, text, *, final=False):
                spoken.append(text)

        return Recorder()

    async def test_the_step_limit_is_spoken(self, wired):
        session = wired[0]
        spoken: list[str] = []
        session.tts = self._recorder(spoken)  # type: ignore[assignment]

        await session._speak_problem({"kind_detail": "step_limit"})

        assert spoken, "running out of steps must not be silent"
        assert "ran out of steps" in spoken[0]
        # Actionable, not just an alarm.
        assert "carry on" in spoken[0] or "smaller" in spoken[0]

    async def test_a_model_failure_is_spoken(self, wired):
        session = wired[0]
        spoken: list[str] = []
        session.tts = self._recorder(spoken)  # type: ignore[assignment]

        await session._speak_problem({"kind_detail": "llm", "message": "boom"})

        assert spoken and "connection" in spoken[0]

    async def test_an_unmodelled_failure_still_says_something(self, wired):
        session = wired[0]
        spoken: list[str] = []
        session.tts = self._recorder(spoken)  # type: ignore[assignment]

        await session._speak_problem({"kind_detail": "something_new"})

        assert spoken, "a new failure kind must still not be silent"

    async def test_text_only_mode_is_unaffected(self, wired):
        """No synthesiser means nothing to say, and nothing to break."""
        session = wired[0]
        session.tts = None
        await session._speak_problem({"kind_detail": "step_limit"})

    async def test_the_step_limit_leaves_a_trace_in_the_log(self):
        """Nothing recorded it, so a cut-short turn vanished from the log."""
        import inspect

        from surtitle.core.agent import AgentLoop

        source = inspect.getsource(AgentLoop.run)
        assert "step limit reached" in source, (
            "a turn cut short by the budget must be visible in the log"
        )


class TestSuppressionIsObservable:
    """A suppressed transcript must not be invisible.

    While this was a debug line, a permanently stuck suppression produced no log
    output at all — which is why the fault was repeatedly misdiagnosed as a broken
    microphone.
    """

    def _service(self):
        from surtitle.voice.stt import SpeechToText

        return SpeechToText(make_settings(stt_api="v2"), on_transcript=lambda event: None)

    def test_dropping_a_transcript_is_logged(self, caplog):
        service = self._service()
        service.set_suppression(True)

        with caplog.at_level("INFO"):
            for _ in range(2):
                service._note_suppressed("the agent's own voice", kind="Flux")

        assert service._suppressed_transcripts == 2
        assert "echo suppression" in caplog.text

    def test_the_counter_resets_when_suppression_lifts(self):
        service = self._service()
        service.set_suppression(True)
        service._note_suppressed("something", kind="Flux")
        assert service._suppressed_transcripts == 1

        service.set_suppression(True)  # each speaking turn starts counting again

        assert service._suppressed_transcripts == 0


async def _noop(*_args, **_kwargs) -> None:
    return None


async def _socket(socket):
    return socket


def json_type(payload: str) -> str:
    import json

    try:
        return str(json.loads(payload).get("type"))
    except (TypeError, ValueError):
        return "?"
