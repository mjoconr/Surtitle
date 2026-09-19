"""A delegated agent: the part of the work that is only reading.

The parent splits off an investigation, the child runs it with no conversation
history and no ability to change anything, and the parent gets back what it found.
Two reasons that is worth a second agent rather than more turns of the first:

* **Context.** The reading that answers a question is usually several times larger
  than the answer. In the parent's transcript it would sit there for the rest of the
  conversation, pushing out the conversation itself; in a child it is discarded the
  moment the answer is handed over.
* **Breadth.** Several children run in one parent round, so a question with four
  independent parts costs one round rather than four.

The child is deliberately powerless. It has the tools that look and nothing that
writes, runs or installs — every one of those would otherwise be editing the
project while the user hears only the parent — and it cannot delegate again. What
it produces is an answer and a list of what it read; the parent decides what to do
with that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SUBAGENT_PROMPT", "SubagentOutcome", "build_subagent_prompt"]


SUBAGENT_PROMPT = """You are a sub-agent of Surtitle, working inside the project
directory "{root_name}". Another agent is working on the user's request and has
handed you one part of it.

You have **no conversation history**: the task below is everything you know, and
nothing you say reaches the user. You are not being read; you are being used.
What you return is what the parent agent gets to work with.

## What you can do

Read, list and search files in this project. You cannot write, edit, run commands,
install anything or ask a question — those tools are not available to you, and a
request for one is a task that has to go back to your parent. Do not describe what
you would do with them.

## How to answer

- **Answer the question that was asked**, not the one nearby. If it cannot be
  settled from the files, say what is missing rather than guessing.
- **Name your evidence.** "`config/service.ini` sets `max_attempts = 5`" is worth
  ten times "the retry limit is five". The parent will cite you, and it cannot
  check what you did not name.
- **Be brief.** No preamble, no restatement of the task, no offer to continue.
  Three sentences and the file names will usually do; a list where the answer
  really is a list.
- **Say what you did not establish.** If part of the task was unanswerable, one
  line saying so is worth more than silence: the parent is deciding what to do
  next on the strength of your answer.
"""


def build_subagent_prompt(root_name: str) -> str:
    """The child's system prompt. Named for the function, like the parent's."""
    return SUBAGENT_PROMPT.format(root_name=root_name)


@dataclass(slots=True)
class SubagentOutcome:
    """What a child hands back to its parent."""

    answer: str = ""
    steps: int = 0
    # Why the child stopped: `complete` when it answered, `step_limit` when it ran
    # out. The parent is told, because "I could not finish" is a different fact
    # from "there is nothing there".
    reason: str = ""
    # What it actually opened, so the parent can go straight to the source rather
    # than re-searching for it.
    files: list[str] = field(default_factory=list)

    def as_data(self) -> dict[str, object]:
        return {
            "answer": self.answer,
            "steps": self.steps,
            "stopped": self.reason,
            "read": self.files,
        }
