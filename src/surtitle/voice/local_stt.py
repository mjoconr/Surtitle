"""Local speech-to-text, running an ONNX model inside this process.

The contract is identical to :class:`surtitle.voice.stt.SpeechToText`, so the
session cannot tell which one it has: PCM16 frames in, ``TranscriptEvent`` out.

Two differences from the hosted engine are visible to the user and are worth
understanding:

**Turn detection is acoustic.** Deepgram's Flux decides an endpoint from *what
was said*, which is why it does not cut you off mid-thought. A streaming
zipformer offers no such judgement, so a turn ends when you have been silent for
long enough — helped by the fact that the default model, unlike the older ones,
punctuates: a sentence that ends in a full stop is a thought that closed, and one
that trails off gets more patience. Two things soften what is otherwise a timer:
:func:`looks_unfinished` and :func:`extension_ms` grade the wait by what the
transcript looks like; and the ceiling in
:data:`~surtitle.config.Settings.local_max_utterance_ms` is a backstop against
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
# When a turn is closed regardless of whether the speaker paused.
#
# The backstop above needs a pause as well as the clock, because a clock cannot tell a
# finished thought from a comma — and that is right for speech. It is not enough for
# audio that never falls quiet: measured in a real session, a room loud enough to keep
# the silence clock at zero (the agent's own voice leaking in after suppression was
# lifted early) produced a single utterance of **67 seconds**, decoded to the letter
# "S". Nothing was ever going to count as a pause, so nothing was ever going to end it.
# This ceiling is deliberately far above any spoken turn so it can only ever catch
# that case, and it scales with a raised utterance limit.
_HARD_CEILING_MS = 60_000.0
# An utterance at least this long that decoded to a couple of characters is a
# recognition failure, not speech. Handing it on costs a whole turn and an answer to
# nothing; asking again costs a sentence.
_IMPLAUSIBLE_AFTER_MS = 10_000.0
_IMPLAUSIBLE_CHARS = 2

# How much trailing silence must be in hand before *anything* may end a turn.
#
# A streaming transducer emits a word only once it has heard the audio that
# follows it, so the silence after a sentence is also what flushes the last word
# of it. Measured by decoding one clip with varying amounts of silence appended:
#
#     0.32 s   "I'M FROM THE CUTTER LYING OFF THE COA"
#     0.80 s   "I'M FROM THE CUTTER LYING OFF THE COAST"
#
# So this bounds the ordinary silence rule and the backstop alike, whatever the
# configured window says. A shorter one would quietly clip the end off every
# utterance, which reads as a recognition failure rather than as a setting -- and
# the shipped default is exactly this value, so the floor changes nothing until
# somebody lowers it.
_FLUSH_MS = 800.0

# How much silence :meth:`LocalSpeechToText.finish_utterance` queues when the
# microphone is switched off mid-sentence. Longer than the turn floor above
# because it is measured per clip rather than on average, and because it costs
# nothing: the pad is decoded, not played, so 1.6 s of it is about 0.13 s of CPU.
# Measured through the engine on far-field clips, decoding the same audio with an
# 0.8/1.0/1.2 s pad left the last word or two off ("…talk to Steve" for "…talk to
# Steve next week?") and 1.6 s completed it.
_FINISH_PAD_MS = 1600.0

# How long :meth:`LocalSpeechToText.finish_utterance` waits for the decoder to
# reach the pad it queued. The work itself is a fraction of a second; the wait is
# really for a backlog in front of it, and the caller cannot hold the microphone
# toggle open for long. Nothing depends on the answer: the turn closes when the
# pad is reached, whether that is now or a moment later.
_FINISH_TIMEOUT_S = 2.0


class _FlashPad(bytes):
    """The silence :meth:`LocalSpeechToText.finish_utterance` queues.

    A marker as well as audio. The decode loop closes the turn when it *reaches*
    this batch, not when the request was made, so speech that was already queued
    is always decoded into the turn before it ends — and a request that arrives
    while the decoder is behind cannot cut an utterance short.
    """


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
    is deliberately cheap: not every local model punctuates — the default one
    does, the older one does not — so a transcript without terminal punctuation
    *might* be finished and might not.

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

    Turn-taking on a local model is a guess. The default model punctuates, which
    makes "the ingest worker is loud." distinguishable from "the ingest worker
    is"; the older model emits no punctuation at all, and there "the ingest worker
    is" and "the ingest worker is loud" are the same kind of thing to it. So the
    guess is graded, and it leans the way the cheaper mistake lies — a moment of
    extra patience costs nothing, cutting an explanation off mid-thought costs the
    whole answer.

    * A trailing function word ("and", "the", "because") is strong evidence that
      more is coming, and earns the full extension.
    * Otherwise, if the transcript cannot be *shown* to be a finished thought —
      which, with a model that emits no punctuation, is nearly all of them — the
      speaker gets the benefit of the doubt and part of the extension. Treating
      "cannot prove it finished" as "finished" is what made the agent start work
      on half an explanation.
    * Terminal punctuation, when the model does emit it, is the one positive sign
      that the thought closed, and gets the plain silence.

    No answer is shorter than :data:`_FLUSH_MS`: below that the recogniser is
    still holding the last word, so a shorter window would not end the turn
    early — it would end it with the sentence truncated.
    """
    base = max(settings.local_eot_silence_ms, _FLUSH_MS)
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
        # Set when the utterance has been closed by :meth:`finish_utterance`,
        # which is how "that is my turn" arrives without any more audio. Read and
        # written on the event loop, cleared at each turn boundary.
        self._finish_requested = False
        self._turn_closed = asyncio.Event()
        # Whether any audio has arrived for the utterance in progress. Recorded
        # when it is queued rather than when it is decoded, because the two are
        # not the same thing: a decoder that is behind has heard nothing *yet*,
        # and "nothing yet" must not be read as "nothing said".
        self._audio_since_turn = False

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
        self._audio_since_turn = True
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

    async def finish_utterance(self) -> bool:
        """Close the current turn, including the word the recogniser is holding.

        Called when the user switches the microphone off mid-sentence: they are
        saying "that is my turn", and no more audio is coming. A streaming
        transducer emits a word only once it has heard the audio that follows it,
        so without this the end of the sentence would be missing from the turn —
        the same truncation the turn boundary avoids, arriving by another route.

        The silence is pushed through the ordinary queue, so the turn boundary,
        the final transcript and the stream reset all come out of the code that
        handles a pause. Returns whether this engine has taken responsibility for
        delivering the utterance: true once the pad is queued, because the turn
        closes when the decoder reaches it even if that is a moment later. False
        means nothing was heard, or the engine is not running, and the caller
        should send what it has.

        The wait is for the turn boundary, and it is bounded because the caller is
        holding a microphone toggle open; a decoder that is behind simply reports
        its turn a little later.
        """
        if self._stopped.is_set() or self._failed or self._worker is None:
            return False
        if not self._audio_since_turn and not self._spoken_text:
            # Nothing has arrived since the last turn: there is no word being
            # held, and a turn boundary here would only report silence.
            return False
        self._turn_closed.clear()
        self._enqueue(_FlashPad(self._flush_pad()))
        try:
            await asyncio.wait_for(self._turn_closed.wait(), timeout=_FINISH_TIMEOUT_S)
        except TimeoutError:
            log.info(
                "the recogniser is still decoding what was said; the turn will close "
                "when it reaches the end of it"
            )
        return True

    def _flush_pad(self) -> bytes:
        """The silence a streaming recogniser needs to emit its last word."""
        samples = int(self.settings.stt_sample_rate * _FINISH_PAD_MS / 1000.0)
        return b"\x00\x00" * samples

    def _enqueue(self, batch: bytes) -> None:
        """Queue one batch ahead of the frame batching in :meth:`push_audio`."""
        if self._batch:
            # Whatever is buffered is older audio and has to stay in front of it.
            with contextlib.suppress(asyncio.QueueFull):
                self._batches.put_nowait(b"".join(self._batch))
            self._batch.clear()
        try:
            self._batches.put_nowait(batch)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._batches.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._batches.put_nowait(batch)

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
                if isinstance(batch, _FlashPad):
                    # The end of what the user said has been decoded, so the turn
                    # may be closed -- and only now, which is what keeps a request
                    # that arrives while the decoder is behind from cutting an
                    # utterance short.
                    self._finish_requested = True
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
        # clock alone at 20.16 s, part-way through a sentence. The pause it waits
        # for is the flush, not a token one: closing sooner would cut the last word
        # off the very sentence it is trying to rescue.
        backstop = self._utterance_ms >= self.settings.local_max_utterance_ms
        paused = self._silence_ms >= _FLUSH_MS
        if backstop and paused:
            log.warning(
                "turn closed by the backstop after %.1fs of continuous speech while "
                "still unfinished; raise SURTITLE_LOCAL_MAX_UTTERANCE_MS if the "
                "speaker needs longer",
                self._utterance_ms / 1000.0,
            )
        ceiling = max(_HARD_CEILING_MS, 3 * self.settings.local_max_utterance_ms)
        exhausted = self._utterance_ms >= ceiling
        if exhausted:
            log.warning(
                "turn closed at the %.0fs ceiling with no pause in it; %.1fs of audio produced %r",
                ceiling / 1000.0,
                self._utterance_ms / 1000.0,
                text[:40],
            )
        ended = (
            self._silence_ms >= threshold
            or (backstop and paused)
            or exhausted
            # The user switched the microphone off, and the silence that flushes
            # the last word has already been decoded: whatever the transcript
            # looks like, this is the end of the turn. Note that it does not also
            # require a pause — the flush itself revises the text, and a revision
            # resets the silence clock.
            or self._finish_requested
        )
        if not ended and not endpoint:
            return

        # The turn is over. Emit whatever was heard before the boundary, then the
        # boundary itself, and start a fresh stream for the next utterance.
        final_text = text
        if (
            final_text
            and len(final_text) <= _IMPLAUSIBLE_CHARS
            and self._utterance_ms >= _IMPLAUSIBLE_AFTER_MS
        ):
            # Not a transcript. Something was heard for ten seconds or more and the
            # recogniser came back with a letter or two, which is a failure to decode
            # rather than a thing somebody said.
            log.warning(
                "local STT decoded %r from %.1fs of audio; asking for it again rather "
                "than passing it on",
                final_text,
                self._utterance_ms / 1000.0,
            )
            await self._report(
                "I did not catch that — say it again.",
            )
            final_text = ""
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
        # After the session has been told, so whoever asked for this turn to close
        # can rely on the transcript having been handed over.
        self._turn_closed.set()

    def _reset_utterance(self, session: _LocalSession) -> None:
        """Clear per-utterance state after a turn boundary."""
        session.reset()
        self._silence_ms = 0.0
        self._utterance_ms = 0.0
        self._spoken_text = ""
        self._emitted_text = ""
        self._finish_requested = False
        self._audio_since_turn = False

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
