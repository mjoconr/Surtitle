"""Run the eval tasks and report what happened.

    uv run python -m evals --list
    uv run python -m evals
    uv run python -m evals example --repeat 3 --label effort-high --effort high

Exit status is 1 when a task fails a check, so a run can gate a change.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from evals import harness
from surtitle.config import get_settings
from surtitle.store.settings_store import SettingsStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Score the agent on tasks taken from real sessions.",
    )
    parser.add_argument("selector", nargs="?", help="only tasks whose id contains this")
    parser.add_argument("--list", action="store_true", help="show the tasks and exit")
    parser.add_argument("--repeat", type=int, default=1, help="run each task this many times")
    parser.add_argument("--model", help="override the model for this run")
    parser.add_argument(
        "--effort",
        choices=["minimal", "low", "medium", "high"],
        help="override the reasoning effort for this run",
    )
    parser.add_argument("--max-tokens", type=int, help="override the per-round output cap")
    parser.add_argument("--max-steps", type=int, help="override the step budget per turn")
    parser.add_argument("--label", default="", help="suffix for the saved run file")
    parser.add_argument("--no-write", action="store_true", help="do not save this run")
    parser.add_argument("--quiet", action="store_true", help="only the summary")
    return parser


def _settings(args: argparse.Namespace) -> Any:
    """The settings the app would use, with this run's overrides on top.

    Read through the settings store rather than the environment alone, so a key
    saved in the app's own Settings screen is visible here too.
    """
    settings = SettingsStore(get_settings()).effective()
    overrides: dict[str, Any] = {}
    if args.model:
        overrides["deepseek_model"] = args.model
    if args.effort:
        overrides["reasoning_effort"] = args.effort
    if args.max_tokens:
        overrides["max_tokens"] = args.max_tokens
    if args.max_steps:
        overrides["max_steps"] = args.max_steps
    return settings.model_copy(update=overrides) if overrides else settings


def _line(task: dict[str, Any], outcome: harness.Outcome, checks: list[harness.CheckResult]) -> str:
    if outcome.skipped:
        return f"SKIP  {task['id']:<34} {outcome.skipped}"
    verdict = "PASS" if all(check.ok for check in checks) else "FAIL"
    return (
        f"{verdict}  {task['id']:<34} {outcome.steps:>3} step(s)  "
        f"{outcome.prompt_tokens + outcome.completion_tokens:>7} tok  "
        f"{outcome.duration_s:>6.1f}s  {outcome.reason}"
    )


async def _run(args: argparse.Namespace, tasks: list[dict[str, Any]]) -> int:
    settings = _settings(args)
    missing = [name for name in settings.missing_credentials() if name == "DEEPSEEK_API_KEY"]
    if missing:
        print(
            "This measures a real model, so it needs DEEPSEEK_API_KEY — in the "
            "environment or saved in the app's Settings.",
            file=sys.stderr,
        )
        return 2

    results: list[tuple[dict[str, Any], harness.Outcome, list[harness.CheckResult]]] = []

    def report(task, outcome, checks) -> None:
        if args.quiet:
            return
        print(_line(task, outcome, checks), flush=True)
        for check in checks:
            if not check.ok:
                print(f"        - {check.kind}: {check.detail}")
        if outcome.error:
            print(f"        ! {outcome.error}")

    started = time.time()
    results = await harness.run_all(
        tasks, settings, repeat=args.repeat, max_steps=args.max_steps, on_result=report
    )
    summary = harness.summarise(results)

    print(
        f"\n{summary['passed']}/{summary['tasks']} passed"
        + (f" ({summary['skipped']} skipped)" if summary["skipped"] else "")
        + f"  steps={summary['steps']}  tokens={summary['prompt_tokens']}"
        f"/{summary['completion_tokens']}  {summary['seconds']}s"
    )
    if not args.no_write:
        path = harness.write_run(
            {
                "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "settings": {
                    "model": settings.deepseek_model,
                    "reasoning_effort": settings.reasoning_effort,
                    "max_tokens": settings.max_tokens,
                    "max_steps": settings.max_steps,
                },
                "summary": summary,
                "results": [
                    {
                        "task": task["id"],
                        "outcome": outcome.to_dict(),
                        "checks": [check.to_dict() for check in checks],
                    }
                    for task, outcome, checks in results
                ],
            },
            label=args.label,
        )
        print(f"run written to {path}")
    _ = started
    return 0 if summary["tasks"] and summary["passed"] == summary["tasks"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        tasks = harness.load_tasks(args.selector)
    except ValueError as exc:
        print(f"task problem: {exc}", file=sys.stderr)
        return 2

    if args.list:
        for task in tasks:
            checks = ", ".join(
                check if isinstance(check, str) else check["kind"] for check in task["checks"]
            )
            print(f"{task['id']:<34} {task_root_label(task):<28} [{checks}]")
            prompts = harness.task_prompts(task)
            opening = (
                prompts[0] if len(prompts) == 1 else f"{len(prompts)} turns, starting: {prompts[0]}"
            )
            print(f"    {harness._excerpt(opening, 150)}")
        print(f"\n{len(tasks)} task(s)")
        return 0

    return asyncio.run(_run(args, tasks))


def task_root_label(task: dict[str, Any]) -> str:
    root = harness.task_root(task)
    return root.name + ("" if root.is_dir() else " (missing)")


if __name__ == "__main__":
    raise SystemExit(main())
