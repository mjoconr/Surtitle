"""Local speech-to-text, running an ONNX model inside this process.

The contract is identical to :class:`surtitle.voice.stt.SpeechToText`, so the
session cannot tell which one it has: PCM16 frames in, ``TranscriptEvent`` out.

Two differences from the hosted engine are visible to the user and are worth
understanding:

**Turn detection is acoustic.** Deepgram's Flux decides an endpoint from *what
was said*, which is why it does not cut you off mid-thought. A streaming
zipformer offers no such judgement, so a turn ends when you have been silent for
long enough. Two things soften that: :func:`looks_unfinished` and
:func:`extension_ms` grade the wait by what the transcript looks like, giving an
apparently unfinished thought more patience than a finished one; and the ceiling
in :data:`~surtitle.config.Settings.local_max_utterance_ms` is a backstop against
someone who never pauses, never a way to end a turn by clock. It remains a
heuristic, not understanding, and ``docs/VOICE.md`` says so.

**Partial results are cumulative.** The recogniser re-emits the whole utterance
for the current stream, which is exactly the shape ``Session._accumulate``
already expects from Flux — replace, never append — so no session-side merging
logic changes for the local path.

All model work happens on one dedicated thread (``_EXECUTOR``). sherpa-onnx
streams and ONNX sessions are not documented as thread-safe, and the cost of
being certain is a single thread per engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from surtitle.config import Settings
from surtitle.voice import models
from surtitle.voice.stt import TranscriptEvent, TranscriptHandler

__all__ = ["LocalSpeechToText", "looks_unfinished"]

log = logging.getLogger(__name__)

# A single worker thread per process. A local engine that is never started costs
# nothing, and one thread means ONNX calls can never interleave.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-stt")

# Audio is accumulated and decoded in batches. Decoding every 32 ms frame is
# almost entirely Python and ONNX call overhead; 320 ms amortises it tenfold and
# is far below the scale of a conversational turn.
_BATCH_FRAMES = 10
# One batch is 320 ms, so this bounds the backlog at ~20 s of audio. Beyond that
# audio is dropped, matching the hosted client's reasoning: a transcript that
# lags the speaker is worse than a clipped phoneme.
_MAX_BATCHES = 64
# Below this RMS the batch is treated as silence for the endpoint timer. The
# recogniser itself does not report "no speech", and a level check is enough to
# decide whether the silence clock advances.
_RMS_FLOOR = 0.004
# How much silence the backstop needs before it may close a turn. One full batch,
# so it means "the speaker actually paused" rather than a gap between words: the
# backstop exists to stop someone who never pauses, and must never be the thing
# that cuts off someone who is still talking.
_CEILING_PAUSE_MS = 300.0

# Words that mean the sentence has not finished, so the turn stays open a little
# longer. Deliberately short: an over-eager list makes the agent feel
# unresponsive, which is a worse failure than occasionally interrupting.
_TRAILING_CUES = frozenset(
    {
        "and",
        "but",
        "or",
        "nor",
        "so",
        "because",
        "if",
        "then",
        "when",
        "while",
        "that",
        "which",
        "with",
        "without",
        "for",
        "from",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "as",
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "will",
        "please",
        "uh",
        "um",
        "er",
    }
)
_TERMINATORS = ".!?…"


def looks_unfinished(text: str) -> bool:
    """True when a transcript is probably mid-thought.

    Used to extend the silence window before declaring a turn over. The signal
    is deliberately cheap: this streaming model does not punctuate, so a
    transcript without terminal punctuation *might* be finished and might not.

    See :func:`extension_ms` for how the two strengths of signal are graded. The
    asymmetry is deliberate — a false positive costs a slightly longer pause
    before the agent answers, a false negative cuts the user off mid-sentence.
    """
    cleaned = text.strip()
    if not cleaned:
        return False
    return cleaned[-1] not in _TERMINATORS


def extension_ms(settings: Settings, text: str) -> int:
    """How long to be silent before ending a turn, given what was heard.

    Turn-taking on a local model is a guess, because the recogniser emits no
    punctuation: "the ingest worker is" and "the ingest worker is loud" are
    the same kind of thing to it. So the guess is graded, and it leans the way the
    cheaper mistake lies — a moment of extra patience costs nothing, cutting an
    explanation off mid-thought costs the whole answer.

    * A trailing function word ("and", "the", "because") is strong evidence that
      more is coming, and earns the full extension.
    * Otherwise, if the transcript cannot be *shown* to be a finished thought —
      which, with a model that emits no punctuation, is nearly all of them — the
      speaker gets the benefit of the doubt and part of the extension. Treating
      "cannot prove it finished" as "finished" is what made the agent start work
      on half an explanation.
    * Terminal punctuation, when the model does emit it, is the one positive sign
      that the thought closed, and gets the plain silence.
    """
    base = settings.local_eot_silence_ms
    extended = max(base, settings.local_eot_extend_ms)
    if _is_trailing_cue(text):
        return extended
    if looks_unfinished(text):
        return base + (extended - base) // 2
    return base


def _is_trailing_cue(text: str) -> bool:
    """True when a transcript ends in a function word that implies more to come."""
    cleaned = text.strip().rstrip(",.!?;:")
    if not cleaned:
        return False
    return cleaned.split()[-1].lower() in _TRAILING_CUES


def _rms(frame: bytes) -> float:
    """Loudness of one PCM16 frame, 0..1.

    Computed with :mod:`array` rather than numpy: an optional dependency in the
    hot audio path is one more thing the release has to install, and this is a
    few hundred samples per frame.
    """
    from array import array

    samples = array("h")
    samples.frombytes(frame[: len(frame) - (len(frame) % 2)])
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += value * value
    return (total / len(samples)) ** 0.5 / 32768.0


class _LocalSession:
    """One sherpa-onnx recogniser stream, owned by the worker thread.

    Wrapped in an object so the tuning rules read as code: the decode loop calls
    :meth:`feed`, then asks for :meth:`result` and :meth:`endpoint`.
    """

    def __init__(self, sherpa_onnx: Any, settings: Settings, paths: dict[str, str]) -> None:
        self._sh = sherpa_onnx
        self.settings = settings
        self.paths = paths
        self.model_type = paths.get("model_type", "")

        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=paths["tokens"],
            encoder=paths["encoder"],
            decoder=paths["decoder"],
            joiner=paths["joiner"],
            num_threads=2,
            sample_rate=settings.stt_sample_rate,
            feature_dim=80,
            # Endpointing is handled in this module rather than by sherpa's own
            # rules, because the extension heuristic needs to *delay* an
            # endpoint and sherpa would already have torn the stream down.
            enable_endpoint_detection=False,
            model_type=self.model_type,
        )
        self._stream = self._recognizer.create_stream()

    def feed(self, batch: bytes) -> tuple[str, bool]:
        """Accept one batch of PCM16 and decode everything now available.

        Returns the transcript for the utterance so far and whether sherpa
        considers this an endpoint. The endpoint flag is reported up rather than
        acted on, so the caller owns the turn policy and can see it.
        """
        from array import array

        samples = array("h")
        samples.frombytes(batch[: len(batch) - (len(batch) % 2)])
        float_samples = [value / 32768.0 for value in samples]
        self._stream.accept_waveform(self.settings.stt_sample_rate, float_samples)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        return self.result(), self.endpoint()

    def result(self) -> str:
        return str(self._recognizer.get_result(self._stream) or "")

    def endpoint(self) -> bool:
        return bool(self._recognizer.is_endpoint(self._stream))

    def reset(self) -> None:
        """Close the current utterance and begin a new one."""
        self._recognizer.reset(self._stream)

    def close(self) -> None:
        """Nothing to release explicitly; the objects are garbage collected."""
        self._stream = None
        self._recognizer = None


class LocalSpeechToText:
    """A streaming recogniser backed by a local ONNX model."""

    def __init__(
        self,
        settings: Settings,
        *,
        on_transcript: TranscriptHandler,
        on_error: Any = None,
    ) -> None:
        self.settings = settings
        self._on_transcript = on_transcript
        self._on_error = on_error

        self._paths = models.resolve_stt(settings)
        self._batch: list[bytes] = []
        self._batches: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_MAX_BATCHES)
        self._worker: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._suppress_finals = False

        # Diagnostics, mirroring the hosted client so the Activity panel and the
        # log read the same whichever engine produced them.
        self._dropped_frames = 0
        self._suppressed_transcripts = 0
        self._frames_in = 0
        self._silence_ms = 0.0
        self._utterance_ms = 0.0
        # Last transcript the recogniser produced, used only to detect that the
        # speaker is still talking.
        self._spoken_text = ""
        # Last transcript handed to the session, used only to suppress duplicate
        # caption updates for an unchanged revision.
        self._emitted_text = ""
        self._failed = False

    # --- configuration ---------------------------------------------------
    @property
    def url(self) -> str:
        """Where recognition comes from, for ``doctor`` and the log.

        Deliberately not a fake socket URL: a local model has no endpoint, and
        printing one would send the next reader looking for a server.
        """
        return f"local:{self.settings.local_stt_model}"

    @property
    def uses_flux(self) -> bool:
        """Always false: a local model has no contextual turn detector."""
        return False

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    # --- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Load the model and begin decoding.

        The import and the model load both happen here rather than at import
        time, so the application, its test suite and its release archive never
        require the optional extra.
        """
        if self._worker is not None and not self._worker.done():
            return
        self._stopped.clear()
        self._worker = asyncio.create_task(self._decode_loop(), name="local-stt")

    async def stop(self) -> None:
        self._stopped.set()
        with contextlib.suppress(asyncio.QueueFull):
            self._batches.put_nowait(None)
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None

    # --- audio in --------------------------------------------------------
    def push_audio(self, frame: bytes) -> None:
        """Queue a PCM16 frame, batching ~320 ms before decoding.

        Dropping the oldest batch when the queue is full is deliberate and
        logged: an unbounded queue would let the transcript fall further behind
        the speaker, and a stalled decoder is otherwise invisible.
        """
        if self._stopped.is_set() or not frame:
            return
        self._frames_in += 1
        self._batch.append(frame)
        if len(self._batch) < _BATCH_FRAMES:
            return
        batch = b"".join(self._batch)
        self._batch.clear()
        try:
            self._batches.put_nowait(batch)
        except asyncio.QueueFull:
            self._dropped_frames += 1
            if self._dropped_frames == 1 or self._dropped_frames % 50 == 0:
                log.warning(
                    "local speech audio backing up: %d batch(es) dropped",
                    self._dropped_frames,
                )
            with contextlib.suppress(asyncio.QueueEmpty):
                self._batches.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._batches.put_nowait(batch)

    def set_suppression(self, suppressed: bool) -> None:
        """Enable or disable dropping finals while the agent is speaking.

        Read from the worker thread, so it is a plain flag rather than
        something that needs the event loop.
        """
        self._suppress_finals = suppressed
        if suppressed:
            self._suppressed_transcripts = 0

    # --- decoding --------------------------------------------------------
    async def _decode_loop(self) -> None:
        """Decode batches on one thread, translating endpoints into turns."""
        loop = asyncio.get_running_loop()
        try:
            session = await loop.run_in_executor(_EXECUTOR, self._build_session)
        except Exception as exc:
            self._failed = True
            await self._report(
                f"Local speech recognition could not start ({type(exc).__name__}: {exc})."
            )
            return

        log.info(
            "local STT ready (%s, silence %.0fms)",
            self.settings.local_stt_model,
            self.settings.local_eot_silence_ms,
        )
        try:
            while not self._stopped.is_set():
                try:
                    batch = await asyncio.wait_for(self._batches.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if batch is None:
                    return
                try:
                    text, endpoint = await loop.run_in_executor(_EXECUTOR, session.feed, batch)
                except Exception as exc:
                    self._failed = True
                    log.warning("local STT decode failed: %s", exc, exc_info=True)
                    await self._report("Local speech recognition stopped after an error.")
                    return
                await self._handle_batch(session, text, endpoint, batch)
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await loop.run_in_executor(_EXECUTOR, session.close)

    def _build_session(self) -> _LocalSession:
        """Import sherpa-onnx and construct the recogniser, on the worker thread."""
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise ImportError(
                "the local voice extra is not installed. Run "
                "`uv sync --extra voice-local` (or `pip install "
                "'surtitle[voice-local]'`), then restart."
            ) from exc
        return _LocalSession(sherpa_onnx, self.settings, self._paths)

    async def _handle_batch(
        self, session: _LocalSession, text: str, endpoint: bool, batch: bytes
    ) -> None:
        """Apply the turn policy to one decoded batch."""
        batch_ms = 1000.0 * (len(batch) / 2) / self.settings.stt_sample_rate
        self._utterance_ms += batch_ms
        text = text.strip()

        silent = _rms(batch) < _RMS_FLOOR
        # A *changed* transcript means words arrived, whatever the level says; a
        # batch that is not silent means audio is arriving even if the recogniser
        # has not revised the text yet. Both reset the silence clock, and they are
        # kept separate from what was last *emitted* so that dropping a duplicate
        # caption update cannot make a pause look like speech.
        grew = bool(text) and text != self._spoken_text
        if grew or not silent:
            self._silence_ms = 0.0
        else:
            self._silence_ms += batch_ms
        if text:
            self._spoken_text = text

        # Live captions first, then the boundary, so the last partial text is on
        # screen as the turn begins rather than a frame later. An unchanged
        # revision is dropped: the recogniser repeats the same utterance every
        # batch while the speaker pauses, and re-emitting it would put a duplicate
        # caption update on the wire ~30 times a second.
        if text and not endpoint and text != self._emitted_text:
            self._emitted_text = text
            if self._suppress_finals:
                self._note_suppressed(text)
            else:
                await self._on_transcript(TranscriptEvent(text=text, final=False, confidence=0.0))

        threshold = extension_ms(self.settings, text)
        # The ceiling is a backstop, not a turn rule: a turn ends when the thought
        # sounds finished, and a clock cannot know that. It may only close a turn
        # once the speaker has actually paused, so a long explanation is never cut
        # off mid-word -- which is exactly what happened when this fired on the
        # clock alone at 20.16 s, part-way through a sentence.
        backstop = self._utterance_ms >= self.settings.local_max_utterance_ms
        paused = self._silence_ms >= _CEILING_PAUSE_MS
        if backstop and paused:
            log.warning(
                "turn closed by the backstop after %.1fs of continuous speech while "
                "still unfinished; raise SURTITLE_LOCAL_MAX_UTTERANCE_MS if the "
                "speaker needs longer",
                self._utterance_ms / 1000.0,
            )
        ended = self._silence_ms >= threshold or (backstop and paused)
        if not ended and not endpoint:
            return

        # The turn is over. Emit whatever was heard before the boundary, then the
        # boundary itself, and start a fresh stream for the next utterance.
        final_text = text
        if final_text and self._suppress_finals:
            self._note_suppressed(final_text)
        elif final_text and final_text != self._emitted_text:
            # A final that repeats the last partial verbatim is not worth a second
            # event: the session's merge rule would replace it with itself.
            await self._on_transcript(
                TranscriptEvent(
                    text=final_text,
                    final=True,
                    confidence=0.0,
                    is_end_of_turn=False,
                )
            )
        if not self._suppress_finals:
            await self._on_transcript(TranscriptEvent(text="", final=True, is_end_of_turn=True))
        self._reset_utterance(session)

    def _reset_utterance(self, session: _LocalSession) -> None:
        """Clear per-utterance state after a turn boundary."""
        session.reset()
        self._silence_ms = 0.0
        self._utterance_ms = 0.0
        self._spoken_text = ""
        self._emitted_text = ""

    def _note_suppressed(self, text: str) -> None:
        """Record a transcript dropped as the agent's own voice.

        Logged at INFO for the first few and then sparsely: suppression that
        never lifts looks exactly like a broken microphone, and that failure was
        invisible in this project once already.
        """
        self._suppressed_transcripts += 1
        if self._suppressed_transcripts <= 3 or self._suppressed_transcripts % 25 == 0:
            log.info(
                "dropping local transcript %d during playback (echo suppression): %r",
                self._suppressed_transcripts,
                text[:60],
            )

    async def _report(self, message: str) -> None:
        if self._on_error is not None:
            with contextlib.suppress(Exception):
                await self._on_error(message)
