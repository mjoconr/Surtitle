"""Deepgram speech-to-text over a streaming WebSocket.

Audio is captured in the browser and relayed here as raw linear16 PCM frames.
Keeping capture and playback in the browser means macOS and Windows share one
code path and the Python process never touches an audio device.

Two backends are supported, because they differ in how the end of a turn is
detected and that is the single biggest contributor to conversational feel:

* **v2 / Flux** (default) — a model trained for *contextual* end-of-turn
  detection. It uses the linguistic content, not just silence, so it does not
  cut you off mid-thought and does not wait a fixed timeout after you finish.
* **v1 / Nova** — interim results plus ``endpointing`` silence detection. Kept as
  a fallback for accounts or regions where Flux is unavailable.

The client is deliberately dumb about policy: it forwards interim text for live
captions and reports an end-of-turn signal, leaving the decision of what to do
with it to the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import websockets

from surtitle.config import (
    DEEPGRAM_LISTEN_URL,
    DEEPGRAM_LISTEN_V2_URL,
    Settings,
)

__all__ = ["SpeechToText", "TranscriptEvent", "listen_url"]

log = logging.getLogger(__name__)

# Reconnect backoff: quick first retries, then settle to a steady interval.
_BACKOFF_SCHEDULE = (0.5, 1.0, 2.0, 4.0, 8.0)
_QUEUE_MAX = 256

# Message types meaning "the speaker has finished". Kept as a set because the
# exact naming differs between the v1 and v2 event vocabularies.
_END_OF_TURN_TYPES = frozenset(
    {
        "UtteranceEnd",
        "end_of_turn",
        "EndOfTurn",
        "SpeechFinished",
    }
)

# Message types known to carry a Flux turn update. The vocabulary has changed
# between revisions, so this is a hint list rather than an exhaustive one; see
# _looks_like_flux_turn for the shape-based fallback.
_FLUX_TURN_TYPES = frozenset({"TurnInfo", "turn_info", "Turn", "transcript"})

# Fields whose presence indicates a turn-shaped payload regardless of its
# declared type, so a renamed event still produces captions and turn boundaries.
_FLUX_MARKER_FIELDS = ("transcript", "words", "end_of_turn", "turn_event")


@dataclass(slots=True, frozen=True)
class TranscriptEvent:
    """One transcription update.

    ``final`` marks text that will not change again. ``is_end_of_turn`` is the
    stronger signal that the speaker has finished, not merely paused.
    """

    text: str
    final: bool
    confidence: float = 0.0
    is_end_of_turn: bool = False


TranscriptHandler = Callable[[TranscriptEvent], Awaitable[None]]


def _looks_like_nova_results(payload: dict[str, Any]) -> bool:
    """True when a payload carries the v1/Nova transcript envelope."""
    channel = payload.get("channel")
    if isinstance(channel, list):
        channel = channel[0] if channel else None
    if not isinstance(channel, dict):
        return False
    alternatives = channel.get("alternatives")
    return isinstance(alternatives, list) and bool(alternatives)


def _explain(exc: BaseException) -> str:
    """Summarise a connection failure, including the server's own reason.

    An HTTP rejection is the most likely failure, and its response body names the
    exact problem ("Unknown query parameters: channels"). A bare exception class
    hides that, which is why the first real run produced an opaque reconnect loop
    instead of a fixable message.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if response is not None and status is not None:
        detail = ""
        with contextlib.suppress(Exception):
            detail = bytes(response.body or b"").decode("utf-8", errors="replace")[:200]
        message = ""
        with contextlib.suppress(Exception):
            message = str(json.loads(detail).get("err_msg") or "")
        return f"HTTP {status}: {message or detail}"
    return f"{type(exc).__name__}: {exc}"


def listen_url(settings: Settings) -> str:
    """Build the Deepgram listen socket URL for the configured backend.

    Exported so diagnostics probe the *exact* URL the session will use. A
    duplicated parameter set in the doctor drifted from this one and reported a
    healthy configuration as broken, which is worse than having no diagnostic.
    """
    if settings.stt_api == "v2":
        # Flux accepts a much smaller parameter set than Nova and rejects the
        # rest with HTTP 400. Verified against the live endpoint: `channels`,
        # `language`, `interim_results`, `punctuate`, `smart_format`,
        # `vad_events`, `endpointing`, `utterance_end_ms` and `multichannel` are
        # all refused. `encoding` and `sample_rate` must be supplied together.
        flux: dict[str, object] = {
            "model": settings.stt_model,
            "encoding": "linear16",
            "sample_rate": settings.stt_sample_rate,
        }
        if settings.eot_threshold is not None:
            flux["eot_threshold"] = settings.eot_threshold
        if settings.eot_timeout_ms is not None:
            flux["eot_timeout_ms"] = settings.eot_timeout_ms
        return f"{DEEPGRAM_LISTEN_V2_URL}?{urlencode(flux)}"

    nova: dict[str, object] = {
        "model": settings.stt_model,
        "language": settings.stt_language,
        "encoding": "linear16",
        "sample_rate": settings.stt_sample_rate,
        "channels": 1,
        "interim_results": "true",
        "punctuate": "true",
        "smart_format": "true",
        "vad_events": "true",
        "utterance_end_ms": str(max(1000, settings.endpointing_ms * 3)),
        "endpointing": str(settings.endpointing_ms),
    }
    return f"{DEEPGRAM_LISTEN_URL}?{urlencode(nova)}"


class SpeechToText:
    """A single streaming transcription session."""

    def __init__(
        self,
        settings: Settings,
        *,
        on_transcript: TranscriptHandler,
        on_error: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self._on_transcript = on_transcript
        self._on_error = on_error
        self._api_key = settings.deepgram_key() or ""

        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopped = asyncio.Event()
        # Set while TTS is playing so echo-contaminated finals can be dropped.
        self._suppress_finals = False
        self._dropped_frames = 0

    # --- configuration ---------------------------------------------------
    @property
    def uses_flux(self) -> bool:
        """True when the contextual turn detector is in use."""
        return self.settings.stt_api == "v2"

    @property
    def url(self) -> str:
        """The socket URL, built by the shared helper."""
        return listen_url(self.settings)

    # --- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Begin the receive/send pumps."""
        if self._tasks:
            return
        self._stopped.clear()
        self._tasks = [asyncio.create_task(self._run(), name="deepgram-stt")]

    async def stop(self) -> None:
        """Stop the session, draining the queue."""
        self._stopped.set()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    # --- audio in --------------------------------------------------------
    def push_audio(self, frame: bytes) -> None:
        """Queue an audio frame, dropping the oldest data if we fall behind.

        Dropping is the right failure here: buffering indefinitely would make
        transcription lag the speaker, which is worse than a clipped phoneme.
        """
        if self._stopped.is_set() or not frame:
            return
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._dropped_frames += 1
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(frame)

    def set_suppression(self, suppressed: bool) -> None:
        """Enable or disable dropping of finals while the agent is speaking.

        Browser echo cancellation does most of this work; this is the safety net
        for devices where it is unavailable.
        """
        self._suppress_finals = suppressed

    # --- pumps -----------------------------------------------------------
    async def _run(self) -> None:
        """Keep a transcription socket open for the life of the session."""
        attempt = 0
        connected_once = False
        while not self._stopped.is_set():
            try:
                await self._connect_and_pump()
                connected_once = True
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopped.is_set():
                    return
                delay = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
                attempt += 1
                log.warning(
                    "Deepgram STT disconnected (%s); retrying in %.1fs", _explain(exc), delay
                )
                if self._on_error:
                    if connected_once:
                        notice = "Speech recognition dropped; reconnecting."
                    else:
                        notice = (
                            "Speech recognition is unavailable "
                            f"({type(exc).__name__}). Check your Deepgram key and network."
                        )
                    await self._on_error(notice)
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                    return  # stop() was called during the backoff
                except TimeoutError:
                    continue

    async def _connect_and_pump(self) -> None:
        headers = {"Authorization": f"Token {self._api_key}"}
        async with websockets.connect(
            self.url,
            additional_headers=headers,
            open_timeout=15,
            # Deepgram sends audio as binary frames; allow generous size.
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            log.info(
                "Deepgram STT connected (%s / %s)",
                self.settings.stt_api,
                self.settings.stt_model,
            )
            sender = asyncio.create_task(self._send_loop(socket))
            try:
                await self._receive_loop(socket)
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender
                with contextlib.suppress(Exception):
                    await socket.send(json.dumps({"type": "CloseStream"}))

    async def _send_loop(self, socket: Any) -> None:
        """Forward queued audio until stopped."""
        while not self._stopped.is_set():
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                # Keep the socket warm during quiet stretches.
                with contextlib.suppress(Exception):
                    await socket.send(json.dumps({"type": "KeepAlive"}))
                continue
            if frame is None:
                return
            await socket.send(frame)

    async def _receive_loop(self, socket: Any) -> None:
        """Translate Deepgram's server events into transcript updates."""
        async for raw in socket:
            if self._stopped.is_set():
                return
            if isinstance(raw, (bytes, bytearray)):
                continue  # audio does not flow back on this socket
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue

            message_type = str(payload.get("type") or "")

            if message_type == "Results":
                await self._handle_v1_results(payload)
            elif message_type in _END_OF_TURN_TYPES:
                # The speaker finished. Flux reports this from the text content;
                # v1 reports it after sustained silence (UtteranceEnd).
                if not self._suppress_finals:
                    await self._on_transcript(
                        TranscriptEvent(text="", final=True, is_end_of_turn=True)
                    )
            elif message_type == "Error":
                raise RuntimeError(f"Deepgram error: {payload.get('description') or payload}")
            elif _looks_like_nova_results(payload):
                # Flux may reuse the v1 results envelope. Accepting both shapes
                # means the pipeline works whichever the service sends, which
                # matters because Flux's exact schema could not be confirmed
                # without live credentials.
                await self._handle_v1_results(payload)
            elif message_type in _FLUX_TURN_TYPES or _looks_like_flux_turn(payload):
                await self._handle_flux_turn(payload)
            else:
                log.debug(
                    "Deepgram STT: unhandled %r event, keys=%s",
                    message_type or "(none)",
                    list(payload)[:8],
                )
            # Metadata, SpeechStarted and unknown informational types are ignored
            # rather than treated as failures, so a new server-side event cannot
            # break an otherwise working session.

    # --- v1 (Nova) -------------------------------------------------------
    async def _handle_v1_results(self, payload: dict[str, Any]) -> None:
        channel = payload.get("channel") or {}
        if isinstance(channel, list):
            channel = channel[0] if channel else {}
        alternatives = channel.get("alternatives") or []
        if not alternatives:
            return
        best = alternatives[0]
        text = (best.get("transcript") or "").strip()
        is_final = bool(payload.get("is_final"))
        speech_final = bool(payload.get("speech_final"))
        confidence = float(best.get("confidence") or 0.0)

        if not text:
            # An empty final still marks a boundary worth reporting.
            if is_final and speech_final and not self._suppress_finals:
                await self._on_transcript(TranscriptEvent(text="", final=True, is_end_of_turn=True))
            return

        if is_final and self._suppress_finals:
            # Almost certainly the agent hearing itself; ignore silently.
            log.debug("dropping final transcript during playback: %r", text[:60])
            return

        await self._on_transcript(
            TranscriptEvent(
                text=text,
                final=is_final,
                confidence=confidence,
                is_end_of_turn=is_final and speech_final,
            )
        )

    # --- v2 (Flux) -------------------------------------------------------
    async def _handle_flux_turn(self, payload: dict[str, Any]) -> None:
        """Handle a Flux turn update.

        Flux reports the in-progress transcript and, separately, when the turn
        has ended. Field names are read defensively and fall back across the
        plausible spellings so a schema revision degrades to "captions keep
        working" instead of an exception.
        """
        text = _first_string(payload, "transcript", "text")
        if text is None:
            text = _transcript_from_words(payload.get("words"))

        event_type = _first_string(payload, "event", "turn_event", "reason") or ""
        end_reason = _first_string(payload, "end_of_turn_reason", "turn_end_reason") or ""
        confidence = _first_float(payload, "confidence", "avg_confidence") or 0.0

        is_end = (
            bool(payload.get("end_of_turn"))
            or bool(payload.get("is_end_of_turn"))
            or str(event_type).lower().replace("_", "") in {"endofturn", "end"}
            or str(end_reason).lower() not in {"", "none"}
        )

        if not text:
            # An end-of-turn with no text is still a valid boundary: the user
            # may have said something that produced no transcript.
            if is_end and not self._suppress_finals:
                await self._on_transcript(TranscriptEvent(text="", final=True, is_end_of_turn=True))
            return

        if self._suppress_finals:
            log.debug("dropping Flux transcript during playback: %r", text[:60])
            return

        await self._on_transcript(
            TranscriptEvent(
                text=text,
                final=is_end,
                confidence=confidence,
                is_end_of_turn=is_end,
            )
        )


def _looks_like_flux_turn(payload: dict[str, Any]) -> bool:
    """True when a payload has the shape of a turn update.

    Used as a fallback so a renamed ``type`` still yields captions instead of
    silently dropping every transcript.
    """
    return any(field in payload for field in _FLUX_MARKER_FIELDS)


def _first_string(payload: dict[str, Any], *names: str) -> str | None:
    """Return the first present, non-empty string field among ``names``."""
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_float(payload: dict[str, Any], *names: str) -> float | None:
    """Return the first present numeric field among ``names``."""
    for name in names:
        value = payload.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _transcript_from_words(words: Any) -> str | None:
    """Rebuild a transcript from a per-word array, for schemas that omit it.

    Flux-style responses sometimes carry words only; without this the live
    caption would sit empty while the user is talking.
    """
    if not isinstance(words, list):
        return None
    parts = []
    for word in words:
        if isinstance(word, dict):
            token = word.get("word") or word.get("text") or word.get("punctuated_word")
            if isinstance(token, str) and token.strip():
                parts.append(token.strip())
        elif isinstance(word, str) and word.strip():
            parts.append(word.strip())
    if not parts:
        return None
    return " ".join(parts)
