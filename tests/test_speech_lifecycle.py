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
import contextlib
import json
import logging
import time

import pytest

from surtitle.config import Settings
from surtitle.llm.chat import StreamEvent, ToolCallDelta, Usage
from surtitle.tools.registry import ToolRegistry, default_tool_list
from surtitle.voice import stt as stt_module
from surtitle.voice.stt import SpeechToText
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
    """Stands in for the recogniser, recording suppression changes and audio."""

    def __init__(self) -> None:
        self.suppression: list[bool] = []
        self.audio: list[bytes] = []

    def set_suppression(self, suppressed: bool) -> None:
        self.suppression.append(suppressed)

    def push_audio(self, frame: bytes) -> None:
        self.audio.append(frame)


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


class TestALongThinkDoesNotLookLikeStaleSuppression:
    """Suppression must not lift the instant a reply starts.

    The watchdog measures silence against the last audio frame *sent*. During a
    think — or simply between turns — that timestamp is minutes old, so its next
    tick saw a huge gap, declared suppression stale, and lifted it about 0.2s into
    the reply. The microphone was then live for the whole of it, which is the
    opposite of what suppression is for. The log said so plainly: "echo
    suppression had outlived the agent's audio by 115.6s (0 transcript(s) were
    discarded while it was on)" — the counter was zero precisely because
    suppression had not been doing anything.
    """

    async def test_the_silence_clock_starts_when_speaking_starts(self, wired):
        session, _tts, _stt = wired
        session._last_audio_out_at = asyncio.get_running_loop().time() - 120.0
        session._last_audio_out_seconds = 0.5

        await session._on_speaking_started()

        silent_for = asyncio.get_running_loop().time() - session._last_audio_out_at
        assert silent_for < 1.0, (
            "the clock is still reading the think that preceded the reply, so the "
            "watchdog will call live suppression stale"
        )

    async def test_the_watchdog_does_not_lift_suppression_during_that_gap(self, wired):
        session, _tts, stt = wired
        session._last_audio_out_at = asyncio.get_running_loop().time() - 120.0
        session._last_audio_out_seconds = 0.5
        await session._on_speaking_started()
        stt.suppression.clear()

        task = asyncio.create_task(session._watch_echo_suppression())
        try:
            # Longer than one tick of the watchdog's 0.5s loop.
            await asyncio.sleep(0.7)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert stt.suppression == [], (
            "suppression was lifted while the agent was still speaking, so its own "
            "voice reaches the recogniser"
        )
        assert session._suppression_released is False


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

        await session._speak_problem({"reason": "step_limit"})

        assert spoken, "reaching the step limit must not be silent"
        assert "step limit" in spoken[0]
        # Not phrased as a failure: nothing went wrong, the turn reached its
        # budget with work outstanding. And actionable, not just an alarm.
        assert "continue" in spoken[0]

    async def test_the_step_limit_is_also_spoken_from_an_error_event(self, wired):
        """The older `kind_detail` shape must keep working."""
        session = wired[0]
        spoken: list[str] = []
        session.tts = self._recorder(spoken)  # type: ignore[assignment]

        await session._speak_problem({"kind_detail": "step_limit"})

        assert spoken and "step limit" in spoken[0]

    async def test_a_finished_turn_is_not_announced_as_a_stop(self, wired):
        """The done event carries `complete`; that needs no spoken explanation."""
        import inspect

        source = inspect.getsource(type(wired[0])._run_turn)
        assert '"complete"' in source, (
            "the loop must skip the spoken stop reason for an ordinary finish"
        )

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


class TestTheProgressLineDoesNotEndTheTurn:
    """A "still working" line is not the end of the turn.

    Marking it final makes the synthesiser report that speaking has finished, which
    releases echo suppression while the agent is still working and still going to
    speak. The speaking state then flips mid-turn and the agent's own voice is let
    back in through the microphone.
    """

    async def test_progress_is_not_marked_final(self, wired):
        session = wired[0]
        recorded: list[tuple[str, bool]] = []

        class Recorder:
            is_speaking = False

            def speak(self, text, *, final=False):
                recorded.append((text, final))

        session.tts = Recorder()  # type: ignore[assignment]
        await session._speak_progress(20)

        assert recorded, "a long silent turn should say it is still working"
        text, final = recorded[0]
        assert "still working" in text.lower()
        assert final is False, "the turn has not finished, so this must not be final"

    async def test_the_turn_end_marker_is_separate(self, wired):
        """`end_of_turn` is what marks the end, and only that."""
        _session, tts, _stt = wired
        assert hasattr(tts, "end_of_turn")

        import inspect

        from surtitle.core.session import Session

        source = inspect.getsource(Session._run_turn)
        assert "end_of_turn()" in source, (
            "the end of the turn must be announced deliberately, not implied by a progress line"
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


class TestAnUtteranceIsNotCutInHalf:
    """A recogniser end-of-turn is not always the end of a sentence.

    From a real session: "So we could work out a simulation" and "of this."
    arrived 1.5 s apart as two turns, so the agent began answering half a
    sentence, and the fragments that followed cancelled the turn before it — so
    the first three attempts produced nothing at all. The fix holds the text
    briefly and merges whatever continues it.
    """

    @pytest.fixture
    def session(self, tmp_path):
        session, _tts, _stt = _wired_with_recorder(tmp_path)
        return session

    async def test_a_following_fragment_is_merged_into_one_utterance(self, session):
        from surtitle.voice.stt import TranscriptEvent

        # The recogniser reports the first half and declares the turn over.
        await session._on_transcript(
            TranscriptEvent(
                text="So we could work out a simulation", final=True, is_end_of_turn=True
            )
        )
        # The rest of the sentence arrives while the first half is still held.
        await session._on_transcript(
            TranscriptEvent(text="of this.", final=True, is_end_of_turn=True)
        )
        await _settle(session)

        assert _voice_utterances(session) == ["So we could work out a simulation of this."], (
            "the two halves of one sentence must reach the model as one message"
        )

    async def test_a_genuine_stop_delivers_without_much_delay(self, session):
        from surtitle.voice.stt import TranscriptEvent

        await session._on_transcript(
            TranscriptEvent(text="Yes, that is right.", final=True, is_end_of_turn=True)
        )
        await _settle(session)

        assert _voice_utterances(session) == ["Yes, that is right."], (
            "holding must not lose an utterance that is not continued"
        )

    async def test_the_hold_can_be_turned_off(self, tmp_path):
        from surtitle.voice.stt import TranscriptEvent

        session, _tts, _stt = _wired_with_recorder(tmp_path, SURTITLE_STT_MERGE_HOLD_MS=0)

        await session._on_transcript(
            TranscriptEvent(text="Commit me now.", final=True, is_end_of_turn=True)
        )
        # The commit is still scheduled even with no hold, so it must be waited
        # for — zero means "do not wait for more", not "do not schedule".
        await _settle(session)

        assert _voice_utterances(session) == ["Commit me now."]

    async def test_two_separate_sentences_are_not_run_together(self, tmp_path):
        """A pause between two questions must stay two questions.

        The hold waits for *more text*, not for a fixed delay: once a full window
        passes with nothing further transcribed, the sentence is over.
        """
        from surtitle.voice.stt import TranscriptEvent

        session, _tts, _stt = _wired_with_recorder(tmp_path, SURTITLE_STT_MERGE_HOLD_MS=120)
        await session._on_transcript(
            TranscriptEvent(
                text="What does the slow speed arm do?", final=True, is_end_of_turn=True
            )
        )
        await _settle(session)
        await session._on_transcript(
            TranscriptEvent(text="And is it set in the config?", final=True, is_end_of_turn=True)
        )
        await _settle(session)

        assert _voice_utterances(session) == [
            "What does the slow speed arm do?",
            "And is it set in the config?",
        ], "two questions asked with a pause between them are two turns"


class TestEchoSuppressionCannotGetStuck:
    """Suppression that outlives the agent's audio silently eats the user's words.

    In the session this was found in, `dropping Flux transcript 25 during playback`
    logged a complete sentence — "The question is, uh, is the slower speed losing
    more than we…" — being discarded while the client had already stopped playing.
    The microphone looked open, loud audio was arriving, and the words never
    appeared. It is the one voice failure that leaves no trace on screen.
    """

    async def test_a_mic_opened_on_an_idle_session_is_not_suppressed(self, wired):
        """The old re-arm bug: the button itself kept suppression switched on."""
        session, _tts, stt = wired
        session._speaking = False

        await session.handle_mic(True)

        assert stt.suppression[-1] is False

    async def test_suppression_is_lifted_when_the_audio_has_long_since_stopped(self, wired):
        session, _tts, stt = wired
        # A real stt object is not needed beyond suppression bookkeeping; the
        # watchdog only ever calls set_suppression on it.
        loop = asyncio.get_running_loop()
        session._speaking = True
        session._closed = False
        session.settings.echo_suppression_max_ms = 100
        # The last audio went out well beyond its own duration ago.
        session._last_audio_out_seconds = 0.5
        session._last_audio_out_at = loop.time() - 10.0
        stt.set_suppression(True)
        session._suppression_released = False

        watchdog = asyncio.create_task(session._watch_echo_suppression())
        try:
            for _ in range(40):
                await asyncio.sleep(0.05)
                if session._suppression_released:
                    break
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

        assert session._suppression_released, "stuck suppression must not be permanent"
        assert stt.suppression[-1] is False

    async def test_audio_still_flowing_does_not_release_suppression(self, wired):
        session, _tts, stt = wired
        loop = asyncio.get_running_loop()
        session._closed = False
        session._speaking = True
        session.settings.echo_suppression_max_ms = 100
        session._last_audio_out_seconds = 4.0  # a long sentence, still in flight
        session._last_audio_out_at = loop.time()
        stt.set_suppression(True)
        session._suppression_released = False

        watchdog = asyncio.create_task(session._watch_echo_suppression())
        try:
            await asyncio.sleep(0.7)
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

        assert session._suppression_released is False, (
            "the agent is still speaking; releasing here would transcribe its own voice"
        )

    async def test_the_ui_is_told_when_transcripts_are_being_discarded(self, wired):
        session, _tts, _stt = wired
        events: list[dict] = []
        while not session._outbox.empty():
            session._outbox.get_nowait()

        session._announce_suppression(True)
        while not session._outbox.empty():
            events.append(session._outbox.get_nowait().data)

        assert events and events[-1].get("echo_suppressed") is True, (
            "a discarded transcript must be visible in the UI, not only in the log"
        )


async def _settle(session) -> None:
    """Wait for the scheduled commit, which is what delivers an utterance."""
    task = session._commit_task
    if task is not None:
        await task


def _voice_utterances(session) -> list[str]:
    """Every spoken utterance the session handed to the agent, drained in order."""
    texts: list[str] = []
    while not session._outbox.empty():
        event = session._outbox.get_nowait()
        if event.kind.value == "user_text" and event.data.get("source") == "voice":
            texts.append(str(event.data.get("text", "")))
    return texts


def _wired_with_recorder(tmp_path, **overrides):
    """A session with a recording recogniser and no synthesiser."""
    from surtitle.core.session import Session
    from surtitle.store.db import Store

    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("P", tmp_path)
    record = store.create_session(project.id)

    async def send(_payload):
        return None

    async def send_audio(_data):
        return None

    values = {"SURTITLE_HOME": str(tmp_path), "voice_enabled": True}
    values.update(overrides)
    session = Session(
        session_id=record.id,
        project_id=project.id,
        root=tmp_path,
        settings=make_settings(**values),
        store=store,
        deepseek=None,
        send=send,
        send_audio=send_audio,
    )
    stt = RecordingStt()
    session.stt = stt  # type: ignore[assignment]
    return session, None, stt


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


class _ScriptedModel:
    """A model that replays one scripted stream per round.

    ``block_first`` holds the first round open, so a test can act while a turn is
    genuinely in flight rather than pretending that one is.
    """

    def __init__(self, scripts: list[list[StreamEvent]], *, block_first: bool = False) -> None:
        self.scripts = scripts
        self.calls = 0
        self._block_first = block_first
        self.release = None
        # What each round was shown, so a test can assert on the order requests ran.
        self.prompts: list[list] = []

    async def stream(self, messages, *, tools=None):
        self.calls += 1
        self.prompts.append(list(messages))
        script = self.scripts[min(self.calls - 1, len(self.scripts) - 1)]
        for event in script:
            yield event
        if self._block_first and self.calls == 1:
            self.release = asyncio.get_running_loop().create_future()
            await self.release

    async def aclose(self) -> None:
        return None


def _tool_round(name: str, arguments: dict) -> list[StreamEvent]:
    return [
        StreamEvent(kind="reasoning", text="Working."),
        StreamEvent(
            kind="tool_call",
            tool_call=ToolCallDelta(index=0, id="c1", name=name, arguments=json.dumps(arguments)),
        ),
        StreamEvent(kind="done", finish_reason="tool_calls"),
        StreamEvent(kind="usage", usage=Usage()),
    ]


def _answer_round(text: str = "<say>Done.</say>") -> list[StreamEvent]:
    return [
        StreamEvent(kind="text", text=text),
        StreamEvent(kind="done", finish_reason="stop"),
        StreamEvent(kind="usage", usage=Usage()),
    ]


async def _blocked_turn(session):
    """Start a real turn that stays in flight, and wait until it is blocked.

    ``block_first`` holds the model's first round open, so a test can act while a
    turn is genuinely running rather than pretending that one is.
    """
    model = _ScriptedModel([_answer_round()], block_first=True)
    session.deepseek = model
    session.registry = ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"])
    await session.handle_text("a long job")
    for _ in range(50):
        if model.release is not None:
            return model
        await asyncio.sleep(0)
    raise AssertionError("the first turn never reached the model")


class TestTheTurnEndingIsRecorded:
    """A turn must write down how it ended rather than leave it to be inferred.

    Reported as "the latest chat just stopped again", where a refresh showed more
    of what had happened but still could not say why the turn had ended. From the
    stored transcript alone a spent step budget, an empty model round, a failure
    and a restart are indistinguishable — and nothing logged or stored the
    difference, so explaining that one report meant reading the database by hand.
    """

    async def test_a_finished_turn_records_that_it_finished(self, wired):
        session, _tts, _stt = wired
        session.deepseek = _ScriptedModel([_answer_round()])

        await session._run_turn("hello")

        record = session.store.get_session(session.session_id)
        assert record.last_end_reason == "complete"
        assert record.last_end_at, "the ending is given a time, so it can be ordered"

    async def test_a_turn_that_spends_its_budget_says_so(self, wired):
        """The ending from the report: work done, no answer, nothing said why."""
        session, _tts, _stt = wired
        session.settings.max_steps = 1
        session.registry = ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"])
        session.deepseek = _ScriptedModel([_tool_round("list_dir", {"path": "."})])

        await session._run_turn("hello")

        record = session.store.get_session(session.session_id)
        assert record.last_end_reason == "step_limit"
        assert record.last_end_steps == 1
        assert "step" in (record.last_end_detail or "").lower(), (
            "the stored explanation is what a reopened conversation shows"
        )

    async def test_a_new_turn_clears_the_previous_ending(self, wired):
        """A stale reason would answer "why does this look stopped" wrongly."""
        session, _tts, _stt = wired
        session.store.record_turn_end(session.session_id, reason="step_limit", steps=3)
        assert session.store.get_session(session.session_id).last_end_reason == "step_limit"

        session.store.start_turn(session.session_id)

        assert session.store.get_session(session.session_id).last_end_reason is None

    async def test_the_ending_is_logged_with_what_the_turn_used(self, wired, caplog):
        """The line that makes "it just stopped" a ten-second answer next time."""
        session, _tts, _stt = wired
        session._turn_started_at = time.time() - 5
        session._turn_prompt_tokens = 1200
        session._turn_completion_tokens = 300
        session._turn_spoken_chars = 84

        with caplog.at_level(logging.INFO):
            await session._record_turn_end(
                {"reason": "no_answer", "steps": 7, "detail": "No reply."}
            )

        assert "reason=no_answer" in caplog.text
        assert "steps=7" in caplog.text
        assert "spoken=84" in caplog.text, "the reader needs to know whether anything was heard"
        assert "tokens=1200/300" in caplog.text, "a turn's size is part of why it ended"

    async def test_what_is_counted_as_spoken_is_what_was_synthesised(self, wired):
        """The loop's own field misses the repaired closing line, so it is not used."""
        from surtitle.core.speak import Chunk, ChunkKind

        session, _tts, _stt = wired
        spoken: list[str] = []

        class Recorder:
            is_speaking = False

            def speak(self, text, *, final=False):
                spoken.append(text)

        session.tts = Recorder()  # type: ignore[assignment]
        session._turn_spoken_chars = 0

        await session._speak_chunk(Chunk(ChunkKind.SAY, "I looked, and here is what I found."))
        await session._speak_chunk(Chunk(ChunkKind.DISPLAY, "code that is never spoken"))

        assert session._turn_spoken_chars == len("I looked, and here is what I found.")
        assert spoken == ["I looked, and here is what I found."]

    async def test_usage_is_counted_per_turn_without_the_run_counters(self, wired):
        """The record is written even where no run-wide stats object is wired up."""
        from surtitle.core.events import Event, EventKind

        session, _tts, _stt = wired
        session.stats = None
        session._turn_prompt_tokens = 0
        session._turn_completion_tokens = 0

        session._count(Event(EventKind.USAGE, data={"prompt_tokens": 40, "completion_tokens": 2}))

        assert session._turn_prompt_tokens == 40
        assert session._turn_completion_tokens == 2

    async def test_a_silent_closing_round_is_heard_not_just_recorded(self, wired):
        """The report was "it stopped", from someone who is listening.

        This is the 0.8.1 turn end to end: a spoken preamble, a tool call, then two
        rounds that produce nothing at all. Recording `no_answer` is not the fix —
        saying it is. Driving the real session is what proves the sentence reaches
        the synthesiser rather than only the database and the log.
        """
        session, _tts, _stt = wired
        spoken: list[str] = []

        class Recorder:
            is_speaking = False

            def speak(self, text, *, final=False):
                spoken.append(text)

            def end_of_turn(self):
                return None

        session.tts = Recorder()  # type: ignore[assignment]
        session.registry = ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"])
        empty = [StreamEvent(kind="done", finish_reason="stop")]
        session.deepseek = _ScriptedModel(
            [
                [
                    StreamEvent(kind="text", text="<say>Let me read the harness.</say>"),
                    StreamEvent(
                        kind="tool_call",
                        tool_call=ToolCallDelta(
                            index=0, id="c1", name="list_dir", arguments=json.dumps({"path": "."})
                        ),
                    ),
                    StreamEvent(kind="done", finish_reason="tool_calls"),
                    StreamEvent(kind="usage", usage=Usage()),
                ],
                empty,
                empty,
            ]
        )

        await session._run_turn("build the sim")

        record = session.store.get_session(session.session_id)
        assert record.last_end_reason == "no_answer"
        assert any("without saying anything" in line for line in spoken), (
            f"a turn that produced nothing must say so aloud; heard {spoken!r}"
        )
        assert session._turn_spoken_chars > 0, "the log must not report this as a silent turn"


class TestRequestsArrivingMidTurn:
    """A request that arrives while the agent is working must not be lost.

    Reported as losing the ability to type while the agent thinks, answered with
    "Still working on the previous request. Stop it first or wait." — and an
    utterance captured in the same window was dropped with no message at all. A
    turn can run for minutes, so the next thought has to be kept rather than
    refused or thrown away.
    """

    @staticmethod
    def _drain(session) -> list:
        events = []
        while not session._outbox.empty():
            events.append(session._outbox.get_nowait())
        return events

    async def test_a_typed_request_during_a_turn_is_queued_not_refused(self, wired):
        from surtitle.core.events import EventKind

        session, _tts, _stt = wired
        session._turn = asyncio.create_task(asyncio.sleep(30))

        try:
            await session.handle_text("and also check the notes")

            events = self._drain(session)
            assert [event.kind for event in events] == [EventKind.USER_TEXT], (
                "a queued request is an acknowledgement, not an error"
            )
            assert events[0].data["queued"] is True, "the browser is told it is waiting"
            assert session._queue == [("and also check the notes", "typed")]
        finally:
            session._turn.cancel()

    async def test_the_queued_request_runs_when_the_turn_finishes(self, wired):
        session, _tts, _stt = wired
        session.registry = ToolRegistry([t for t in default_tool_list() if t.name == "list_dir"])
        session.deepseek = _ScriptedModel([_answer_round()])
        session._queue.append(("the second question", "typed"))

        session._drain_queue()
        assert session._turn is not None, "the held request was never started"
        await session._turn

        assert session._queue == [], "it must not run twice"
        assert session.store.get_session(session.session_id).last_end_reason == "complete"

    async def test_a_closed_session_does_not_start_queued_work(self, wired):
        """Shutdown must not begin a turn on the way out."""
        session, _tts, _stt = wired
        session._queue.append(("too late", "typed"))
        await session.close()

        session._drain_queue()

        assert session._queue == [("too late", "typed")]
        assert session._turn is None or session._turn.done()


class TestStopAndPush:
    """Two ways out of a turn that is taking too long.

    Stop halts the work and drops what was waiting behind it. Push is for the case
    where the next message matters more than the current one: it stops the turn and
    takes its place. Until now there was no way to do either from the interface —
    the protocol had a cancel command and the client never sent it.
    """

    @staticmethod
    def _drain(session) -> list:
        events = []
        while not session._outbox.empty():
            events.append(session._outbox.get_nowait())
        return events

    async def _running_turn(self, session):
        """Start a real turn that stays in flight, and wait until it is blocked."""
        return await _blocked_turn(session)

    async def test_push_stops_the_turn_and_takes_its_place(self, wired):
        from surtitle.core.events import EventKind

        session, _tts, _stt = wired
        model = await self._running_turn(session)
        self._drain(session)

        await session.handle_text("actually, do this instead", interrupt=True)
        await session._turn

        events = self._drain(session)
        assert not [e for e in events if e.kind is EventKind.ERROR], (
            "pushing is a valid action, not the old 'stop it first' refusal"
        )
        assert [e for e in events if e.kind is EventKind.USER_TEXT], "the browser is told"
        assert session._queue == [], "the pushed request ran; it was not left waiting"
        assert model.calls >= 2, "the pushed request reached the model"
        assert session.store.get_session(session.session_id).last_end_reason == "complete"

    async def test_a_queued_request_is_still_behind_a_pushed_one(self, wired):
        """Push takes its place; it does not throw away what was already waiting."""
        session, _tts, _stt = wired
        model = await self._running_turn(session)
        await session.handle_text("waiting behind", interrupt=False)

        await session.handle_text("pushed", interrupt=True)
        # Let the pushed turn — and then the held one — run to the end.
        for _ in range(50):
            if session._turn is not None and not session._turn.done():
                await asyncio.sleep(0)
                continue
            if not session._queue:
                break
            await asyncio.sleep(0)

        answered = [prompt[-1]["content"] for prompt in model.prompts]
        assert answered[:2] == ["a long job", "pushed"], (
            f"the pushed request goes first, ahead of the one already waiting: {answered}"
        )
        assert "waiting behind" in answered, "the held request still runs, afterwards"

    async def test_stop_drops_what_was_waiting(self, wired):
        from surtitle.core.events import EventKind
        from surtitle.server import _dispatch

        session, _tts, _stt = wired
        await self._running_turn(session)
        await session.handle_text("still waiting", interrupt=False)

        await _dispatch(session, {"kind": "cancel"})
        await asyncio.sleep(0)

        assert session._queue == [], "stop means stop: nothing runs after it"
        assert session._turn is None or session._turn.done()
        kinds = [e.kind for e in self._drain(session)]
        assert EventKind.STATE in kinds, "the browser is told the turn ended"


class TestASpokenTurnReplacesTheRunningOne:
    """Speaking over the agent must replace the turn, not duplicate it.

    Reported from the real 2026-09-19 session: two `turn ended` lines seven seconds
    apart, two assistant messages written into one turn, and their reasoning
    interleaved row by row. A request was waiting behind the running turn, the user
    spoke, and `_start_spoken_turn` cancelled the turn — whose exit drains the
    queue and starts the waiting request — and then assigned its own turn on top of
    it. Two turns ran; the session held a handle on only the later one, so Stop
    could not reach the other.
    """

    async def test_two_turns_never_run_at_once(self, wired, monkeypatch):
        session, _tts, _stt = wired
        running = 0
        high_water = 0
        original = type(session)._run_turn

        async def counted(self, text: str) -> None:
            nonlocal running, high_water
            running += 1
            high_water = max(high_water, running)
            try:
                await original(self, text)
            finally:
                running -= 1

        monkeypatch.setattr(type(session), "_run_turn", counted)

        model = await _blocked_turn(session)
        await session.handle_text("waiting behind", interrupt=False)
        assert session._queue == [("waiting behind", "typed")]

        await session._start_spoken_turn("what I just said")

        # Let every turn that is going to run, run to its end. The queue emptying
        # is not enough on its own: a turn is popped off it and *then* scheduled,
        # so the session's own handle has to be finished too.
        for _ in range(500):
            if (
                running == 0
                and not session._queue
                and (session._turn is None or session._turn.done())
            ):
                break
            await asyncio.sleep(0)

        assert high_water == 1, (
            f"{high_water} turns ran at once; the session can only track one of them"
        )
        assert session._queue == [], "the request waiting behind it must still run"
        answered = [prompt[-1]["content"] for prompt in model.prompts]
        assert answered == ["a long job", "what I just said", "waiting behind"], (
            f"the utterance replaces the running turn and the held request follows: {answered}"
        )


class TestAudioForAClosedMicrophone:
    """Reported: the mic in the window was off, but it was still converting voice.

    The browser is supposed to stop sending when the mic is toggled off, and it did
    not — a worklet that never received its mute message kept posting frames — so
    the recogniser went on converting speech the user had switched off, and a
    transcript from it could start a turn. The log shows the shape: a microphone
    closed at 15:04:42 and an utterance committed at 15:04:55, from 236 seconds of
    audio that had been streaming since before the close.

    The server cannot fix the browser, but it can refuse the audio.
    """

    async def test_frames_are_refused_while_the_microphone_is_closed(self, wired):
        session, _tts, stt = wired
        session._state.mic_open = False

        await session.handle_audio(b"\x01\x02" * 100)

        assert stt.audio == [], "a closed microphone must not be recognised"
        assert session._frames_in == 0, "and it must not count as captured audio"
        assert session._frames_closed == 1, "the client fault is counted for the log"

    async def test_frames_are_accepted_once_the_microphone_is_open(self, wired):
        session, _tts, stt = wired
        await session.handle_mic(True)

        await session.handle_audio(b"\x01\x02" * 100)

        assert len(stt.audio) == 1
        assert session._frames_in == 1
        assert session._frames_closed == 0


class FakeSttSocket:
    """A recognition socket that fails on demand, twice over.

    `fail` is consumed one connect attempt at a time: a string raises that error
    from the receive loop, `None` keeps the socket up until it is stopped.
    """

    def __init__(self, fail: list[str | None]) -> None:
        self.fail = fail
        self.attempts = 0
        self.sent: list[object] = []

    async def send(self, payload: object) -> None:
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        index = min(self.attempts - 1, len(self.fail) - 1)
        failure = self.fail[index] if self.fail else None
        if failure:
            raise RuntimeError(failure)
        # Connected and quiet: hold here until the test stops the engine.
        await asyncio.sleep(3600)
        raise StopAsyncIteration


class TestWhatADroppedRecognitionSocketSays:
    """A provider blip is not a configuration error.

    Deepgram closes healthy sockets with "An internal server error occurred; please
    try again later", and the engine reconnects within a second. The notice for that
    used to be the one for a key that does not work — "Speech recognition is
    unavailable (RuntimeError). Check your Deepgram key and network." — because the
    "have we ever connected" flag was set after the pump *returned*, which is when
    the socket has already ended. So the first drop after a working connection sent
    the user to check a key that was fine.
    """

    async def test_a_blip_says_reconnecting_and_then_says_it_recovered(self, monkeypatch):
        sockets = FakeSttSocket(["Deepgram error: An internal server error occurred", None])
        notices: list[str | None] = []

        async def on_error(message):
            notices.append(message)

        engine = SpeechToText(
            make_settings(stt_api="v2"), on_transcript=lambda event: None, on_error=on_error
        )
        engine._stopped = asyncio.Event()

        class Context:
            async def __aenter__(self):
                sockets.attempts += 1
                return sockets

            async def __aexit__(self, *_exc):
                return False

        def fake_connect(*_args, **_kwargs):
            return Context()

        monkeypatch.setattr(stt_module.websockets, "connect", fake_connect)

        task = asyncio.create_task(engine._run())
        # Long enough for the first attempt to fail and the second to come up.
        for _ in range(50):
            if len(notices) >= 2:
                break
            await asyncio.sleep(0.05)
        engine._stopped.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert notices[0] == "Speech recognition dropped; reconnecting."
        assert "key" not in (notices[0] or ""), "a blip is not a key problem"
        assert notices[1] is None, "coming back is reported, so the notice can be retracted"

    async def test_a_socket_that_never_comes_up_is_a_key_problem(self, monkeypatch):
        """The other direction has to keep working: a key that cannot connect at all
        is exactly what the fix text is for."""
        notices: list[str | None] = []

        async def on_error(message):
            notices.append(message)

        engine = SpeechToText(
            make_settings(stt_api="v2"), on_transcript=lambda event: None, on_error=on_error
        )
        engine._stopped = asyncio.Event()

        class Failing:
            async def __aenter__(self):
                raise ConnectionRefusedError("no route to host")

            async def __aexit__(self, *_exc):
                return False

        monkeypatch.setattr(stt_module.websockets, "connect", lambda *a, **k: Failing())

        task = asyncio.create_task(engine._run())
        for _ in range(50):
            if notices:
                break
            await asyncio.sleep(0.05)
        engine._stopped.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert notices and "Check your Deepgram key and network." in (notices[0] or "")
        assert engine._ever_connected is False


class TestTheUtteranceLogReportsTheUtterance:
    """The counters run from the moment the microphone opens, not per utterance.

    Logging them directly printed "66.18s" beside a two-second sentence, which reads
    as the utterance being enormous — and it was read that way, in a real report,
    before a look at the code said otherwise. The line now gives the speech since the
    previous utterance and the session total, labelled.
    """

    def test_the_log_distinguishes_the_utterance_from_the_session(self, caplog):

        from surtitle.core.session import Session

        session = Session.__new__(Session)
        session._audio_seconds = 66.18
        session._seconds_at_utterance = 64.0

        spoken = session._audio_seconds - session._seconds_at_utterance

        assert round(spoken, 2) == 2.18
        assert session._audio_seconds == 66.18, "the session total is still available"

    def test_the_line_names_both(self):
        import inspect

        from surtitle.core.session import Session

        source = inspect.getsource(Session._start_spoken_turn)

        assert "of speech" in source and "into this listening session" in source
        assert "self._seconds_at_utterance = self._audio_seconds" in source
