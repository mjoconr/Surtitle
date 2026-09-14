"""Streaming DeepSeek client.

DeepSeek's API is OpenAI-compatible, but two details are not, and both matter:

* ``reasoning_content`` carries the thinking stream in a separate field. It must
  be shown in the UI for transparency and must *never* be spoken.
* Tool calls arrive as fragments spread across many deltas, correlated by
  ``index``. They have to be accumulated before they can be executed; treating a
  single delta as a complete call is the classic cause of "arguments is not valid
  JSON" bugs.

We talk raw HTTP rather than using an SDK, so the request shape is visible and
the wire format can be replayed in tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from surtitle.config import Settings

__all__ = [
    "ChatMessage",
    "DeepSeekClient",
    "StreamEvent",
    "ToolCallDelta",
    "Usage",
]

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4
_BACKOFF = (1.0, 2.0, 4.0, 8.0)


class DeepSeekError(RuntimeError):
    """Raised when the model API cannot be used."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(slots=True)
class ToolCallDelta:
    """One tool call being assembled across several deltas."""

    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""

    def to_message_dict(self) -> dict[str, Any]:
        """The form required in an assistant message sent back to the API."""
        return {
            "id": self.id or f"call_{self.index}",
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }

    def parsed_arguments(self) -> tuple[dict[str, Any] | None, str | None]:
        """Parse the accumulated arguments, returning (args, error)."""
        raw = self.arguments.strip() or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"arguments are not valid JSON ({exc.msg} at position {exc.pos})"
        if not isinstance(parsed, dict):
            return None, "arguments must be a JSON object"
        return parsed, None


@dataclass(slots=True)
class Usage:
    """Token accounting for one completion."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(slots=True)
class StreamEvent:
    """A single update from the completion stream.

    Exactly one of the payload fields is meaningful, discriminated by ``kind``.
    """

    kind: str  # "text" | "reasoning" | "tool_call" | "usage" | "done"
    text: str = ""
    tool_call: ToolCallDelta | None = None
    usage: Usage | None = None
    finish_reason: str | None = None


# The message shape accepted by the API. Kept as a plain dict alias because the
# API is loosely typed and messages pass through to the wire unchanged.
ChatMessage = dict[str, Any]


@dataclass(slots=True)
class _Accumulator:
    """Mutable state for one completion turn."""

    tool_calls: dict[int, ToolCallDelta] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None


class DeepSeekClient:
    """Async client for DeepSeek chat completions."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=20.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> DeepSeekClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- request building -------------------------------------------------
    def _payload(
        self,
        messages: Iterable[ChatMessage],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": list(messages),
            "stream": True,
            "max_tokens": self.settings.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        if self.settings.thinking_enabled:
            # DeepSeek's thinking control. Sent only when enabled so a provider
            # that does not support it is not sent an unknown field.
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.settings.reasoning_effort
        else:
            # Temperature is only meaningful with thinking off; DeepSeek ignores
            # or rejects it otherwise, which is a common source of confusion.
            payload["temperature"] = self.settings.temperature
        return payload

    def _headers(self) -> dict[str, str]:
        key = self.settings.deepseek_key()
        if not key:
            raise DeepSeekError(
                "DEEPSEEK_API_KEY is not configured. Add it in Settings or in your .env file."
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    # --- streaming --------------------------------------------------------
    async def stream(
        self,
        messages: Iterable[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one completion, yielding text, reasoning and tool-call events.

        Retries connection-level failures that happen *before* any content has
        been produced. Once bytes have been delivered a retry would duplicate
        output, so the error is surfaced instead.
        """
        url = f"{self.settings.safe_base_url}/chat/completions"
        payload = self._payload(messages, tools)

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            produced = False
            try:
                async with self._client.stream(
                    "POST", url, json=payload, headers=self._headers()
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        raise self._http_error(response.status_code, body)

                    async for event in self._iter_sse(response):
                        produced = True
                        yield event
                    return
            except DeepSeekError as exc:
                if not exc.retryable or produced or attempt == _MAX_RETRIES - 1:
                    raise
                last_error = exc
            except (httpx.HTTPError, httpx.StreamError) as exc:
                if produced or attempt == _MAX_RETRIES - 1:
                    raise DeepSeekError(
                        f"Lost connection to the model API: {type(exc).__name__}: {exc}",
                        retryable=True,
                    ) from exc
                last_error = exc

            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            log.warning("DeepSeek request failed (%s); retrying in %.0fs", last_error, delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

        raise DeepSeekError(f"Request failed after {_MAX_RETRIES} attempts: {last_error}")

    @staticmethod
    def _http_error(status: int, body: bytes) -> DeepSeekError:
        detail = ""
        with contextlib.suppress(Exception):
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or "")
                elif error:
                    detail = str(error)
                else:
                    detail = str(parsed.get("message") or "")
        if not detail:
            detail = body.decode("utf-8", errors="replace")[:300]

        if status in (401, 403):
            message = (
                "The DeepSeek API rejected the key. Check DEEPSEEK_API_KEY in Settings. "
                f"({status}: {detail})"
            )
        elif status == 402:
            message = f"DeepSeek reports insufficient balance. ({detail})"
        elif status == 404:
            message = (
                f"Model {detail or 'not found'}: the configured model may not exist. "
                "Check the model name in Settings."
            )
        elif status == 429:
            message = f"DeepSeek rate limit reached. ({detail})"
        else:
            message = f"DeepSeek API error {status}: {detail}"

        return DeepSeekError(message, status=status, retryable=status in _RETRY_STATUS)

    async def _iter_sse(self, response: httpx.Response) -> AsyncIterator[StreamEvent]:
        """Parse a server-sent-events body into :class:`StreamEvent` objects."""
        accumulator = _Accumulator()

        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith(":"):
                continue  # comment / keep-alive
            if not line.startswith("data:"):
                continue

            data = line[5:].strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    break
                continue

            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                log.debug("skipping unparseable stream chunk: %s", data[:120])
                continue

            usage_payload = chunk.get("usage")
            if isinstance(usage_payload, dict):
                accumulator.usage = _parse_usage(usage_payload)

            choices = chunk.get("choices") or []
            if not choices:
                continue

            choice = choices[0] or {}
            delta = choice.get("delta") or {}

            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                yield StreamEvent(kind="reasoning", text=reasoning)

            content = delta.get("content")
            if isinstance(content, str) and content:
                yield StreamEvent(kind="text", text=content)

            for raw_call in delta.get("tool_calls") or []:
                if not isinstance(raw_call, dict):
                    continue
                index = int(raw_call.get("index") or 0)
                call = accumulator.tool_calls.setdefault(index, ToolCallDelta(index=index))
                if raw_call.get("id"):
                    call.id = str(raw_call["id"])
                function = raw_call.get("function") or {}
                if isinstance(function, dict):
                    if function.get("name"):
                        call.name = str(function["name"])
                    fragment = function.get("arguments")
                    if isinstance(fragment, str):
                        call.arguments += fragment
                yield StreamEvent(kind="tool_call", tool_call=call)

            finish_reason = choice.get("finish_reason")
            if finish_reason:
                accumulator.finish_reason = str(finish_reason)

        if accumulator.tool_calls:
            yield StreamEvent(
                kind="tool_call",
                tool_call=None,
                finish_reason=accumulator.finish_reason,
            )
        yield StreamEvent(kind="usage", usage=accumulator.usage)
        yield StreamEvent(kind="done", finish_reason=accumulator.finish_reason)

    # --- helpers for the agent loop ---------------------------------------
    @staticmethod
    def complete_tool_calls(events: Iterable[StreamEvent]) -> list[ToolCallDelta]:
        """Not used by the loop, kept for tests and tooling."""
        calls: dict[int, ToolCallDelta] = {}
        for event in events:
            if event.tool_call is not None:
                calls[event.tool_call.index] = event.tool_call
        return [calls[k] for k in sorted(calls)]


def _parse_usage(payload: dict[str, Any]) -> Usage:
    """Parse a usage block, including the nested prompt cache details."""
    prompt_details = payload.get("prompt_tokens_details") or {}
    completion_details = payload.get("completion_tokens_details") or {}
    return Usage(
        prompt_tokens=int(payload.get("prompt_tokens") or 0),
        completion_tokens=int(payload.get("completion_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
        cached_tokens=int(prompt_details.get("cached_tokens") or 0),
    )
