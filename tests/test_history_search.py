"""Tests for cross-session retrieval.

The storage half of durable memory already existed — every conversation is in
SQLite — but there was no way to *find* anything in it. History was read linearly
by session id, so knowledge was written and never reached again unless the agent
happened to write it into the notebook.

The notebook holds what the agent chose to record. This finds what it did not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from surtitle.store.db import Store
from surtitle.tools.fs_tools import ToolContext
from surtitle.tools.registry import default_registry


@pytest.fixture
def populated(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("plant", tmp_path)
    first = store.create_session(project.id, "4C-120 investigation")
    second = store.create_session(project.id, "unrelated chat")

    store.add_message(first.id, "user", "why is 4C-120 down?")
    store.add_message(
        first.id,
        "assistant",
        "4C-120 is down because the iod service crashed. Restart with dsh-plant restart 4C-120.",
    )
    store.add_message(second.id, "user", "write me a poem about wool")
    return store, project, first, second


class TestSearch:
    def test_it_finds_a_relevant_exchange(self, populated):
        store, _project, _first, _second = populated
        results = store.search_conversations("4C-120")
        assert results, "a known identifier was not found"
        assert any("4C-120" in item["excerpt"] for item in results)

    def test_the_cause_is_findable_by_its_own_wording(self, populated):
        """Not every useful fact contains the identifier that leads to it."""
        store, _project, _first, _second = populated
        results = store.search_conversations("iod service crashed")
        assert results, "the recorded cause was not retrievable"
        assert any("iod" in item["excerpt"] for item in results)

    def test_results_name_the_conversation_they_came_from(self, populated):
        store, _project, _first, _second = populated
        results = store.search_conversations("4C-120")
        assert results[0]["session_title"] == "4C-120 investigation"

    def test_it_does_not_return_unrelated_conversations(self, populated):
        store, _project, _first, _second = populated
        results = store.search_conversations("poem")
        assert len(results) == 1
        assert "poem" in results[0]["excerpt"]

    def test_punctuation_in_the_query_is_not_treated_as_syntax(self, populated):
        """A machine name or path must not break the query."""
        store, _project, _first, _second = populated
        for query in ("4C-120", "dsh-plant", "iod service", '4C"120', "why is"):
            results = store.search_conversations(query)
            assert isinstance(results, list), f"{query!r} raised"

    def test_an_unmatched_query_returns_nothing_rather_than_everything(self, populated):
        store, _project, _first, _second = populated
        assert store.search_conversations("zebra") == []

    def test_an_empty_query_returns_nothing(self, populated):
        store, _project, _first, _second = populated
        assert store.search_conversations("   ") == []

    def test_the_current_session_can_be_excluded(self, populated):
        """Offering the agent its own recent words back is noise."""
        store, _project, first, _second = populated
        assert store.search_conversations("poem", exclude_session=first.id)
        assert store.search_conversations("poem", exclude_session=_second.id) == []

    def test_the_limit_is_honoured(self, populated):
        store, project, _first, _second = populated
        session = store.create_session(project.id, "many")
        for index in range(20):
            store.add_message(session.id, "user", f"repeated marker {index}")
        assert len(store.search_conversations("marker", limit=3)) <= 3

    def test_excerpts_are_bounded(self, populated):
        store, project, _first, _second = populated
        session = store.create_session(project.id, "long")
        store.add_message(session.id, "user", "haystack " * 2000)
        results = store.search_conversations("haystack")
        assert results
        assert len(results[0]["excerpt"]) <= 320


class TestSearchTool:
    async def test_the_agent_can_search_its_own_history(self, populated):
        store, project, _first, second = populated
        registry = default_registry()
        ctx = ToolContext(
            root=Path(store.path).parent, session_id=second.id, project_id=project.id, store=store
        )
        result = await registry.dispatch("search_history", ctx, {"query": "4C-120"})
        assert result.ok, result.error
        assert result.data["results"], "the tool found nothing"
        assert "4C-120" in result.display

    async def test_searching_from_the_same_session_includes_it_and_says_so(self, populated):
        """The agent must be able to recover its own record — and know it is its own.

        This used to exclude the current session, on the grounds that the agent
        would be handed its own last utterance as "memory". The concern is real but
        the cure was worse: it removed the only way to find what the agent itself
        had already done, in exactly the situation that needs it — a long session
        whose earlier turns have aged out of the replayed context. An agent that
        cannot find its own work re-derives it, or reports that somebody else must
        have done it. So the current session is searched, and every hit is labelled
        with where it came from so its own words cannot pass as outside memory.
        """
        store, project, first, _second = populated
        registry = default_registry()
        ctx = ToolContext(
            root=Path(store.path).parent, session_id=first.id, project_id=project.id, store=store
        )
        result = await registry.dispatch("search_history", ctx, {"query": "4C-120"})
        assert result.ok
        assert any(item["session_id"] == first.id for item in result.data["results"])
        assert all(
            item["from"] == "this conversation"
            for item in result.data["results"]
            if item["session_id"] == first.id
        )

    async def test_another_sessions_hits_are_labelled_as_such(self, populated):
        """So a fact from a previous session is not mistaken for this one's."""
        store, project, _first, second = populated
        ctx = ToolContext(
            root=Path(store.path).parent, session_id=second.id, project_id=project.id, store=store
        )
        result = await default_registry().dispatch("search_history", ctx, {"query": "4C-120"})
        assert result.ok
        assert result.data["results"], "the other session's finding must still be reachable"
        assert all(item["from"] == "an earlier conversation" for item in result.data["results"])

    async def test_a_miss_is_reported_plainly(self, populated):
        store, project, _first, second = populated
        ctx = ToolContext(
            root=Path(store.path).parent, session_id=second.id, project_id=project.id, store=store
        )
        result = await default_registry().dispatch("search_history", ctx, {"query": "zebra"})
        assert result.ok
        assert result.data["results"] == []
        assert "nothing" in result.display.lower()

    async def test_an_empty_query_is_refused(self, populated):
        store, project, _first, second = populated
        ctx = ToolContext(
            root=Path(store.path).parent, session_id=second.id, project_id=project.id, store=store
        )
        result = await default_registry().dispatch("search_history", ctx, {"query": "  "})
        assert not result.ok

    async def test_it_needs_no_approval(self):
        """Reading one's own notes is not a mutating action."""
        assert default_registry().requires_approval("search_history", {"query": "x"}) is False

    async def test_without_a_store_it_fails_clearly(self, tmp_path):
        ctx = ToolContext(root=tmp_path)
        result = await default_registry().dispatch("search_history", ctx, {"query": "x"})
        assert not result.ok
        assert "not available" in result.error
