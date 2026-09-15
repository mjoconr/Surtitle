"""Configuration for Surtitle.

Everything the user can tune lives here and is loaded, in order of precedence,
from real environment variables, then ``.env.local``, then ``.env``. No secrets
are ever baked into the package.
"""

from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "Surtitle"
ENV_PREFIX = "SURTITLE_"

# Where the app keeps its database, logs and default project workspace. This
# mirrors platform convention so uninstalling is a matter of deleting one tree.
_PLATFORM_DATA_DIRS: dict[str, str] = {
    "darwin": "~/Library/Application Support/Surtitle",
    "win32": "~/AppData/Local/Surtitle",
}
_FALLBACK_DATA_DIR = "~/.local/share/surtitle"

# The DeepSeek API is OpenAI-compatible, so the raw HTTP surface is stable.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# Deepgram streaming component APIs. Verified against live docs in Phase 4.
DEEPGRAM_LISTEN_URL = "wss://api.deepgram.com/v1/listen"
# Listen v2 exposes Flux, which does contextual end-of-turn detection rather
# than relying on a silence timer. That difference is what makes a voice agent
# feel like it is listening rather than waiting out a timeout.
DEEPGRAM_LISTEN_V2_URL = "wss://api.deepgram.com/v2/listen"
DEEPGRAM_SPEAK_URL = "wss://api.deepgram.com/v1/speak"
DEEPGRAM_STT_MODEL = "flux-general-en"
DEEPGRAM_TTS_MODEL = "aura-2-thalia-en"

# Local (sherpa-onnx) defaults. These are registry keys in
# :mod:`surtitle.voice.models`, which owns the URLs and checksums; keeping
# the *names* here means configuration needs no import of the model layer.
LOCAL_STT_MODEL = "streaming-zipformer-en-2023-06-26"
LOCAL_TTS_MODEL = "vits-piper-en_US-lessac-medium"


class ConfigError(RuntimeError):
    """Raised when the app cannot start because configuration is unusable."""


def default_data_dir() -> Path:
    """Return the per-platform directory for app state."""
    if override := os.environ.get(f"{ENV_PREFIX}HOME"):
        return Path(override).expanduser().resolve()
    raw = _PLATFORM_DATA_DIRS.get(sys.platform, _FALLBACK_DATA_DIR)
    return Path(raw).expanduser()


def app_root() -> Path:
    """Directory configuration is resolved from.

    In a source checkout this is the repository root, where ``.env`` belongs.
    """
    return Path(__file__).resolve().parent.parent.parent


def candidate_env_files() -> tuple[Path, ...]:
    """Where configuration is looked for, in loading order.

    These are resolved to *absolute* paths on purpose. pydantic-settings resolves
    a relative ``env_file`` against the current working directory, which makes the
    effective configuration depend on where the process happened to start. That is
    not hypothetical: the launcher runs from ``scripts/``, so a stray
    ``scripts/.env`` silently set the STT model and overrode every shipped
    default, producing a voice loop that looked like a code bug.

    Real environment variables still take precedence over all of these.
    """
    root = app_root()
    candidates = [root / ".env.local", root / ".env"]
    package_env = Path(__file__).resolve().parent / ".env"
    if package_env not in candidates:
        candidates.append(package_env)
    return tuple(candidates)


def loaded_env_files() -> tuple[Path, ...]:
    """Which configuration files exist and were therefore read."""
    return tuple(path for path in candidate_env_files() if path.is_file())


class Settings(BaseSettings):
    """Runtime configuration.

    Secrets are held as :class:`SecretStr` so they cannot leak through reprs,
    logs or tracebacks.
    """

    model_config = SettingsConfigDict(
        # Absolute paths, so the answer does not depend on the launch directory.
        env_file=candidate_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Accept constructor arguments by field name as well as by alias. Without
        # this, `Settings(voice_enabled=False)` is silently *ignored* rather than
        # rejected, because pydantic-settings only reads aliases by default —
        # which makes test fixtures quietly assert the wrong thing.
        populate_by_name=True,
    )

    # --- credentials -----------------------------------------------------
    deepseek_api_key: SecretStr | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepgram_api_key: SecretStr | None = Field(default=None, alias="DEEPGRAM_API_KEY")

    # --- models ----------------------------------------------------------
    deepseek_model: str = Field(default="deepseek-flash", alias="DEEPSEEK_MODEL")
    deepseek_base_url: str = Field(default=DEEPSEEK_BASE_URL, alias="DEEPSEEK_BASE_URL")
    reasoning_effort: str = Field(default="low", alias="SURTITLE_REASONING_EFFORT")
    thinking_enabled: bool = Field(default=True, alias="SURTITLE_THINKING")
    temperature: float = Field(default=0.7, alias="SURTITLE_TEMPERATURE")
    max_tokens: int = Field(default=4096, alias="SURTITLE_MAX_TOKENS")

    # --- agent loop limits ----------------------------------------------
    # A backstop against runaway tool use, not a work budget.
    #
    # This was 24, which cut off legitimate work: a request to investigate and
    # report on a machine ran 24 rounds of real, productive tool calls and was
    # stopped mid-task with no answer. The guard against a *stuck* agent is the
    # repeat-call guard, which refuses consecutive identical calls on an escalating
    # threshold and feeds back a reminder — a model that repeats itself cannot make
    # progress, and that is caught mechanically. A model making twenty-four
    # *different* calls is usually working, so this has to be high enough to be a
    # genuine backstop rather than a budget that decides when to give up.
    max_steps: int = Field(default=200, alias="SURTITLE_MAX_STEPS")
    request_timeout: float = Field(default=180.0, alias="SURTITLE_REQUEST_TIMEOUT")

    # --- voice -----------------------------------------------------------
    # Which implementation of each half of the pipeline to use:
    #   "deepgram" — the hosted streaming service (the default).
    #   "local"    — sherpa-onnx inside this process, with no network at all.
    # The two directions are independent, so "local ears, hosted voice" is a
    # supported combination. Selecting a backend implies nothing about the other.
    stt_backend: str = Field(default="deepgram", alias="SURTITLE_STT_BACKEND")
    tts_backend: str = Field(default="deepgram", alias="SURTITLE_TTS_BACKEND")

    # "v2" uses Flux with contextual turn detection; "v1" uses Nova with
    # endpointing. v2 is the default because turn-taking quality is the single
    # biggest contributor to conversational feel. Deepgram backend only; the
    # local backend has its own settings below.
    stt_api: str = Field(default="v2", alias="SURTITLE_STT_API")
    stt_model: str = Field(default=DEEPGRAM_STT_MODEL, alias="DEEPGRAM_STT_MODEL")
    stt_language: str = Field(default="en", alias="DEEPGRAM_STT_LANGUAGE")
    # Milliseconds of silence before the turn is declared finished. Only used on
    # the v1 path, where no contextual turn detector is available.
    endpointing_ms: int = Field(default=300, alias="SURTITLE_ENDPOINTING_MS")
    # Flux end-of-turn tuning. `threshold` is how confident the model must be
    # that you have finished before it ends the turn (higher = waits longer);
    # `timeout` is the ceiling on how long it will keep a turn open.
    eot_threshold: float | None = Field(default=None, alias="SURTITLE_EOT_THRESHOLD")
    eot_timeout_ms: int | None = Field(default=None, alias="SURTITLE_EOT_TIMEOUT_MS")
    tts_model: str = Field(default=DEEPGRAM_TTS_MODEL, alias="DEEPGRAM_TTS_MODEL")
    # 1.0 == natural speed. Deepgram accepts a speed multiplier; playback-rate
    # scaling in the browser is the fallback if a model rejects it.
    tts_speed: float = Field(default=1.0, alias="SURTITLE_TTS_SPEED")
    tts_sample_rate: int = Field(default=24000, alias="SURTITLE_TTS_SAMPLE_RATE")
    # Capture rate is fixed by Deepgram's linear16 expectation.
    stt_sample_rate: int = Field(default=16000, alias="SURTITLE_STT_SAMPLE_RATE")
    voice_enabled: bool = Field(default=True, alias="SURTITLE_VOICE")

    # --- local (sherpa-onnx) voice ---------------------------------------
    # Model names are registry keys in :mod:`surtitle.voice.models`, not
    # paths, so a model can be swapped without touching the filesystem layout.
    local_stt_model: str = Field(default=LOCAL_STT_MODEL, alias="SURTITLE_LOCAL_STT_MODEL")
    local_tts_model: str = Field(default=LOCAL_TTS_MODEL, alias="SURTITLE_LOCAL_TTS_MODEL")
    # Prefer the int8 encoder when the model ships both: roughly half the RAM
    # for a small accuracy cost, which is the right trade on a laptop CPU.
    local_stt_int8: bool = Field(default=True, alias="SURTITLE_LOCAL_STT_INT8")

    # Turn detection. The local recogniser has no contextual end-of-turn model,
    # so a turn ends on trailing silence — and, when the transcript looks
    # unfinished, on a longer silence instead. See docs/VOICE.md.
    local_eot_silence_ms: int = Field(default=800, alias="SURTITLE_LOCAL_EOT_SILENCE_MS")
    local_eot_extend_ms: int = Field(default=1200, alias="SURTITLE_LOCAL_EOT_EXTEND_MS")
    # Hard ceiling: a monologue still has to become a turn at some point.
    local_max_utterance_ms: int = Field(default=20000, alias="SURTITLE_LOCAL_MAX_UTTERANCE_MS")
    # Where model files live. Defaults under the data directory so uninstalling
    # is still "delete one tree".
    models_dir: Path | None = Field(default=None, alias="SURTITLE_MODELS_DIR")

    # --- server ----------------------------------------------------------
    host: str = Field(default="127.0.0.1", alias="SURTITLE_HOST")
    port: int = Field(default=8765, alias="SURTITLE_PORT")
    open_browser: bool = Field(default=True, alias="SURTITLE_OPEN_BROWSER")
    log_level: str = Field(default="info", alias="SURTITLE_LOG_LEVEL")

    # --- storage ---------------------------------------------------------
    data_dir: Path = Field(default_factory=default_data_dir, alias="SURTITLE_HOME")

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        allowed = {"debug", "info", "warning", "error", "critical"}
        normalised = value.lower()
        if normalised not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return normalised

    @field_validator("reasoning_effort")
    @classmethod
    def _check_effort(cls, value: str) -> str:
        allowed = {"minimal", "low", "medium", "high"}
        normalised = value.lower()
        if normalised not in allowed:
            raise ValueError(f"reasoning_effort must be one of {sorted(allowed)}, got {value!r}")
        return normalised

    @field_validator("stt_api")
    @classmethod
    def _check_stt_api(cls, value: str) -> str:
        allowed = {"v1", "v2"}
        normalised = value.lower()
        if normalised not in allowed:
            raise ValueError(f"stt_api must be one of {sorted(allowed)}, got {value!r}")
        return normalised

    @field_validator("stt_backend", "tts_backend")
    @classmethod
    def _check_backend(cls, value: str) -> str:
        allowed = {"deepgram", "local"}
        normalised = value.lower()
        if normalised not in allowed:
            raise ValueError(f"voice backend must be one of {sorted(allowed)}, got {value!r}")
        return normalised

    @field_validator("tts_speed")
    @classmethod
    def _check_speed(cls, value: float) -> float:
        if not 0.5 <= value <= 2.0:
            raise ValueError(f"tts_speed must be between 0.5 and 2.0, got {value}")
        return value

    # --- derived paths ---------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "surtitle.db"

    @property
    def default_workspace_dir(self) -> Path:
        """Where new projects are created when the user does not pick a folder."""
        return self.data_dir / "projects"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "surtitle.log"

    @property
    def models_path(self) -> Path:
        """Where local speech models are cached.

        Under the data directory by default, so removing the app removes them,
        and overridable because a shared or read-only install may want them
        somewhere else entirely.
        """
        return self.models_dir or (self.data_dir / "models")

    # --- credential helpers ---------------------------------------------
    def deepseek_key(self) -> str | None:
        """Return the DeepSeek key value, or ``None`` when unset."""
        return self.deepseek_api_key.get_secret_value() if self.deepseek_api_key else None

    def deepgram_key(self) -> str | None:
        """Return the Deepgram key value, or ``None`` when unset."""
        return self.deepgram_api_key.get_secret_value() if self.deepgram_api_key else None

    def needs_credential(self, name: str) -> bool:
        """True when ``name`` is required by the selected backends.

        Only a *Deepgram* backend needs a Deepgram key. A fully local setup
        needs no key at all, which is the whole point of it — and asking for one
        would make a working offline configuration look broken.
        """
        if name == "DEEPGRAM_API_KEY":
            if not self.voice_enabled:
                return False
            return "deepgram" in (self.stt_backend, self.tts_backend)
        return name == "DEEPSEEK_API_KEY"

    def missing_credentials(self) -> list[str]:
        """Names of credentials that are absent, for ``doctor`` and startup errors."""
        missing = []
        if self.needs_credential("DEEPSEEK_API_KEY") and not self.deepseek_key():
            missing.append("DEEPSEEK_API_KEY")
        if self.needs_credential("DEEPGRAM_API_KEY") and not self.deepgram_key():
            missing.append("DEEPGRAM_API_KEY")
        return missing

    def require_credentials(self, *, voice: bool | None = None) -> None:
        """Raise :class:`ConfigError` when a needed credential is absent.

        ``voice`` forces the Deepgram requirement on or off regardless of the
        stored setting, so text-only mode can run without a Deepgram key. A
        local-only voice configuration never needs one, because nothing in it
        talks to Deepgram.
        """
        missing = []
        if not self.deepseek_key():
            missing.append("DEEPSEEK_API_KEY")
        need_deepgram = self.needs_credential("DEEPGRAM_API_KEY")
        if voice is not None:
            need_deepgram = voice and "deepgram" in (self.stt_backend, self.tts_backend)
        if need_deepgram and not self.deepgram_key():
            missing.append("DEEPGRAM_API_KEY")
        if missing:
            joined = ", ".join(missing)
            raise ConfigError(
                f"Missing required credential(s): {joined}.\n"
                "Copy .env.example to .env and fill them in, or export them in your shell."
            )

    def ensure_data_dir(self) -> Path:
        """Create the data directory if needed and return it."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir

    @property
    def safe_base_url(self) -> str:
        """Base URL with any trailing slash removed."""
        return self.deepseek_base_url.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests that monkeypatch the environment."""
    get_settings.cache_clear()


def setup_logging(settings: Settings | None = None, *, force: bool = False) -> logging.Logger:
    """Configure root logging once, to stderr and the app log file.

    Uses ``rich`` when it is importable so CLI output stays readable, and falls
    back to a plain formatter otherwise.
    """
    settings = settings or get_settings()
    root = logging.getLogger()
    if root.handlers and not force:
        return logging.getLogger("surtitle")

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root.setLevel(level)

    try:
        from rich.logging import RichHandler

        stream_handler: logging.Handler = RichHandler(
            rich_tracebacks=True, show_path=False, markup=False
        )
        stream_handler.setFormatter(logging.Formatter("%(message)s", datefmt="%H:%M:%S"))
    except Exception:  # noqa: BLE001 - never let logging setup break startup
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(stream_handler)

    try:
        settings.ensure_data_dir()
        file_handler = logging.FileHandler(settings.log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )
        root.addHandler(file_handler)
    except OSError:  # pragma: no cover - read-only or unavailable data dir
        pass

    return logging.getLogger("surtitle")
