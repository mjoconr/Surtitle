"""Tests for the agent loop.

These drive the loop with a scripted model stream, so the tests are exact and
offline. They cover the behaviours that make this a voice agent rather than a
generic tool runner: the speak/display split, automatic narration repair, the
approval gate, and the step cap.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from surtitle.config import Settings
from surtitle.core.agent import AgentLoop, ApprovalBroker, _title_from, build_system_prompt
from surtitle.core.events import EventKind
from surtitle.core.speak import Chunk, ChunkKind
from surtitle.llm.chat import ChatError, StreamEvent, ToolCallDelta, Usage
from surtitle.tools.artifacts import make_spreadsheet
from surtitle.tools.fs_tools import ToolResult, list_dir, read_file
from surtitle.tools.registry import Tool, ToolRegistry


class FakeClient:
    """A model that replays a fixed script, one stream per call."""

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": list(messages), "tools": tools})
        script = self.scripts[min(len(self.calls) - 1, len(self.scripts) - 1)]
        for event in script:
            yield event

    async def aclose(self) -> None:
        return None


def text_script(*deltas: str, usage: Usage | None = None) -> list[StreamEvent]:
    """A script that emits text deltas then finishes."""
    events = [StreamEvent(kind="text", text=delta) for delta in deltas]
    events.append(StreamEvent(kind="usage", usage=usage or Usage()))
    events.append(StreamEvent(kind="done", finish_reason="stop"))
    return events


def tool_script(
    *, name: str, arguments: dict[str, Any], preamble: str = "", call_id: str = "c1"
) -> list[StreamEvent]:
    """A script that narrates, then calls one tool."""
    events: list[StreamEvent] = []
    if preamble:
        events.append(StreamEvent(kind="text", text=preamble))
    call = ToolCallDelta(index=0, id=call_id, name=name, arguments=json.dumps(arguments))
    events.append(StreamEvent(kind="tool_call", tool_call=call))
    events.append(StreamEvent(kind="usage", usage=Usage()))
    events.append(StreamEvent(kind="done", finish_reason="tool_calls"))
    return events


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DEEPSEEK_API_KEY="test-key",
        DEEPGRAM_API_KEY="test-key",
        max_steps=3,
        thinking_enabled=False,
    )


def make_loop(settings, tmp_path, client, **kwargs) -> AgentLoop:
    registry = kwargs.pop("registry", ToolRegistry([]))
    return AgentLoop(
        settings,
        root=tmp_path,
        session_id="s1",
        client=client,
        registry=registry,
        **kwargs,
    )


async def collect(loop: AgentLoop, user_text: str = "hello", **kwargs) -> list:
    events = []
    async for event in loop.run([], user_text, **kwargs):
        events.append(event)
    return events


def kinds(events) -> list[str]:
    return [e.kind.value for e in events]


def says(events) -> list[str]:
    return [e.data["text"] for e in events if e.kind is EventKind.SAY]


def displayed(events) -> str:
    return "".join(e.data["text"] for e in events if e.kind is EventKind.AGENT_TEXT)


class TestSpeakLayerIntegration:
    async def test_say_blocks_become_say_events(self, settings, tmp_path):
        client = FakeClient([text_script("<say>Hello there.</say>")])
        loop = make_loop(settings, tmp_path, client)
        events = await collect(loop)
        assert "Hello there." in says(events)

    async def test_display_blocks_are_not_spoken(self, settings, tmp_path):
        client = FakeClient([text_script("<say>Done.</say><display>| a | b |</display>")])
        loop = make_loop(settings, tmp_path, client)
        events = await collect(loop)
        assert all("a | b" not in text for text in says(events))
        assert "a | b" in displayed(events)

    async def test_spoken_text_reaches_tts_while_the_turn_is_running(self, settings, tmp_path):
        """`on_chunk` must fire before the turn ends, not after."""
        client = FakeClient([text_script("<say>First thing. Second thing.</say>")])
        loop = make_loop(settings, tmp_path, client)
        seen: list[tuple[str, bool]] = []
        done = False

        async def on_chunk(chunk: Chunk) -> None:
            seen.append((chunk.text, done))

        async for event in loop.run([], "go", on_chunk=on_chunk):
            if event.kind is EventKind.DONE:
                done = True

        assert seen, "nothing was sent to text-to-speech"
        assert any(not finished for _text, finished in seen), (
            "all speech was emitted only after the turn finished"
        )
        assert any(text == "First thing." for text, _ in seen)

    async def test_unspoken_turn_is_repaired(self, settings, tmp_path):
        """A model that ignores the tagging contract must not produce silence."""
        client = FakeClient([text_script("Revenue rose eight percent this quarter.")])
        loop = make_loop(settings, tmp_path, client)
        events = await collect(loop)
        assert says(events)
        assert "Revenue rose eight percent this quarter." in says(events)[0]

    async def test_repaired_text_is_also_spoken_to_tts(self, settings, tmp_path):
        client = FakeClient([text_script("All finished.")])
        loop = make_loop(settings, tmp_path, client)
        spoken: list[str] = []

        async def on_chunk(chunk: Chunk) -> None:
            if chunk.kind is ChunkKind.SAY:
                spoken.append(chunk.text)

        await collect(loop, on_chunk=on_chunk)
        assert spoken == ["All finished."]

    async def test_reasoning_is_never_spoken(self, settings, tmp_path):
        script = [
            StreamEvent(kind="reasoning", text="Let me think about the numbers."),
            StreamEvent(kind="text", text="<say>It is eight percent.</say>"),
            StreamEvent(kind="done"),
        ]
        client = FakeClient([script])
        loop = make_loop(settings, tmp_path, client)
        spoken: list[str] = []

        async def on_chunk(chunk: Chunk) -> None:
            spoken.append(chunk.text)

        await collect(loop, on_chunk=on_chunk)
        assert all("think about" not in text for text in spoken)
        assert "It is eight percent." in spoken

    async def test_thinking_is_streamed_on_the_side_channel(self, settings, tmp_path):
        script = [
            StreamEvent(kind="reasoning", text="Considering options."),
            StreamEvent(kind="text", text="<say>Okay.</say>"),
            StreamEvent(kind="done"),
        ]
        client = FakeClient([script])
        loop = make_loop(settings, tmp_path, client)
        side: list = []
        loop.set_emitter(lambda event: _collect_side(side, event))
        await collect(loop)

        import asyncio

        await asyncio.sleep(0)
        assert any(e.kind is EventKind.THINKING for e in side)


async def _collect_side(sink: list, event) -> None:
    sink.append(event)


class TestToolCalling:
    async def test_tool_is_executed_and_result_fed_back(self, settings, tmp_path):
        (tmp_path / "notes.txt").write_text("the secret word is platypus", encoding="utf-8")

        registry = ToolRegistry(
            [
                Tool(
                    name="read_file",
                    description="read",
                    parameters={"type": "object", "properties": {}},
                    handler=read_file,
                    approval="never",
                )
            ]
        )
        client = FakeClient(
            [
                tool_script(
                    name="read_file",
                    arguments={"path": "notes.txt"},
                    preamble="<say>Let me look.</say>",
                ),
                text_script("<say>It is platypus.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, registry=registry)
        events = await collect(loop)

        assert EventKind.TOOL_CALL in [e.kind for e in events]
        assert EventKind.TOOL_RESULT in [e.kind for e in events]
        assert "It is platypus." in says(events)

        # The second request must include the tool result, or the model cannot
        # know what the tool returned.
        second = client.calls[1]["messages"]
        tool_messages = [m for m in second if m.get("role") == "tool"]
        assert tool_messages, "tool result was not fed back to the model"
        assert "platypus" in tool_messages[0]["content"]

    async def test_loop_terminates_when_the_model_stops_calling_tools(self, settings, tmp_path):
        registry = ToolRegistry(
            [
                Tool(
                    name="list_dir",
                    description="list",
                    parameters={"type": "object", "properties": {}},
                    handler=list_dir,
                    approval="never",
                )
            ]
        )
        client = FakeClient(
            [
                tool_script(
                    name="list_dir", arguments={"path": "."}, preamble="<say>Checking.</say>"
                ),
                text_script("<say>Nothing there.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, registry=registry)
        events = await collect(loop)
        event_kinds = kinds(events)
        assert event_kinds, "no events were emitted"
        assert event_kinds[-1] == "done"
        assert len(client.calls) == 2

    async def test_step_cap_reports_truncation(self, settings, tmp_path):
        # A model that calls a tool forever must be stopped.
        registry = ToolRegistry(
            [
                Tool(
                    name="list_dir",
                    description="list",
                    parameters={"type": "object", "properties": {}},
                    handler=list_dir,
                    approval="never",
                )
            ]
        )
        client = FakeClient(
            [
                tool_script(name="list_dir", arguments={"path": "."}),
            ]
        )
        loop = make_loop(settings, tmp_path, client, registry=registry)
        events = await collect(loop)
        assert len(client.calls) == settings.max_steps
        errors = [e for e in events if e.kind is EventKind.ERROR]
        assert errors and errors[0].data["kind_detail"] == "step_limit"
        assert any(e.kind is EventKind.DONE and e.data.get("truncated") for e in events)

    async def test_malformed_arguments_are_reported_to_the_model(self, settings, tmp_path):
        registry = ToolRegistry(
            [
                Tool(
                    name="read_file",
                    description="read",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda ctx, **kw: None,
                    approval="never",
                )
            ]
        )
        bad_call = ToolCallDelta(index=0, id="c1", name="read_file", arguments="{not json")
        client = FakeClient(
            [
                [
                    StreamEvent(kind="text", text="<say>Trying.</say>"),
                    StreamEvent(kind="tool_call", tool_call=bad_call),
                    StreamEvent(kind="done", finish_reason="tool_calls"),
                ],
                text_script("<say>That did not work.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, registry=registry)
        events = await collect(loop)

        results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
        assert results and results[0].data["ok"] is False
        assert "JSON" in (results[0].data.get("error") or "")
        # The model is told, so it can retry correctly.
        tool_messages = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
        assert "JSON" in tool_messages[0]["content"]

    async def test_artifact_events_are_emitted(self, settings, tmp_path):
        registry = ToolRegistry(
            [
                Tool(
                    name="make_spreadsheet",
                    description="sheet",
                    parameters={"type": "object", "properties": {}},
                    handler=make_spreadsheet,
                    approval="never",
                )
            ]
        )
        client = FakeClient(
            [
                tool_script(
                    name="make_spreadsheet",
                    arguments={
                        "path": "out.xlsx",
                        "sheets": [{"name": "S", "rows": [[1, 2]]}],
                    },
                    preamble="<say>Building the sheet.</say>",
                ),
                text_script("<say>Done.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, registry=registry)
        events = await collect(loop)
        artifacts = [e for e in events if e.kind is EventKind.ARTIFACT]
        assert artifacts and artifacts[0].data["path"] == "out.xlsx"


class TestApprovalGate:
    def make_registry(self):
        calls: list[dict[str, Any]] = []

        def mutating(ctx, path: str = "x"):
            calls.append({"path": path})
            return ToolResult(ok=True, display=f"wrote {path}")

        return (
            ToolRegistry(
                [
                    Tool(
                        name="write_file",
                        description="write",
                        parameters={"type": "object", "properties": {}},
                        handler=mutating,
                        approval="ask",
                        mutating=True,
                    )
                ]
            ),
            calls,
        )

    async def test_approval_is_requested_for_mutating_tools(self, settings, tmp_path):
        registry, calls = self.make_registry()
        client = FakeClient(
            [
                tool_script(name="write_file", arguments={"path": "a.txt"}),
                text_script("<say>Done.</say>"),
            ]
        )
        approvals = ApprovalBroker()
        loop = make_loop(settings, tmp_path, client, registry=registry, approvals=approvals)

        requests: list = []
        async for event in loop.run([], "write it"):
            if event.kind is EventKind.APPROVAL_REQUEST:
                requests.append(event)
                approvals.resolve(event.data["call_id"], allowed=True)

        assert len(requests) == 1
        assert requests[0].data["name"] == "write_file"
        assert requests[0].data["mutating"] is True
        assert calls == [{"path": "a.txt"}], "the tool should have run after approval"

    async def test_declining_prevents_execution_and_informs_the_model(self, settings, tmp_path):
        registry, calls = self.make_registry()
        client = FakeClient(
            [
                tool_script(name="write_file", arguments={"path": "a.txt"}),
                text_script("<say>Understood.</say>"),
            ]
        )
        approvals = ApprovalBroker()
        loop = make_loop(settings, tmp_path, client, registry=registry, approvals=approvals)

        async for event in loop.run([], "write it"):
            if event.kind is EventKind.APPROVAL_REQUEST:
                approvals.resolve(event.data["call_id"], allowed=False)

        assert calls == [], "a declined tool must not run"
        tool_messages = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
        assert "declined" in tool_messages[0]["content"].lower()

    async def test_remembering_skips_the_second_prompt(self, settings, tmp_path):
        registry, calls = self.make_registry()
        client = FakeClient(
            [
                tool_script(name="write_file", arguments={"path": "a.txt"}, call_id="c1"),
                tool_script(name="write_file", arguments={"path": "b.txt"}, call_id="c2"),
                text_script("<say>Both done.</say>"),
            ]
        )
        approvals = ApprovalBroker()
        loop = make_loop(settings, tmp_path, client, registry=registry, approvals=approvals)

        prompts = 0
        async for event in loop.run([], "write both"):
            if event.kind is EventKind.APPROVAL_REQUEST:
                prompts += 1
                approvals.resolve(event.data["call_id"], allowed=True, remember=True)

        assert prompts == 1, "remembering should suppress the second prompt"
        assert calls == [{"path": "a.txt"}, {"path": "b.txt"}]

    async def test_already_trusted_tools_do_not_prompt(self, settings, tmp_path):
        registry, calls = self.make_registry()
        client = FakeClient(
            [
                tool_script(name="write_file", arguments={"path": "a.txt"}),
                text_script("<say>Done.</say>"),
            ]
        )
        approvals = ApprovalBroker()
        approvals.trust(["write_file"])
        loop = make_loop(settings, tmp_path, client, registry=registry, approvals=approvals)

        prompts = 0
        async for event in loop.run([], "write it"):
            if event.kind is EventKind.APPROVAL_REQUEST:
                prompts += 1

        assert prompts == 0
        assert calls

    async def test_read_only_tools_never_prompt(self, settings, tmp_path):
        (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
        registry = ToolRegistry(
            [
                Tool(
                    name="read_file",
                    description="read",
                    parameters={"type": "object", "properties": {}},
                    handler=read_file,
                    approval="never",
                )
            ]
        )
        client = FakeClient(
            [
                tool_script(name="read_file", arguments={"path": "a.txt"}),
                text_script("<say>Read it.</say>"),
            ]
        )
        loop = make_loop(settings, tmp_path, client, registry=registry)
        events = await collect(loop)
        assert EventKind.APPROVAL_REQUEST not in [e.kind for e in events]


class TestFailureHandling:
    async def test_model_error_is_reported_and_turn_ends(self, settings, tmp_path):
        class FailingClient:
            async def stream(self, messages, *, tools=None):
                raise ChatError("The DeepSeek API rejected the key.")
                yield  # pragma: no cover - makes this an async generator

            async def aclose(self) -> None:
                return None

        loop = make_loop(settings, tmp_path, FailingClient())
        events = await collect(loop)
        errors = [e for e in events if e.kind is EventKind.ERROR]
        assert errors and "rejected the key" in errors[0].data["message"]
        assert any(e.kind is EventKind.DONE and e.data.get("failed") for e in events)

    async def test_cancellation_stops_cleanly(self, settings, tmp_path):
        client = FakeClient([text_script("<say>Something long.</say>")])
        loop = make_loop(settings, tmp_path, client)
        loop.cancel()
        events = await collect(loop)
        # A cancelled turn reports idle rather than pretending to have finished.
        assert any(e.kind is EventKind.STATE for e in events)


class TestPromptContract:
    def test_prompt_defines_both_channels(self):
        prompt = build_system_prompt("myproject")
        assert "<say>" in prompt
        assert "<display>" in prompt
        assert "myproject" in prompt

    def test_prompt_forbids_speaking_paths_and_tables(self):
        prompt = build_system_prompt("p")
        assert "path" in prompt.lower()
        assert "table" in prompt.lower()

    def test_title_from_short_text(self):
        assert _title_from("Summarise the Q3 report") == "Summarise the Q3 report"

    def test_title_is_truncated_on_a_word_boundary(self):
        title = _title_from("word " * 40)
        assert len(title) <= 61
        assert title.endswith("…")
        assert not title.rstrip("…").endswith(" ")

    def test_title_of_empty_text(self):
        assert _title_from("   ") == "New conversation"
