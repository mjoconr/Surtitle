"""Updating from GitHub: the decision and the commands, not the network.

Resolving a release and running git are both injected, so this covers which
command is chosen for which target and what happens when git refuses — the parts
that decide whether a user's checkout is moved or left alone. The real pull is
verified by running ``surtitle update --check`` against a real checkout.
"""

from __future__ import annotations

import pytest

from surtitle import update as updater


class _Done:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Which:
    """A stand-in for :mod:`shutil` that only answers ``which``."""

    def __init__(self, **found: str) -> None:
        self._found = found

    def which(self, name: str) -> str | None:
        return self._found.get(name)


@pytest.fixture
def git(monkeypatch, tmp_path):
    """A checkout that git is available for, with the runner recorded."""
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return _Done()

    monkeypatch.setattr(updater, "git_checkout", lambda: tmp_path)
    monkeypatch.setattr(updater, "shutil", _Which(git="/usr/bin/git"))
    return calls, runner


class TestVersionCompare:
    def test_a_later_tag_is_newer(self):
        assert updater._is_newer("v0.2.0", "0.1.0") is True

    def test_the_same_version_is_not_newer(self):
        assert updater._is_newer("v0.1.0", "0.1.0") is False

    def test_an_older_tag_is_not_newer(self):
        assert updater._is_newer("v0.0.9", "0.1.0") is False

    def test_a_release_candidate_does_not_crash_the_check(self):
        assert updater._is_newer("v0.2.0-rc1", "0.1.0") is True


class TestLatestRelease:
    def test_the_tag_is_read_from_the_payload(self):
        assert updater.latest_release(fetcher=lambda: {"tag_name": "v9.9.9"}) == "v9.9.9"

    def test_an_unreachable_api_is_not_an_error(self):
        def boom():
            raise OSError("no network")

        assert updater.latest_release(fetcher=boom) == ""

    def test_a_payload_without_a_tag_is_empty(self):
        assert updater.latest_release(fetcher=lambda: {}) == ""


class TestCheck:
    def test_a_git_checkout_reports_how_far_behind_main_it_is(self, git):
        calls, _runner = git

        def counting(argv, **kwargs):
            calls.append(list(argv))
            if "rev-parse" in argv:
                return _Done(stdout="main\n")
            if "rev-list" in argv:
                return _Done(stdout="3\n")
            return _Done()

        status = updater.check(runner=counting, fetcher=lambda: {"tag_name": "v0.1.0"})

        assert status.kind == "git"
        assert status.branch == "main"
        assert status.behind == 3
        assert status.main_available is True
        assert status.release_available is False, "the newest release is the one already running"

    def test_a_release_archive_is_only_told_about_releases(self, monkeypatch):
        monkeypatch.setattr(updater, "git_checkout", lambda: None)
        status = updater.check(fetcher=lambda: {"tag_name": "v9.9.9"})
        assert status.kind == "archive"
        assert status.release_available is True
        assert status.main_available is False
        assert "9.9.9" in status.detail


class TestApply:
    def test_it_refuses_an_unknown_target(self):
        result = updater.apply("nightly")
        assert result.ok is False
        assert "unknown" in result.message

    def test_a_non_checkout_is_told_to_download(self, monkeypatch):
        monkeypatch.setattr(updater, "git_checkout", lambda: None)
        result = updater.apply("release")
        assert result.ok is False
        assert updater.RELEASES_PAGE in result.message, "it must say where to get the release"

    def test_main_is_a_fast_forward_pull(self, git):
        calls, runner = git

        result = updater.apply("main", runner=runner)

        assert result.ok is True
        flattened = [" ".join(call) for call in calls]
        assert any("fetch" in line and "main" in line for line in flattened)
        assert any("merge" in line and "--ff-only" in line for line in flattened)
        assert "restart" in result.message.lower()

    def test_a_release_checks_out_the_tag(self, git):
        calls, runner = git

        result = updater.apply("release", runner=runner, fetcher=lambda: {"tag_name": "v9.9.9"})

        assert result.ok is True
        flattened = [" ".join(call) for call in calls]
        assert any("fetch" in line and "--tags" in line for line in flattened)
        assert any("checkout" in line and "v9.9.9" in line for line in flattened)

    def test_a_release_already_checked_out_does_not_move(self, git):
        _, runner = git

        result = updater.apply("release", runner=runner, fetcher=lambda: {"tag_name": "v0.1.0"})

        assert result.ok is True
        assert "already on" in result.message

    def test_a_failed_fast_forward_is_reported_and_stops(self, monkeypatch, tmp_path):
        monkeypatch.setattr(updater, "git_checkout", lambda: tmp_path)
        monkeypatch.setattr(updater, "shutil", _Which(git="/usr/bin/git"))
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            if "merge" in argv:
                return _Done(1, stderr="fatal: Not possible to fast-forward, aborting.")
            return _Done()

        result = updater.apply("main", runner=runner)

        assert result.ok is False
        assert "fast-forward" in result.message
        # Nothing after the failed merge: in particular no dependency re-sync on
        # a tree that did not move.
        assert not any("sync" in " ".join(call) for call in calls)

    def test_a_failed_fetch_says_it_could_not_reach_github(self, monkeypatch, tmp_path):
        monkeypatch.setattr(updater, "git_checkout", lambda: tmp_path)
        monkeypatch.setattr(updater, "shutil", _Which(git="/usr/bin/git"))

        result = updater.apply("main", runner=lambda argv, **kw: _Done(1, stderr="boom"))

        assert result.ok is False
        assert "reach GitHub" in result.message


class TestKind:
    """Three answers, because the fix differs for each."""

    def test_a_clone_with_git_can_update_itself(self, monkeypatch, tmp_path):
        monkeypatch.setattr(updater, "git_checkout", lambda: tmp_path)
        monkeypatch.setattr(updater, "shutil", _Which(git="/usr/bin/git"))
        assert updater.kind() == "git"

    def test_a_clone_without_git_is_not_called_an_archive(self, monkeypatch, tmp_path):
        """Saying "not a checkout" would send the user after the wrong problem."""
        monkeypatch.setattr(updater, "git_checkout", lambda: tmp_path)
        monkeypatch.setattr(updater, "shutil", _Which())
        assert updater.kind() == "no-git"

    def test_no_history_at_all_is_an_archive(self, monkeypatch):
        monkeypatch.setattr(updater, "git_checkout", lambda: None)
        assert updater.kind() == "archive"

    def test_a_checkout_without_git_says_which_problem_it_is(self, monkeypatch, tmp_path):
        monkeypatch.setattr(updater, "git_checkout", lambda: tmp_path)
        monkeypatch.setattr(updater, "shutil", _Which())
        status = updater.check(fetcher=lambda: {"tag_name": "v9.9.9"})
        assert status.kind == "no-git"
        assert "git is not installed" in status.detail
        assert "9.9.9" in status.detail, "it should still say a release is available"


class TestApplyWithoutGit:
    def test_a_checkout_without_git_is_told_to_install_git(self, monkeypatch, tmp_path):
        monkeypatch.setattr(updater, "git_checkout", lambda: tmp_path)
        monkeypatch.setattr(updater, "shutil", _Which())

        result = updater.apply("main")

        assert result.ok is False
        assert "git is not installed" in result.message
        assert updater.RELEASES_PAGE in result.message

    def test_a_non_checkout_is_told_it_has_no_history_to_pull(self, monkeypatch):
        monkeypatch.setattr(updater, "git_checkout", lambda: None)

        result = updater.apply("release")

        assert result.ok is False
        assert "no git history" in result.message
        assert updater.RELEASES_PAGE in result.message
