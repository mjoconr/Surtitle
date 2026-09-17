"""Installing the offline engines: the decision, not the download.

Everything here runs without a network, a package manager, or sherpa-onnx: the
command is decided from what is on the machine, and the subprocess and the model
download are injected. The parts that genuinely need Windows and a real wheel are
verified by ``surtitle voice install`` on a real machine.
"""

from __future__ import annotations

import threading

import pytest

from surtitle.config import Settings
from surtitle.voice import install as voice
from surtitle.voice import models


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Settings(SURTITLE_HOME=str(home))


class _Row:
    """The shape of a ``models.ModelStatus`` that ``state`` cares about."""

    def __init__(self, present: bool, key: str = "model"):
        self.present = present
        self.key = key


class TestRuntimePlan:
    def test_a_checkout_with_uv_syncs_the_extra_from_the_lock(self, tmp_path):
        plan = voice.runtime_plan(uv="/usr/bin/uv", checkout=tmp_path, executable="/py/python")
        assert plan.command[:3] == ["/usr/bin/uv", "sync", "--extra"]
        assert "voice-local" in plan.command
        # --inexact matters: a plain sync prunes the extra it was just asked for.
        assert "--inexact" in plan.command
        assert plan.cwd == tmp_path

    def test_without_uv_pip_installs_the_two_wheels(self, tmp_path):
        plan = voice.runtime_plan(uv="", checkout=tmp_path, executable="/py/python")
        assert plan.command[:4] == ["/py/python", "-m", "pip", "install"]
        joined = " ".join(plan.command)
        assert "sherpa-onnx>=" in joined
        # The bindings and the native runtime are one ABI and must move together.
        assert "sherpa-onnx-core>=" in joined
        assert plan.cwd is None

    def test_without_a_checkout_pip_is_used_even_with_uv(self, monkeypatch):
        """A release archive has no pyproject.toml, so uv sync means nothing."""
        monkeypatch.setattr(voice, "_checkout_root", lambda: None)
        plan = voice.runtime_plan(uv="/usr/bin/uv", executable="/py/python")
        assert plan.command[:3] == ["/py/python", "-m", "pip"]

    def test_the_plan_names_itself_for_a_report(self, tmp_path):
        assert voice.runtime_plan(uv="/uv", checkout=tmp_path).label
        assert voice.runtime_plan(uv="", checkout=None).label


class TestState:
    def test_ready_needs_both_halves(self, settings):
        snapshot = voice.state(settings, runtime=True, statuses=[_Row(True)])
        assert snapshot.ready is True
        assert snapshot.runtime is True and snapshot.models is True

    def test_missing_engines_are_named(self, settings):
        snapshot = voice.state(settings, runtime=False, statuses=[_Row(True)])
        assert snapshot.ready is False
        assert "engines" in snapshot.detail

    def test_missing_models_are_counted(self, settings):
        snapshot = voice.state(settings, runtime=True, statuses=[_Row(False), _Row(False)])
        assert snapshot.ready is False
        assert "2" in snapshot.detail

    def test_no_registered_models_is_not_ready(self, settings):
        snapshot = voice.state(settings, runtime=True, statuses=[])
        assert snapshot.ready is False


class TestInstall:
    def test_an_importable_engine_is_not_reinstalled(self, settings, monkeypatch):
        monkeypatch.setattr(voice, "runtime_present", lambda: True)
        ran: list[object] = []
        downloaded: list[int] = []
        monkeypatch.setattr(models, "download", lambda s, progress=None: downloaded.append(1))

        result = voice.install(settings, runner=lambda *a, **k: ran.append(a))

        assert result.ok is True
        assert ran == [], "the engine install must not run when it already imports"
        assert downloaded == [1]

    def test_a_failed_engine_install_is_reported_with_its_output(self, settings, monkeypatch):
        monkeypatch.setattr(voice, "runtime_present", lambda: False)
        monkeypatch.setattr(
            voice, "runtime_plan", lambda **kw: voice.InstallPlan(["/uv", "sync"], label="uv sync")
        )

        class Done:
            returncode = 1
            stdout = ""
            stderr = "error: no wheel for this platform"

        result = voice.install(settings, runner=lambda *a, **k: Done())

        assert result.ok is False
        assert "uv sync failed" in result.message
        assert "no wheel" in result.message
        assert result.steps == ["uv sync"]

    def test_an_engine_that_installs_but_does_not_import_is_reported(self, settings, monkeypatch):
        """pip can report success while the wheel is not importable here."""
        states = iter([False, False])
        monkeypatch.setattr(voice, "runtime_present", lambda: next(states))
        monkeypatch.setattr(
            voice, "runtime_plan", lambda **kw: voice.InstallPlan(["/uv", "sync"], label="uv sync")
        )

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        result = voice.install(settings, runner=lambda *a, **k: Done())

        assert result.ok is False
        assert "cannot be imported" in result.message

    def test_a_successful_install_downloads_the_models(self, settings, monkeypatch):
        states = iter([False, True])
        monkeypatch.setattr(voice, "runtime_present", lambda: next(states))
        monkeypatch.setattr(
            voice, "runtime_plan", lambda **kw: voice.InstallPlan(["/uv", "sync"], label="uv sync")
        )
        downloaded: list[int] = []
        monkeypatch.setattr(models, "download", lambda s, progress=None: downloaded.append(1))

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        result = voice.install(settings, runner=lambda *a, **k: Done())

        assert result.ok is True
        assert downloaded == [1]
        assert "restart" in result.message.lower()

    def test_a_model_download_failure_is_reported(self, settings, monkeypatch):
        monkeypatch.setattr(voice, "runtime_present", lambda: True)

        def boom(settings, progress=None):
            raise models.ModelUnavailable("no network", fix="try again later")

        monkeypatch.setattr(models, "download", boom)

        result = voice.install(settings)

        assert result.ok is False
        assert "no network" in result.message


class TestInstallJob:
    def test_a_job_runs_in_the_background_and_reports_when_done(self, settings, monkeypatch):
        monkeypatch.setattr(voice, "runtime_present", lambda: True)
        monkeypatch.setattr(models, "download", lambda s, progress=None: [])

        job = voice.InstallJob()

        assert job.start(settings) is True
        assert job.wait(timeout=5.0) is True
        snapshot = job.snapshot()
        assert snapshot["running"] is False
        assert snapshot["ok"] is True
        assert snapshot["percent"] == 100.0

    def test_a_second_start_while_running_is_refused(self, settings, monkeypatch):
        gate = threading.Event()
        monkeypatch.setattr(
            voice,
            "install",
            lambda s, progress=None, runner=None: (
                gate.wait(5.0),
                voice.InstallResult(True, "ok"),
            )[1],
        )

        job = voice.InstallJob()
        assert job.start(settings) is True
        assert job.start(settings) is False, "two downloads must not run at once"
        gate.set()
        assert job.wait(timeout=5.0) is True

    def test_progress_becomes_a_percentage(self):
        job = voice.InstallJob()
        job._progress(models.Progress(asset="stt", stage="download", received=50, total=100))
        snapshot = job.snapshot()
        assert snapshot["percent"] == 50.0
        assert "stt" in snapshot["message"]

    def test_a_failure_message_survives_to_the_snapshot(self, settings, monkeypatch):
        monkeypatch.setattr(
            voice, "install", lambda s, progress=None: voice.InstallResult(False, "it failed")
        )
        job = voice.InstallJob()
        job.start(settings)
        assert job.wait(timeout=5.0) is True
        snapshot = job.snapshot()
        assert snapshot["ok"] is False
        assert snapshot["message"] == "it failed"
