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
        that lives in it. Real tasks belong in the gitignored ``evals/tasks/``.

        Inside the repository is the test, not inside ``evals/``: an example that
        surveys this project's own code is as portable as one that runs against the
        sample, and it is the only kind that can measure reading something real.
        """
        repository = harness.PACKAGE_DIR.resolve().parent
        examples = sorted(harness.EXAMPLES_DIR.glob("*.json"))
        assert examples, "the committed examples are what a fresh checkout runs"
        for path in examples:
            task = json.loads(path.read_text(encoding="utf-8"))
            root = harness.task_root(task).resolve()
            assert root == repository or repository in root.parents, (
                f"{path.name} points outside this repository ({root}); a committed "
                "task must not name a project on this machine"
            )

    def test_a_selector_filters_and_an_unknown_one_is_an_error(self, tmp_path, monkeypatch):
        """Built on a directory this test owns: the real task set is per-machine —
        `evals/tasks/` is gitignored — so a test that counted it would pass here and
        fail in CI."""
        for name, task_id in (("a.json", "wanted-one"), ("b.json", "other-one")):
            (tmp_path / name).write_text(
                json.dumps({"id": task_id, "root": "/tmp", "prompt": "p", "checks": ["answered"]}),
                encoding="utf-8",
            )
        monkeypatch.setattr(harness, "EXAMPLES_DIR", tmp_path)
        monkeypatch.setattr(harness, "TASKS_DIR", tmp_path / "absent")

        assert [task["id"] for task in harness.load_tasks("wanted")] == ["wanted-one"]
        assert len(harness.load_tasks()) == 2
        with pytest.raises(ValueError):
            harness.load_tasks("no-such-task")

    def test_a_local_task_replaces_an_example_of_the_same_id(self, tmp_path, monkeypatch):
        """Yours wins: it is the one written against the machine in front of you."""
        example = tmp_path / "examples"
        mine = tmp_path / "mine"
        example.mkdir()
        mine.mkdir()
        for directory, prompt in ((example, "the example"), (mine, "mine")):
            (directory / "t.json").write_text(
                json.dumps(
                    {"id": "same-id", "root": "/tmp", "prompt": prompt, "checks": ["answered"]}
                ),
                encoding="utf-8",
            )
        monkeypatch.setattr(harness, "EXAMPLES_DIR", example)
        monkeypatch.setattr(harness, "TASKS_DIR", mine)

        tasks = harness.load_tasks()
        assert [task["prompt"] for task in tasks] == ["mine"]

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

    def test_a_task_may_be_a_conversation(self):
        """A fresh conversation has no history, so a single-turn task cannot
        measure replay, ageing, or the transcript window. `turns` is how those are
        reached; a one-turn task is still just a `prompt`."""
        single = {"id": "s", "root": "/tmp", "prompt": "one", "checks": ["answered"]}
        conversation = {
            "id": "c",
            "root": "/tmp",
            "turns": ["first", "second"],
            "checks": ["answered"],
        }
        assert harness.task_prompts(single) == ["one"]
        assert harness.task_prompts(conversation) == ["first", "second"]

    def test_a_task_with_neither_prompt_nor_turns_is_rejected(self):
        with pytest.raises(ValueError, match="prompt"):
            harness.validate_task({"id": "x", "root": "/tmp", "checks": ["answered"]})

    def test_a_conversation_with_a_blank_turn_is_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            harness.validate_task(
                {"id": "x", "root": "/tmp", "turns": ["first", "  "], "checks": ["answered"]}
            )


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
        calls = [{"name": "read_file", "arguments": {"path": "src/ingest.py"}, "ok": True}]
        assert self.check(
            {"kind": "tool_called", "name": "read_file"}, outcome(tool_calls=calls)
        ).ok
        assert self.check(
            {"kind": "tool_called", "name": "read_file", "contains": "ingest.py"},
            outcome(tool_calls=calls),
        ).ok
        assert not self.check(
            {"kind": "tool_called", "name": "read_file", "contains": "parser.py"},
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
            {"name": "edit_file", "arguments": {"path": "src/ingest.py"}, "ok": True},
            {"name": "read_file", "arguments": {"path": "src/ingest.py"}, "ok": True},
        ]
        result = self.check(
            {"kind": "writes_within", "paths": ["docs/state/"]}, outcome(tool_calls=calls)
        )
        assert not result.ok
        assert "docs/current_state.md" in result.detail
        assert "ingest.py" in result.detail, "the edit is an offender too"
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


class TestTheRunDoesNotTouchTheProject:
    """A run is allowed to write, so the write must land somewhere disposable.

    This is not hypothetical: the first live run of the skills example closed one of
    the sample project's notes in place, which dirtied a committed file and quietly
    changed the answer to ``example-open-items`` for every run after it.
    """

    @pytest.fixture
    def project(self, tmp_path):
        root = tmp_path / "checkout"
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "note.md").write_text("original", encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
        (root / ".venv" / "lib").mkdir(parents=True)
        (root / ".venv" / "lib" / "big.py").write_text("x" * 100, encoding="utf-8")
        return root

    def test_the_work_root_is_a_copy(self, project, tmp_path):
        work_root = harness.sandbox_root({"id": "t"}, project, tmp_path)

        assert work_root != project
        assert (work_root / "docs" / "note.md").read_text(encoding="utf-8") == "original"

    def test_writing_in_the_copy_leaves_the_project_alone(self, project, tmp_path):
        work_root = harness.sandbox_root({"id": "t"}, project, tmp_path)

        (work_root / "docs" / "note.md").write_text("edited", encoding="utf-8")

        assert (project / "docs" / "note.md").read_text(encoding="utf-8") == "original"

    def test_the_caches_a_copy_does_not_need_are_left_behind(self, project, tmp_path):
        """Copied whole, a real project's virtualenv would cost more than the run."""
        work_root = harness.sandbox_root({"id": "t"}, project, tmp_path)

        assert not (work_root / ".git").exists()
        assert not (work_root / ".venv").exists()
        assert (work_root / "docs").is_dir(), "and the project's own files are still there"

    def test_a_task_may_ask_for_the_project_itself(self, project, tmp_path):
        """For a throwaway checkout, or when something the skip list drops is needed."""
        assert harness.sandbox_root({"id": "t", "in_place": True}, project, tmp_path) == project

    def test_in_place_must_be_a_boolean(self):
        task = {
            "id": "t",
            "root": "/tmp",
            "prompt": "p",
            "checks": ["answered"],
            "in_place": "yes",
        }
        with pytest.raises(ValueError, match="in_place"):
            harness.validate_task(task)

    async def test_the_scratch_space_is_removed_when_the_run_ends(self, tmp_path, monkeypatch):
        """Otherwise a suite leaves one copy of a project per task in the temp
        directory — the leak the job registry was fixed for."""
        from surtitle.config import Settings

        project = tmp_path / "project"
        project.mkdir()
        seen: dict = {}

        async def fake(task, settings, root, workdir, outcome):
            seen["workdir"] = workdir
            assert workdir.is_dir()
            return outcome

        monkeypatch.setattr(harness, "_run_task_in", fake)
        task = {"id": "t", "root": str(project), "prompt": "p", "checks": ["answered"]}
        await harness.run_task(task, Settings(DEEPSEEK_API_KEY="unused"))

        assert not seen["workdir"].exists()

    async def test_a_project_that_cannot_be_copied_does_not_end_the_suite(
        self, tmp_path, monkeypatch
    ):
        """The other tasks' results are already gathered; one unreadable checkout
        must not throw them away."""
        from surtitle.config import Settings

        project = tmp_path / "checkout"
        project.mkdir()

        def cant(src, dst, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(harness.shutil, "copytree", cant)
        task = {"id": "t", "root": str(project), "prompt": "p", "checks": ["answered"]}

        result = await harness.run_task(task, Settings(DEEPSEEK_API_KEY="unused"))

        assert "PermissionError" in result.error
        assert not result.tool_calls, "nothing was run, so nothing can be scored"


class TestCli:
    def test_listing_the_tasks_needs_no_credentials(self, capsys):
        assert eval_cli.main(["--list"]) == 0
        printed = capsys.readouterr().out
        assert "example-retry-limit" in printed

    def test_a_bad_selector_exits_before_doing_anything(self, capsys):
        assert eval_cli.main(["no-such-task"]) == 2
        assert "no task matches" in capsys.readouterr().err
