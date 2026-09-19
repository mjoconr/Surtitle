"""Choosing the model provider, and what the chosen one is sent.

Every provider here speaks the same wire format, so the interesting parts are the
things that differ: where the request goes, which key it carries, which model name,
and which request fields only one provider understands. A field sent to a provider
that does not know it rejects the whole request, which reaches the user as "that
model does not work" rather than as a rejected field.
"""

from __future__ import annotations

import json

import httpx
import pytest

from surtitle.config import Settings
from surtitle.llm.chat import ChatClient, ChatError
from surtitle.providers import chat_provider_ids, resolve_chat


def settings(**overrides) -> Settings:
    values = {"DEEPSEEK_API_KEY": "sk-deepseek", "OPENROUTER_API_KEY": "sk-openrouter"}
    values.update(overrides)
    return Settings(**values)


class TestWhichProviderATurnGoesTo:
    def test_the_default_is_deepseek(self):
        target = resolve_chat(settings())

        assert target.id == "deepseek"
        assert target.base_url == "https://api.deepseek.com"
        assert target.model == "deepseek-flash"
        assert target.api_key == "sk-deepseek"
        assert target.thinking_controls is True

    def test_openrouter_is_another_endpoint_key_and_model(self):
        target = resolve_chat(settings(SURTITLE_LLM_PROVIDER="openrouter"))

        assert target.id == "openrouter"
        assert target.base_url == "https://openrouter.ai/api/v1"
        assert target.api_key == "sk-openrouter"
        assert target.model == "openai/gpt-4o-mini"
        assert target.thinking_controls is False, "the thinking controls are DeepSeek's"

    def test_a_local_provider_needs_no_key_and_takes_no_auth(self):
        target = resolve_chat(settings(SURTITLE_LLM_PROVIDER="ollama"))

        assert target.id == "ollama"
        assert target.base_url == "http://127.0.0.1:11434/v1"
        assert target.api_key is None
        assert target.needs_key is False
        assert target.kind == "local"

    def test_the_local_endpoint_can_be_moved(self):
        """Somebody running the model server on another machine or port is not an
        edge case; it is how a bigger model gets used from a laptop."""
        target = resolve_chat(
            settings(
                SURTITLE_LLM_PROVIDER="ollama",
                SURTITLE_OLLAMA_BASE_URL="http://192.168.1.9:8000/v1/",
            )
        )

        assert target.base_url == "http://192.168.1.9:8000/v1", "trailing slash trimmed"

    def test_each_provider_keeps_its_own_model(self):
        """Switching away and back must not lose the model chosen for the other."""
        chosen = settings(
            SURTITLE_LLM_PROVIDER="ollama",
            OLLAMA_MODEL="qwen2.5-coder",
            DEEPSEEK_MODEL="deepseek-v4-pro",
        )

        assert resolve_chat(chosen).model == "qwen2.5-coder"
        assert resolve_chat(chosen.model_copy(update={"llm_provider": "deepseek"})).model == (
            "deepseek-v4-pro"
        )

    def test_an_unknown_provider_falls_back_rather_than_failing_a_turn(self):
        """A setting from a newer version, or a hand-edited file. Keeping the agent
        working matters more than reporting the typo, and the settings screen shows
        what is actually selected."""
        target = resolve_chat(settings(SURTITLE_LLM_PROVIDER="somebody-elses-model"))

        assert target.id == "deepseek"

    def test_every_offered_provider_can_be_resolved(self):
        """The list the settings screen offers and the table that resolves it are the
        same table, so this is what keeps them from drifting."""
        for provider_id in chat_provider_ids():
            target = resolve_chat(settings(SURTITLE_LLM_PROVIDER=provider_id))

            assert target.id == provider_id
            assert target.model, f"{provider_id} resolves to no model"
            assert target.base_url, f"{provider_id} resolves to no endpoint"

    def test_the_offered_providers_include_one_that_runs_here(self):
        kinds = {
            resolve_chat(settings(SURTITLE_LLM_PROVIDER=pid)).kind for pid in chat_provider_ids()
        }

        assert "local" in kinds, "a model that runs on this machine is the point of no key"


class TestWhatTheChosenProviderIsSent:
    def test_the_model_is_the_one_resolved_for_the_provider(self):
        client = ChatClient(settings(SURTITLE_LLM_PROVIDER="ollama", OLLAMA_MODEL="llama3.2"))

        assert client._payload([{"role": "user", "content": "hi"}], None)["model"] == "llama3.2"

    def test_deepseek_gets_its_thinking_controls(self):
        client = ChatClient(settings(SURTITLE_LLM_PROVIDER="deepseek", thinking_enabled=True))

        payload = client._payload([{"role": "user", "content": "hi"}], None)

        assert payload["thinking"] == {"type": "enabled"}
        assert "reasoning_effort" in payload
        assert "temperature" not in payload, "ignored by DeepSeek with thinking on"

    def test_nobody_else_is_sent_the_thinking_controls(self):
        """A provider handed an unknown field rejects the request, which is how a
        second provider "does not work" for no visible reason."""
        for provider in ("openrouter", "ollama"):
            client = ChatClient(settings(SURTITLE_LLM_PROVIDER=provider, thinking_enabled=True))

            payload = client._payload([{"role": "user", "content": "hi"}], None)

            assert "thinking" not in payload, provider
            assert "reasoning_effort" not in payload, provider
            assert "temperature" in payload, provider

    def test_a_keyed_provider_sends_its_key(self):
        client = ChatClient(settings(SURTITLE_LLM_PROVIDER="openrouter"))

        assert client._headers()["Authorization"] == "Bearer sk-openrouter"

    def test_a_keyless_provider_sends_no_authorization(self):
        client = ChatClient(settings(SURTITLE_LLM_PROVIDER="ollama"))

        assert "Authorization" not in client._headers()

    def test_a_missing_key_names_the_provider_and_the_variable(self):
        """With more than one provider, "the API key is missing" is not something
        anybody can act on."""
        client = ChatClient(settings(SURTITLE_LLM_PROVIDER="openrouter", OPENROUTER_API_KEY=""))

        with pytest.raises(ChatError) as raised:
            client._headers()

        message = str(raised.value)
        assert "OpenRouter" in message
        assert "OPENROUTER_API_KEY" in message

    def test_the_provider_is_resolved_per_use_not_at_birth(self):
        """The server builds one client at start-up and hands it to every session, so
        a client that resolved in its constructor kept using the provider selected
        when the process started — choosing another in Settings changed nothing until
        a restart, and the failure looked like the new provider rejecting the key."""
        shared = settings()
        client = ChatClient(shared)

        assert client.target.id == "deepseek"

        shared.llm_provider = "ollama"

        assert client.target.id == "ollama", "a long-lived client has to follow the setting"


class TestTheRequestThatGoesOut:
    async def test_it_posts_to_the_provider_endpoint_and_parses_the_stream(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            body = (
                b'data: {"choices":[{"index":0,"delta":{"content":"Hello "},"finish_reason":null}]}\n\n'
                b'data: {"choices":[{"index":0,"delta":{"content":"there"},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
                b"data: [DONE]\n\n"
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = ChatClient(settings(SURTITLE_LLM_PROVIDER="ollama"), client=http)
            events = [event async for event in client.stream([{"role": "user", "content": "hi"}])]

        assert seen["url"] == "http://127.0.0.1:11434/v1/chat/completions"
        assert seen["auth"] is None
        assert "".join(event.text or "" for event in events) == "Hello there"
        assert [event.kind for event in events][-1] == "done"

    async def test_a_rejected_key_is_reported_with_the_provider_that_rejected_it(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, content=json.dumps({"error": {"message": "no such key"}}).encode()
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = ChatClient(settings(SURTITLE_LLM_PROVIDER="openrouter"), client=http)
            with pytest.raises(ChatError) as raised:
                async for _event in client.stream([{"role": "user", "content": "hi"}]):
                    pass

        message = str(raised.value)
        assert "OpenRouter" in message
        assert "OPENROUTER_API_KEY" in message
        assert "no such key" in message, "the provider's own words are kept"


class TestUsageIsPricedForTheModelThatRan:
    """The cost shown is for the model that actually answered.

    It was priced from `deepseek_model` whatever the provider, so a model running on
    this machine — which costs nothing — was reported with DeepSeek's rates. A wrong
    cost is worse than an absent one, which is the rule the price table already
    states for a model it has never heard of.
    """

    def test_deepseek_is_priced(self):
        from surtitle.stats import resolve_price

        chosen = settings(SURTITLE_LLM_PROVIDER="deepseek")

        assert resolve_price(resolve_chat(chosen).model, chosen) is not None

    def test_a_provider_with_no_price_table_gets_no_figure(self):
        from surtitle.stats import resolve_price

        for provider in ("openrouter", "ollama"):
            chosen = settings(SURTITLE_LLM_PROVIDER=provider)

            assert resolve_price(resolve_chat(chosen).model, chosen) is None, provider

    def test_the_session_prices_the_resolved_model(self):
        """Read as source, because the alternative is a whole session per provider
        to make the same point: the call site must not reach for DeepSeek's name."""
        import inspect
        import re

        from surtitle.core import session as session_module

        source = inspect.getsource(session_module.Session._count)

        assert "resolve_chat(self.settings).model" in source
        assert not re.search(r"resolve_price\(\s*self\.settings\.deepseek_model", source)
