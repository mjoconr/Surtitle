"""Release-build helpers that are easy to get wrong per platform.

The archive's whole promise is that it runs on a machine with no Python, so
these cover the discovery that makes the smoke test meaningful. A Windows venv
keeps its interpreter in `Scripts/`, which is the layout that was missed and
made verification run the raw runtime instead — an interpreter that cannot
import the application, so the check passed for the wrong reason.
"""

from __future__ import annotations

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
