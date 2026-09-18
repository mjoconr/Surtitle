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
