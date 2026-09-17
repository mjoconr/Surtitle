"""Release-build helpers that are easy to get wrong per platform.

The archive's whole promise is that it runs on a machine with no Python, so
these cover the discovery that makes the smoke test meaningful. A Windows venv
keeps its interpreter in `Scripts/`, which is the layout that was missed and
made verification run the raw runtime instead — an interpreter that cannot
import the application, so the check passed for the wrong reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import build_release


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class TestVenvInterpreters:
    def test_a_windows_venv_interpreter_is_found(self, tmp_path):
        """The layout that was missed: venv/Scripts/python.exe."""
        venv = tmp_path / "venv"
        expected = _touch(venv / "Scripts" / "python.exe")
        assert build_release._interpreter_in(venv, windows=True) == expected

    def test_a_posix_venv_interpreter_is_found(self, tmp_path):
        venv = tmp_path / "venv"
        expected = _touch(venv / "bin" / "python")
        assert build_release._interpreter_in(venv, windows=False) == expected

    def test_a_windows_venv_without_an_interpreter_reports_none(self, tmp_path):
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        assert build_release._interpreter_in(venv, windows=True) is None

    def test_the_two_layouts_are_not_confused(self, tmp_path):
        """A Scripts/ interpreter is not a POSIX venv, and vice versa."""
        venv = tmp_path / "venv"
        _touch(venv / "Scripts" / "python.exe")
        assert build_release._interpreter_in(venv, windows=False) is None


class TestBundledRuntimeInterpreters:
    def test_a_windows_runtime_is_found_at_the_top_level(self, tmp_path):
        runtime = tmp_path / "python"
        expected = _touch(runtime / "python.exe")
        assert build_release._interpreter_in(runtime, windows=True) == expected

    def test_a_posix_runtime_is_found_in_bin(self, tmp_path):
        runtime = tmp_path / "python"
        expected = _touch(runtime / "bin" / "python3")
        assert build_release._interpreter_in(runtime, windows=False) == expected

    def test_an_empty_root_reports_none(self, tmp_path):
        assert build_release._interpreter_in(tmp_path / "nothing", windows=True) is None
        assert build_release._interpreter_in(tmp_path / "nothing", windows=False) is None


class TestCandidateOrder:
    def test_windows_checks_scripts_first(self, tmp_path):
        assert build_release._interpreter_candidates(tmp_path, windows=True)[0] == (
            tmp_path / "Scripts" / "python.exe"
        )

    def test_posix_checks_the_versioned_name_first(self, tmp_path):
        first = build_release._interpreter_candidates(tmp_path, windows=False)[0]
        assert first.parent == tmp_path / "bin"
        assert first.name.startswith("python3.")


class TestLockExport:
    """The archive must carry the versions that were tested, not a fresh resolve."""

    def _recorder(self, tmp_path):
        calls: list[list[str]] = []

        def fake_run(argv, *, cwd=None, capture=False):
            calls.append(list(argv))
            Path(argv[argv.index("--output-file") + 1]).write_text("fastapi==0.115.0\n")
            return ""

        return calls, fake_run

    def test_the_export_is_frozen_and_excludes_the_project(self, tmp_path, monkeypatch):
        calls, fake_run = self._recorder(tmp_path)
        monkeypatch.setattr(build_release, "run", fake_run)

        build_release.export_lock_requirements("uv", tmp_path / "lock.txt")

        argv = calls[0]
        assert argv[:2] == ["uv", "export"]
        # --frozen is what makes it the lock rather than a resolution, and
        # --no-emit-project keeps the app out so it cannot become an editable
        # link back to the build machine.
        assert "--frozen" in argv
        assert "--no-emit-project" in argv
        assert "--extra" not in argv

    def test_the_voice_local_extra_is_included_when_asked(self, tmp_path, monkeypatch):
        calls, fake_run = self._recorder(tmp_path)
        monkeypatch.setattr(build_release, "run", fake_run)

        build_release.export_lock_requirements("uv", tmp_path / "lock.txt", voice_local=True)

        assert "--extra" in calls[0]
        assert "voice-local" in calls[0]

    def test_an_empty_export_is_refused(self, tmp_path, monkeypatch):
        def fake_run(argv, *, cwd=None, capture=False):
            Path(argv[argv.index("--output-file") + 1]).write_text("")
            return ""

        monkeypatch.setattr(build_release, "run", fake_run)
        with pytest.raises(SystemExit, match="no dependencies at all"):
            build_release.export_lock_requirements("uv", tmp_path / "lock.txt")

    def test_a_failed_export_is_reported(self, tmp_path, monkeypatch):
        def fake_run(argv, *, cwd=None, capture=False):
            raise SystemExit("command failed (1): uv export")

        monkeypatch.setattr(build_release, "run", fake_run)
        with pytest.raises(SystemExit, match="could not export"):
            build_release.export_lock_requirements("uv", tmp_path / "lock.txt")


class TestWheelhouse:
    def test_wheels_come_from_the_runtime_pip(self, tmp_path, monkeypatch):
        """uv has no `pip download`, and a POSIX venv has no pip, so use the runtime."""
        calls: list[list[str]] = []

        def fake_run(argv, *, cwd=None, capture=False):
            calls.append(list(argv))
            return ""

        monkeypatch.setattr(build_release, "run", fake_run)
        runtime = tmp_path / "python" / "python.exe"
        requirements = tmp_path / "lock.txt"
        destination = tmp_path / "wheelhouse"

        build_release.build_wheelhouse(runtime, requirements, destination)

        argv = calls[0]
        assert argv[:3] == [str(runtime), "-m", "pip"]
        assert "download" in argv
        assert str(requirements) in argv
        assert str(destination) in argv

    def test_a_failed_download_does_not_fail_the_build(self, tmp_path, monkeypatch):
        """The environment is already complete, so this stays a convenience."""

        def fake_run(argv, *, cwd=None, capture=False):
            raise SystemExit("command failed (2): pip download")

        monkeypatch.setattr(build_release, "run", fake_run)
        # Must not raise.
        build_release.build_wheelhouse(
            tmp_path / "python", tmp_path / "lock.txt", tmp_path / "wheelhouse"
        )


class TestArchiveLayout:
    """Windows and POSIX archives differ on purpose, and the rule lives here.

    The release workflow used to assert one layout for both — a
    ``venv/Scripts/python.exe`` that a Windows archive never ships — so every
    Windows release failed after a full build. These pin the shared rule so a
    workflow cannot quietly reintroduce the assumption.
    """

    def _windows_archive(self, root: Path) -> Path:
        _touch(root / "run.bat")
        _touch(root / "python" / "python.exe")
        return root

    def _posix_archive(self, root: Path) -> Path:
        _touch(root / "run.sh")
        _touch(root / "venv" / "bin" / "python")
        return root

    def test_a_windows_archive_uses_the_bundled_runtime(self, tmp_path):
        build_release.assert_archive_layout(self._windows_archive(tmp_path), "win32")

    def test_a_windows_archive_must_not_ship_a_venv(self, tmp_path):
        root = self._windows_archive(tmp_path)
        (root / "venv").mkdir()
        with pytest.raises(SystemExit, match="must not contain venv"):
            build_release.assert_archive_layout(root, "win32")

    def test_a_windows_archive_without_the_runtime_is_refused(self, tmp_path):
        _touch(tmp_path / "run.bat")
        with pytest.raises(SystemExit, match="no interpreter"):
            build_release.assert_archive_layout(tmp_path, "win32")

    def test_a_posix_archive_uses_its_venv(self, tmp_path):
        build_release.assert_archive_layout(self._posix_archive(tmp_path), "darwin")

    def test_a_posix_archive_without_the_venv_is_refused(self, tmp_path):
        _touch(tmp_path / "run.sh")
        with pytest.raises(SystemExit, match="no interpreter"):
            build_release.assert_archive_layout(tmp_path, "linux")

    def test_the_launcher_must_match_the_platform(self, tmp_path):
        """run.sh alone is a POSIX archive, not a Windows one."""
        _touch(tmp_path / "run.sh")
        _touch(tmp_path / "python" / "python.exe")
        with pytest.raises(SystemExit, match=r"no run\.bat"):
            build_release.assert_archive_layout(tmp_path, "win32")


class TestArchivePlatform:
    """The archive says which platform it is, not the machine checking it."""

    def test_the_manifest_decides_not_the_verifying_machine(self, tmp_path):
        (tmp_path / "BUILD-INFO.json").write_text('{"platform": "win32"}', encoding="utf-8")
        assert build_release.archive_platform(tmp_path, fallback="darwin") == "win32"

    def test_a_missing_manifest_falls_back(self, tmp_path):
        assert build_release.archive_platform(tmp_path, fallback="linux") == "linux"

    def test_an_unreadable_manifest_falls_back(self, tmp_path):
        (tmp_path / "BUILD-INFO.json").write_text("{not json", encoding="utf-8")
        assert build_release.archive_platform(tmp_path, fallback="darwin") == "darwin"


class TestWorkflowsUseTheSharedVerifier:
    """A second copy of the layout rules is what broke the Windows release."""

    WORKFLOWS = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml",
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml",
    )

    def test_every_workflow_verifies_through_build_release(self):
        for workflow in self.WORKFLOWS:
            assert "--verify-only" in workflow.read_text(encoding="utf-8"), (
                f"{workflow.name} does not re-verify the artifact it will publish"
            )

    def test_no_workflow_reimplements_interpreter_discovery(self):
        for workflow in self.WORKFLOWS:
            assert "Scripts" not in workflow.read_text(encoding="utf-8"), (
                f"{workflow.name} re-implements the archive layout; interpreter "
                "discovery belongs in scripts/build_release.py"
            )
