"""Persisted settings and credentials.

Two files, deliberately separate, mirroring the security posture of DeepSeek
Harness:

* ``settings.json`` — non-secret preferences (models, voice, reasoning effort).
  Safe to read, copy and paste into a bug report.
* ``.credentials.json`` — API keys only. Created ``0600`` inside a ``0700``
  directory, and refused on load if it is group- or other-readable.

The single most important rule in this module: **a secret value never leaves
this process toward the UI.** The HTTP surface returns ``configured: true`` and
nothing else — no value, no suffix, no length. Environment variables always win
over the stored file and are reported as read-only, so a key exported in the
shell cannot be silently shadowed by a stale file entry.

Settings are written atomically (temp file plus ``os.replace``) so a crash
mid-write cannot leave an unreadable config behind.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from surtitle.config import DEEPGRAM_STT_MODEL, DEEPGRAM_TTS_MODEL, Settings

__all__ = [
    "PROVIDER_SPECS",
    "CredentialState",
    "ProviderSpec",
    "SettingsStore",
    "SettingsValidationError",
]

log = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"
CREDENTIALS_FILENAME = ".credentials.json"
CREDENTIALS_VERSION = 1

# POSIX: any group/other permission bit on the credentials file is a refusal.
_GROUP_OTHER_BITS = 0o077
_FILE_MODE = 0o600
_DIR_MODE = 0o700


class SettingsValidationError(ValueError):
    """Raised when a settings patch is not acceptable.

    Carries the offending field so the UI can highlight it.
    """

    def __init__(self, message: str, *, field_name: str | None = None) -> None:
        super().__init__(message)
        self.field_name = field_name


@dataclass(slots=True, frozen=True)
class Capability:
    """One thing Surtitle needs a service for, and how a provider is chosen.

    The unit the settings screen is built around, and the unit a person thinks in:
    which model, which speech recognition, which voice, which search. Everything the
    screen shows for a capability — its section, its selector, its providers, their
    key fields, their install state — is derived from this table and
    :data:`PROVIDER_SPECS`, so adding a provider is one entry rather than an entry
    here, an entry there, and a page of hand-written UI.
    """

    id: str
    label: str
    # The preference that selects the provider, when there is a choice to make. A
    # capability with one provider has nothing to select — the LLM has one today —
    # and the section shows that provider's settings instead of a selector with a
    # single option.
    setting: str | None
    providers: tuple[str, ...]
    # A choice meaning "pick for me", offered first. Only search has one: the best
    # provider there depends on whether a key is configured at all.
    automatic: str | None = None


# The four services, in the order they are configured. Speech to text before text
# to speech because a voice conversation starts with the microphone.
CAPABILITIES: tuple[Capability, ...] = (
    Capability("llm", "Model", None, ("deepseek",)),
    Capability("stt", "Speech to text", "stt_backend", ("deepgram", "local")),
    Capability("tts", "Text to speech", "tts_backend", ("deepgram", "local")),
    Capability(
        "search",
        "Search",
        "search_provider",
        ("duckduckgo", "tavily"),
        automatic="automatic",
    ),
)

CAPABILITIES_BY_ID = {capability.id: capability for capability in CAPABILITIES}


@dataclass(slots=True, frozen=True)
class ProviderSpec:
    """How one provider is addressed: what it can do, and how to reach it.

    ``api_key_env`` is a *reference* (the environment variable name), never the
    secret itself, so provider configuration can be stored and displayed safely.
    It is ``None`` for a provider that needs no credential — a local engine, or a
    search that this application performs itself.
    """

    id: str
    label: str
    # What this provider can serve. A provider may serve more than one capability:
    # Deepgram recognises and speaks, and the local engines do both.
    capabilities: tuple[str, ...]
    base_url: str = ""
    # The environment variable that holds its key, or None when it needs none.
    api_key_env: str | None = None
    models: tuple[str, ...] = ()
    default_model: str = ""
    docs_url: str = ""
    # ``models`` endpoint used to validate a key and refresh the model list.
    discovery_path: str | None = "/models"
    needs_key: bool = True
    # "api" is somebody else's service, "local" runs on this machine and has to be
    # installed, "builtin" is done by this application with no service at all.
    kind: str = "api"
    # The install action a local provider needs, if any.
    install: str | None = None


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "deepseek": ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        capabilities=("llm",),
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        models=("deepseek-flash", "deepseek-v4-pro"),
        default_model="deepseek-flash",
        docs_url="https://platform.deepseek.com/api_keys",
    ),
    "deepgram": ProviderSpec(
        id="deepgram",
        label="Deepgram",
        capabilities=("stt", "tts"),
        api_key_env="DEEPGRAM_API_KEY",
        base_url="https://api.deepgram.com",
        models=(DEEPGRAM_STT_MODEL, DEEPGRAM_TTS_MODEL),
        default_model=DEEPGRAM_STT_MODEL,
        docs_url="https://console.deepgram.com/",
        # Deepgram has no cheap unauthenticated model listing; the streaming
        # socket probe in :mod:`surtitle.doctor` is the validation path.
        discovery_path=None,
    ),
    "local": ProviderSpec(
        id="local",
        label="On this machine",
        capabilities=("stt", "tts"),
        needs_key=False,
        kind="local",
        # The engines and their models are hundreds of megabytes, so choosing this
        # is not enough: the section has to say whether they are here and offer to
        # fetch them.
        install="voice",
    ),
    "duckduckgo": ProviderSpec(
        id="duckduckgo",
        label="This application fetches them",
        capabilities=("search",),
        needs_key=False,
        kind="builtin",
        docs_url="https://duckduckgo.com/",
    ),
    "tavily": ProviderSpec(
        id="tavily",
        label="Tavily",
        capabilities=("search",),
        api_key_env="TAVILY_API_KEY",
        base_url="https://api.tavily.com",
        docs_url="https://app.tavily.com/home",
        # A key is validated by asking what it has used. It is a real authenticated
        # call, it costs nothing, and it distinguishes a revoked key from a working
        # one — which is the whole job of the Test button. Tavily has no model list,
        # so the verifier reports the provider and an empty list of models.
        discovery_path="/usage",
    ),
}


@dataclass(slots=True)
class CredentialState:
    """Everything the UI may know about one credential. Never the value."""

    ref: str
    configured: bool
    source: str | None = None  # "env" | "file" | None
    writable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "configured": self.configured,
            "source": self.source,
            "writable": self.writable,
        }


# Preferences a user may change, with their constraints. Everything not listed
# here is considered environment-only and is reported as such rather than being
# silently persisted.
@dataclass(slots=True, frozen=True)
class _Field:
    name: str
    kind: type
    label: str
    help: str
    choices: tuple[Any, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    section: str = "general"


SETTINGS_FIELDS: tuple[_Field, ...] = (
    _Field(
        "deepseek_model",
        str,
        "Model",
        "DeepSeek model used for the agent loop.",
        choices=PROVIDER_SPECS["deepseek"].models,
        section="llm",
    ),
    _Field(
        "reasoning_effort",
        str,
        "Reasoning effort",
        "How long the model thinks before it acts. 'high' is the default, because "
        "low effort costs the work rather than the speaking; lower it only if "
        "spoken turns feel slow.",
        choices=("minimal", "low", "medium", "high"),
        section="llm",
    ),
    _Field(
        "search_provider",
        str,
        "Search provider",
        "'automatic' fetches the results here when there is no key, and uses Tavily "
        "when there is one. Fetching them here needs nothing configured and a third "
        "party never sees the query — it is this application reading a search page, "
        "which is why it is rate-limited. Tavily is an API: a key under API keys, no "
        "rate limit, and the query goes to them.",
        choices=("automatic", "duckduckgo", "tavily"),
        section="search",
    ),
    _Field(
        "thinking_enabled",
        bool,
        "Thinking mode",
        "Stream the model's reasoning to the UI. Reasoning is never spoken aloud.",
        section="llm",
    ),
    _Field(
        "temperature",
        float,
        "Temperature",
        "Higher is more varied, lower is more predictable.",
        minimum=0.0,
        maximum=2.0,
        section="llm",
    ),
    _Field(
        "max_steps",
        int,
        "Max steps",
        "Backstop against runaway tool use, not a work budget. The repeat-call "
        "guard already stops a stuck agent, so leave this high.",
        minimum=1,
        maximum=2000,
        section="agent",
    ),
    _Field(
        "voice_enabled",
        bool,
        "Voice output",
        "Speak replies aloud. Turning this off gives a text-only session.",
        section="general",
    ),
    _Field(
        "stt_backend",
        str,
        "Speech-to-text engine",
        "'deepgram' streams from the hosted service (needs a key). "
        "'local' recognises on this machine, offline.",
        choices=("deepgram", "local"),
        section="stt",
    ),
    _Field(
        "tts_backend",
        str,
        "Text-to-speech engine",
        "'deepgram' uses the hosted Aura voices (needs a key). "
        "'local' speaks on this machine, offline.",
        choices=("deepgram", "local"),
        section="tts",
    ),
    _Field(
        "stt_model",
        str,
        "Deepgram speech-to-text model",
        "Listen model used for transcription when the engine is deepgram.",
        section="stt",
    ),
    _Field(
        "tts_model",
        str,
        "Deepgram text-to-speech voice",
        "Aura voice used for spoken replies when the engine is deepgram.",
        section="tts",
    ),
    _Field(
        "local_stt_model",
        str,
        "Local speech-to-text model",
        "Model key from `surtitle models list`. Used when the engine is local.",
        section="stt",
    ),
    _Field(
        "local_tts_model",
        str,
        "Local text-to-speech voice",
        "Model key from `surtitle models list`. Used when the engine is local.",
        section="tts",
    ),
    _Field(
        "local_eot_silence_ms",
        int,
        "Local end-of-turn silence (ms)",
        "How long you must pause before a locally recognised turn is finished. "
        "A local model has no contextual end-of-turn detector, so this is a timer.",
        minimum=200,
        maximum=5000,
        section="stt",
    ),
    _Field(
        "local_eot_extend_ms",
        int,
        "Local unfinished-sentence extension (ms)",
        "Longer wait applied when the transcript ends in 'and', 'the', or similar, "
        "so a half-finished thought is not cut off.",
        minimum=200,
        maximum=8000,
        section="stt",
    ),
    _Field(
        "local_max_utterance_ms",
        int,
        "Longest single spoken turn (ms)",
        "A backstop, not a turn rule: a turn ends when you stop talking. This only "
        "stops someone who never pauses from holding one turn open, and it fires on "
        "the first pause after this much continuous speech — never mid-sentence.",
        minimum=5000,
        maximum=600000,
        section="stt",
    ),
    _Field(
        "tts_speed",
        float,
        "Speaking rate",
        "1.0 is natural. Lower is slower and more deliberate.",
        minimum=0.5,
        maximum=2.0,
        section="tts",
    ),
    _Field(
        "endpointing_ms",
        int,
        "End-of-turn silence (ms)",
        "How long you must pause before your turn is considered finished.",
        minimum=100,
        maximum=2000,
        section="stt",
    ),
)

_FIELDS_BY_NAME = {f.name: f for f in SETTINGS_FIELDS}


def _atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int | None = None) -> None:
    """Write JSON atomically, optionally forcing ``mode`` on the result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is not None and os.name == "posix":
        # A directory we cannot chmod (network mount, unusual filesystem) is not
        # fatal; the file mode below is what actually protects the secret.
        with contextlib.suppress(OSError):  # pragma: no cover - unusual filesystems
            path.parent.chmod(_DIR_MODE)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, tolerating absence and reporting corruption clearly."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsValidationError(f"cannot read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsValidationError(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}). "
            "Delete or fix the file to continue."
        ) from exc
    if not isinstance(data, dict):
        raise SettingsValidationError(f"{path.name} must contain a JSON object")
    return data


class SettingsStore:
    """Reads and writes persisted settings plus credentials.

    The store is deliberately the only place that touches the credentials file.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings_path = settings.data_dir / SETTINGS_FILENAME
        self.credentials_path = settings.data_dir / CREDENTIALS_FILENAME
        self._stored: dict[str, Any] = {}
        self._credentials: dict[str, str] = {}
        # Snapshot which credentials came from the launch configuration (the real
        # environment, a .env file, or constructor kwargs) *before* effective()
        # folds stored values into the same object.
        #
        # Without this snapshot a stored key becomes indistinguishable from an
        # environment one after the first effective() call, so the UI reports it
        # as read-only and the user cannot edit the key they just saved. That was
        # a real, reported bug: once a key was entered it could never be changed.
        self._launch_credentials: dict[str, str] = {
            ref: value
            for ref, value in (
                ("DEEPSEEK_API_KEY", settings.deepseek_key()),
                ("DEEPGRAM_API_KEY", settings.deepgram_key()),
            )
            if value and value.strip()
        }
        self.load()

    # --- loading ---------------------------------------------------------
    def load(self) -> None:
        """Re-read both files from disk."""
        self._stored = _read_json(self.settings_path)
        self._credentials = self._load_credentials()

    def _load_credentials(self) -> dict[str, str]:
        """Load credential references, enforcing file permissions on POSIX."""
        path = self.credentials_path
        if not path.exists():
            return {}

        if os.name == "posix":
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & _GROUP_OTHER_BITS:
                raise SettingsValidationError(
                    f"{path} is readable beyond its owner (mode {oct(mode)}). "
                    f'Run "chmod 600 {path}" and restart.'
                )

        data = _read_json(path)
        refs = data.get("refs")
        if refs is None:
            return {}
        if not isinstance(refs, dict):
            raise SettingsValidationError(f"{path.name}: 'refs' must be an object")
        cleaned: dict[str, str] = {}
        for ref, value in refs.items():
            if not isinstance(value, str) or not value.strip():
                # An empty value means "not set", matching DSH's behaviour.
                continue
            cleaned[str(ref)] = value
        return cleaned

    def ensure_initialised(self) -> None:
        """Create the data directory and an empty settings file if missing."""
        self.settings.ensure_data_dir()
        if not self.settings_path.exists():
            self.save_settings({})

    # --- effective configuration ----------------------------------------
    def effective(self) -> Settings:
        """Settings with stored preferences and credentials applied.

        Process environment variables take precedence over both files. Mutating
        the already-cached :class:`Settings` instance in place keeps every
        existing consumer (agent loop, voice layer) working unchanged.
        """
        target = self.settings
        for name, value in self._stored.items():
            spec = _FIELDS_BY_NAME.get(name)
            if spec is None:
                continue
            if os.environ.get(f"SURTITLE_{name.upper()}") is not None:
                continue  # explicit env override wins
            if name in {"deepseek_model", "stt_model", "tts_model"} and os.environ.get(
                _ENV_FOR_PREF.get(name, "")
            ):
                continue
            setattr(target, name, value)

        # Credentials: the environment and .env win over the stored file.
        #
        # Driven by the provider list rather than one block per provider. Adding a
        # provider — a search key, a second model vendor — is then a spec and a field
        # on Settings, and a key saved in the settings screen reaches the code that
        # uses it. Written the other way it does not: the key saves, the screen says
        # "configured", and the tool behaves as though it were never set.
        from pydantic import SecretStr

        for spec in PROVIDER_SPECS.values():
            # Keyless providers — a local engine, a search this application does
            # itself — have no credential to apply.
            if not spec.api_key_env:
                continue
            field = spec.api_key_env.lower()
            if field not in type(target).model_fields:
                continue
            if key := self.credential_value(spec.api_key_env):
                setattr(target, field, SecretStr(key))
        return target

    # --- credentials -----------------------------------------------------
    def _settings_value(self, ref: str) -> str | None:
        """Return a credential supplied by the launch configuration.

        ``Settings`` is loaded by pydantic-settings, which reads ``.env`` files as
        well as the real environment. Those values are invisible to
        ``os.environ``, so without this a working key would be reported as missing
        and then silently shadowed by anything saved here.

        Only the snapshot taken at construction counts. Reading the live attribute
        would also pick up values that :meth:`effective` wrote in, making a
        stored key look like an environment one.
        """
        return self._launch_credentials.get(ref)

    def credential_value(self, ref: str) -> str | None:
        """Resolve a credential in the order the running process resolves it.

        Real environment first, then the pydantic-loaded configuration (which
        covers ``.env``), then the stored file.
        """
        from_env = os.environ.get(ref)
        if from_env and from_env.strip():
            return from_env.strip()
        from_settings = self._settings_value(ref)
        if from_settings and from_settings.strip():
            return from_settings.strip()
        return self._credentials.get(ref)

    def credential_state(self, ref: str) -> CredentialState:
        """Describe a credential without revealing it."""
        from_env = os.environ.get(ref)
        if from_env and from_env.strip():
            return CredentialState(ref=ref, configured=True, source="env", writable=False)
        from_settings = self._settings_value(ref)
        if from_settings and from_settings.strip():
            # Supplied by the launch configuration, so editing it here would
            # appear to do nothing.
            return CredentialState(ref=ref, configured=True, source="env", writable=False)
        if self._credentials.get(ref):
            return CredentialState(ref=ref, configured=True, source="file", writable=True)
        return CredentialState(ref=ref, configured=False, source=None, writable=True)

    def set_credential(self, ref: str, value: str) -> CredentialState:
        """Store a credential after validating it looks like a key.

        Refuses to shadow a credential supplied by the environment or a ``.env``
        file, since that would appear to have no effect on the running process.
        """
        self._validate_ref(ref)
        cleaned = value.strip()
        self._validate_credential_value(ref, cleaned)

        existing = self.credential_state(ref)
        if existing.configured and not existing.writable:
            raise SettingsValidationError(
                f"{ref} is already set by an environment variable or a .env file, which takes "
                "precedence over anything saved here. Unset it there before saving it in the app.",
                field_name=ref,
            )

        self._credentials[ref] = cleaned
        self._write_credentials()
        return self.credential_state(ref)

    def clear_credential(self, ref: str) -> CredentialState:
        """Remove a stored credential."""
        self._validate_ref(ref)
        existing = self.credential_state(ref)
        if existing.configured and existing.source == "env":
            raise SettingsValidationError(
                f"{ref} comes from the environment or a .env file, so it cannot be removed here. "
                "Unset it there instead.",
                field_name=ref,
            )
        self._credentials.pop(ref, None)
        self._write_credentials()
        return self.credential_state(ref)

    def _write_credentials(self) -> None:
        payload: dict[str, Any] = {
            "version": CREDENTIALS_VERSION,
            "refs": dict(sorted(self._credentials.items())),
        }
        _atomic_write_json(self.credentials_path, payload, mode=_FILE_MODE)

    @staticmethod
    def _validate_ref(ref: str) -> None:
        if not ref or not ref.isascii():
            raise SettingsValidationError(f"invalid credential reference: {ref!r}")

    @staticmethod
    def _validate_credential_value(ref: str, value: str) -> None:
        """Reject obviously malformed paste input before it is stored.

        Deliberately mirrors the client-side gate DSH applies: an empty value,
        an ``NAME=value`` line copied from a .env file, or a wrapped/quoted
        value are the three mistakes people actually make.
        """
        if not value:
            raise SettingsValidationError("The key is empty.", field_name=ref)
        if "\n" in value or "=" in value:
            raise SettingsValidationError(
                "That looks like a whole line from a file. Paste only the key value, "
                "without a NAME= prefix or quotes.",
                field_name=ref,
            )
        if value != value.strip() or (value[0] in "\"'" and value[-1] == value[0]):
            raise SettingsValidationError(
                "Remove the surrounding quotes from the key.", field_name=ref
            )
        if not all(0x21 <= ord(ch) <= 0x7E for ch in value):
            raise SettingsValidationError(
                "The key contains whitespace or non-printable characters.", field_name=ref
            )

    # --- settings --------------------------------------------------------
    def stored_settings(self) -> dict[str, Any]:
        """The raw stored preference document."""
        return dict(self._stored)

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate and persist a preference patch, then return the new document.

        Unknown keys are rejected rather than ignored, so a typo in the UI
        surfaces immediately instead of appearing to save and silently doing
        nothing.
        """
        if not isinstance(patch, dict):
            raise SettingsValidationError("Settings payload must be an object.")

        updated = dict(self._stored)
        for name, value in patch.items():
            spec = _FIELDS_BY_NAME.get(name)
            if spec is None:
                raise SettingsValidationError(f"Unknown setting {name!r}.", field_name=str(name))
            updated[name] = _coerce(spec, value)

        self.settings.ensure_data_dir()
        _atomic_write_json(self.settings_path, updated)
        self._stored = updated
        self.effective()
        return dict(updated)

    def reset_settings(self) -> dict[str, Any]:
        """Discard all stored preferences (credentials are untouched)."""
        _atomic_write_json(self.settings_path, {})
        self._stored = {}
        return {}

    # --- UI surface ------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Everything the settings screen needs, with no secret values.

        Credentials appear only as ``configured``/``source``/``writable``. This
        method is the contract; do not add a value field to it.
        """
        effective = self.effective()
        sections: dict[str, list[dict[str, Any]]] = {}
        for spec in SETTINGS_FIELDS:
            sections.setdefault(spec.section, []).append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "help": spec.help,
                    "kind": spec.kind.__name__,
                    "choices": list(spec.choices) if spec.choices else None,
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "value": getattr(effective, spec.name),
                    "stored": spec.name in self._stored,
                    "env_locked": _env_locked(spec.name),
                }
            )

        # Capabilities, each with the providers that serve it and everything needed
        # to configure one: the section a person opens, the selector, the key field
        # for the provider that needs one, and whether a local engine is here yet.
        local = _local_engine_state(effective) if self._wants_local_engines(effective) else None
        capabilities = []
        for capability in CAPABILITIES:
            providers = []
            for provider_id in capability.providers:
                spec = PROVIDER_SPECS[provider_id]
                entry: dict[str, Any] = {
                    "id": spec.id,
                    "label": spec.label,
                    "kind": spec.kind,
                    "api_key_env": spec.api_key_env,
                    "base_url": spec.base_url,
                    "models": list(spec.models),
                    "default_model": spec.default_model,
                    "docs_url": spec.docs_url,
                    "install": spec.install,
                    "credential": self.credential_state(spec.api_key_env).to_dict()
                    if spec.api_key_env
                    else None,
                }
                if spec.kind == "local" and local is not None:
                    entry["local"] = local
                providers.append(entry)
            capabilities.append(
                {
                    "id": capability.id,
                    "label": capability.label,
                    "setting": capability.setting,
                    "automatic": capability.automatic,
                    "providers": providers,
                }
            )

        return {
            "data_dir": str(effective.data_dir),
            "settings_path": str(self.settings_path),
            "credentials_path": str(self.credentials_path),
            "sections": sections,
            "capabilities": capabilities,
            # The screen builds its navigation from these rather than keeping its own
            # list, so a capability added here appears there without an edit.
            "section_labels": {
                **{capability.id: capability.label for capability in CAPABILITIES},
                "agent": "Agent",
                "general": "General",
            },
            "section_order": [*[c.id for c in CAPABILITIES], "agent", "general"],
        }

    def _wants_local_engines(self, settings: Settings) -> bool:
        """Whether anything selected would run on this machine.

        Statting the model files and asking whether the runtime imports is cheap but
        not free, and there is no reason to do it for a configuration that is all
        hosted.
        """
        selected = {settings.stt_backend, settings.tts_backend}
        return "local" in selected and settings.voice_enabled


_ENV_FOR_PREF = {
    "deepseek_model": "DEEPSEEK_MODEL",
    "stt_model": "DEEPGRAM_STT_MODEL",
    "tts_model": "DEEPGRAM_TTS_MODEL",
    # The engine selectors have a plain SURTITLE_<NAME> alias, so the generic
    # check in _env_locked already covers them; these are only listed because the
    # model *names* use the backend-specific prefixes above.
    "local_stt_model": "SURTITLE_LOCAL_STT_MODEL",
    "local_tts_model": "SURTITLE_LOCAL_TTS_MODEL",
}


def _local_engine_state(settings: Settings) -> dict[str, Any]:
    """Whether the on-this-machine speech engines are ready, and what is missing.

    Imported here rather than at module scope: the voice package pulls in audio and
    model code that a machine using hosted engines never needs, and this runs only
    when something local is actually selected. Nothing is loaded — the models are
    stat-ed and the runtime is imported, which is what the tray polls already.
    """
    from surtitle.voice.install import state as install_state

    snapshot = install_state(settings)
    return {
        "ready": snapshot.ready,
        "detail": snapshot.detail,
        "missing_models": snapshot.missing_models,
        "missing_bytes": snapshot.missing_bytes,
    }


def _env_locked(name: str) -> bool:
    """True when an environment variable pins this preference."""
    if os.environ.get(f"SURTITLE_{name.upper()}") is not None:
        return True
    env_name = _ENV_FOR_PREF.get(name)
    return bool(env_name and os.environ.get(env_name))


def _coerce(spec: _Field, value: Any) -> Any:
    """Validate and convert one setting value, with a helpful error on failure."""
    kind = spec.kind

    if kind is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise SettingsValidationError(f"{spec.label} must be true or false.", field_name=spec.name)

    if kind is int:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise SettingsValidationError(f"{spec.label} must be a number.", field_name=spec.name)
        try:
            number = int(float(value))
        except (TypeError, ValueError) as exc:
            raise SettingsValidationError(
                f"{spec.label} must be a whole number.", field_name=spec.name
            ) from exc
    elif kind is float:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise SettingsValidationError(f"{spec.label} must be a number.", field_name=spec.name)
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise SettingsValidationError(
                f"{spec.label} must be a number.", field_name=spec.name
            ) from exc
    else:
        if not isinstance(value, str):
            raise SettingsValidationError(f"{spec.label} must be text.", field_name=spec.name)
        text = value.strip()
        if not text:
            raise SettingsValidationError(f"{spec.label} cannot be empty.", field_name=spec.name)
        if spec.choices and text not in spec.choices:
            raise SettingsValidationError(
                f"{spec.label} must be one of: {', '.join(map(str, spec.choices))}.",
                field_name=spec.name,
            )
        return text

    if spec.minimum is not None and number < spec.minimum:
        raise SettingsValidationError(
            f"{spec.label} must be at least {spec.minimum}.", field_name=spec.name
        )
    if spec.maximum is not None and number > spec.maximum:
        raise SettingsValidationError(
            f"{spec.label} must be at most {spec.maximum}.", field_name=spec.name
        )
    return number
