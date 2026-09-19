"""Downloading, verifying and swapping in a release.

The network is injected, so what is covered here is the part that decides whether
a user's installation is replaced: which asset is chosen, whether the checksum is
enforced, and — most importantly — that the swap script leaves a working install
behind whether it succeeds or fails. That last one is exercised for real, by
running the generated script.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

from surtitle import selfupdate
from surtitle.config import Settings


def _settings(tmp_path) -> Settings:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return Settings(SURTITLE_HOME=str(home))


def _windows_payload(name: str) -> dict:
    return {
        "tag_name": "v9.9.9",
        "assets": [
            {"name": name, "browser_download_url": "https://example/asset", "size": 10},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://example/sums"},
        ],
    }


class TestAssetSelection:
    def test_windows_takes_the_zip(self):
        payload = {
            "tag_name": "v1",
            "assets": [
                {"name": "surtitle-1-darwin-arm64.tar.gz", "browser_download_url": "mac"},
                {"name": "surtitle-1-win32-AMD64.zip", "browser_download_url": "win"},
            ],
        }
        assert selfupdate.asset_for(payload, platform="win32", machine="AMD64").url == "win"

    def test_a_platform_without_uname_still_uses_the_machine_it_was_given(self, monkeypatch):
        """Windows has no `os.uname`, and the machine must not evaporate with it.

        Reproduced from CI, which is the only place it shows: every other test here
        runs on a machine where `os.uname` exists, so an implementation that asked
        `hasattr(os, "uname")` about the *argument* looked correct. On Windows it
        discarded the machine, matched no asset, and offered no update at all.
        """
        monkeypatch.delattr(selfupdate.os, "uname", raising=False)
        payload = {
            "tag_name": "v1",
            "assets": [{"name": "surtitle-1-win32-AMD64.zip", "browser_download_url": "win"}],
        }

        assert selfupdate.asset_for(payload, platform="win32", machine="AMD64").url == "win"

    def test_macos_takes_the_tarball_for_its_architecture(self):
        payload = {
            "tag_name": "v1",
            "assets": [
                {"name": "surtitle-1-win32-AMD64.zip", "browser_download_url": "win"},
                {"name": "surtitle-1-darwin-arm64.tar.gz", "browser_download_url": "mac"},
            ],
        }
        assert selfupdate.asset_for(payload, platform="darwin", machine="arm64").url == "mac"

    def test_a_release_with_no_build_for_this_platform_is_none(self):
        payload = {
            "tag_name": "v1",
            "assets": [{"name": "surtitle-1-win32-AMD64.zip", "browser_download_url": "win"}],
        }
        assert selfupdate.asset_for(payload, platform="darwin", machine="arm64") is None

    def test_an_intel_mac_is_not_handed_the_arm_build(self):
        """Reported as the release not running at all on an Intel Mac.

        The archive carries its own interpreter, so an arm64 build there is not
        slow or degraded — it fails to start. Before this, the architecture only
        decided which asset to *prefer*: an Intel Mac matched nothing and was given
        the first macOS asset in the list, which was the arm64 one.
        """
        payload = {
            "tag_name": "v1",
            "assets": [
                {"name": "surtitle-1-darwin-arm64.tar.gz", "browser_download_url": "arm"},
            ],
        }
        assert selfupdate.asset_for(payload, platform="darwin", machine="x86_64") is None, (
            "no update is better than one that cannot run"
        )

    def test_an_intel_mac_takes_the_intel_build_when_both_are_published(self):
        payload = {
            "tag_name": "v1",
            "assets": [
                {"name": "surtitle-1-darwin-arm64.tar.gz", "browser_download_url": "arm"},
                {"name": "surtitle-1-darwin-x86_64.tar.gz", "browser_download_url": "intel"},
            ],
        }
        assert selfupdate.asset_for(payload, platform="darwin", machine="x86_64").url == "intel"
        assert selfupdate.asset_for(payload, platform="darwin", machine="arm64").url == "arm"

    def test_apple_silicon_may_fall_back_to_intel_under_rosetta(self):
        """The one direction that works, and only as a fallback."""
        payload = {
            "tag_name": "v1",
            "assets": [
                {"name": "surtitle-1-darwin-x86_64.tar.gz", "browser_download_url": "intel"},
            ],
        }
        assert selfupdate.asset_for(payload, platform="darwin", machine="arm64").url == "intel"

    def test_an_asset_that_names_no_architecture_is_still_usable(self):
        payload = {
            "tag_name": "v1",
            "assets": [{"name": "surtitle-1-darwin.tar.gz", "browser_download_url": "any"}],
        }
        assert selfupdate.asset_for(payload, platform="darwin", machine="x86_64").url == "any"

    def test_the_names_the_builder_makes_are_the_names_the_updater_reads(self):
        """The two live in different files, and nothing else connects them.

        A rename on either side would leave every Mac of one architecture with no
        usable asset — quietly, because the release still looks complete. That is
        how an Intel Mac came to be offered the arm64 build in the first place.
        """
        from scripts import build_release

        published = {
            build_release.archive_stem("1.0.0", platform_name=name, machine=machine): url
            for name, machine, url in (
                ("win32", "AMD64", "win"),
                ("darwin", "arm64", "arm"),
                ("darwin", "x86_64", "intel"),
            )
        }
        assert sorted(published) == [
            "surtitle-1.0.0-darwin-arm64",
            "surtitle-1.0.0-darwin-x86_64",
            "surtitle-1.0.0-win32-AMD64",
        ]

        payload = {
            "tag_name": "v1",
            "assets": [
                {
                    "name": f"{stem}.zip" if "win32" in stem else f"{stem}.tar.gz",
                    "browser_download_url": url,
                }
                for stem, url in published.items()
            ],
        }
        assert selfupdate.asset_for(payload, platform="darwin", machine="x86_64").url == "intel"
        assert selfupdate.asset_for(payload, platform="darwin", machine="arm64").url == "arm"
        assert selfupdate.asset_for(payload, platform="win32", machine="AMD64").url == "win"


class TestChecksums:
    def test_the_published_sums_are_parsed(self):
        payload = _windows_payload("surtitle-9.9.9-win32-AMD64.zip")
        sums = selfupdate.expected_sums(
            payload, fetch_text=lambda url: "abc123  a.zip\ndef456 *b.zip\n"
        )
        assert sums == {"a.zip": "abc123", "b.zip": "def456"}

    def test_a_release_without_sums_yields_nothing(self):
        payload = {"tag_name": "v1", "assets": []}
        assert selfupdate.expected_sums(payload, fetch_text=lambda url: "") == {}

    def test_unreadable_sums_yield_nothing(self):
        payload = _windows_payload("x.zip")

        def boom(url):
            raise OSError("no network")

        assert selfupdate.expected_sums(payload, fetch_text=boom) == {}


class TestStaging:
    def _zip(self, path: Path) -> str:
        with zipfile.ZipFile(path, "w") as bundle:
            bundle.writestr("BUILD-INFO.json", "{}")
            bundle.writestr("run.bat", "@echo off\n")
            bundle.writestr("src/app.py", "x = 1\n")
        return selfupdate.sha256_of(path)

    def test_a_verified_release_is_staged_beside_the_install(self, tmp_path):
        root = tmp_path / "Surtitle"
        root.mkdir()
        (root / "BUILD-INFO.json").write_text("{}", encoding="utf-8")
        archive = tmp_path / "asset.zip"
        digest = self._zip(archive)
        name = "surtitle-9.9.9-win32-AMD64.zip"
        data = archive.read_bytes()

        staged, error = selfupdate.stage(
            _settings(tmp_path),
            _windows_payload(name),
            root=root,
            platform="win32",
            machine="AMD64",
            opener=lambda url: data,
            fetch_text=lambda url: f"{digest}  {name}\n",
        )

        assert error == ""
        assert staged == tmp_path / "Surtitle.new", "the swap must be a rename, not a copy"
        assert (staged / "BUILD-INFO.json").is_file()
        assert (staged / "run.bat").is_file()

    def test_a_checksum_mismatch_stops_the_update(self, tmp_path):
        root = tmp_path / "Surtitle"
        root.mkdir()
        (root / "BUILD-INFO.json").write_text("{}", encoding="utf-8")
        archive = tmp_path / "asset.zip"
        self._zip(archive)
        name = "surtitle-9.9.9-win32-AMD64.zip"

        staged, error = selfupdate.stage(
            _settings(tmp_path),
            _windows_payload(name),
            root=root,
            platform="win32",
            machine="AMD64",
            opener=lambda url: archive.read_bytes(),
            fetch_text=lambda url: f"{'0' * 64}  {name}\n",
        )

        assert staged is None
        assert "checksum" in error
        assert not (tmp_path / "Surtitle.new").exists()

    def test_a_release_without_a_checksum_is_refused(self, tmp_path):
        root = tmp_path / "Surtitle"
        root.mkdir()
        (root / "BUILD-INFO.json").write_text("{}", encoding="utf-8")
        name = "surtitle-9.9.9-win32-AMD64.zip"

        staged, error = selfupdate.stage(
            _settings(tmp_path),
            _windows_payload(name),
            root=root,
            platform="win32",
            machine="AMD64",
            opener=lambda url: b"whatever",
            fetch_text=lambda url: "",
        )

        assert staged is None
        assert "checksum" in error, "an update must not skip the only check it has"

    def test_an_asset_that_is_not_a_build_is_refused(self, tmp_path):
        root = tmp_path / "Surtitle"
        root.mkdir()
        (root / "BUILD-INFO.json").write_text("{}", encoding="utf-8")
        archive = tmp_path / "asset.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("readme.txt", "not a build")
        name = "surtitle-9.9.9-win32-AMD64.zip"
        digest = selfupdate.sha256_of(archive)

        staged, error = selfupdate.stage(
            _settings(tmp_path),
            _windows_payload(name),
            root=root,
            platform="win32",
            machine="AMD64",
            opener=lambda url: archive.read_bytes(),
            fetch_text=lambda url: f"{digest}  {name}\n",
        )

        assert staged is None
        assert "Surtitle build" in error


class TestExtraction:
    def test_a_single_top_level_directory_is_flattened(self, tmp_path):
        source = tmp_path / "surtitle"
        (source / "src").mkdir(parents=True)
        (source / "BUILD-INFO.json").write_text("{}", encoding="utf-8")
        (source / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        archive = tmp_path / "asset.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(source, arcname="surtitle")

        destination = tmp_path / "out"
        selfupdate._extract(archive, destination)

        assert (destination / "BUILD-INFO.json").is_file()
        assert (destination / "src" / "app.py").is_file()
        assert not (destination / "surtitle").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX swap script")
class TestTheSwap:
    """The generated updater is run for real: this is the step that replaces the app."""

    def _prepare(self, tmp_path, *, staged: bool) -> tuple[Path, Path, Path]:
        root = tmp_path / "Surtitle"
        root.mkdir()
        (root / "marker.txt").write_text("old", encoding="utf-8")
        staged_dir = tmp_path / "Surtitle.new"
        if staged:
            staged_dir.mkdir()
            (staged_dir / "marker.txt").write_text("new", encoding="utf-8")
        script = selfupdate.write_updater(_settings(tmp_path), staged_dir, root)
        return root, staged_dir, script

    @staticmethod
    def _run_swap(script: Path, root: Path, staged: Path) -> tuple[int, str]:
        """Run the updater against a process that is alive and then exits.

        The server it waits for must be a *live* process when the updater starts —
        that is the real situation, and it also keeps the pid from being reused by
        the updater itself. It then has to be reaped by someone, or `kill -0` keeps
        succeeding on the zombie; the test process does that here, which is what
        the server's own parent does in reality.
        """
        holder = subprocess.Popen(["/bin/sh", "-c", "sleep 0.4"])
        runner = subprocess.Popen(
            ["/bin/sh", str(script), str(holder.pid), str(root), str(staged), ""],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        holder.wait(timeout=15)
        _, err = runner.communicate(timeout=30)
        return runner.returncode, err

    def test_the_install_is_replaced_and_the_backup_removed(self, tmp_path):
        root, staged, script = self._prepare(tmp_path, staged=True)

        code, err = self._run_swap(script, root, staged)

        assert code == 0, err
        assert (root / "marker.txt").read_text(encoding="utf-8") == "new"
        assert not staged.exists()
        assert not (tmp_path / "Surtitle.old").exists(), "the backup must not be left behind"

    def test_a_failed_swap_puts_the_working_install_back(self, tmp_path):
        root, staged, script = self._prepare(tmp_path, staged=False)

        code, _ = self._run_swap(script, root, staged)

        assert code != 0
        assert (root / "marker.txt").read_text(encoding="utf-8") == "old", (
            "a failed update must leave a working install, not half of one"
        )
        assert not (tmp_path / "Surtitle.old").exists()

    def test_the_swap_does_not_happen_while_the_process_is_alive(self, tmp_path):
        """It must not start moving files the running server still has open."""
        root, staged, script = self._prepare(tmp_path, staged=True)
        holder = subprocess.Popen(["/bin/sh", "-c", "sleep 0.8"])
        try:
            runner = subprocess.Popen(
                ["/bin/sh", str(script), str(holder.pid), str(root), str(staged), ""],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # While the holder is alive, nothing may have moved.
            time.sleep(0.25)
            assert (root / "marker.txt").read_text(encoding="utf-8") == "old"
            holder.wait(timeout=15)
            _, err = runner.communicate(timeout=30)
        finally:
            if holder.poll() is None:
                holder.kill()
        assert runner.returncode == 0, err
        assert (root / "marker.txt").read_text(encoding="utf-8") == "new"


class TestTheUpdaterIsNotRunFromTheInstall:
    """The bug that made every in-place update on Windows do nothing.

    ``run.bat`` does ``cd /d "%~dp0"``, so the server's working directory is the
    install directory, and the updater used to inherit it. Windows refuses to
    rename a directory that any process has as its current directory, so the first
    ``Move-Item`` always failed, the script threw, and the app came back on the old
    version with nothing said. The POSIX swap tests never saw it because POSIX
    allows that rename and because they run the script from outside the directory
    it moves.
    """

    def test_the_updater_runs_with_a_working_directory_outside_the_install(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "Surtitle"
        root.mkdir()
        staged = tmp_path / "Surtitle.new"
        staged.mkdir()
        script = selfupdate.write_updater(_settings(tmp_path), staged, root)

        seen: list[dict] = []
        monkeypatch.setattr(
            selfupdate.subprocess,
            "Popen",
            lambda argv, **kwargs: seen.append({"argv": argv, **kwargs}) or object(),
        )

        selfupdate._launch(script, root, staged, root / "run.sh")

        assert seen, "the updater was never started"
        cwd = Path(seen[0]["cwd"]).resolve()
        assert not cwd.is_relative_to(root.resolve()), (
            "the updater must not run from the install directory: Windows will not "
            "rename a directory that is a process's working directory"
        )
        assert cwd == script.parent.resolve()

    def test_the_script_is_written_outside_the_install(self, tmp_path):
        root = tmp_path / "Surtitle"
        root.mkdir()
        settings = _settings(tmp_path)

        script = selfupdate.write_updater(settings, tmp_path / "staged", root)

        assert not Path(script).resolve().is_relative_to(root.resolve())
        assert Path(script).parent == selfupdate.updates_dir(settings)
        assert Path(script).parent.is_dir(), "the working directory has to exist"

    def test_both_scripts_change_directory_before_moving_anything(self):
        """Belt and braces for a host that launches them some other way."""
        from surtitle.selfupdate import _UPDATER_PS1, _UPDATER_SH

        assert _UPDATER_SH.index('cd "$here"') < _UPDATER_SH.index(
            'move_with_retry "$install" "$backup"'
        ), "the shell script must change directory before moving the install"
        assert _UPDATER_PS1.index("Set-Location -LiteralPath $here") < _UPDATER_PS1.index(
            "Move-WithRetry -From $Install -To $backup"
        ), "the PowerShell script must change directory before moving the install"

    def test_a_move_is_retried_but_a_missing_source_is_not(self):
        """Windows releases a directory handle a moment after the process exits."""
        from surtitle.selfupdate import _UPDATER_PS1, _UPDATER_SH

        assert "move_with_retry" in _UPDATER_SH and "sleep 0.5" in _UPDATER_SH
        assert "Move-WithRetry" in _UPDATER_PS1 and "Start-Sleep -Milliseconds 500" in _UPDATER_PS1
        # Retrying a source that does not exist would only make a failure slow.
        assert '[ -e "$from" ] || return 1' in _UPDATER_SH
        assert "if (-not (Test-Path -LiteralPath $From)) { return $false }" in _UPDATER_PS1


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX swap script")
class TestTheUpdaterReportsWhatItDid:
    """The swap runs after the app exits, so a file is the only way it can report."""

    def _prepare(self, tmp_path, *, staged: bool):
        root = tmp_path / "Surtitle"
        root.mkdir()
        (root / "marker.txt").write_text("old", encoding="utf-8")
        staged_dir = tmp_path / "Surtitle.new"
        if staged:
            staged_dir.mkdir()
            (staged_dir / "marker.txt").write_text("new", encoding="utf-8")
        settings = _settings(tmp_path)
        script = selfupdate.write_updater(settings, staged_dir, root)
        return settings, root, staged_dir, script

    def _run(self, script, root, staged, relaunch=""):
        holder = subprocess.Popen(["/bin/sh", "-c", "sleep 0.3"])
        runner = subprocess.Popen(
            ["/bin/sh", str(script), str(holder.pid), str(root), str(staged), relaunch],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        holder.wait(timeout=15)
        runner.communicate(timeout=60)
        return runner.returncode

    def test_a_successful_swap_says_so(self, tmp_path):
        settings, root, staged, script = self._prepare(tmp_path, staged=True)

        assert self._run(script, root, staged) == 0

        result = selfupdate.last_attempt(settings)
        assert result is not None
        assert result["ok"] is True
        assert result["at"] > 0
        assert selfupdate.log_path(settings).is_file()

    def test_a_failed_swap_says_why_and_leaves_the_app_alone(self, tmp_path):
        settings, root, staged, script = self._prepare(tmp_path, staged=False)

        assert self._run(script, root, staged) != 0

        result = selfupdate.last_attempt(settings)
        assert result is not None and result["ok"] is False
        assert "could not be moved" in result["message"]
        assert (root / "marker.txt").read_text(encoding="utf-8") == "old"

    def test_the_app_is_started_again_even_when_the_swap_failed(self, tmp_path):
        """Being left with no app at all is worse than the update not happening."""
        _settings_unused, root, staged, script = self._prepare(tmp_path, staged=False)
        marker = tmp_path / "relaunched.txt"
        relaunch = tmp_path / "relaunch.sh"
        relaunch.write_text(f'#!/bin/sh\necho started > "{marker}"\n', encoding="utf-8")
        relaunch.chmod(0o755)

        self._run(script, root, staged, relaunch=str(relaunch))

        for _ in range(50):
            if marker.is_file():
                break
            time.sleep(0.1)
        assert marker.is_file(), "a failed update must not leave the user with no app"

    def test_the_log_records_the_steps(self, tmp_path):
        settings, root, staged, script = self._prepare(tmp_path, staged=True)

        self._run(script, root, staged)

        log = selfupdate.log_path(settings).read_text(encoding="utf-8")
        assert "waiting for pid" in log
        assert "the new build is in place" in log


class TestLastAttempt:
    """Reading the outcome, which is written by PowerShell or sh, not by Python."""

    def test_nothing_recorded_is_none(self, tmp_path):
        assert selfupdate.last_attempt(_settings(tmp_path)) is None

    def test_an_ok_result(self, tmp_path):
        settings = _settings(tmp_path)
        selfupdate.updates_dir(settings).mkdir(parents=True, exist_ok=True)
        (selfupdate.updates_dir(settings) / "last-update.txt").write_text(
            "1789000000 ok updated\n", encoding="utf-8"
        )
        result = selfupdate.last_attempt(settings)
        assert result is not None
        assert result["ok"] is True and result["at"] == 1789000000.0

    def test_a_failure_keeps_its_whole_message(self, tmp_path):
        settings = _settings(tmp_path)
        selfupdate.updates_dir(settings).mkdir(parents=True, exist_ok=True)
        (selfupdate.updates_dir(settings) / "last-update.txt").write_text(
            "1789000000 failed could not move C:\\Wooltech\\Surtitle aside\n", encoding="utf-8"
        )
        result = selfupdate.last_attempt(settings)
        assert result is not None
        assert result["ok"] is False
        assert result["message"] == "could not move C:\\Wooltech\\Surtitle aside"
        assert result["log"].endswith("apply-update.log")

    def test_a_byte_order_mark_is_tolerated(self, tmp_path):
        """PowerShell 5.1 writes one with -Encoding UTF8."""
        settings = _settings(tmp_path)
        selfupdate.updates_dir(settings).mkdir(parents=True, exist_ok=True)
        (selfupdate.updates_dir(settings) / "last-update.txt").write_bytes(
            "\ufeff1789000000 ok updated\n".encode("utf-8")
        )
        assert selfupdate.last_attempt(settings)["ok"] is True

    @pytest.mark.parametrize("text", ["", "   ", "not a result", "12345 ok"])
    def test_a_malformed_result_is_ignored_rather_than_shown(self, tmp_path, text):
        settings = _settings(tmp_path)
        selfupdate.updates_dir(settings).mkdir(parents=True, exist_ok=True)
        (selfupdate.updates_dir(settings) / "last-update.txt").write_text(text, encoding="utf-8")
        assert selfupdate.last_attempt(settings) is None
