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
from surtitle.store.db import REASONING_ROLE, Store

DOC = """# Project conventions

- Machines are reached with `fleet-cli -m <SITE> call`, never by ssh.
- Tokens live in `access.tokens` at the project root.
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

        assert "fleet-cli" in prompt
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
        assert "fleet-cli" in prompt, (
            "project learnings must come from the files, not from the conversation"
        )

    def test_a_project_file_added_mid_session_is_picked_up(self, session, tmp_path):
        """Re-read per turn, so a file the agent writes counts immediately."""
        assert "fleet-cli" not in session._system_prompt()

        (tmp_path / "AGENTS.md").write_text(DOC, encoding="utf-8")

        assert "fleet-cli" in session._system_prompt()

    def test_the_notebook_also_reaches_the_prompt(self, session, tmp_path):
        """Recorded notes accumulate across sessions in the same way."""
        session.store.add_message(session.session_id, "user", "seed")
        from surtitle.tools.environment import write_notes

        write_notes(tmp_path, "- node-120 is on the same bus as node-117")

        # Delivered with the turn's changing context rather than in the system
        # prompt, so the head of the request stays cacheable. It still reaches the
        # agent on every turn, which is what this asserts.
        assert "same bus" in session._context_note()


class TestTheWindowKeepsTheEndOfTheConversation:
    """The replay window is the *newest* messages, and it counts the conversation.

    Both halves of that sentence were wrong, and the failure they produced did not
    look like a bug from outside. The window was taken from the *beginning*
    (`ORDER BY id ASC LIMIT 40`), and reasoning is stored as one row per step, so
    a session filled its 40 rows within the first turn or two and the model's
    context then froze at the opening exchange. Everything the agent had since
    done — including work it had built and committed itself — was absent from
    every later turn, which is why it re-proposed that work and then reported that
    "somebody" had already done it.
    """

    def test_the_newest_exchange_survives_and_the_opening_ages_out(self, session):
        for index in range(200):
            session.store.add_message(session.session_id, "user", f"question {index}")
            session.store.add_message(session.session_id, "assistant", f"answer {index}")

        history = session._build_history()

        assert len(history) == 40, "the window should hold exactly the limit"
        assert history[-1]["content"] == "answer 199", (
            "the most recent reply is what the next turn must be able to read"
        )
        assert history[0]["content"] == "question 180", "the oldest forty are the ones kept"
        assert all("question 0" not in item["content"] for item in history), (
            "the opening of a long conversation is the part that should age out"
        )

    def test_reasoning_does_not_consume_the_window(self, session):
        """It is one row per step; counting it evicts the conversation itself."""
        session.store.add_message(session.session_id, "user", "the question")
        session.store.add_message(session.session_id, "assistant", "the answer")
        for step in range(200):
            session.store.add_message(session.session_id, REASONING_ROLE, f"thinking {step}")

        history = session._build_history()

        assert history == [
            {"role": "user", "content": "the question"},
            {"role": "assistant", "content": "the answer"},
        ]

    def test_a_cancelled_exchange_is_not_replayed_twice(self, session):
        """`_rolled_back` still marks a turn whose user message is already stored."""
        session.store.add_message(session.session_id, "user", "the question")
        session.store.add_message(session.session_id, "assistant", "")
        session._rolled_back = True

        history = session._build_history()

        assert history == [{"role": "user", "content": "the question"}], (
            "an empty interrupted reply must not reach the model, and the stored "
            "user message must be kept for the loop to append afresh"
        )


class TestThePlanIsNotLostWithTheConversation:
    """The plan is on the user's screen, so the agent must be able to read it.

    `todo_write` only ever wrote. Nothing replayed the plan, so an agent whose
    context had moved on could not answer "which item is still unticked?" — and in
    a real session, asked exactly that, it could not.
    """

    def test_the_current_plan_reaches_the_prompt(self, session):
        session.store.set_todos(
            session.session_id,
            [
                {"content": "Write the renderer", "status": "completed"},
                {"content": "Report and ask about committing", "status": "in_progress"},
            ],
        )

        note = session._context_note()

        assert "Report and ask about committing" in note, (
            "the agent cannot answer a question about an item it cannot see"
        )
        assert "Write the renderer" in note
        assert "[x]" in note and "[>]" in note, "the state of each item must be legible"
        assert "1/2" in note, "how far along the plan is must be legible"

    def test_an_unfinished_item_is_called_out(self, session):
        session.store.set_todos(
            session.session_id,
            [{"content": "Report and ask about committing", "status": "pending"}],
        )

        note = session._context_note()

        assert "Report and ask about committing" in note
        assert "Not finished on that list" in note, (
            "an item left unticked is a standing claim that work is outstanding"
        )

    def test_an_empty_plan_adds_nothing(self, session):
        assert "Your plan, as the user is looking at it" not in session._context_note()


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


class TestAnAgedWorkLog:
    """A turn's work log is the model's memory of its own work, and it grows forever.

    Measured on a real conversation, the work logs were 62% of the replayed
    history. Ageing them has one constraint that shapes the whole design: the
    provider matches whole cache prefixes, so a shortened history that changed
    shape between requests would rewrite the middle of the request and make
    everything after it a cache miss — fifty times the cost of a hit. It is
    therefore written once, into a separate model-facing column, and the user's own
    transcript keeps the full text.
    """

    @staticmethod
    def _turn(index: int, *, lines: int = 20) -> str:
        listing = "\n".join(f"- run_shell(cmd {index}-{n}) -> " + "x" * 300 for n in range(lines))
        return f"Answer {index}.\n\n[work this turn]\n{listing}"

    def test_only_turns_past_the_window_are_aged(self, session):
        from surtitle.core.session import _AGED_WORK_KEEP_TURNS

        for index in range(_AGED_WORK_KEEP_TURNS + 3):
            session.store.add_message(session.session_id, "assistant", self._turn(index))

        session._age_work_logs()

        rows = session.store.list_messages(session.session_id, roles=("assistant",))
        aged = [row for row in rows if row.model_content is not None]
        assert len(aged) == 3, "the newest turns keep their log; the rest are shortened"
        assert all("Answer 0" in row.model_content for row in aged[:1])

    def test_ageing_a_turn_twice_changes_nothing(self, session):
        """Rewriting it again would invalidate the cache from that point on."""
        for index in range(8):
            session.store.add_message(session.session_id, "assistant", self._turn(index))

        session._age_work_logs()
        once = [row.model_content for row in session.store.list_messages(session.session_id)]
        session._age_work_logs()

        assert [
            row.model_content for row in session.store.list_messages(session.session_id)
        ] == once

    def test_the_aged_form_is_what_the_model_is_given(self, session):
        for index in range(8):
            session.store.add_message(session.session_id, "assistant", self._turn(index))
        session.store.add_message(session.session_id, "user", "and now?")

        session._age_work_logs()
        history = session._build_history()

        assert history, "there is a conversation to replay"
        aged = [message for message in history if "…and" in message["content"]]
        assert aged, "the model is given the shortened form"
        rows = session.store.list_messages(session.session_id, roles=("assistant",))
        assert all("…and" not in row.content for row in rows), (
            "the user's own transcript keeps every action"
        )
