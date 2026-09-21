"""Local text-to-speech, running an ONNX voice inside this process.

Separate from :mod:`surtitle.voice.tts` so that the hosted engine can be
imported and used without the optional extra, and so that a test of the local
voice does not need to reach inside the Deepgram socket machinery.

The engine-independent half of the work — the queue, the barge-in generation
counter, and the speaking transitions that drive echo suppression — lives in
:class:`~surtitle.voice.tts._UtteranceWorker` and is shared with the hosted
engine. Only :meth:`LocalTextToSpeech._load` and
:meth:`LocalTextToSpeech._produce` are specific to this engine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from surtitle.config import Settings
from surtitle.voice.tts import (
    _LOCAL_BACKOFF,
    BargeIn,
    Utterance,
    _UtteranceWorker,
    float_to_pcm16,
    resample_linear,
)

__all__ = ["LocalTextToSpeech"]

log = logging.getLogger(__name__)


class LocalTextToSpeech(_UtteranceWorker):
    """Speaks through a local sherpa-onnx VITS/Kokoro model.

    Synthesis is produce-then-play rather than socket-streamed, so ``_produce``
    yields each synthesised chunk as it is generated. A barge-in that arrives
    mid-synthesis cannot cancel the ONNX call itself — the thread is already
    running — but its output is discarded on return, which is the local
    equivalent of Deepgram's ``Clear``: what the user must not hear is audio for
    text they interrupted.
    """

    failure_notice = "Local voice output failed; replies will be shown, not spoken."
    backoff = _LOCAL_BACKOFF

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        from surtitle.voice import models

        self._paths = models.resolve_tts(settings)
        self._tts: Any = None

    @property
    def url(self) -> str:
        """Where audio comes from, for the log and ``doctor``."""
        return f"local:{self.settings.local_tts_model}"

    def _load(self) -> Any:
        """Import sherpa-onnx and construct the synthesiser, on the worker thread."""
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise ImportError(
                "the local speech engines are not installed — they are an "
                "optional extra of about 30 MB. Open Settings, choose the local "
                "voice provider, and press Install; or run `uv sync --extra "
                "voice-local` where Surtitle is installed, then restart."
            ) from exc

        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=self._paths["model"],
            tokens=self._paths["tokens"],
            data_dir=self._paths["data_dir"],
            # tts_speed is a multiplier; the model's length_scale is its inverse,
            # so 1.2x speech is a shorter scale. Applied here rather than in the
            # browser so pitch is preserved.
            length_scale=1.0 / max(0.5, min(2.0, self.settings.tts_speed)),
        )
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(vits=vits, num_threads=2)
        )
        return sherpa_onnx.OfflineTts(config)

    async def _produce(self, utterance: Utterance, generation: int) -> Any:
        """Synthesise one utterance and yield its audio, resampled if needed."""
        loop = asyncio.get_running_loop()
        if self._tts is None:
            self._tts = await loop.run_in_executor(None, self._load)
            log.info(
                "local TTS ready (%s, %d Hz -> %d Hz)",
                self.settings.local_tts_model,
                self._tts.sample_rate,
                self.settings.tts_sample_rate,
            )

        audio = await loop.run_in_executor(None, self._tts.generate, utterance.text)
        if generation != self._generation:
            # The user interrupted while this was synthesising. Dropping the
            # result here is the whole point: audio for cancelled text must never
            # reach the browser.
            raise BargeIn

        samples = resample_linear(audio.samples, audio.sample_rate, self.settings.tts_sample_rate)
        pcm = float_to_pcm16(samples)
        # Yielded in ~100 ms frames so a long sentence is not one enormous frame
        # and a barge-in mid-sentence stops within a frame rather than after the
        # whole reply has been handed over.
        stride = max(1, self.settings.tts_sample_rate // 10)
        for start in range(0, len(pcm) // 2, stride):
            end = min(start + stride, len(pcm) // 2)
            yield pcm[start * 2 : end * 2]

    async def _close(self) -> None:
        """Nothing to release; the model is garbage collected with the engine."""
        self._tts = None
