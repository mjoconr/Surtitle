"""The tray icon's portable half: what it says, and when it gives up.

The Windows half — the ctypes window, the message pump, the shell calls — cannot
run here, and pretending otherwise with mocks of ``user32`` would test the mocks
rather than the icon. Those are verified on a real machine instead. What is
tested here is everything that decides *what* the user sees: the wording, the
menu, the numbers, and the discovery of a running server.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from surtitle.config import Settings
from surtitle.local_api import (
    ServerInstance,
    clear_instance,
    find_instance,
    instance_path,
    read_instance,
    write_instance,
)
from surtitle.tray import (
    ACTIONS,
    SurtitleTray,
    format_status,
    format_usage,
    human_count,
    menu_entries,
    summary_text,
    tooltip_text,
)

# The notification area truncates a tooltip at 128 wide characters including the
# terminator, and the shell silently drops an update that does not fit rather
# than shortening it — so the text has to be bounded here, not by luck.
TOOLTIP_LIMIT = 127


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Settings(DEEPSEEK_API_KEY="sk-test", SURTITLE_HOME=str(home))


@pytest.fixture
def snapshot():
    return {
        "app": "Surtitle",
        "version": "0.1.0",
        "pid": 4242,
        "host": "127.0.0.1",
        "port": 8765,
        "url": "http://127.0.0.1:8765",
        "started_at": 1789600000.0,
        "data_dir": "C:/Users/mike/AppData/Local/Surtitle",
        "model": "deepseek-flash",
        "voice_enabled": True,
        "voice_backends": {"stt": "deepgram", "tts": "local"},
        "deepseek_configured": True,
        "deepgram_configured": False,
        "sessions": 2,
        "usage": {
            "uptime_seconds": 3845,
            "model_calls": 37,
            "turns": 12,
            "tool_calls": 48,
            "errors": 1,
            "prompt_tokens": 132400,
            "completion_tokens": 21300,
            "reasoning_tokens": 4100,
            "cached_tokens": 118000,
            "total_tokens": 153700,
            "cost_usd": 0.4231,
            "priced": True,
        },
        "storage": {
            "projects": 3,
            "conversations": 14,
            "archived": 2,
            "messages": 512,
            "tool_calls": 348,
            "db_bytes": 4404019,
        },
    }


class TestTooltip:
    def test_nothing_answering_says_so(self):
        assert "starting" in tooltip_text(None)

    def test_carries_the_state_a_user_wants_at_a_glance(self, snapshot):
        text = tooltip_text(snapshot, version="0.1.0")
        assert "2 live" in text
        assert "12 turns" in text
        assert "153.7k tok" in text
        assert "$0.42" in text
        assert "1h 04m" in text

    def test_fits_the_shell_limit(self, snapshot):
        # Name plus every field at their widest.
        snapshot["sessions"] = 999
        snapshot["usage"]["total_tokens"] = 999_999_999
        snapshot["usage"]["cost_usd"] = 1234.56
        snapshot["usage"]["uptime_seconds"] = 359_999
        assert len(tooltip_text(snapshot, version="0.1.0")) <= TOOLTIP_LIMIT

    def test_an_unpriced_run_shows_no_money(self, snapshot):
        """A $0.00 would read as "free", which is a worse answer than silence."""
        snapshot["usage"]["priced"] = False
        assert "$" not in tooltip_text(snapshot)
        assert "$" not in summary_text(snapshot)


class TestMenu:
    def test_offers_the_core_actions_when_the_server_answers(self, snapshot):
        entries = menu_entries(snapshot)
        offered = {entry.action for entry in entries if entry.action}
        # The update rows depend on how Surtitle was installed (a git checkout
        # versus a release archive), so they are asserted on their own below.
        assert {"open", "status", "usage", "voice", "stop"} <= offered
        assert offered <= set(ACTIONS)

    def test_nothing_is_actionable_when_the_server_is_gone(self):
        entries = menu_entries(None)
        assert all(not entry.action or not entry.enabled for entry in entries)
        stop = next(entry for entry in entries if entry.action == "stop")
        assert stop.enabled is False

    def test_the_header_states_are_labels_not_commands(self, snapshot):
        entries = menu_entries(snapshot)
        assert entries[0].action is None and entries[0].enabled is False
        assert entries[1].action is None and entries[1].enabled is False

    def test_a_stale_menu_cannot_be_shown(self, snapshot):
        """The menu is rebuilt on every right-click, so it tracks live state."""
        # Row 1 is the summary line; row 0 is the name and never changes.
        before = menu_entries(snapshot)[1].label
        snapshot["sessions"] = 7
        assert before != menu_entries(snapshot)[1].label

    def test_labels_fit_a_menu_row(self, snapshot):
        for entry in menu_entries(snapshot):
            assert len(entry.label) <= 64

    def test_separators_carry_no_action(self, snapshot):
        for entry in menu_entries(snapshot):
            if entry.separator:
                assert entry.action is None


class TestStatusDialog:
    def test_reports_what_is_running_and_what_is_configured(self, snapshot):
        text = format_status(snapshot)
        assert "http://127.0.0.1:8765" in text
        assert "deepseek-flash" in text
        assert "deepgram → local" in text
        assert "Deepgram: missing" in text
        assert "4242" in text

    def test_reports_what_is_stored(self, snapshot):
        text = format_status(snapshot)
        assert "14 conversation(s)" in text
        assert "512 message(s)" in text
        assert "4.2 MB" in text

    def test_a_path_with_spaces_survives(self, snapshot):
        snapshot["data_dir"] = "C:/Users/Mike O'Connor/AppData/Local/Surtitle"
        assert "Mike O'Connor" in format_status(snapshot)


class TestUsageDialog:
    def test_splits_cached_from_uncached_input(self, snapshot):
        text = format_usage(snapshot)
        assert "132,400" in text
        assert "118,000 cached" in text
        assert "21,300" in text
        assert "4,100 reasoning" in text
        assert "153,700" in text

    def test_reports_work_not_just_tokens(self, snapshot):
        text = format_usage(snapshot)
        assert "37" in text  # model calls
        assert "12" in text  # turns
        assert "48" in text  # tool calls
        assert "Errors" in text

    def test_states_the_cost_and_where_the_rates_came_from(self, snapshot):
        text = format_usage(snapshot)
        assert "$0.42" in text
        assert "SURTITLE_PRICE_*" in text

    def test_refuses_to_estimate_an_unpriced_model(self, snapshot):
        snapshot["usage"]["priced"] = False
        text = format_usage(snapshot)
        assert "not priced" in text
        assert "$0.00" not in text


class TestHumanCount:
    def test_small_numbers_are_exact(self):
        assert human_count(823) == "823"

    def test_thousands(self):
        assert human_count(12_400) == "12.4k"

    def test_millions(self):
        assert human_count(1_800_000) == "1.8M"


class TestInstanceFile:
    def test_round_trip(self, settings):
        written = write_instance(settings, host="127.0.0.1", port=9123, version="0.1.0")
        read = read_instance(settings)
        assert read is not None
        assert (read.host, read.port, read.pid) == ("127.0.0.1", 9123, written.pid)
        assert read.url == "http://127.0.0.1:9123"

    def test_a_corrupt_note_reads_as_absent(self, settings):
        """A half-written file must not crash the command that reads it."""
        path = instance_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert read_instance(settings) is None

    def test_a_note_without_a_port_is_ignored(self, settings):
        path = instance_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1, "host": "127.0.0.1"}), encoding="utf-8")
        assert read_instance(settings) is None

    def test_clearing_removes_it(self, settings):
        write_instance(settings, host="127.0.0.1", port=9123, version="0.1.0")
        clear_instance(settings)
        assert read_instance(settings) is None

    def test_clearing_leaves_another_process_note_alone(self, settings):
        """Two installs can share a data directory; neither may erase the other."""
        write_instance(settings, host="127.0.0.1", port=9123, version="0.1.0", pid=1)
        clear_instance(settings)
        assert read_instance(settings) is not None

    def test_from_dict_tolerates_a_partial_payload(self):
        instance = ServerInstance.from_dict({"port": 9000})
        assert instance.port == 9000
        assert instance.pid == 0


class TestDiscovery:
    def test_a_live_server_is_found_through_its_note(self, settings, monkeypatch):
        write_instance(settings, host="127.0.0.1", port=9123, version="0.1.0")
        monkeypatch.setattr(
            "surtitle.local_api.fetch_status",
            lambda url, timeout=None: {"version": "0.1.0", "pid": 77} if "9123" in url else None,
        )
        found = find_instance(settings)
        assert found is not None
        assert found.port == 9123
        assert found.pid == 77

    def test_a_stale_note_is_discarded(self, settings, monkeypatch):
        """Nothing answering means the note is a lie, so it is removed."""
        write_instance(settings, host="127.0.0.1", port=9123, version="0.1.0")
        monkeypatch.setattr("surtitle.local_api.fetch_status", lambda url, timeout=None: None)
        assert find_instance(settings) is None
        assert read_instance(settings) is None

    def test_an_explicit_url_wins_over_the_note(self, settings, monkeypatch):
        write_instance(settings, host="127.0.0.1", port=9123, version="0.1.0")
        seen: list[str] = []

        def fake(url, timeout=None):
            seen.append(url)
            return {"pid": 5}

        monkeypatch.setattr("surtitle.local_api.fetch_status", fake)
        found = find_instance(settings, url="http://127.0.0.1:9999")
        assert found is not None and found.port == 9999
        assert seen == ["http://127.0.0.1:9999"]


class TestPolling:
    """The poller decides when the icon appears to stop working."""

    def test_a_responding_server_is_published(self, monkeypatch):
        payload = {"pid": 1, "sessions": 2}
        monkeypatch.setattr("surtitle.tray.fetch_status", lambda url, timeout=None: payload)
        tray = SurtitleTray("http://127.0.0.1:1", interval=0.001, live=True)
        thread = threading.Thread(target=tray._poll, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2.0
        while tray.snapshot is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert tray.snapshot == payload
        tray.stop()
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "the poller must exit when the icon is stopped"

    def test_repeated_failures_close_the_icon(self, monkeypatch):
        monkeypatch.setattr("surtitle.tray.fetch_status", lambda url, timeout=None: None)
        tray = SurtitleTray("http://127.0.0.1:1", interval=0.001, live=True)
        tray._poll()
        assert tray.server_gone is True
        assert tray.snapshot is None

    def test_a_server_that_goes_away_is_noticed(self, monkeypatch):
        replies = [{"pid": 1}, {"pid": 1}, None, None, None]
        monkeypatch.setattr(
            "surtitle.tray.fetch_status",
            lambda url, timeout=None: replies.pop(0) if replies else None,
        )
        tray = SurtitleTray("http://127.0.0.1:1", interval=0.001, live=True)
        tray._poll()
        assert tray.server_gone is True

    def test_a_server_that_never_answers_gives_up_after_the_grace_period(self, monkeypatch):
        monkeypatch.setattr("surtitle.tray.fetch_status", lambda url, timeout=None: None)
        monkeypatch.setattr("surtitle.tray._GRACE_SECONDS", 0.0)
        tray = SurtitleTray("http://127.0.0.1:1", interval=0.001)
        tray._poll()
        assert tray.server_gone is True

    def test_stopping_the_server_is_a_request_not_a_kill(self, monkeypatch):
        asked: list[str] = []
        monkeypatch.setattr(
            "surtitle.tray.request_shutdown", lambda url, timeout=None: asked.append(url) or True
        )
        tray = SurtitleTray("http://127.0.0.1:8765")
        tray._select("stop")
        assert asked == ["http://127.0.0.1:8765"]

    def test_a_refused_stop_is_reported_rather_than_ignored(self, monkeypatch):
        monkeypatch.setattr("surtitle.tray.request_shutdown", lambda url, timeout=None: False)
        shown: list[str] = []
        tray = SurtitleTray("http://127.0.0.1:8765")
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))
        tray._select("stop")
        assert shown and "did not accept" in shown[0]

    def test_opening_uses_the_url_that_answered(self, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr("surtitle.tray.open_browser", lambda url: opened.append(url))
        tray = SurtitleTray("http://127.0.0.1:9123")
        tray._select("open")
        assert opened == ["http://127.0.0.1:9123"]

    def test_an_unknown_action_is_ignored(self):
        SurtitleTray("http://127.0.0.1:1")._select("nonsense")


def test_every_offered_action_is_handled():
    """A menu item with no handler is a click that does nothing."""
    import inspect

    source = inspect.getsource(SurtitleTray._select)
    for action in ACTIONS:
        assert f'"{action}"' in source, f"the {action!r} menu item has no handler"


class TestLocalVoiceMenu:
    """The menu answers "is offline speech available?" without opening a dialog."""

    def test_offers_install_when_offline_speech_is_missing(self, snapshot):
        snapshot["local_voice"] = {"ready": False, "runtime": False, "models": True}
        entry = next(e for e in menu_entries(snapshot) if e.action == "voice")
        assert entry.enabled is True
        assert "Install" in entry.label

    def test_progress_is_in_the_label_and_a_second_click_is_refused(self, snapshot):
        snapshot["local_voice"] = {"ready": False, "install": {"running": True, "percent": 42.4}}
        entry = next(e for e in menu_entries(snapshot) if e.action == "voice")
        assert entry.enabled is False
        assert "42%" in entry.label

    def test_says_so_when_it_is_already_installed(self, snapshot):
        snapshot["local_voice"] = {"ready": True, "install": {"running": False}}
        entry = next(e for e in menu_entries(snapshot) if e.action == "voice")
        assert entry.enabled is False
        assert "installed" in entry.label.lower()

    def test_it_is_not_actionable_when_the_server_is_gone(self):
        entry = next(e for e in menu_entries(None) if e.action == "voice")
        assert entry.enabled is False


class TestLocalVoiceAction:
    def _tray(self, voice) -> SurtitleTray:
        tray = SurtitleTray("http://127.0.0.1:8765")
        tray._snapshot = {"local_voice": voice}
        return tray

    def test_it_asks_before_downloading(self, monkeypatch):
        tray = self._tray({"ready": False})
        asked: list[str] = []
        monkeypatch.setattr(tray, "_confirm", lambda text, title: asked.append(text) or True)
        monkeypatch.setattr(tray, "_message", lambda text, title: None)
        posted: list[str] = []
        monkeypatch.setattr(
            "surtitle.tray.request_voice_install",
            lambda url, timeout=None: posted.append(url) or {"started": True},
        )

        tray._select("voice")

        assert asked and "Download" in asked[0]
        assert posted == ["http://127.0.0.1:8765"]

    def test_a_cancelled_question_downloads_nothing(self, monkeypatch):
        tray = self._tray({"ready": False})
        monkeypatch.setattr(tray, "_confirm", lambda text, title: False)
        posted: list[str] = []
        monkeypatch.setattr(
            "surtitle.tray.request_voice_install",
            lambda url, timeout=None: posted.append(url) or {"started": True},
        )

        tray._select("voice")

        assert posted == []

    def test_an_installed_voice_is_not_reinstalled(self, monkeypatch):
        tray = self._tray({"ready": True})
        posted: list[str] = []
        monkeypatch.setattr(
            "surtitle.tray.request_voice_install",
            lambda url, timeout=None: posted.append(url) or {},
        )
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))

        tray._select("voice")

        assert posted == []
        assert shown and "already" in shown[0]

    def test_an_install_already_running_is_reported(self, monkeypatch):
        tray = self._tray({"ready": False})
        monkeypatch.setattr(tray, "_confirm", lambda text, title: True)
        monkeypatch.setattr(
            "surtitle.tray.request_voice_install", lambda url, timeout=None: {"started": False}
        )
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))

        tray._select("voice")

        assert shown and "already running" in shown[0]

    def test_a_server_that_stops_answering_is_reported(self, monkeypatch):
        tray = SurtitleTray("http://127.0.0.1:8765")
        tray._snapshot = None
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))

        tray._select("voice")

        assert shown and "not answering" in shown[0]

    def test_a_refused_request_is_reported(self, monkeypatch):
        tray = self._tray({"ready": False})
        monkeypatch.setattr(tray, "_confirm", lambda text, title: True)
        monkeypatch.setattr("surtitle.tray.request_voice_install", lambda url, timeout=None: None)
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))

        tray._select("voice")

        assert shown and "did not accept" in shown[0]

    def test_the_icon_is_asked_for_a_yes_or_no(self):
        """No icon means no confirmation, and no confirmation means no download."""
        tray = SurtitleTray("http://127.0.0.1:1")
        assert tray._confirm("text", "title") is False

    def test_the_confirmation_quotes_the_real_download_size(self, monkeypatch):
        """The models are hundreds of MB; a remembered round number would mislead."""
        tray = self._tray({"ready": False, "missing_bytes": 394_600_000})
        asked: list[str] = []
        monkeypatch.setattr(tray, "_confirm", lambda text, title: asked.append(text) or True)
        monkeypatch.setattr(tray, "_message", lambda text, title: None)
        monkeypatch.setattr(
            "surtitle.tray.request_voice_install",
            lambda url, timeout=None: {"started": True},
        )

        tray._select("voice")

        assert asked and "MB" in asked[0]

    def test_it_invents_no_size_when_the_server_did_not_say(self, monkeypatch):
        tray = self._tray({"ready": False})
        asked: list[str] = []
        monkeypatch.setattr(tray, "_confirm", lambda text, title: asked.append(text) or True)
        monkeypatch.setattr(tray, "_message", lambda text, title: None)
        monkeypatch.setattr(
            "surtitle.tray.request_voice_install",
            lambda url, timeout=None: {"started": True},
        )

        tray._select("voice")

        assert asked and "MB" not in asked[0]


class TestUpdateMenu:
    """Updating differs by install kind, so the rows do too."""

    def test_a_git_checkout_is_offered_release_and_main(self, snapshot):
        snapshot["update"] = {"kind": "git", "job": {"running": False}}
        offered = {e.action for e in menu_entries(snapshot) if e.action}
        assert {"update_release", "update_main"} <= offered
        assert "update_page" not in offered

    def test_a_release_archive_is_offered_the_download_page(self, snapshot):
        """It cannot replace its own running files, so it must not pretend to."""
        snapshot["update"] = {"kind": "archive", "job": {"running": False}}
        offered = {e.action for e in menu_entries(snapshot) if e.action}
        assert "update_page" in offered
        assert "update_main" not in offered

    def test_a_self_updating_archive_is_offered_an_in_place_update(self, snapshot):
        """It stages the release and swaps itself in, so no browser trip is needed."""
        snapshot["update"] = {"kind": "archive", "self_update": True, "job": {"running": False}}
        offered = {e.action for e in menu_entries(snapshot) if e.action}
        assert "update_release" in offered
        assert "update_page" not in offered, "there is nothing to download by hand"
        assert "update_main" not in offered, "an archive has no main to follow"

    def test_an_unpacked_source_tree_is_offered_the_download_page(self, snapshot):
        """No build marker to replace and no history to pull, so say where to get it."""
        snapshot["update"] = {"kind": "archive", "self_update": False, "job": {"running": False}}
        offered = {e.action for e in menu_entries(snapshot) if e.action}
        assert "update_page" in offered

    def test_an_update_in_flight_is_not_clickable_twice(self, snapshot):
        snapshot["update"] = {"kind": "git", "job": {"running": True}}
        entry = next(e for e in menu_entries(snapshot) if e.action == "update_release")
        assert entry.enabled is False
        assert "Updat" in entry.label

    def test_the_update_row_is_not_actionable_when_the_server_is_gone(self):
        entry = next(e for e in menu_entries(None) if e.action == "update_release")
        assert entry.enabled is False


class TestUpdateAction:
    def _tray(self) -> SurtitleTray:
        return SurtitleTray("http://127.0.0.1:8765")

    def _record(self, monkeypatch, tray, answer=None):
        asked: list[str] = []
        posted: list[str] = []
        monkeypatch.setattr(tray, "_confirm", lambda text, title: asked.append(text) or True)
        monkeypatch.setattr(tray, "_message", lambda text, title: None)
        monkeypatch.setattr(
            "surtitle.tray.request_update",
            lambda url, target="release", timeout=None: (
                posted.append(target) or (answer or {"started": True})
            ),
        )
        return asked, posted

    def test_the_question_names_the_destination(self, monkeypatch):
        tray = self._tray()
        asked, posted = self._record(monkeypatch, tray)

        tray._select("update_main")

        assert asked and "main" in asked[0]
        assert posted == ["main"]

    def test_the_release_target_asks_for_a_release(self, monkeypatch):
        tray = self._tray()
        asked, posted = self._record(monkeypatch, tray)

        tray._select("update_release")

        assert asked and "release" in asked[0]
        assert posted == ["release"]

    def test_cancelling_updates_nothing(self, monkeypatch):
        tray = self._tray()
        _, posted = self._record(monkeypatch, tray)
        monkeypatch.setattr(tray, "_confirm", lambda text, title: False)

        tray._select("update_release")

        assert posted == []

    def test_an_already_running_update_is_reported(self, monkeypatch):
        tray = self._tray()
        monkeypatch.setattr(tray, "_confirm", lambda text, title: True)
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))
        monkeypatch.setattr(
            "surtitle.tray.request_update",
            lambda url, target="release", timeout=None: {"started": False},
        )

        tray._select("update_release")

        assert shown and "already running" in shown[0]

    def test_the_download_page_opens_for_an_archive(self, monkeypatch):
        tray = self._tray()
        opened: list[str] = []
        monkeypatch.setattr("surtitle.tray.open_browser", lambda url: opened.append(url))

        tray._select("update_page")

        assert opened and "releases" in opened[0]


class TestShellMenu:
    """Launcher and sign-in rows, for a user who never ran an installer."""

    def test_a_downloaded_archive_is_offered_a_menu_entry_and_sign_in(self, snapshot):
        snapshot["shell"] = {"supported": True, "menu": False, "startup": False}
        entries = menu_entries(snapshot)
        assert "shell_menu" in {e.action for e in entries if e.action}
        sign_in = next(e for e in entries if e.action == "shell_startup")
        assert sign_in.label == "Start at sign-in"

    def test_an_existing_menu_entry_is_not_offered_again(self, snapshot):
        snapshot["shell"] = {"supported": True, "menu": True, "startup": True}
        entries = menu_entries(snapshot)
        assert "shell_menu" not in {e.action for e in entries if e.action}
        sign_in = next(e for e in entries if e.action == "shell_startup")
        assert "Don't start" in sign_in.label

    def test_nothing_is_offered_where_there_is_no_such_entry(self, snapshot):
        """Windows and macOS have launcher entries; Linux has neither."""
        snapshot["shell"] = {"supported": False, "menu": False, "startup": False}
        offered = {e.action for e in menu_entries(snapshot) if e.action}
        assert "shell_menu" not in offered
        assert "shell_startup" not in offered

    def test_the_rows_are_absent_when_the_server_says_nothing_about_them(self, snapshot):
        offered = {e.action for e in menu_entries(snapshot) if e.action}
        assert "shell_menu" not in offered


class TestShellAction:
    def _tray(self, startup: bool) -> SurtitleTray:
        tray = SurtitleTray("http://127.0.0.1:8765")
        tray._snapshot = {"shell": {"supported": True, "menu": True, "startup": startup}}
        return tray

    def _record(self, monkeypatch, tray, answer=None):
        asked: list[dict] = []
        shown: list[str] = []
        monkeypatch.setattr(
            "surtitle.tray.request_shell",
            lambda url, **kwargs: asked.append(kwargs) or (answer or {"ok": True}),
        )
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))
        return asked, shown

    def test_turning_sign_in_on_asks_for_on(self, monkeypatch):
        tray = self._tray(startup=False)
        asked, shown = self._record(monkeypatch, tray)

        tray._select("shell_startup")

        assert asked == [{"menu": None, "startup": True}]
        assert shown and "will start" in shown[0]

    def test_turning_sign_in_off_asks_for_off(self, monkeypatch):
        tray = self._tray(startup=True)
        asked, shown = self._record(monkeypatch, tray)

        tray._select("shell_startup")

        assert asked == [{"menu": None, "startup": False}]
        assert shown and "no longer" in shown[0]

    def test_adding_the_menu_entry_asks_for_it(self, monkeypatch):
        tray = self._tray(startup=False)
        asked, shown = self._record(monkeypatch, tray)

        tray._select("shell_menu")

        assert asked == [{"menu": True, "startup": None}]
        assert shown and "Start Menu entry" in shown[0]

    def test_a_refused_change_shows_the_reason(self, monkeypatch):
        tray = self._tray(startup=False)
        _, shown = self._record(monkeypatch, tray, answer={"ok": False, "error": "not writable"})

        tray._select("shell_menu")

        assert shown and "not writable" in shown[0]

    def test_a_server_that_does_not_answer_is_reported(self, monkeypatch):
        tray = self._tray(startup=False)
        monkeypatch.setattr("surtitle.tray.request_shell", lambda url, **kwargs: None)
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))

        tray._select("shell_menu")

        assert shown and "did not accept" in shown[0]


class TestVersionControlMenu:
    """The row that answers "can the agent use git on this machine?"."""

    def _entry(self, snapshot, vcs=None):
        if vcs is not None:
            snapshot["vcs"] = vcs
        return next(e for e in menu_entries(snapshot) if e.action == "vcs")

    def test_offers_the_download_when_both_are_missing(self, snapshot):
        entry = self._entry(
            snapshot,
            {"tools": [{"name": "git", "available": False}, {"name": "svn", "available": False}]},
        )
        assert entry.enabled is True
        assert "Install" in entry.label
        assert "git" in entry.label and "svn" in entry.label

    def test_names_only_what_is_missing(self, snapshot):
        entry = self._entry(
            snapshot,
            {
                "tools": [
                    {"name": "git", "available": True, "source": "portable"},
                    {"name": "svn", "available": False},
                ]
            },
        )
        assert "missing svn" in entry.label
        assert "missing git" not in entry.label

    def test_progress_is_in_the_label_and_a_second_click_is_refused(self, snapshot):
        entry = self._entry(snapshot, {"install": {"running": True, "percent": 63.2}})
        assert entry.enabled is False
        assert "63%" in entry.label

    def test_says_so_when_both_are_already_there(self, snapshot):
        entry = self._entry(
            snapshot,
            {
                "tools": [
                    {"name": "git", "available": True, "source": "portable"},
                    {"name": "svn", "available": True, "source": "portable"},
                ]
            },
        )
        assert entry.enabled is False
        assert "portable" in entry.label

    def test_it_is_not_actionable_when_the_server_is_gone(self):
        entry = next(e for e in menu_entries(None) if e.action == "vcs")
        assert entry.enabled is False

    def test_a_snapshot_without_the_key_still_offers_a_disabled_row(self, snapshot):
        """An older server, or the moment before the first poll."""
        entry = next(e for e in menu_entries(snapshot) if e.action == "vcs")
        assert entry.enabled is False


class TestVersionControlAction:
    def _tray(self, vcs) -> SurtitleTray:
        tray = SurtitleTray("http://127.0.0.1:8765")
        tray._snapshot = {"vcs": vcs}
        return tray

    def test_elsewhere_it_points_at_the_package_manager(self, monkeypatch):
        """The portable builds exist for Windows; pretending otherwise is worse."""
        monkeypatch.setattr("surtitle.tray.is_windows", lambda: False)
        tray = self._tray({"tools": [{"name": "git", "available": False}]})
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))

        tray._select("vcs")

        assert shown and "only published for Windows" in shown[0]

    def test_it_asks_before_downloading(self, monkeypatch):
        monkeypatch.setattr("surtitle.tray.is_windows", lambda: True)
        tray = self._tray({"tools": [{"name": "git", "available": False}]})
        asked: list[str] = []
        monkeypatch.setattr(tray, "_confirm", lambda text, title: asked.append(text) or True)
        monkeypatch.setattr(
            "surtitle.tray.request_vcs_install", lambda url, timeout=None: {"started": True}
        )
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))

        tray._select("vcs")

        assert asked and "git" in asked[0]
        assert shown and "background" in shown[0]

    def test_a_refusal_stops_before_the_request(self, monkeypatch):
        monkeypatch.setattr("surtitle.tray.is_windows", lambda: True)
        tray = self._tray({"tools": [{"name": "git", "available": False}]})
        monkeypatch.setattr(tray, "_confirm", lambda text, title: False)
        called: list[int] = []
        monkeypatch.setattr(
            "surtitle.tray.request_vcs_install", lambda url, timeout=None: called.append(1)
        )

        tray._select("vcs")

        assert called == []

    def test_nothing_to_do_is_said_rather_than_downloaded(self, monkeypatch):
        monkeypatch.setattr("surtitle.tray.is_windows", lambda: True)
        tray = self._tray(
            {"tools": [{"name": "git", "available": True}, {"name": "svn", "available": True}]}
        )
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))

        tray._select("vcs")

        assert shown and "already installed" in shown[0]


class TestReleaseNotice:
    """The tray is what tells a Windows user a new version exists."""

    def _snapshot_with(self, snapshot, *, available=True, can_announce=True, kind="git"):
        snapshot["release"] = {
            "available": available,
            "can_announce": can_announce,
            "latest": {"version": "0.5.0"} if available else None,
        }
        snapshot["update"] = {"kind": kind, "self_update": kind != "git"}
        return snapshot

    def test_the_version_number_is_in_the_menu_row(self, snapshot):
        entry = next(
            e for e in menu_entries(self._snapshot_with(snapshot)) if "is available" in e.label
        )
        assert "0.5.0" in entry.label

    def test_an_installable_update_offers_the_update_action(self, snapshot):
        entry = next(
            e for e in menu_entries(self._snapshot_with(snapshot)) if "is available" in e.label
        )
        assert entry.action == "update_release"

    def test_an_unpacked_source_tree_is_sent_to_the_download_page(self, snapshot):
        """It cannot replace itself, so offering that would be a lie."""
        snapshot = self._snapshot_with(snapshot, kind="archive")
        snapshot["update"] = {"kind": "archive", "self_update": False}
        entry = next(e for e in menu_entries(snapshot) if "is available" in e.label)
        assert entry.action == "update_page"
        assert "download" in entry.label

    def test_no_row_when_there_is_nothing_new(self, snapshot):
        snapshot = self._snapshot_with(snapshot, available=False)
        assert not [e for e in menu_entries(snapshot) if "is available" in e.label]


class TestReleaseNoticeDelivery:
    def _tray(self, monkeypatch, payload, *, balloon_ok=True):
        tray = SurtitleTray("http://127.0.0.1:8765")
        monkeypatch.setattr("surtitle.tray.fetch_release", lambda url, timeout=None: payload)
        shown: list[str] = []
        monkeypatch.setattr(tray, "_message", lambda text, title: shown.append(text))

        class Icon:
            def balloon(self, text, title=None):
                shown.append(text)
                return balloon_ok

        tray._icon = Icon()
        return tray, shown

    def test_a_new_release_is_announced_and_counted(self, monkeypatch):
        payload = {
            "available": True,
            "can_announce": True,
            "latest": {"version": "0.5.0"},
        }
        tray, shown = self._tray(monkeypatch, payload)
        counted: list[str] = []
        monkeypatch.setattr(
            "surtitle.tray.acknowledge_release", lambda url, timeout=None: counted.append(url)
        )

        tray._check_release()

        assert shown and "0.5.0" in shown[0]
        assert counted == ["http://127.0.0.1:8765"]

    def test_nothing_is_counted_when_the_budget_is_spent(self, monkeypatch):
        """Otherwise the last three announcements would be spent in one poll."""
        payload = {"available": True, "can_announce": False, "latest": {"version": "0.5.0"}}
        tray, shown = self._tray(monkeypatch, payload)
        counted: list[int] = []
        monkeypatch.setattr(
            "surtitle.tray.acknowledge_release", lambda url, timeout=None: counted.append(1)
        )

        tray._check_release()

        assert shown == []
        assert counted == []

    def test_nothing_is_announced_when_there_is_no_new_version(self, monkeypatch):
        tray, shown = self._tray(monkeypatch, {"available": False, "can_announce": False})
        counted: list[int] = []
        monkeypatch.setattr(
            "surtitle.tray.acknowledge_release", lambda url, timeout=None: counted.append(1)
        )

        tray._check_release()

        assert shown == []
        assert counted == []

    def test_a_refused_balloon_falls_back_to_a_box(self, monkeypatch):
        payload = {"available": True, "can_announce": True, "latest": {"version": "0.5.0"}}
        tray, shown = self._tray(monkeypatch, payload, balloon_ok=False)
        monkeypatch.setattr("surtitle.tray.acknowledge_release", lambda url, timeout=None: None)

        tray._check_release()

        assert shown and "0.5.0" in shown[-1]

    def test_the_check_is_not_made_on_every_poll(self, monkeypatch):
        """The server caches the lookup; the tray must not ask GitHub per poll."""
        tray = SurtitleTray("http://127.0.0.1:8765")
        monkeypatch.setattr(tray, "_check_release", lambda: None)
        threads: list[object] = []
        monkeypatch.setattr(
            "surtitle.tray.threading.Thread",
            lambda **kwargs: threads.append(kwargs) or _NoThread(),
        )

        tray._maybe_notice_release()
        tray._maybe_notice_release()
        tray._maybe_notice_release()

        assert len(threads) == 1


class _NoThread:
    """Stands in for a thread that has already finished."""

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return False
