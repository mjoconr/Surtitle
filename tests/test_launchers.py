"""The launchers must find a ``uv`` that the current shell cannot see.

Reported from a real Windows run: install uv, then ``run.bat`` in the same
window, and it stops with "no Python environment was found" — immediately
followed by advice to install uv, which had just been installed.

The cause is that uv's own installer updates the *user PATH* but not the
environment of the shell that ran it, so ``where uv`` fails in the very window
that installed it while ``%USERPROFILE%\\.local\\bin\\uv.exe`` sits there. The
launchers only looked on ``PATH``.

This is worth a test rather than a fix and forget, because the broken version
reads perfectly well: ``where uv`` looks like exactly the right check. Only the
knowledge that the installer does not touch the current process's environment
makes the fallback obviously necessary.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
RUN_BAT = SCRIPTS / "run.bat"
RUN_PS1 = SCRIPTS / "run.ps1"
RUN_SH = SCRIPTS / "run.sh"

# What uv's installers print, and what this project's failure message must be
# consistent with: the Windows installer unpacks into %USERPROFILE%\.local\bin
# and the POSIX one into ~/.local/bin.
WINDOWS_UV_PATH = r".local\bin\uv.exe"
POSIX_UV_PATH = ".local/bin/uv"


@pytest.fixture(scope="module")
def bat() -> str:
    return RUN_BAT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ps1() -> str:
    return RUN_PS1.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sh() -> str:
    return RUN_SH.read_text(encoding="utf-8")


class TestUvDiscovery:
    def test_the_batch_launcher_checks_where_uv_actually_lands(self, bat):
        assert WINDOWS_UV_PATH in bat, (
            "run.bat must look in uv's install location, not only on PATH"
        )

    def test_the_powershell_launcher_checks_where_uv_actually_lands(self, ps1):
        assert WINDOWS_UV_PATH in ps1

    def test_the_posix_launcher_checks_where_uv_actually_lands(self, sh):
        assert POSIX_UV_PATH in sh

    def test_every_launcher_searches_for_uv_the_same_way(self, bat, ps1, sh):
        """Three launchers, one rule: PATH first, then the install location."""
        assert WINDOWS_UV_PATH in bat and WINDOWS_UV_PATH in ps1
        assert POSIX_UV_PATH in sh

    def test_the_installer_and_the_launchers_agree(self):
        """install.ps1 always knew; a launcher that disagrees breaks the docs."""
        install = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
        assert WINDOWS_UV_PATH in install


class TestFailureMessage:
    """When nothing is found, say the thing that is actually wrong."""

    def test_the_batch_message_explains_the_path_gap(self, bat):
        tail = bat.split("cannot start")[-1]
        assert ".local" in tail, "the message must say where uv unpacks to"
        assert "PATH" in tail, "the message must explain why the shell cannot see it"

    def test_the_powershell_message_explains_the_path_gap(self, ps1):
        tail = ps1.split("cannot start")[-1]
        assert ".local" in tail
        assert "PATH" in tail

    def test_the_posix_message_explains_the_path_gap(self, sh):
        tail = sh.split("cannot start")[-1]
        assert ".local" in tail
        assert "PATH" in tail

    def test_windows_launchers_point_at_the_one_step_installer(self, bat, ps1):
        assert "install.ps1" in bat.split("cannot start")[-1]
        assert "install.ps1" in ps1.split("cannot start")[-1]


class TestDevelopmentVenv:
    """The launchers live in ``scripts/`` in a checkout but at the root in a
    release archive, and ``.venv`` belongs at the checkout root.

    Looking only next to the launcher meant a perfectly good ``.venv`` — the one
    the README's manual instructions tell you to create — was reported as "no
    Python environment was found".
    """

    def test_the_batch_launcher_looks_beside_and_above_itself(self, bat):
        assert r".venv\Scripts\python.exe" in bat
        assert r"..\.venv\Scripts\python.exe" in bat

    def test_the_powershell_launcher_looks_beside_and_above_itself(self, ps1):
        assert r"..\.venv\Scripts\python.exe" in ps1

    def test_the_posix_launcher_looks_beside_and_above_itself(self, sh):
        assert '"$SCRIPT_DIR/.venv/bin/python"' in sh
        assert '"$SCRIPT_DIR/../.venv/bin/python"' in sh

    @pytest.mark.skipif(sys.platform == "win32", reason="run.sh is for macOS and Linux")
    @pytest.mark.skipif(shutil.which("bash") is None, reason="no bash available")
    def test_a_checkout_root_venv_actually_runs(self, tmp_path):
        """Behavioural: a checkout with scripts/run.sh and .venv, and no uv."""
        checkout = tmp_path / "checkout"
        (checkout / "scripts").mkdir(parents=True)
        shutil.copy(RUN_SH, checkout / "scripts" / "run.sh")

        venv_python = checkout / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text('#!/bin/sh\necho "STUB python $*"\n', encoding="utf-8")
        venv_python.chmod(0o755)

        done = subprocess.run(
            ["bash", str(checkout / "scripts" / "run.sh"), "--version"],
            # No uv on PATH and no HOME to find one in, so only the .venv can run.
            env={"HOME": str(tmp_path / "nohome"), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert done.returncode == 0, done.stderr
        assert "STUB python -m surtitle --version" in done.stdout
        assert "no Python environment" not in done.stdout


class TestPosixLauncherBehaviour:
    """Actually run run.sh with a uv that only exists off PATH."""

    @pytest.mark.skipif(sys.platform == "win32", reason="run.sh is for macOS and Linux")
    @pytest.mark.skipif(shutil.which("bash") is None, reason="no bash available")
    def test_a_uv_off_path_is_found_and_used(self, tmp_path):
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        stub = home / ".local" / "bin" / "uv"
        stub.write_text('#!/bin/sh\necho "STUB uv $*"\n', encoding="utf-8")
        stub.chmod(0o755)

        done = subprocess.run(
            ["bash", str(RUN_SH), "--version"],
            # A PATH with no uv on it at all, which is the state of the window
            # that just installed uv.
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert done.returncode == 0, done.stderr
        assert "STUB uv run --no-sync --quiet surtitle --version" in done.stdout
