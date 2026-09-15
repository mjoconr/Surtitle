"""Text-to-speech with low-latency streaming and true barge-in.

Two design points matter more than anything else here, and both survive the
choice of engine:

**One socket per turn, closed on barge-in** (Deepgram). When the user
interrupts, the socket cannot simply stop being read: Deepgram has already
synthesised audio for text we sent, and that backlog would arrive later and
sound like the agent ignoring the interruption. Closing discards it by
construction.

**Sentences, not paragraphs.** The speak layer hands us one sentence at a time,
so the first words are audible while the model is still generating the rest.

The engine-independent parts — the queue, the generation counter that makes
barge-in work, the speaking/no-longer-speaking transitions that drive echo
suppression — live in :class:`_UtteranceWorker`. That is deliberate: echo
suppression that is set and never cleared made the microphone appear dead after
the first reply once already, and a second engine duplicating that logic would
be a second chance to reintroduce it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from array import array
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import websockets
from websockets.protocol import State

from surtitle.config import DEEPGRAM_SPEAK_URL, Settings

__all__ = [
    "BargeIn",
    "TextToSpeech",
    "Utterance",
    "float_to_pcm16",
    "resample_linear",
    "speak_url",
]

log = logging.getLogger(__name__)

_BACKOFF_SCHEDULE = (0.5, 1.0, 2.0, 4.0)

# Local synthesis is CPU-bound and produce-then-play rather than socket-streamed,
# so its backoff is shorter and its retry budget smaller: a failure here is a
# model problem, not a network one, and retrying it forever would just be noise.
_LOCAL_BACKOFF = (0.25, 0.5)


class BargeIn(Exception):
    """Raised inside the synthesis loop when the user interrupts."""


@dataclass(slots=True, frozen=True)
class Utterance:
    """A short piece of text to speak."""

    text: str
    # Set when the speak layer emitted the closing chunk of a ``<say>`` block.
    final: bool = False


AudioHandler = Callable[[bytes, int], Awaitable[None]]
"""Receives (audio_bytes, sequence) for each synthesised chunk."""


def speak_url(settings: Settings, *, speed_supported: bool = True) -> str:
    """Build the Deepgram speak socket URL.

    Shared with the doctor so a diagnostic probes exactly what a session uses.
    ``speed`` is only sent when it differs from natural: not every Aura voice
    accepts it, so it is dropped and retried without it when refused.
    """
    params: dict[str, object] = {
        "model": settings.tts_model,
        "encoding": "linear16",
        "sample_rate": settings.tts_sample_rate,
    }
    if speed_supported and settings.tts_speed != 1.0:
        params["speed"] = settings.tts_speed
    return f"{DEEPGRAM_SPEAK_URL}?{urlencode(params)}"


def float_to_pcm16(samples: Any) -> bytes:
    """Convert float samples in -1..1 to little-endian PCM16 bytes."""
    out = array("h")
    for value in samples:
        scaled = int(value * 32767.0)
        out.append(-32768 if scaled < -32768 else 32767 if scaled > 32767 else scaled)
    return out.tobytes()


class _UtteranceWorker:
    """Queue, barge-in generations and speaking transitions, engine-independent.

    Subclasses implement :meth:`_produce`, which yields PCM16 chunks for one
    utterance. Everything else — including the exact order in which ``on_started``
    and ``on_finished`` fire, which is what keeps echo suppression honest — is
    shared.
    """

    #: Announced start failure text, overridden per engine.
    failure_notice = "Voice output hiccup; reconnecting."
    backoff: tuple[float, ...] = _BACKOFF_SCHEDULE

    def __init__(
        self,
        settings: Settings,
        *,
        on_audio: AudioHandler,
        on_started: Callable[[], Awaitable[None]] | None = None,
        on_finished: Callable[[], Awaitable[None]] | None = None,
        on_error: Callable[[str], Awaitable[None]] | None = None,
        on_speed_fallback: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self._on_audio = on_audio
        self._on_started = on_started
        self._on_finished = on_finished
        self._on_error = on_error
        self._on_speed_fallback = on_speed_fallback

        self._queue: asyncio.Queue[Utterance | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._active = False
        self._stopped = False
        self._sequence = 0
        # Incremented by barge_in(); a worker notices the change and abandons the
        # turn instead of finishing its queue.
        self._generation = 0

    # --- properties ------------------------------------------------------
    @property
    def is_speaking(self) -> bool:
        """True while audio for the current turn is being produced."""
        return self._active

    @property
    def pending(self) -> int:
        """Number of utterances waiting to be synthesised."""
        return self._queue.qsize()

    @property
    def url(self) -> str:
        """Where audio comes from, for the log and ``doctor``."""
        return "local"

    # --- public API ------------------------------------------------------
    async def start(self) -> None:
        """Start the synthesis worker."""
        if self._worker is None or self._worker.done():
            self._stopped = False
            self._worker = asyncio.create_task(self._run(), name="tts-worker")

    async def stop(self) -> None:
        """Shut down the worker and release any engine resource."""
        self._stopped = True
        self._generation += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None
        await self._close()
        await self._finish_if_active()

    def speak(self, text: str, *, final: bool = False) -> None:
        """Queue text for speech. Safe to call from anywhere on the loop."""
        if self._stopped or not text.strip():
            return
        self._queue.put_nowait(Utterance(text=text.strip(), final=final))

    def end_of_turn(self) -> None:
        """No more text is coming for this turn.

        Queues a marker that the worker turns into ``on_finished`` once every
        queued sentence has been synthesised. That callback is what releases echo
        suppression; without it the session stayed marked as speaking after its
        very first reply, so every later transcript was discarded as though it
        were the agent's own voice.
        """
        if self._stopped:
            return
        self._queue.put_nowait(Utterance(text="", final=True))

    async def barge_in(self) -> None:
        """Stop speaking immediately and discard everything queued.

        Audio already handed to the browser is dropped client-side; the
        server-side work is cancelled here.
        """
        self._generation += 1
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        await self._abandon()
        await self._finish_if_active(notify=False)

    async def wait_until_idle(self, *, timeout: float | None = None) -> None:
        """Wait until the queue is drained and synthesis has finished."""

        async def _wait() -> None:
            while self._active or not self._queue.empty():
                await asyncio.sleep(0.05)

        if timeout is None:
            await _wait()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_wait(), timeout=timeout)

    # --- worker ----------------------------------------------------------
    async def _run(self) -> None:
        """Consume utterances until stopped, surviving engine failures."""
        attempt = 0
        try:
            while not self._stopped:
                try:
                    utterance = await self._queue.get()
                except asyncio.CancelledError:
                    raise

                if utterance is None:
                    if self._stopped:
                        return
                    continue

                generation = self._generation
                try:
                    if not utterance.text:
                        # A turn boundary with nothing to say. Speaking must not be
                        # announced for it, or a silent turn (an error, a
                        # display-only reply) would flash echo suppression on and
                        # off and briefly claim the agent is talking.
                        if utterance.final and self._queue.empty():
                            await self._finish_if_active()
                        continue
                    if not self._active:
                        self._active = True
                        if self._on_started:
                            await self._on_started()
                    await self._emit(utterance, generation)
                    if utterance.final and self._queue.empty():
                        # The turn's last sentence has been produced and nothing is
                        # queued behind it, so speaking is over. Reporting that here
                        # is what releases the session's echo suppression.
                        await self._finish_if_active()
                    attempt = 0
                except BargeIn:
                    # Expected: the user interrupted. Nothing to report.
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._stopped or generation != self._generation:
                        continue
                    await self._close()
                    delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                    attempt += 1
                    log.warning("TTS failed (%s); retrying in %.1fs", exc, delay)
                    if self._on_error:
                        await self._on_error(self.failure_notice)
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        raise
                    # Re-queue once so the sentence is not silently lost.
                    if generation == self._generation and not self._stopped:
                        self._queue.put_nowait(utterance)
        finally:
            # Drain anything left and never let teardown raise out of the task.
            while not self._queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
            with contextlib.suppress(Exception):
                await self._close()
            # Do not notify here: the worker is being torn down, and the session
            # decides what to report when it stops the client.
            await self._finish_if_active(notify=False)

    async def _emit(self, utterance: Utterance, generation: int) -> None:
        """Forward one utterance's audio, checking for interruption as it goes."""
        async for chunk in self._produce(utterance, generation):
            if generation != self._generation:
                raise BargeIn
            if not chunk:
                continue
            self._sequence += 1
            await self._on_audio(chunk, self._sequence)

    async def _finish_if_active(self, *, notify: bool = True) -> None:
        """Clear the speaking flag once, optionally notifying the session."""
        if not self._active:
            return
        self._active = False
        if notify and self._on_finished:
            with contextlib.suppress(Exception):
                await self._on_finished()

    # --- engine hooks ----------------------------------------------------
    async def _produce(self, utterance: Utterance, generation: int) -> Any:
        """Yield PCM16 chunks for one utterance. Engines implement this."""
        raise NotImplementedError

    async def _close(self) -> None:
        """Release any engine resource. Default: nothing to do."""
        return

    async def _abandon(self) -> None:
        """Discard pending engine output after an interruption."""
        await self._close()


# ---------------------------------------------------------------------------
# Deepgram
# ---------------------------------------------------------------------------


class TextToSpeech(_UtteranceWorker):
    """Streams spoken audio from Deepgram Aura, one socket per turn."""

    failure_notice = "Voice output hiccup; reconnecting."

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self._api_key = settings.deepgram_key() or ""
        self._socket: object | None = None
        self._socket_lock = asyncio.Lock()
        # Set after Deepgram rejects a speed parameter, so we only retry once.
        self._speed_supported = True

    @property
    def url(self) -> str:
        """The socket URL, built by the shared helper."""
        return speak_url(self.settings, speed_supported=self._speed_supported)

    async def _produce(self, utterance: Utterance, generation: int) -> Any:
        """Send one utterance and stream its audio back."""
        socket = await self._ensure_socket()
        await socket.send(json.dumps({"type": "Speak", "text": utterance.text}))
        # Flush after every utterance so Deepgram synthesises immediately rather
        # than waiting for more text; this is what makes the reply start fast.
        await socket.send(json.dumps({"type": "Flush"}))

        while True:
            if generation != self._generation:
                raise BargeIn
            try:
                message = await asyncio.wait_for(socket.recv(), timeout=30.0)
            except TimeoutError as exc:
                raise RuntimeError("timed out waiting for audio") from exc
            except websockets.ConnectionClosed as exc:
                raise RuntimeError("speech socket closed") from exc

            if generation != self._generation:
                raise BargeIn

            if isinstance(message, (bytes, bytearray)):
                if not message:
                    continue
                yield bytes(message)
                continue

            try:
                payload = json.loads(message)
            except (TypeError, ValueError):
                continue
            message_type = payload.get("type")
            if message_type in {"Flushed", "Cleared"}:
                return
            if message_type == "Error":
                raise RuntimeError(f"Deepgram error: {payload.get('description', payload)}")

    # --- socket management ----------------------------------------------
    async def _ensure_socket(self):
        """Return a live speech socket, opening one if needed."""
        async with self._socket_lock:
            if self._socket is not None and getattr(self._socket, "state", None) is State.OPEN:
                return self._socket
            self._socket = None

            try:
                self._socket = await self._open_socket()
            except Exception:
                if self._speed_supported and self.settings.tts_speed != 1.0:
                    # Most likely the chosen voice rejects `speed`. Drop it and
                    # fall back to playback rate in the browser, so the user still
                    # hears the pace they asked for.
                    log.warning("TTS rejected request; retrying without the speed parameter")
                    self._speed_supported = False
                    if self._on_speed_fallback is not None:
                        await self._on_speed_fallback(self.settings.tts_speed)
                    self._socket = await self._open_socket()
                else:
                    raise

            log.info("Deepgram TTS connected (%s)", self.settings.tts_model)
            return self._socket

    async def _open_socket(self):
        return await websockets.connect(
            self.url,
            additional_headers={"Authorization": f"Token {self._api_key}"},
            open_timeout=15,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )

    async def _close(self) -> None:
        """Close the socket politely, ignoring any error during teardown."""
        async with self._socket_lock:
            socket, self._socket = self._socket, None
        if socket is None:
            return
        with contextlib.suppress(Exception):
            await socket.send(json.dumps({"type": "Close"}))
        with contextlib.suppress(Exception):
            await socket.close()

    async def _abandon(self) -> None:
        """Discard pending audio and close, without waiting for a flush.

        ``Clear`` tells Deepgram to throw away audio it has generated but not
        delivered, which is exactly what an interruption requires.
        """
        async with self._socket_lock:
            socket, self._socket = self._socket, None
        if socket is None:
            return
        with contextlib.suppress(Exception):
            await socket.send(json.dumps({"type": "Clear"}))
        with contextlib.suppress(Exception):
            await socket.send(json.dumps({"type": "Close"}))
        with contextlib.suppress(Exception):
            await socket.close()

    # Kept as an alias: the base worker calls `_abandon`, while the existing
    # regression tests for barge-in patch `_abandon_socket` by name. Renaming a
    # seam that is deliberately patched is how a test suite stops testing what it
    # says it tests.
    _abandon_socket = _abandon


def resample_linear(samples: Any, source_rate: int, target_rate: int) -> Any:
    """Resample float samples by linear interpolation.

    The local voice synthesises at 22050 Hz and the browser is told 24000 Hz, so
    something has to convert. Linear interpolation is not the highest quality
    resampler, but at a 24000/22050 ratio the interpolation error is inaudible,
    and it keeps the client contract — one declared rate, buffers declared at the
    rate the PCM really is — exactly as it is for the hosted engine.
    """
    if source_rate == target_rate:
        return samples
    count = len(samples)
    if count == 0:
        return samples
    out_count = int(count * target_rate / source_rate)
    out = array("f")
    step = source_rate / target_rate
    for index in range(out_count):
        position = index * step
        left = int(position)
        right = left + 1 if left + 1 < count else left
        fraction = position - left
        out.append(samples[left] + (samples[right] - samples[left]) * fraction)
    return out
