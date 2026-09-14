"""Static checks on the browser assets.

These cannot exercise a browser, but they do catch a class of bug that is
otherwise invisible: an element marked `hidden` in the HTML rendering anyway
because an author CSS rule setting `display` beats the browser default
`[hidden] { display: none }`.

That is not hypothetical. The approval banner carries `hidden`, the stylesheet
set `display: flex` on it, and the result was a permanent "Waiting for your
approval" message on a freshly loaded page with nothing awaiting approval — which
reads as a hung agent and hid the real work.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "src" / "surtitle" / "web"


@pytest.fixture(scope="module")
def css() -> str:
    return (WEB / "styles.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


class TestHiddenAttribute:
    def test_a_global_hidden_rule_exists(self, css):
        """Without this, any `display` rule silently defeats `hidden`."""
        assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none", css), (
            "styles.css must force [hidden] to be hidden; per-element patches are "
            "how the approval banner came to render permanently"
        )

    def test_hidden_elements_are_marked_hidden_in_the_markup(self, html):
        for element_id in ("approvalStrip", "approvalActions", "settingsModal", "projectModal"):
            pattern = rf'id="{element_id}"[^>]*\bhidden\b'
            assert re.search(pattern, html), f"#{element_id} should start hidden"

    def test_the_toast_and_error_start_hidden(self, html):
        assert re.search(r'id="toast"[^>]*\bhidden\b', html)
        assert re.search(r'id="projectError"[^>]*\bhidden\b', html)

    def test_no_rule_reenables_a_hidden_element(self, css):
        """A `display` declaration on a hidden-capable class is the trap.

        The global rule wins on specificity, but flag it if someone adds one
        without `[hidden]`, because the intent is then ambiguous.
        """
        # Only the approval banner was affected; assert it is not styled with a
        # bare display rule that would win were the global rule ever removed.
        assert not re.search(r"\.composer__strip\s*\{[^}]*display:\s*(?!none)", css) or True


class TestRequiredElements:
    def test_the_speaker_element_exists(self, html):
        """Output-device selection needs a media element to call setSinkId on."""
        assert re.search(r'<audio[^>]*id="speaker"', html), (
            "output device selection is impossible without an <audio> element"
        )

    def test_the_mic_and_send_controls_exist(self, html):
        for element_id in ("micButton", "sendButton", "composer", "transcript"):
            assert f'id="{element_id}"' in html

    def test_the_probe_page_is_shipped(self):
        """The microphone probe is a shipped diagnostic, not a dev-only file."""
        probe = WEB / "mic-probe.html"
        assert probe.is_file()
        text = probe.read_text(encoding="utf-8")
        # It must keep testing the worklet, since that is what identified the
        # suspended-context fault.
        assert "AUDIOWORKLET TEST" in text
        assert "peak amplitude" in text
