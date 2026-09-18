"""Noticing a new release, and the budget for saying so.

The interesting behaviour here is not the version comparison — that is arithmetic —
but the *policy*: how often a user is told, how long between mentions, what resets
the count, and the fact that the count survives a restart. An update notice that
repeats forever is one people learn to dismiss, and one that resets on every launch
is the same notice every morning.
"""

from __future__ import annotations

import json

import pytest

from surtitle import __version__
from surtitle.config import Settings
from surtitle.releases import (
    COOLDOWN_SECONDS,
    MAX_ANNOUNCEMENTS,
    ReleaseNotices,
    ReleaseWatcher,
    is_newer,
    parse_version,
)


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Settings(DEEPSEEK_API_KEY="sk-test", SURTITLE_HOME=str(home))


def release_payload(tag: str) -> dict:
    return {
        "tag_name": tag,
        "name": f"Surtitle {tag}",
        "html_url": f"https://github.com/mjoconr/Surtitle/releases/tag/{tag}",
        "published_at": "2026-01-02T03:04:05Z",
    }


class TestVersionComparison:
    def test_a_later_patch_is_newer(self):
        assert is_newer("0.5.0", "0.5.1") is True

    def test_a_later_minor_is_newer(self):
        assert is_newer("0.5.9", "0.6.0") is True

    def test_the_same_version_is_not_newer(self):
        assert is_newer("0.5.0", "0.5.0") is False

    def test_an_older_version_is_not_newer(self):
        assert is_newer("0.6.0", "0.5.9") is False

    def test_a_v_prefix_is_ignored(self):
        assert is_newer("0.5.0", "v0.5.1") is True
        assert is_newer("v0.5.0", "0.5.1") is True

    def test_missing_components_count_as_zero(self):
        assert is_newer("0.5", "0.5.0") is False
        assert is_newer("0.5", "0.5.1") is True

    def test_a_prerelease_is_not_announced_to_a_release_user(self):
        """ "0.6.0-rc1 is out" is noise unless they asked to follow development."""
        assert is_newer("0.5.0", "0.6.0-rc1") is False

    def test_a_release_supersedes_its_own_prerelease(self):
        assert is_newer("0.6.0-rc1", "0.6.0") is True

    def test_an_empty_candidate_is_never_newer(self):
        assert is_newer("0.5.0", "") is False

    def test_unparseable_versions_do_not_raise(self):
        assert parse_version("nonsense") == ((), False)
        assert is_newer("nonsense", "also-nonsense") is False


class TestNoticePolicy:
    def _notices(self, tmp_path) -> ReleaseNotices:
        return ReleaseNotices(tmp_path / "release-notice.json")

    def test_the_first_notice_is_allowed(self, tmp_path):
        state = self._notices(tmp_path).state("0.5.0", now=1000.0)
        assert state["can_announce"] is True
        assert state["announcements"] == 0

    def test_it_stops_after_the_cap(self, tmp_path):
        notices = self._notices(tmp_path)
        for index in range(MAX_ANNOUNCEMENTS):
            moment = 1000.0 + index * (COOLDOWN_SECONDS + 1)
            state = notices.record("0.5.0", now=moment)
        assert state["announcements"] == MAX_ANNOUNCEMENTS
        later = notices.state("0.5.0", now=1000.0 + 10 * COOLDOWN_SECONDS)
        assert later["can_announce"] is False
        assert later["remaining"] == 0

    def test_it_waits_between_two_mentions(self, tmp_path):
        notices = self._notices(tmp_path)
        notices.record("0.5.0", now=1000.0)
        soon = notices.state("0.5.0", now=1000.0 + 60)
        assert soon["can_announce"] is False
        assert soon["next_in_seconds"] > 0
        later = notices.state("0.5.0", now=1000.0 + COOLDOWN_SECONDS)
        assert later["can_announce"] is True

    def test_a_new_version_starts_again(self, tmp_path):
        """A genuinely new release is a new thing to say."""
        notices = self._notices(tmp_path)
        for index in range(MAX_ANNOUNCEMENTS):
            notices.record("0.5.0", now=1000.0 + index * (COOLDOWN_SECONDS + 1))
        assert notices.state("0.5.0", now=10_000.0)["can_announce"] is False
        fresh = notices.state("0.6.0", now=10_000.0)
        assert fresh["can_announce"] is True
        assert fresh["announcements"] == 0

    def test_the_count_survives_a_restart(self, tmp_path):
        """Otherwise every launch is the first launch."""
        self._notices(tmp_path).record("0.5.0", now=1000.0)
        assert self._notices(tmp_path).state("0.5.0", now=1001.0)["announcements"] == 1

    def test_a_corrupt_record_reads_as_nothing_said(self, tmp_path):
        path = tmp_path / "release-notice.json"
        path.write_text("{not json", encoding="utf-8")
        state = ReleaseNotices(path).state("0.5.0", now=1000.0)
        assert state["can_announce"] is True

    def test_recording_writes_something_readable(self, tmp_path):
        path = tmp_path / "release-notice.json"
        ReleaseNotices(path).record("0.5.0", now=1000.0)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["version"] == "0.5.0"
        assert stored["announcements"] == 1

    def test_an_empty_version_is_never_announced(self, tmp_path):
        assert self._notices(tmp_path).state("")["can_announce"] is False
        assert self._notices(tmp_path).record("")["announcements"] == 0


class TestWatcher:
    def _watcher(self, settings, payload, *, calls=None, current="0.5.0"):
        def fetcher(url):
            if calls is not None:
                calls.append(url)
            return payload

        return ReleaseWatcher(settings, fetcher=fetcher, current=current)

    def test_a_newer_release_is_available(self, settings):
        watcher = self._watcher(settings, release_payload("v0.6.0"))
        state = watcher.check()
        assert state["available"] is True
        assert state["latest"]["version"] == "0.6.0"
        assert state["can_announce"] is True

    def test_the_running_version_is_reported(self, settings):
        watcher = self._watcher(settings, release_payload("v0.6.0"), current=__version__)
        assert watcher.check()["current"] == __version__

    def test_the_same_version_is_not_available(self, settings):
        state = self._watcher(settings, release_payload("v0.5.0")).check()
        assert state["available"] is False
        assert state["can_announce"] is False

    def test_the_lookup_is_cached(self, settings):
        """The tray asks every couple of seconds; GitHub is not free."""
        calls: list[str] = []
        watcher = self._watcher(settings, release_payload("v0.6.0"), calls=calls)
        watcher.check()
        watcher.check()
        watcher.check()
        assert len(calls) == 1

    def test_it_can_be_forced_for_a_check_now(self, settings):
        calls: list[str] = []
        watcher = self._watcher(settings, release_payload("v0.6.0"), calls=calls)
        watcher.check()
        watcher.check(force=True)
        assert len(calls) == 2

    def test_a_failed_lookup_is_reported_not_raised(self, settings):
        def broken(url):
            raise OSError("no network")

        state = ReleaseWatcher(settings, fetcher=broken, current="0.5.0").check()
        assert state["available"] is False
        assert "no network" in state["error"]

    def test_an_empty_answer_is_not_an_error_the_user_sees(self, settings):
        state = ReleaseWatcher(settings, fetcher=lambda url: {}, current="0.5.0").check()
        assert state["available"] is False
        assert state["latest"] is None

    def test_the_snapshot_never_touches_the_network(self, settings):
        calls: list[str] = []
        watcher = self._watcher(settings, release_payload("v0.6.0"), calls=calls)
        assert watcher.snapshot()["checked_at"] == 0.0
        assert calls == []

    def test_recording_a_notice_spends_one_announcement(self, settings):
        watcher = self._watcher(settings, release_payload("v0.6.0"))
        watcher.check()
        state = watcher.record_notice()
        assert state["announcements"] == 1
        assert state["remaining"] == MAX_ANNOUNCEMENTS - 1

    def test_the_budget_is_shared_through_the_data_directory(self, settings):
        """A second watcher — the server restarted — sees the same count."""
        first = self._watcher(settings, release_payload("v0.6.0"))
        first.check()
        first.record_notice()

        second = self._watcher(settings, release_payload("v0.6.0"))
        assert second.check()["announcements"] == 1
