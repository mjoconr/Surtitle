"""Tests for the local speech engines and the voice engine selector.

The local engines talk to ``sherpa_onnx``, which is an optional extra. These tests
therefore never import it for real: a fake module is installed in ``sys.modules``
so the adapter's own logic — batching, endpoint policy, suppression, resampling,
barge-in — is exercised with no ONNX and no model files.

That is deliberate rather than a shortcut. The things most likely to break here
are the *policy* decisions (when a turn ends, what is emitted, what is dropped on
an interruption), and those are exactly the things a real model makes harder to
test because its output varies.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import ClassVar

import pytest

from surtitle.config import Settings
from surtitle.voice.local_tts import LocalTextToSpeech
from surtitle.voice.tts import (
    float_to_pcm16,
    resample_linear,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "DEEPSEEK_API_KEY": "k",
        "SURTITLE_MODELS_DIR": str(tmp_path / "models"),
    }
    values.update(overrides)
    return Settings(**values)


def install_fake_models(tmp_path, settings) -> None:
    """Create files of the right size so resolve_* passes without a download."""
    from surtitle.voice import models

    for resolver, attr in (
        (models.resolve_stt, settings.local_stt_model),
        (models.resolve_tts, settings.local_tts_model),
    ):
        asset = models.MODEL_REGISTRY[attr]
        root = models.model_root(settings, asset)
        for entry in asset.files:
            target = root / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
            # Cheap: the sizes only need to match, and `truncate` avoids writing
            # 18 MB of padding per test.
            with target.open("r+b") as handle:
                handle.truncate(entry.size)
        for directory in asset.requires_dirs:
            (root / directory).mkdir(parents=True, exist_ok=True)
        del resolver


class FakeStream:
    """Stands in for sherpa_onnx.OnlineStream."""

    def __init__(self):
        self.accepted = 0
        self.samples = 0
        # Frames accepted and not yet decoded. The real sherpa API answers
        # is_ready() from exactly this, and a fake that always answers True makes
        # `while is_ready(...): decode(...)` spin forever — which is a fake worth
        # getting right, because a hang is much harder to debug than a failure.
        self.pending = 0

    def accept_waveform(self, sample_rate, samples):
        self.accepted += 1
        self.samples += len(samples)
        self.pending += 1

    def input_finished(self):
        return


class FakeRecognizer:
    """Stands in for sherpa_onnx.OnlineRecognizer.

    A class rather than a stub object because the engine calls the classmethod
    ``from_transducer``; the instance it returns is the one under test.
    """

    #: The instance ``from_transducer`` should return, set by ``fake_sherpa``.
    next_instance: ClassVar[FakeRecognizer | None] = None
    #: Transcripts handed out one per decode call, set per test. Class-level
    #: because the engine only ever sees the class.
    script: ClassVar[list[str]] = []
    endpoints: ClassVar[int] = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.resets = 0
        self.decodes = 0
        self.result = ""
        if type(self).next_instance is None:
            type(self).next_instance = self

    @classmethod
    def from_transducer(cls, **kwargs):
        cls.next_instance = None
        return cls(**kwargs)

    def create_stream(self, hotwords=None):
        return FakeStream()

    def is_ready(self, stream) -> bool:
        return stream.pending > 0

    def decode_stream(self, stream) -> None:
        self.decodes += 1
        stream.pending = max(0, stream.pending - 1)
        if FakeRecognizer.script:
            # The script is a queue of transcripts, one per decode call. The last
            # value repeats once it is exhausted, which is what the real model
            # does while the speaker pauses: the same text, again.
            self.result = FakeRecognizer.script.pop(0)

    def is_endpoint(self, stream) -> bool:
        return bool(FakeRecognizer.endpoints) and self.decodes >= FakeRecognizer.endpoints

    def reset(self, stream) -> None:
        self.resets += 1
        self.result = ""

    def get_result(self, stream) -> str:
        return self.result


@pytest.fixture(autouse=True)
def clean_recognizer_state():
    """Reset the fake recogniser between tests, not while one is running.

    ``FakeRecognizer`` is stateful by design — the script of transcripts belongs
    to the test that set it — so resetting it inside ``fake_sherpa`` would wipe
    the script every time an engine was constructed. That produced a recogniser
    which decoded happily and emitted nothing, which is a confusing failure to
    debug in a test that is supposed to be the easy kind.
    """
    FakeRecognizer.next_instance = None
    FakeRecognizer.script = []
    FakeRecognizer.endpoints = 0
    yield
    FakeRecognizer.next_instance = None
    FakeRecognizer.script = []
    FakeRecognizer.endpoints = 0


def fake_sherpa() -> types.ModuleType:
    """A stand-in ``sherpa_onnx`` module.

    ``from_transducer`` is a classmethod on the real class, so the fake keeps that
    shape: a plain lambda inside a fresh ``type()`` loses the implicit class
    argument and silently returns the wrong object.
    """
    module = types.ModuleType("sherpa_onnx")
    module.OnlineRecognizer = FakeRecognizer
    module.OnlineStream = FakeStream
    module.OfflineTts = type("OfflineTts", (), {})
    module.OfflineTtsConfig = type("OfflineTtsConfig", (), {})
    module.OfflineTtsModelConfig = type("OfflineTtsModelConfig", (), {})
    module.OfflineTtsVitsModelConfig = type(
        "OfflineTtsVitsModelConfig", (), {"__init__": lambda self, **kw: None}
    )
    return module


# ---------------------------------------------------------------------------
# Turn policy — pure functions, no model at all
# ---------------------------------------------------------------------------


class TestTurnPolicy:
    """The local engine's replacement for Flux's contextual turn detector."""

    def test_a_function_word_at_the_end_earns_the_longest_extension(self, tmp_path):
        from surtitle.voice.local_stt import extension_ms

        settings = make_settings(tmp_path)
        assert extension_ms(settings, "read the file and") == settings.local_eot_extend_ms
        assert extension_ms(settings, "what is the revenue") == settings.local_eot_silence_ms
        assert extension_ms(settings, "done.") == settings.local_eot_silence_ms

    def test_extension_is_never_shorter_than_the_base_silence(self, tmp_path):
        from surtitle.voice.local_stt import extension_ms

        # A configuration where the extension is *shorter* than the base must not
        # make a turn end sooner than the plain silence rule.
        settings = make_settings(
            tmp_path,
            SURTITLE_LOCAL_EOT_SILENCE_MS="1500",
            SURTITLE_LOCAL_EOT_EXTEND_MS="300",
        )
        assert extension_ms(settings, "and") == 1500

    def test_empty_text_is_not_treated_as_unfinished(self, tmp_path):
        from surtitle.voice.local_stt import looks_unfinished

        assert looks_unfinished("") is False
        assert looks_unfinished("   ") is False

    def test_punctuation_marks_a_finished_thought(self):
        from surtitle.voice.local_stt import looks_unfinished

        assert looks_unfinished("read the report") is True
        for terminal in (".", "!", "?", "…"):
            assert looks_unfinished(f"read the report{terminal}") is False


# ---------------------------------------------------------------------------
# Local STT
# ---------------------------------------------------------------------------


class TestLocalStt:
    def _engine(self, tmp_path, monkeypatch):
        """Build a local STT engine backed by the fake recogniser."""
        from surtitle.voice import local_stt

        settings = make_settings(tmp_path)
        install_fake_models(tmp_path, settings)
        monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_sherpa())
        events: list = []

        async def on_transcript(event):
            events.append(event)

        engine = local_stt.LocalSpeechToText(settings, on_transcript=on_transcript)
        return engine, events

    async def test_start_loads_the_model_and_decodes(self, tmp_path, monkeypatch):
        FakeRecognizer.script = ["hello there", "hello there friend"]
        engine, events = self._engine(tmp_path, monkeypatch)
        await engine.start()
        try:
            await asyncio.sleep(0.05)
            assert FakeRecognizer.next_instance is not None, "the model was never built"
            assert FakeRecognizer.next_instance.kwargs.get("enable_endpoint_detection") is False, (
                "sherpa's own endpoint rules cannot be delayed, so they must stay off"
            )
            # Two batches of ten frames, so the second transcript is decoded and
            # the revision is visible as a change rather than as the same text.
            for _ in range(20):
                engine.push_audio(b"\x00\x01" * 512)
            await asyncio.sleep(0.4)
        finally:
            await engine.stop()
        decoded = [e for e in events if e.text and not e.is_end_of_turn]
        assert decoded, "the recogniser produced text but nothing was emitted"
        assert decoded[-1].text == "hello there friend"
        assert decoded[-1].final is False

    async def test_an_unchanged_revision_is_not_re_emitted(self, tmp_path, monkeypatch):
        """The recogniser repeats itself every batch while the speaker pauses."""
        FakeRecognizer.script = ["same"] * 400
        engine, events = self._engine(tmp_path, monkeypatch)
        await engine.start()
        try:
            await asyncio.sleep(0.05)
            for _ in range(40):
                engine.push_audio(b"\x00\x01" * 512)
                await asyncio.sleep(0.01)
        finally:
            await engine.stop()
        undecided = [e for e in events if e.text and not e.is_end_of_turn]
        assert len(undecided) == 1, f"a duplicate caption was emitted {len(undecided)} times"

    async def test_trailing_silence_ends_the_turn_exactly_once(self, tmp_path, monkeypatch):
        FakeRecognizer.script = ["open the report"] * 400
        engine, events = self._engine(tmp_path, monkeypatch)
        await engine.start()
        try:
            await asyncio.sleep(0.05)
            for _ in range(10):
                engine.push_audio(b"\x00\x01" * 512)
                await asyncio.sleep(0.005)
            # Silence: quiet PCM, enough batches to pass the silence threshold.
            for _ in range(60):
                engine.push_audio(b"\x00\x00" * 512)
                await asyncio.sleep(0.02)
        finally:
            await engine.stop()
        ends = [e for e in events if e.is_end_of_turn]
        assert len(ends) == 1, "a single utterance must produce exactly one turn boundary"
        assert FakeRecognizer.next_instance.resets == 1, (
            "the stream must be reset for the next utterance"
        )

    async def test_suppression_drops_transcripts_while_the_agent_speaks(
        self, tmp_path, monkeypatch
    ):
        """Otherwise the agent transcribes its own voice."""
        FakeRecognizer.script = ["the agent hearing itself"] * 400
        engine, events = self._engine(tmp_path, monkeypatch)
        await engine.start()
        try:
            await asyncio.sleep(0.05)
            engine.set_suppression(True)
            for _ in range(10):
                engine.push_audio(b"\x00\x01" * 512)
                await asyncio.sleep(0.005)
        finally:
            await engine.stop()
        assert not [e for e in events if e.text], "a suppressed transcript leaked through"

    async def test_suppression_is_released(self, tmp_path, monkeypatch):
        FakeRecognizer.script = ["first"] * 5 + ["second"] * 400
        engine, events = self._engine(tmp_path, monkeypatch)
        await engine.start()
        try:
            await asyncio.sleep(0.05)
            engine.set_suppression(True)
            for _ in range(3):
                engine.push_audio(b"\x00\x01" * 512)
                await asyncio.sleep(0.005)
            engine.set_suppression(False)
            for _ in range(10):
                engine.push_audio(b"\x00\x01" * 512)
                await asyncio.sleep(0.005)
        finally:
            await engine.stop()
        assert any(e.text for e in events), "the microphone stayed deaf after playback"

    async def test_a_missing_extra_is_reported_not_raised(self, tmp_path, monkeypatch):
        """The app must stay usable with no local extra installed."""
        from surtitle.voice import local_stt

        settings = make_settings(tmp_path)
        install_fake_models(tmp_path, settings)
        monkeypatch.setitem(sys.modules, "sherpa_onnx", None)
        notices: list[str] = []

        async def on_error(message: str) -> None:
            notices.append(message)

        async def on_transcript(_event) -> None:
            return

        engine = local_stt.LocalSpeechToText(
            settings, on_transcript=on_transcript, on_error=on_error
        )
        await engine.start()
        await asyncio.sleep(0.2)
        await engine.stop()
        assert notices, "a missing extra must be reported to the user"
        assert "voice-local" in notices[0], notices[0]

    async def test_dropped_batches_are_counted(self, tmp_path, monkeypatch):
        """A stalled decoder must be visible rather than silently deaf."""
        from surtitle.voice import local_stt

        recognizer = FakeRecognizer()
        settings = make_settings(tmp_path)
        install_fake_models(tmp_path, settings)
        monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_sherpa())
        del recognizer

        async def on_transcript(_event) -> None:
            return

        engine = local_stt.LocalSpeechToText(settings, on_transcript=on_transcript)
        # Fill the queue without ever starting the worker, which is what a stalled
        # decoder looks like from the caller's side. The engine must not be marked
        # stopped, or every frame would be discarded before it is even batched.
        for _ in range(local_stt._MAX_BATCHES * local_stt._BATCH_FRAMES + 200):
            engine.push_audio(b"\x00\x01" * 512)
        assert engine.dropped_frames > 0, "a stalled decoder must be visible"


# ---------------------------------------------------------------------------
# Local TTS helpers
# ---------------------------------------------------------------------------


class TestResampling:
    """The local voice is 22050 Hz; the browser is told 24000 Hz."""

    def test_resampling_preserves_duration(self):
        source = [0.0] * 22050  # exactly one second at 22050 Hz
        out = resample_linear(source, 22050, 24000)
        assert len(out) == 24000

    def test_identical_rates_are_a_no_op(self):
        source = [0.1, 0.2, 0.3]
        assert resample_linear(source, 24000, 24000) is source

    def test_empty_input_does_not_raise(self):
        assert len(resample_linear([], 22050, 24000)) == 0

    def test_interpolation_shape_on_a_longer_signal(self):
        """A ramp resampled up should stay a ramp, and stay in range."""
        source = [index / 100.0 for index in range(100)]
        out = resample_linear(source, 100, 200)
        assert len(out) == 200
        assert out[0] == pytest.approx(source[0])
        assert out[-1] == pytest.approx(source[-1], abs=0.02)
        assert all(0.0 <= value <= 1.0 for value in out)


class TestPcmConversion:
    def test_full_scale_is_clipped_not_wrapped(self):
        """A wrapped sample is a loud click, which is worse than clipping."""
        pcm = float_to_pcm16([2.0, -2.0, 0.0])
        values = list(pcm)
        assert len(pcm) == 6
        import array

        samples = array.array("h")
        samples.frombytes(pcm)
        assert samples[0] == 32767
        assert samples[1] == -32768
        assert samples[2] == 0
        del values

    def test_silence_stays_silent(self):
        import array

        samples = array.array("h")
        samples.frombytes(float_to_pcm16([0.0] * 10))
        assert set(samples) == {0}


# ---------------------------------------------------------------------------
# Local TTS worker semantics
# ---------------------------------------------------------------------------


class FakeAudio:
    def __init__(self, samples, sample_rate=22050):
        self.samples = samples
        self.sample_rate = sample_rate


class FakeTts:
    """Stands in for sherpa_onnx.OfflineTts."""

    sample_rate = 22050

    def __init__(self, *, delay: float = 0.0, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.generated: list[str] = []

    def generate(self, text: str):
        self.generated.append(text)
        if self.fail:
            raise RuntimeError("synthesis failed")
        if self.delay:
            import time

            time.sleep(self.delay)
        return FakeAudio([0.1] * 22050)


class TestLocalTts:
    def _engine(self, tmp_path, monkeypatch, fake: FakeTts):
        settings = make_settings(tmp_path)
        install_fake_models(tmp_path, settings)
        monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_sherpa())
        chunks: list[int] = []
        started: list[int] = []
        finished: list[int] = []

        async def on_audio(data, sequence):
            chunks.append(len(data))

        async def on_started():
            started.append(1)

        async def on_finished():
            finished.append(1)

        engine = LocalTextToSpeech(
            settings,
            on_audio=on_audio,
            on_started=on_started,
            on_finished=on_finished,
        )
        engine._tts = fake
        return engine, chunks, started, finished

    async def test_end_of_turn_releases_suppression(self, tmp_path, monkeypatch):
        """The exact bug that once made the microphone look dead after one reply."""
        from surtitle.voice.tts import Utterance

        fake = FakeTts()
        engine, chunks, started, finished = self._engine(tmp_path, monkeypatch, fake)
        await engine.start()
        try:
            engine.speak(Utterance("hello").text)
            engine.end_of_turn()
            await engine.wait_until_idle(timeout=10)
        finally:
            await engine.stop()
        assert chunks, "no audio was produced"
        assert started == [1], "speaking start must be announced exactly once"
        assert finished == [1], "speaking finish must be announced, or suppression sticks"
        assert engine.is_speaking is False

    async def test_a_silent_turn_does_not_claim_the_agent_is_speaking(self, tmp_path, monkeypatch):
        fake = FakeTts()
        engine, _chunks, started, _finished = self._engine(tmp_path, monkeypatch, fake)
        await engine.start()
        try:
            engine.end_of_turn()
            await engine.wait_until_idle(timeout=10)
        finally:
            await engine.stop()
        assert started == [], "nothing was said, so the agent was never speaking"

    async def test_barge_in_discards_in_flight_synthesis(self, tmp_path, monkeypatch):
        """Audio for interrupted text must never reach the browser."""
        fake = FakeTts(delay=0.3)
        engine, chunks, _started, _finished = self._engine(tmp_path, monkeypatch, fake)
        await engine.start()
        try:
            engine.speak("a sentence that will be interrupted")
            engine.speak("a second sentence that must never be spoken")
            engine.end_of_turn()
            await asyncio.sleep(0.05)
            await engine.barge_in()
            await asyncio.sleep(0.6)
        finally:
            await engine.stop()
        assert chunks == [], f"interrupted audio was still delivered ({chunks})"
        assert fake.generated == ["a sentence that will be interrupted"], (
            "the queued second sentence must not be synthesised at all"
        )
        assert engine.is_speaking is False

    async def test_a_synthesis_failure_is_reported_and_does_not_kill_the_worker(
        self, tmp_path, monkeypatch
    ):
        fake = FakeTts(fail=True)
        settings = make_settings(tmp_path)
        install_fake_models(tmp_path, settings)
        monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_sherpa())
        notices: list[str] = []

        async def on_audio(_data, _sequence):
            return

        async def on_error(message: str) -> None:
            notices.append(message)

        engine = LocalTextToSpeech(settings, on_audio=on_audio, on_error=on_error)
        engine._tts = fake
        await engine.start()
        try:
            engine.speak("this will fail")
            engine.end_of_turn()
            await asyncio.sleep(1.2)
        finally:
            await engine.stop()
        assert notices, "a failed synthesis must be reported, not silent"
        assert any("spoken" in n or "voice" in n.lower() for n in notices)
