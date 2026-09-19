# Evals

Measures the agent on tasks taken from work that really happened, so a change to
the prompt, the model settings or the tools can be judged instead of guessed at.

```bash
uv run python -m evals --list          # what is in here
uv run python -m evals                 # every task, once
uv run python -m evals example         # only ids containing "example"
uv run python -m evals --repeat 3 --label effort-high --effort high
```

A run needs a real `DEEPSEEK_API_KEY` — it measures a model, so it cannot be
offline. The harness *itself* is covered offline by `tests/test_eval_harness.py`.

## Where tasks live

- `evals/examples/tasks/` — committed, and run against the sample project at
  `evals/examples/sample-project/`. Nothing here is anybody's real work; it is
  what a fresh checkout can run.
- `evals/tasks/` — **yours, and gitignored.** A task names a real project, so the
  question, the expected answer and the checkout it lives in are all project data.
  Keep client work — machine names, serials, internal paths, knowledge-base
  references — out of this repository; put the task here instead.

A task of yours sharing an id with an example replaces it. `root` may be absolute,
may contain `$VARS`, or may be relative to `evals/`.

## What a run does

Each task is one fresh conversation against the project the task names, driven
through `Session._run_turn` — the code path the application uses. So the system
prompt, the project instructions, the plan replay, the closing-round rules and the
turn-end record are all the real ones, and a change to any of them shows up here.
Built-in tools are trusted up front because nothing can answer an approval prompt
mid-run; a missing project root is reported as **skipped**, never as a pass.

Each result is saved to `evals/runs/`, which is gitignored. Keep the run file from
before a change and compare: pass rate, steps, tokens, wall time.

## Checks

The checks are deliberately few and objective. Anything that needs a judgement
about the quality of the prose belongs in a person reading it, not in a score that
gates a change.

| Check | Passes when |
|---|---|
| `answered` | the turn stored a non-empty answer |
| `ended_complete` | the turn ended `complete`, not `step_limit`/`no_answer`/`failed` |
| `tool_called` | a tool was called — `{"name": "read_file", "contains": "X"}` narrows it to the arguments |
| `tool_not_called` | a tool was never called (with `contains`, never called *with* those arguments) |
| `writes_within` | every `write_file`/`edit_file` stayed under one of `paths`; omit `paths` to forbid writes entirely |
| `answer_contains` | `all_of` every string, `any_of` at least one, `none_of` none (case-insensitive) |
| `plan_written` | `todo_write` recorded a plan |
| `max_steps` | the turn used no more than `value` steps |
| `no_failed_tools` | at most `allow` tool calls failed |

`answer_contains` reads the answer only, not the `[work this turn]` log under it: a
tool result that happens to contain a string must not be able to satisfy a check
about what the agent concluded.

## Adding a task

One JSON file in `evals/tasks/` (yours, gitignored) — or in
`evals/examples/tasks/` if it must be runnable by anyone. A task is only worth
adding if its answer is *verifiable* — a name, a number, a decision recorded in
the checkout — and if a plausible wrong answer exists, so the task can fail.

```json
{
  "id": "short-kebab-case",
  "root": "/path/to/project",
  "prompt": "What the user would actually type or say.",
  "origin": "Where this came from and how the answer was established.",
  "checks": [
    "answered",
    {"kind": "ended_complete"},
    {"kind": "answer_contains", "all_of": ["the thing that must be named"]},
    {"kind": "max_steps", "value": 40}
  ]
}
```

Good sources for new tasks, in rough order of value:

- A transcript of the work being done by hand: any agent or shell history where a
  question was answered against a checkout. The human turns are the prompts; the
  tool calls and replies that follow show what a competent run looked like and what
  the answer contained.
- The project's own state records — an open-items file, a decision log. Each entry
  is a question with a recorded answer, which is exactly what a check needs.
- This application's own session database: every time Surtitle stopped short or had
  to be told "continue" was a real failing worth preventing.

Prefer tasks that pin a name or a decision. "Summarise this file" is not a task; it
is a spelling test for the prompt.

Keep the wording generic in anything committed here. If a task can only be written
by naming a client's machine, path or knowledge base, it belongs in `evals/tasks/`,
which is not committed.
