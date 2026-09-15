"""Learnings in the project's Markdown must survive a growing conversation.

The failure this guards against is quiet and cumulative. The conversation is
trimmed as it grows — only the last `_HISTORY_LIMIT` messages are replayed — so
anything the agent worked out and kept in the conversation is eventually gone. A
later session starts from the project's files alone.

That makes two things load-bearing:

1. the project's documentation is re-injected on **every** turn, from disk, so it
   never decays with the conversation; and
2. the agent is told, as a standing rule, to write what it learns back into those
   files rather than only saying it.

Both are asserted here, because a regression in either is invisible until a
session months later starts ignorant.
"""

from __future__ import annotations

import pytest

from surtitle.config import Settings
from surtitle.core.session import Session
from surtitle.store.db import Store

DOC = """# Project conventions

- Machines are reached with `plant-mcp-cli -m <SITE> call`, never by ssh.
- Tokens live in `plant.tokens` at the project root.
"""


@pytest.fixture
def session(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("P", tmp_path)
    record = store.create_session(project.id)

    async def send(_payload):
        return None

    async def send_audio(_data):
        return None

    return Session(
        session_id=record.id,
        project_id=project.id,
        root=tmp_path,
        settings=Settings(
            DEEPSEEK_API_KEY="sk-test",
            SURTITLE_HOME=str(tmp_path / "home"),
            voice_enabled=False,
        ),
        store=store,
        deepseek=None,
        send=send,
        send_audio=send_audio,
    )


class TestInstructionsAreReinjected:
    def test_the_project_instructions_reach_the_prompt(self, session, tmp_path):
        (tmp_path / "AGENTS.md").write_text(DOC, encoding="utf-8")

        prompt = session._system_prompt()

        assert "plant-mcp-cli" in prompt
        assert "Tokens live in" in prompt

    def test_they_survive_a_conversation_far_longer_than_the_history_window(
        self, session, tmp_path
    ):
        """The conversation is trimmed; the project's files are not."""
        (tmp_path / "AGENTS.md").write_text(DOC, encoding="utf-8")
        # Far more messages than `_HISTORY_LIMIT`, so nothing of the original
        # conversation would still be replayed.
        for index in range(200):
            session.store.add_message(session.session_id, "user", f"question {index}")
            session.store.add_message(session.session_id, "assistant", f"answer {index}")

        history = session._build_history()
        prompt = session._system_prompt()

        assert len(history) <= 40, "the history window should have trimmed the conversation"
        assert "plant-mcp-cli" in prompt, (
            "project learnings must come from the files, not from the conversation"
        )

    def test_a_project_file_added_mid_session_is_picked_up(self, session, tmp_path):
        """Re-read per turn, so a file the agent writes counts immediately."""
        assert "plant-mcp-cli" not in session._system_prompt()

        (tmp_path / "AGENTS.md").write_text(DOC, encoding="utf-8")

        assert "plant-mcp-cli" in session._system_prompt()

    def test_the_notebook_also_reaches_the_prompt(self, session, tmp_path):
        """Recorded notes accumulate across sessions in the same way."""
        session.store.add_message(session.session_id, "user", "seed")
        from surtitle.tools.environment import write_notes

        write_notes(tmp_path, "- 4C-120 is on the same bus as 4C-117")

        assert "same bus" in session._system_prompt()


class TestThePrimaryFileIsNeverLost:
    """It carries the project's conventions, so its absence is the worst case."""

    def test_an_oversized_agents_md_is_shown_in_part_not_dropped(self, session, tmp_path):
        (tmp_path / "AGENTS.md").write_text("x" * 60_000, encoding="utf-8")

        prompt = session._system_prompt()

        assert "AGENTS.md" in prompt, "the primary file must always be present"
        assert "truncated; read the file for the rest" in prompt

    def test_a_truncated_file_is_declared_as_partial(self, session, tmp_path):
        """A fragment must never be mistaken for the whole document."""
        (tmp_path / "AGENTS.md").write_text("y" * 60_000, encoding="utf-8")

        prompt = session._system_prompt()

        assert "Instructions shown in part" in prompt
        assert "Read the file itself" in prompt

    def test_a_secondary_file_too_large_is_named_rather_than_shredded(self, session, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# tiny\n", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("z" * 80_000, encoding="utf-8")

        prompt = session._system_prompt()

        assert "Instructions shown in part" not in prompt
        assert "CLAUDE.md" in prompt, "it must still be named so it can be read"
        assert "z" * 1000 not in prompt, "it must not be shredded into the prompt"

    def test_a_normal_project_is_unaffected(self, session, tmp_path):
        (tmp_path / "AGENTS.md").write_text(DOC, encoding="utf-8")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "ACCESS_METHOD.md").write_text("# Access\n", encoding="utf-8")

        prompt = session._system_prompt()

        assert "truncated" not in prompt
        assert "Instructions shown in part" not in prompt


@pytest.fixture(scope="module")
def prompt() -> str:
    """The system prompt with line wrapping collapsed, so phrases match."""
    from surtitle.core.agent import build_system_prompt

    return " ".join(build_system_prompt("project").split())


class TestTheAgentIsToldToWriteLearningsDown:
    """The prompt must make this a standing obligation, not a suggestion."""

    def test_it_says_the_conversation_is_not_durable(self, prompt):
        assert "loaded before you run a single tool" in prompt
        assert "a later session never sees it at all" in prompt

    def test_it_names_the_files_to_update(self, prompt):
        assert "AGENTS.md" in prompt
        assert "docs/CURRENT_STATE.md" in prompt

    def test_it_says_to_edit_rather_than_rewrite(self, prompt):
        assert "edit_file" in prompt
        assert "do not rewrite the file" in prompt

    def test_it_separates_the_notebook_from_the_project_docs(self, prompt):
        assert ".surtitle/notes.md" in prompt
        assert "not part of the project" in prompt

    def test_it_forbids_recording_secret_values(self, prompt):
        assert "name the location, never the value" in prompt

    def test_it_says_not_to_pad(self, prompt):
        assert "If nothing durable was learned, add nothing" in prompt
