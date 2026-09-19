"""An eval harness for the agent, built from work that really happened.

"Smarter" cannot be settled by reading a prompt. This runs the *shipped* code
path — a real :class:`~surtitle.core.session.Session` turn, with the real system
prompt, plan and agent loop — against a task list, and scores what came back with
checks that are objective: which tools were called, whether the turn finished,
what the answer had to contain.

Its value is comparison. Raise the reasoning effort, change the prompt, add a
tool: run the same tasks before and after and see whether the pass rate, the step
count and the token cost moved. Without that, every change is a coin flip.

Two kinds of task:

* ``evals/examples/tasks`` ships with the harness, runs against the sample project
  beside it, and contains nothing from anybody's real work. It is what a fresh
  checkout can run.
* ``evals/tasks`` is yours and is **not** committed: tasks name real projects, and
  the question, the answer and the checkout it lives in are all project data. Keep
  client work out of this repository.

Run it::

    uv run python -m evals --list
    uv run python -m evals                          # every task, once
    uv run python -m evals example --repeat 3       # by substring, three times
    uv run python -m evals --effort high --max-tokens 32768

A run needs a real ``DEEPSEEK_API_KEY`` — it measures a model, so it cannot be
offline. The harness itself is covered offline by ``tests/test_eval_harness.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from surtitle.config import Settings
from surtitle.core.agent import ApprovalBroker
from surtitle.core.session import Session
from surtitle.llm.deepseek import DeepSeekClient
from surtitle.store.db import Store
from surtitle.tools.project_config import load_project_config
from surtitle.tools.registry import ToolRegistry, default_tool_list

PACKAGE_DIR = Path(__file__).resolve().parent
# Tasks that ship with the harness, against the sample project beside them.
EXAMPLES_DIR = PACKAGE_DIR / "examples" / "tasks"
# Your tasks. Gitignored: they name real projects, and that is project data.
TASKS_DIR = PACKAGE_DIR / "tasks"
RUNS_DIR = PACKAGE_DIR / "runs"

# Marks the boundary between the answer and the stored work log. Checks read the
# answer: a tool call that happens to contain the string being looked for must not
# be able to satisfy a check about what the agent concluded.
_WORK_LOG_MARKER = "[work this turn]"

# The checks are deliberately few and objective. Anything needing judgement about
# the quality of the prose belongs in a person's reading, not in a score that
# gates a change.
CHECK_KINDS = (
    "answered",
    "ended_complete",
    "tool_called",
    "tool_not_called",
    "writes_within",
    "answer_contains",
    "any_answer_contains",
    "plan_written",
    "max_steps",
    "no_failed_tools",
)

# The tools that change a project. `writes_within` is the safety check that matters
# for an investigative task: not "did it write anything", but "did it write
# anywhere it should not". A project with its own record-keeping convention — a
# state note the project asks for, say — needs the distinction, because writing
# that note is following the convention rather than misbehaving.
_WRITE_TOOLS = ("write_file", "edit_file")


@dataclass(slots=True)
class CheckResult:
    """One check, its verdict, and why — the detail is what makes a failure useful."""

    kind: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Outcome:
    """What one run of one task produced."""

    task_id: str
    root: str
    reason: str = ""
    answer: str = ""
    # Every turn's answer, oldest first. A one-turn task has one; a conversation
    # has the lot, which is what a memory check has to look at.
    answers: list[str] = field(default_factory=list)
    steps: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    skipped: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- tasks ---------------------------------------------------------------


def load_tasks(selector: str | None = None) -> list[dict[str, Any]]:
    """The examples, then your own tasks, or those whose id contains ``selector``.

    Raises ``ValueError`` on a malformed task rather than skipping it: a task file
    that does not parse is a broken measurement, and silently dropping it would
    report a smaller, greener run. A task of yours sharing an id with an example
    replaces it.
    """
    tasks: dict[str, dict[str, Any]] = {}
    for directory in (EXAMPLES_DIR, TASKS_DIR):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"{path.name}: {exc}") from exc
            validate_task(task, source=path.name)
            tasks[task["id"]] = task
    selected = [task for task in tasks.values() if selector is None or selector in task["id"]]
    if selector is not None and not selected:
        raise ValueError(f"no task matches {selector!r}")
    return selected


def validate_task(task: Any, *, source: str = "task") -> None:
    """Check a task is one the harness can actually run and score."""
    if not isinstance(task, dict):
        raise ValueError(f"{source}: a task must be an object")
    for key in ("id", "root", "checks"):
        if not task.get(key):
            raise ValueError(f"{source}: missing {key!r}")
    prompts = task.get("turns") or ([task["prompt"]] if task.get("prompt") else [])
    if not prompts:
        raise ValueError(f"{source}: give a 'prompt', or 'turns' for a conversation")
    if not all(isinstance(prompt, str) and prompt.strip() for prompt in prompts):
        raise ValueError(f"{source}: every prompt must be a non-empty string")
    if not isinstance(task["checks"], list) or not task["checks"]:
        raise ValueError(f"{source}: 'checks' must be a non-empty list")
    for check in task["checks"]:
        kind = check if isinstance(check, str) else (check or {}).get("kind")
        if kind not in CHECK_KINDS:
            raise ValueError(f"{source}: unknown check {kind!r}; known: {', '.join(CHECK_KINDS)}")


def task_prompts(task: dict[str, Any]) -> list[str]:
    """What is said to the agent, in order — one turn, or a conversation.

    A one-turn task is a question. A multi-turn task is the only way to measure
    what the agent's *history* does for it: a fresh conversation has no history, so
    nothing about replay, ageing or the transcript window can show up in a run of
    single-turn tasks.
    """
    return list(task.get("turns") or [task["prompt"]])


def task_root(task: dict[str, Any]) -> Path:
    """The project a task runs against.

    ``$VARS`` are expanded, and a relative path is resolved against this package
    so a task can point at the sample project beside it without hardcoding where
    the checkout lives.
    """
    raw = os.path.expandvars(str(task["root"])).strip()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PACKAGE_DIR / path).resolve()


# --- running -------------------------------------------------------------


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def run_task(
    task: dict[str, Any],
    settings: Settings,
    *,
    max_steps: int | None = None,
) -> Outcome:
    """Run one task through the real session, and report what happened.

    Deliberately the shipped path: `Session._run_turn` is what the application
    calls, so the system prompt, the project instructions, the plan replay, the
    closing-round rules and the turn-end record are all the ones being measured.
    """
    root = task_root(task)
    outcome = Outcome(task_id=task["id"], root=str(root))
    if not root.is_dir():
        outcome.skipped = f"project not present: {root}"
        return outcome

    if max_steps is not None:
        settings = settings.model_copy(update={"max_steps": max_steps})

    workdir = Path(tempfile.mkdtemp(prefix=f"eval-{task['id']}-"))
    store = Store(workdir / "eval.sqlite")
    project = store.create_project(root.name, root)
    record = store.create_session(project.id)
    client = DeepSeekClient(settings)
    tools = default_tool_list()
    approvals = ApprovalBroker()
    # Nothing in the harness can answer an approval prompt, so every built-in tool
    # is trusted up front. A tool that needs approval for a *reason* — installing
    # packages — is still bounded by the registry itself.
    approvals.trust([tool.name for tool in tools])
    session = Session(
        session_id=record.id,
        project_id=project.id,
        root=root,
        settings=settings,
        store=store,
        deepseek=client,
        send=_noop,
        send_audio=_noop,
        registry=ToolRegistry(tools),
        approvals=approvals,
    )
    session.project_config = load_project_config(root)

    started = time.monotonic()
    try:
        for prompt in task_prompts(task):
            await session._run_turn(prompt)
    except Exception as exc:  # noqa: BLE001 - a broken run is a result, not a crash
        outcome.error = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
        with contextlib.suppress(Exception):
            await session.close()
    outcome.duration_s = round(time.monotonic() - started, 1)

    stored = store.get_session(record.id)
    outcome.reason = (stored.last_end_reason if stored is not None else None) or outcome.reason
    outcome.steps = (stored.last_end_steps if stored is not None else None) or 0
    outcome.prompt_tokens = session._turn_prompt_tokens
    outcome.completion_tokens = session._turn_completion_tokens

    outcome.answers = [
        strip_work_log(message.content)
        for message in store.list_messages(record.id, limit=200, roles=("assistant",))
        if strip_work_log(message.content)
    ]
    outcome.answer = outcome.answers[-1] if outcome.answers else ""
    outcome.tool_calls = [
        {"name": call.name, "arguments": call.arguments, "ok": call.ok}
        for call in store.list_tool_calls(record.id, limit=500)
    ]
    return outcome


def strip_work_log(content: str) -> str:
    """The answer the user read, without the ``[work this turn]`` log under it."""
    return content.partition(_WORK_LOG_MARKER)[0].strip()


# --- scoring -------------------------------------------------------------


def score(task: dict[str, Any], outcome: Outcome) -> list[CheckResult]:
    """Every check in the task, evaluated against one outcome."""
    return [_check(check, outcome) for check in task["checks"]]


def _check(check: Any, outcome: Outcome) -> CheckResult:
    if isinstance(check, str):
        check = {"kind": check}
    kind = check["kind"]

    if kind == "answered":
        text = outcome.answer.strip()
        return CheckResult(kind, bool(text), _excerpt(text) or "no answer")

    if kind == "ended_complete":
        ok = outcome.reason == "complete"
        return CheckResult(kind, ok, f"ended: {outcome.reason or 'nothing recorded'}")

    if kind == "tool_called":
        name = str(check.get("name") or "")
        needle = str(check.get("contains") or "").lower()
        hits = [
            call
            for call in outcome.tool_calls
            if call["name"] == name
            and (not needle or needle in json.dumps(call["arguments"], default=str).lower())
        ]
        wanted = f"{name}({check['contains']})" if check.get("contains") else name
        return CheckResult(kind, bool(hits), f"{wanted}: called {len(hits)} time(s)")

    if kind == "tool_not_called":
        name = str(check.get("name") or "")
        needle = str(check.get("contains") or "").lower()
        hits = [
            call
            for call in outcome.tool_calls
            if call["name"] == name
            and (not needle or needle in json.dumps(call["arguments"], default=str).lower())
        ]
        return CheckResult(kind, not hits, f"{name}: called {len(hits)} time(s)")

    if kind == "writes_within":
        allowed = [str(prefix).lower() for prefix in check.get("paths", [])]
        offenders: list[str] = []
        for call in outcome.tool_calls:
            if call["name"] not in _WRITE_TOOLS:
                continue
            target = str((call["arguments"] or {}).get("path") or "").lower()
            if not any(target.startswith(prefix) for prefix in allowed):
                offenders.append(f"{call['name']}({target or '?'})")
        where = ", ".join(allowed) if allowed else "nowhere"
        detail = (
            f"wrote outside {where}: {', '.join(offenders)}"
            if offenders
            else f"writes within {where}"
        )
        return CheckResult(kind, not offenders, detail)

    if kind == "answer_contains":
        ok, detail = _contains(check, outcome.answer)
        return CheckResult(kind, ok, detail)

    if kind == "any_answer_contains":
        # For a conversation: the fact may have been established in any turn.
        detail = "no answer matched"
        for text in outcome.answers or [outcome.answer]:
            ok, detail = _contains(check, text)
            if ok:
                return CheckResult(kind, True, detail)
        return CheckResult(kind, False, detail)

    if kind == "plan_written":
        for call in outcome.tool_calls:
            if call["name"] == "todo_write" and (call["arguments"] or {}).get("todos"):
                return CheckResult(kind, True, "the plan was written")
        return CheckResult(kind, False, "no plan was written")

    if kind == "max_steps":
        limit = int(check.get("value") or 0)
        return CheckResult(
            kind, 0 < outcome.steps <= limit, f"{outcome.steps} step(s), limit {limit}"
        )

    if kind == "no_failed_tools":
        failed = [call["name"] for call in outcome.tool_calls if call.get("ok") is False]
        allowed = int(check.get("allow") or 0)
        ok = len(failed) <= allowed
        detail = f"{len(failed)} failed call(s)" + (f": {', '.join(failed[:5])}" if failed else "")
        return CheckResult(kind, ok, detail)

    raise ValueError(f"unknown check {kind!r}")  # pragma: no cover - validated on load


def _contains(check: dict[str, Any], text: str) -> tuple[bool, str]:
    """Whether an answer satisfies an ``answer_contains``-style check, and why not."""
    answer = text.lower()
    missing = [needle for needle in check.get("all_of", []) if needle.lower() not in answer]
    if missing:
        return False, f"missing: {', '.join(missing)}"
    any_of = check.get("any_of", [])
    if any_of and not any(needle.lower() in answer for needle in any_of):
        return False, f"none of: {', '.join(any_of)}"
    forbidden = [needle for needle in check.get("none_of", []) if needle.lower() in answer]
    if forbidden:
        return False, f"must not say: {', '.join(forbidden)}"
    return True, _excerpt(text)


def _excerpt(text: str, limit: int = 90) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else f"{one_line[:limit]}…"


# --- the run -------------------------------------------------------------


async def run_all(
    tasks: list[dict[str, Any]],
    settings: Settings,
    *,
    repeat: int = 1,
    max_steps: int | None = None,
    on_result: Any = None,
) -> list[tuple[dict[str, Any], Outcome, list[CheckResult]]]:
    """Run every task ``repeat`` times, reporting each result as it lands."""
    results: list[tuple[dict[str, Any], Outcome, list[CheckResult]]] = []
    for task in tasks:
        for _ in range(max(1, repeat)):
            outcome = await run_task(task, settings, max_steps=max_steps)
            checks = [] if outcome.skipped else score(task, outcome)
            results.append((task, outcome, checks))
            if on_result is not None:
                on_result(task, outcome, checks)
    return results


def summarise(results: list[tuple[dict[str, Any], Outcome, list[CheckResult]]]) -> dict[str, Any]:
    """The numbers a comparison needs: pass rate, steps, tokens, wall time."""
    scored = [(task, outcome, checks) for task, outcome, checks in results if checks]
    passed = [row for row in scored if all(check.ok for check in row[2])]
    return {
        "tasks": len(scored),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(scored), 3) if scored else None,
        "skipped": len(results) - len(scored),
        "steps": sum(outcome.steps for _, outcome, _ in scored),
        "prompt_tokens": sum(outcome.prompt_tokens for _, outcome, _ in scored),
        "completion_tokens": sum(outcome.completion_tokens for _, outcome, _ in scored),
        "seconds": round(sum(outcome.duration_s for _, outcome, _ in scored), 1),
    }


def write_run(payload: dict[str, Any], *, label: str = "") -> Path:
    """Keep the run, so a change can be compared against the one before it."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label}" if label else ""
    path = RUNS_DIR / f"{stamp}{suffix}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
