"""The eval harness has to be trustworthy before it can measure anything.

These run offline: they check that every shipped task is well-formed, that scoring
does what its documentation says, that a missing project is a skip rather than a
free pass, and that the CLI can list the tasks without credentials. A harness that
silently scores nothing — or scores the wrong thing — is worse than no harness,
because it turns a guess into a green tick.
"""

from __future__ import annotations

import json

import pytest
from evals import __main__ as eval_cli
from evals import harness


def outcome(**overrides) -> harness.Outcome:
    base = harness.Outcome(task_id="t", root="/tmp")
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class TestTaskFiles:
    def test_every_shipped_task_is_valid(self):
        tasks = harness.load_tasks()
        assert tasks, "an empty task set measures nothing"
        ids = [task["id"] for task in tasks]
        assert len(ids) == len(set(ids)), f"duplicate task ids: {ids}"

    def test_every_task_has_an_origin(self):
        """A task with no provenance cannot be re-checked when the answer moves."""
        for task in harness.load_tasks():
            assert task.get("origin"), f"{task['id']} does not say where it came from"

    def test_a_committed_example_never_points_outside_this_repository(self):
        """The committed tasks are the ones anybody can run, so they are the ones
        that must not name a checkout on somebody's machine — or the client work
        that lives in it. Real tasks belong in the gitignored ``evals/tasks/``."""
        package = harness.PACKAGE_DIR.resolve()
        examples = sorted(harness.EXAMPLES_DIR.glob("*.json"))
        assert examples, "the committed examples are what a fresh checkout runs"
        for path in examples:
            task = json.loads(path.read_text(encoding="utf-8"))
            root = harness.task_root(task).resolve()
            assert package in root.parents, (
                f"{path.name} points outside the harness ({root}); a committed task "
                "must not name a project on this machine"
            )

    def test_a_selector_filters_and_an_unknown_one_is_an_error(self):
        assert harness.load_tasks("example")
        assert len(harness.load_tasks("example")) < len(harness.load_tasks())
        with pytest.raises(ValueError):
            harness.load_tasks("no-such-task")

    def test_an_unknown_check_is_rejected_rather_than_ignored(self, tmp_path, monkeypatch):
        (tmp_path / "bad.json").write_text(
            json.dumps(
                {
                    "id": "bad",
                    "root": "/tmp",
                    "prompt": "p",
                    "checks": [{"kind": "sounds_about_right"}],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(harness, "TASKS_DIR", tmp_path)
        with pytest.raises(ValueError, match="unknown check"):
            harness.load_tasks()

    def test_a_missing_field_is_rejected(self, tmp_path, monkeypatch):
        (tmp_path / "short.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")
        monkeypatch.setattr(harness, "TASKS_DIR", tmp_path)
        with pytest.raises(ValueError, match="missing"):
            harness.load_tasks()


class TestScoring:
    def check(self, spec, result):
        return harness._check(spec, result)

    def test_answered(self):
        assert self.check("answered", outcome(answer="Something.")).ok
        assert not self.check("answered", outcome(answer="  ")).ok

    def test_ended_complete_is_not_satisfied_by_another_ending(self):
        assert self.check({"kind": "ended_complete"}, outcome(reason="complete")).ok
        for reason in ("step_limit", "no_answer", "failed", "cancelled", ""):
            assert not self.check({"kind": "ended_complete"}, outcome(reason=reason)).ok

    def test_tool_called_narrows_on_the_arguments(self):
        calls = [
            {"name": "read_file", "arguments": {"path": "machine/Grab/Conveyor.lpc"}, "ok": True}
        ]
        assert self.check(
            {"kind": "tool_called", "name": "read_file"}, outcome(tool_calls=calls)
        ).ok
        assert self.check(
            {"kind": "tool_called", "name": "read_file", "contains": "Conveyor.lpc"},
            outcome(tool_calls=calls),
        ).ok
        assert not self.check(
            {"kind": "tool_called", "name": "read_file", "contains": "BaleGate.lpc"},
            outcome(tool_calls=calls),
        ).ok
        assert not self.check(
            {"kind": "tool_called", "name": "run_shell"}, outcome(tool_calls=calls)
        ).ok

    def test_the_bare_string_form_covers_the_checks_that_take_no_arguments(self):
        assert self.check("ended_complete", outcome(reason="complete")).ok
        assert self.check("answered", outcome(answer="a")).ok
        assert self.check("no_failed_tools", outcome(tool_calls=[])).ok

    def test_tool_not_called(self):
        calls = [{"name": "write_file", "arguments": {}, "ok": True}]
        assert not self.check(
            {"kind": "tool_not_called", "name": "write_file"}, outcome(tool_calls=calls)
        ).ok
        assert self.check(
            {"kind": "tool_not_called", "name": "edit_file"}, outcome(tool_calls=calls)
        ).ok

    def test_writes_within_allows_a_project_its_own_records_and_nothing_else(self):
        """A project may ask for a session note; writing one is its convention."""
        record = [
            {
                "name": "write_file",
                "arguments": {"path": "docs/state/sessions/069-x.md"},
                "ok": True,
            }
        ]
        assert self.check(
            {"kind": "writes_within", "paths": ["docs/state/"]}, outcome(tool_calls=record)
        ).ok
        assert not self.check("writes_within", outcome(tool_calls=record)).ok, (
            "with no paths allowed, any write is an offender"
        )

    def test_writes_within_catches_a_write_to_the_wrong_file(self):
        calls = [
            {"name": "write_file", "arguments": {"path": "docs/CURRENT_STATE.md"}, "ok": True},
            {"name": "edit_file", "arguments": {"path": "machine/Grab/Conveyor.lpc"}, "ok": True},
            {"name": "read_file", "arguments": {"path": "machine/Grab/Conveyor.lpc"}, "ok": True},
        ]
        result = self.check(
            {"kind": "writes_within", "paths": ["docs/state/"]}, outcome(tool_calls=calls)
        )
        assert not result.ok
        assert "docs/current_state.md" in result.detail
        assert "conveyor.lpc" in result.detail, "the edit is an offender too"
        assert result.detail.count("(") == 2, "a read is not a write"

    def test_answer_contains_is_case_insensitive_and_handles_all_three_forms(self):
        result = outcome(answer="The retry limit is set in config/service.ini.")
        assert self.check({"kind": "answer_contains", "all_of": ["CONFIG/SERVICE.INI"]}, result).ok
        assert self.check({"kind": "answer_contains", "any_of": ["nope", "retry"]}, result).ok
        assert not self.check({"kind": "answer_contains", "any_of": ["nope", "neither"]}, result).ok
        assert not self.check({"kind": "answer_contains", "all_of": ["batch_size"]}, result).ok
        assert self.check({"kind": "answer_contains", "none_of": ["export"]}, result).ok
        assert not self.check({"kind": "answer_contains", "none_of": ["retry"]}, result).ok

    def test_the_answer_is_read_without_the_work_log(self):
        """A tool result quoting the answer must not satisfy a check about it."""
        result = outcome(
            answer=harness.strip_work_log(
                "I could not find it.\n\n[work this turn]\n- read_file(x) -> max_attempts = 5"
            )
        )
        assert not self.check({"kind": "answer_contains", "all_of": ["max_attempts"]}, result).ok

    def test_plan_written_needs_a_real_plan(self):
        empty = [{"name": "todo_write", "arguments": {"todos": []}, "ok": True}]
        written = [{"name": "todo_write", "arguments": {"todos": [{"content": "x"}]}, "ok": True}]
        assert not self.check("plan_written", outcome(tool_calls=empty)).ok
        assert self.check("plan_written", outcome(tool_calls=written)).ok

    def test_max_steps(self):
        assert self.check({"kind": "max_steps", "value": 10}, outcome(steps=10)).ok
        assert not self.check({"kind": "max_steps", "value": 10}, outcome(steps=11)).ok
        assert not self.check({"kind": "max_steps", "value": 10}, outcome(steps=0)).ok

    def test_no_failed_tools_honours_its_allowance(self):
        calls = [{"name": "run_shell", "arguments": {}, "ok": False}]
        assert not self.check("no_failed_tools", outcome(tool_calls=calls)).ok
        assert self.check({"kind": "no_failed_tools", "allow": 1}, outcome(tool_calls=calls)).ok


class TestSummarise:
    def test_a_pass_rate_counts_whole_tasks_not_checks(self):
        task = {"id": "t", "root": "/tmp", "prompt": "p", "checks": ["answered"]}
        good = harness.CheckResult("answered", True, "ok")
        bad = harness.CheckResult("ended_complete", False, "ended: step_limit")
        results = [
            (task, outcome(answer="a", steps=2, prompt_tokens=10), [good]),
            (task, outcome(answer="b", steps=3, prompt_tokens=20), [good, bad]),
        ]
        summary = harness.summarise(results)
        assert summary["tasks"] == 2
        assert summary["passed"] == 1
        assert summary["pass_rate"] == 0.5
        assert summary["steps"] == 5
        assert summary["prompt_tokens"] == 30

    def test_a_skipped_task_is_not_counted_as_a_pass(self):
        task = {"id": "t", "root": "/tmp", "prompt": "p", "checks": ["answered"]}
        summary = harness.summarise([(task, outcome(skipped="project not present"), [])])
        assert summary == {
            "tasks": 0,
            "passed": 0,
            "pass_rate": None,
            "skipped": 1,
            "steps": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "seconds": 0.0,
        }

    def test_a_saved_run_is_json_serialisable(self, tmp_path, monkeypatch):
        """A run is only useful if it can be read back and compared."""
        monkeypatch.setattr(harness, "RUNS_DIR", tmp_path)
        task = {"id": "t", "root": "/tmp", "prompt": "p", "checks": ["answered"]}
        results = [
            (task, outcome(answer="a", steps=1), [harness.CheckResult("answered", True, "a")])
        ]
        path = harness.write_run(
            {
                "settings": {"model": "deepseek-flash"},
                "summary": harness.summarise(results),
                "results": [
                    {
                        "task": task["id"],
                        "outcome": result.to_dict(),
                        "checks": [check.to_dict() for check in checks],
                    }
                    for _task, result, checks in results
                ],
            },
            label="smoke",
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["summary"]["passed"] == 1
        assert payload["results"][0]["checks"][0]["kind"] == "answered"
        assert path.name.endswith("-smoke.json")


class TestMissingProject:
    async def test_a_missing_root_is_skipped_before_any_model_call(self, tmp_path):
        from surtitle.config import Settings

        task = {
            "id": "gone",
            "root": str(tmp_path / "not-here"),
            "prompt": "p",
            "checks": ["answered"],
        }
        result = await harness.run_task(task, Settings(DEEPSEEK_API_KEY="unused"))
        assert result.skipped, "a task whose project is absent must not be scored"
        assert not result.tool_calls


class TestCli:
    def test_listing_the_tasks_needs_no_credentials(self, capsys):
        assert eval_cli.main(["--list"]) == 0
        printed = capsys.readouterr().out
        assert "example-retry-limit" in printed

    def test_a_bad_selector_exits_before_doing_anything(self, capsys):
        assert eval_cli.main(["no-such-task"]) == 2
        assert "no task matches" in capsys.readouterr().err
