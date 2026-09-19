"""What Surtitle needs a service for, and who can provide it.

Two tables, and everything about service selection comes out of them: the settings
sections, the provider selectors, the key fields, the install affordances, and the
endpoint a model turn is sent to. They live here rather than in the settings store
because they are not a storage concern — the settings screen, the chat client and
the capability model all read the same two tables, and a provider added here appears
in all of them.

A **capability** is a thing that has to be done: run the model, recognise speech,
speak, search. A **provider** is one way of doing it, and may serve more than one
capability — Deepgram recognises and speaks, and so do the engines that run on this
machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from surtitle.config import (
    DEEPGRAM_STT_MODEL,
    DEEPGRAM_TTS_MODEL,
    Settings,
)

__all__ = [
    "CAPABILITIES",
    "CAPABILITIES_BY_ID",
    "PROVIDER_SPECS",
    "Capability",
    "ChatTarget",
    "ProviderSpec",
    "chat_provider_ids",
    "resolve_chat",
]


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
    Capability("llm", "Model", "llm_provider", ("deepseek", "openrouter", "ollama")),
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
    "openrouter": ProviderSpec(
        id="openrouter",
        label="OpenRouter",
        capabilities=("llm",),
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        # Hundreds of models, and the list moves. The Test button asks the provider
        # what this key can use; a model field that had to be guessed at would be
        # stale within a month.
        docs_url="https://openrouter.ai/keys",
    ),
    "ollama": ProviderSpec(
        id="ollama",
        label="On this machine (Ollama)",
        capabilities=("llm",),
        base_url="http://127.0.0.1:11434/v1",
        needs_key=False,
        kind="local",
        docs_url="https://ollama.com/download",
        # Ollama serves an OpenAI-compatible /v1/models, so the same Test button
        # that checks a hosted key lists what has been pulled here.
        discovery_path="/models",
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


# --- the model provider -------------------------------------------------
#
# Which endpoint a turn is sent to, and how it is addressed. Every provider here
# speaks the same wire format, so this is a lookup rather than a second client.

DEFAULT_CHAT_PROVIDER = "deepseek"

# The model preference that belongs to each provider, so switching provider and
# switching back does not lose the model that was chosen for it.
_MODEL_SETTING = {
    "deepseek": "deepseek_model",
    "openrouter": "openrouter_model",
    "ollama": "ollama_model",
}
# Where a provider's endpoint can be moved, for a local server at another address.
_BASE_URL_SETTING = {"ollama": "ollama_base_url"}
# Endpoints that accept DeepSeek's thinking controls. Sent to nobody else: a
# provider handed a field it does not know rejects the whole request, which
# presents as "that model does not work".
_THINKING_CONTROLS = frozenset({"deepseek"})


@dataclass(frozen=True, slots=True)
class ChatTarget:
    """Where one turn is sent, resolved from the settings."""

    id: str
    label: str
    base_url: str
    model: str
    api_key: str | None
    # The environment variable that would hold the key, for the error message.
    api_key_env: str | None
    needs_key: bool
    thinking_controls: bool
    kind: str


def chat_provider_ids() -> tuple[str, ...]:
    """The model providers, in the order the settings screen offers them."""
    return CAPABILITIES_BY_ID["llm"].providers


def resolve_chat(settings: Settings) -> ChatTarget:
    """The provider, endpoint and model a turn will use.

    An unknown provider — a setting from a newer version, or a hand-edited file —
    falls back to the default rather than failing a turn: the useful behaviour is
    to keep working, and the settings screen shows what is actually selected.
    """
    chosen = (getattr(settings, "llm_provider", "") or "").strip().lower()
    provider = chosen if chosen in PROVIDER_SPECS else DEFAULT_CHAT_PROVIDER
    spec = PROVIDER_SPECS[provider]
    base_url = getattr(settings, _BASE_URL_SETTING.get(provider, ""), "") or spec.base_url
    model = str(getattr(settings, _MODEL_SETTING.get(provider, ""), "") or spec.default_model)
    return ChatTarget(
        id=spec.id,
        label=spec.label,
        base_url=base_url.rstrip("/"),
        model=model.strip(),
        api_key=settings.api_key_for(spec.api_key_env),
        api_key_env=spec.api_key_env,
        needs_key=spec.needs_key,
        thinking_controls=provider in _THINKING_CONTROLS,
        kind=spec.kind,
    )
