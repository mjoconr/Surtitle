"""Voice I/O: speech recognition and synthesis, hosted or local.

Audio capture and playback live in the browser; this package holds the
server-side engines and the selection between them.

* :mod:`~surtitle.voice.engine` — chooses which engine each direction uses.
  The session depends on that decision, not on either implementation.
* :mod:`~surtitle.voice.stt` / :mod:`~surtitle.voice.tts` — the hosted
  (Deepgram) clients, plus the worker that both TTS engines share.
* :mod:`~surtitle.voice.local_stt` / :mod:`~surtitle.voice.local_tts` —
  the local engines, which require the optional ``voice-local`` extra. They import
  ``sherpa_onnx`` lazily, inside ``start()``, so this package — and therefore the
  application — stays importable without it.
* :mod:`~surtitle.voice.models` — the model registry, its pinned checksums,
  and the installer.

Which engines exist is a supported choice rather than an implementation detail:
a fully local configuration needs no API key, no network and no per-minute cost,
at the price of silence-based turn detection and a higher word error rate. See
``docs/VOICE.md`` for the measured difference.
"""

from __future__ import annotations

__all__: list[str] = []
