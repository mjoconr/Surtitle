"""Voice engine selection: which implementation of each half of the pipeline.

The two halves of the voice pipeline are independent, and so is the choice of
*how* each is produced. This module is the single place that decides, so
``core/session.py`` never learns whether transcription came from a WebSocket to
Deepgram or from an ONNX model in this process.

Two rules shape the design:

* **A local engine must be optional at import time.** ``sherpa_onnx`` is an extra,
  and the application has to stay importable, testable and startable without it.
  The import therefore happens inside the local engines' ``start()``, never at
  module import.
* **A missing local model is a reported condition, not a crash.** The filesystem
  check happens here, eagerly, so the reason a microphone will not work is known
  before the user clicks it — not discovered by it silently recording nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from surtitle.config import Settings
from surtitle.voice.models import ModelUnavailable
from surtitle.voice.stt import SpeechToText, TranscriptEvent
from surtitle.voice.tts import TextToSpeech

__all__ = [
    "SttEngine",
    "TtsEngine",
    "VoiceBundle",
    "VoiceUnavailable",
    "build_voice",
]


class SttEngine(Protocol):
    """What the session requires of a speech recogniser.

    Mirrors :class:`surtitle.voice.stt.SpeechToText` exactly, so the hosted
    and local implementations are interchangeable and the session does not need
    to know which one it has.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def push_audio(self, frame: bytes) -> None: ...

    def set_suppression(self, suppressed: bool) -> None: ...

    @property
    def url(self) -> str: ...


class TtsEngine(Protocol):
    """What the session requires of a speech synthesiser."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def speak(self, text: str, *, final: bool = False) -> None: ...

    def end_of_turn(self) -> None: ...

    async def barge_in(self) -> None: ...

    async def wait_until_idle(self, *, timeout: float | None = None) -> None: ...

    @property
    def is_speaking(self) -> bool: ...

    @property
    def pending(self) -> int: ...


class VoiceUnavailable(RuntimeError):
    """Voice cannot be provided, with a reason and a fix worth showing a user.

    Reported through the session's ``ready`` payload and by ``doctor`` rather
    than swallowed, because "the microphone does nothing" is the single hardest
    voice failure to diagnose from the outside.
    """

    def __init__(self, reason: str, *, fix: str | None = None, backend: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.fix = fix
        self.backend = backend


@dataclass(slots=True)
class VoiceBundle:
    """The engines a session should run, or why some of them are absent."""

    stt: SttEngine | None = None
    tts: TtsEngine | None = None
    # Every reason a configured direction could not be built. A list rather than
    # a single value because both directions can fail for different reasons, and
    # reporting only the first would hide half of what the user has to fix.
    problems: list[VoiceUnavailable] = field(default_factory=list)

    @property
    def problem(self) -> VoiceUnavailable | None:
        """The first problem, for callers that report only one line."""
        return self.problems[0] if self.problems else None

    @property
    def enabled(self) -> bool:
        """True when at least one direction is available."""
        return self.stt is not None or self.tts is not None

    @property
    def fully_enabled(self) -> bool:
        """True when speech in *and* out are available.

        This is what the UI's mic button reflects: a session with recognition
        but no synthesis is usable, but not as a spoken conversation.
        """
        return self.stt is not None and self.tts is not None


def _check_local_ready(settings: Settings, direction: str) -> None:
    """Raise when the local model for one direction is not installed.

    Called before the engine is constructed, so a broken install is reported at
    startup naming the missing file — rather than discovered when the microphone
    silently records nothing. The *import* of sherpa-onnx is deliberately not
    checked here: that happens when the engine starts, so the message can name
    the install command instead of leaking an ImportError.
    """
    from surtitle.voice import models

    try:
        if direction == "stt":
            models.resolve_stt(settings)
        else:
            models.resolve_tts(settings)
    except ModelUnavailable as exc:
        raise VoiceUnavailable(str(exc), fix=exc.fix, backend="local") from exc


def _local_stt_class():
    from surtitle.voice.local_stt import LocalSpeechToText

    return LocalSpeechToText


def _local_tts_class():
    from surtitle.voice.local_tts import LocalTextToSpeech

    return LocalTextToSpeech


def build_voice(
    settings: Settings,
    *,
    on_transcript: Callable[[TranscriptEvent], Awaitable[None]],
    on_audio: Callable[[bytes, int], Awaitable[None]],
    on_started: Callable[[], Awaitable[None]] | None = None,
    on_finished: Callable[[], Awaitable[None]] | None = None,
    on_error: Callable[[str], Awaitable[None]] | None = None,
    on_speed_fallback: Callable[[float], Awaitable[None]] | None = None,
) -> VoiceBundle:
    """Construct the configured engines, or explain why one is missing.

    The two directions are built independently on purpose. A missing local voice
    model must not take away recognition, and a missing Deepgram key must not
    take away a local voice — the user asked for two things, and half of them
    working is strictly better than none. ``problem`` carries the reason for
    whichever half is absent.
    """
    if not settings.voice_enabled:
        return VoiceBundle()

    stt: SttEngine | None = None
    tts: TtsEngine | None = None
    problems: list[VoiceUnavailable] = []

    if settings.stt_backend == "local":
        try:
            _check_local_ready(settings, "stt")
        except VoiceUnavailable as exc:
            problems.append(exc)
        else:
            stt = _local_stt_class()(settings, on_transcript=on_transcript, on_error=on_error)
    elif settings.deepgram_key():
        stt = SpeechToText(settings, on_transcript=on_transcript, on_error=on_error)
    else:
        problems.append(
            VoiceUnavailable(
                "speech recognition is set to Deepgram but DEEPGRAM_API_KEY is not set",
                fix="Add a Deepgram key in Settings, or switch the engine to local.",
                backend="deepgram",
            )
        )

    if settings.tts_backend == "local":
        try:
            _check_local_ready(settings, "tts")
        except VoiceUnavailable as exc:
            problems.append(exc)
        else:
            tts = _local_tts_class()(
                settings,
                on_audio=on_audio,
                on_started=on_started,
                on_finished=on_finished,
                on_error=on_error,
                on_speed_fallback=on_speed_fallback,
            )
    elif settings.deepgram_key():
        tts = TextToSpeech(
            settings,
            on_audio=on_audio,
            on_started=on_started,
            on_finished=on_finished,
            on_error=on_error,
            on_speed_fallback=on_speed_fallback,
        )
    else:
        problems.append(
            VoiceUnavailable(
                "spoken replies are set to Deepgram but DEEPGRAM_API_KEY is not set",
                fix="Add a Deepgram key in Settings, or switch the engine to local.",
                backend="deepgram",
            )
        )

    return VoiceBundle(stt=stt, tts=tts, problems=problems)
