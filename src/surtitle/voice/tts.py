"""Deepgram text-to-speech with low-latency streaming and true barge-in.

Two design points matter more than anything else here:

**One socket per turn, closed on barge-in.** When the user interrupts, we cannot
simply stop reading from the socket: Deepgram has already synthesised audio for
text we sent, and that backlog would arrive later and sound like the agent
ignoring the interruption. Closing the socket discards it by construction.

**Sentences, not paragraphs.** The speak layer hands us one sentence at a time,
so the first words are audible while the model is still generating the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import websockets
from websockets.protocol import State

from surtitle.config import DEEPGRAM_SPEAK_URL, Settings

__all__ = ["TextToSpeech", "Utterance"]

log = logging.getLogger(__name__)

_BACKOFF_SCHEDULE = (0.5, 1.0, 2.0, 4.0)


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


class TextToSpeech:
    """Streams spoken audio for queued utterances."""

    def __init__(
        self,
        settings: Settings,
        *,
        on_audio: AudioHandler,
        on_started: Callable[[], Awaitable[None]] | None = None,
        on_finished: Callable[[], Awaitable[None]] | None = None,
        on_error: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self._on_audio = on_audio
        self._on_started = on_started
        self._on_finished = on_finished
        self._on_error = on_error
        self._api_key = settings.deepgram_key() or ""

        self._queue: asyncio.Queue[Utterance | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._socket: object | None = None
        self._socket_lock = asyncio.Lock()
        self._active = False
        self._stopped = False
        self._sequence = 0
        # Incremented by barge_in(); a worker notices the change and abandons the
        # turn instead of finishing its queue.
        self._generation = 0
        # Set after Deepgram rejects a speed parameter, so we only retry once.
        self._speed_supported = True

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
        params: dict[str, object] = {
            "model": self.settings.tts_model,
            "encoding": "linear16",
            "sample_rate": self.settings.tts_sample_rate,
        }
        # Not every Aura model accepts `speed`; it is dropped automatically if
        # the first connection is refused (see _ensure_socket).
        if self._speed_supported and self.settings.tts_speed != 1.0:
            params["speed"] = self.settings.tts_speed
        return f"{DEEPGRAM_SPEAK_URL}?{urlencode(params)}"

    # --- public API ------------------------------------------------------
    async def start(self) -> None:
        """Start the synthesis worker."""
        if self._worker is None or self._worker.done():
            self._stopped = False
            self._worker = asyncio.create_task(self._run(), name="deepgram-tts")

    async def stop(self) -> None:
        """Shut down the worker and close the socket."""
        self._stopped = True
        self._generation += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None
        await self._close_socket()
        await self._finish_if_active()

    def speak(self, text: str, *, final: bool = False) -> None:
        """Queue text for speech. Safe to call from anywhere on the loop."""
        if self._stopped or not text.strip():
            return
        self._queue.put_nowait(Utterance(text=text.strip(), final=final))

    async def barge_in(self) -> None:
        """Stop speaking immediately and discard everything queued.

        Audio already handed to the browser is dropped client-side; the
        server-side work is cancelled here.
        """
        self._generation += 1
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        # Tell Deepgram to discard text it has not synthesised, then close so the
        # audio it already produced for cancelled text is never sent.
        await self._abandon_socket()
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
        """Consume utterances until stopped, surviving socket failures."""
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
                    if not self._active:
                        self._active = True
                        if self._on_started:
                            await self._on_started()
                    await self._synthesise(utterance, generation)
                    attempt = 0
                except BargeIn:
                    # Expected: the user interrupted. Nothing to report.
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._stopped or generation != self._generation:
                        continue
                    await self._close_socket()
                    delay = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
                    attempt += 1
                    log.warning("Deepgram TTS failed (%s); retrying in %.1fs", exc, delay)
                    if self._on_error:
                        await self._on_error("Voice output hiccup; reconnecting.")
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        raise
                    # Re-queue once so the sentence is not silently lost.
                    if generation == self._generation and not self._stopped:
                        self._queue.put_nowait(utterance)
        finally:
            # Drain anything left so the socket is not held open, and never let
            # teardown raise out of the task.
            while not self._queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
            with contextlib.suppress(Exception):
                await self._close_socket()
            # Do not notify here: the worker is being torn down, and the session
            # decides what to report when it stops the client.
            await self._finish_if_active(notify=False)

    async def _finish_if_active(self, *, notify: bool = True) -> None:
        """Clear the speaking flag once, optionally notifying the session."""
        if not self._active:
            return
        self._active = False
        if notify and self._on_finished:
            with contextlib.suppress(Exception):
                await self._on_finished()

    async def _synthesise(self, utterance: Utterance, generation: int) -> None:
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
                self._sequence += 1
                await self._on_audio(bytes(message), self._sequence)
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
                    # fall back to browser-side playback rate.
                    log.warning("TTS rejected request; retrying without the speed parameter")
                    self._speed_supported = False
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

    async def _close_socket(self) -> None:
        """Close the socket politely, ignoring any error during teardown."""
        async with self._socket_lock:
            socket, self._socket = self._socket, None
        if socket is None:
            return
        with contextlib.suppress(Exception):
            await socket.send(json.dumps({"type": "Close"}))
        with contextlib.suppress(Exception):
            await socket.close()

    async def _abandon_socket(self) -> None:
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
