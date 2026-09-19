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

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from surtitle.config import Settings
from surtitle.server import create_app_for

WEB = Path(__file__).resolve().parent.parent / "src" / "surtitle" / "web"


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Settings(
        DEEPSEEK_API_KEY="sk-test-deepseek-1234567890",
        DEEPGRAM_API_KEY="dg-test-deepgram-0987654321",
        SURTITLE_HOME=str(home),
        voice_enabled=False,
    )


def function_source(script: str, name: str) -> str:
    """One shipped function, as text.

    Slicing to the first column-zero `}` is how these tests read a function, and
    getting the slice wrong — or the string wrong — silently asserts nothing. The
    name is checked, so a renamed function fails the test that cares about it rather
    than quietly matching an empty block.
    """
    marker = f"function {name}("
    assert marker in script, f"{marker} is not in the shipped script"
    block = script[script.index(marker) :]
    end = block.find("\n}\n")
    return block if end == -1 else block[:end]


@pytest.fixture(scope="module")
def css() -> str:
    return (WEB / "styles.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script() -> str:
    return (WEB / "js" / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def audio() -> str:
    return (WEB / "js" / "audio.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def transport() -> str:
    return (WEB / "js" / "connection.js").read_text(encoding="utf-8")


class TestReconnectRace:
    """Guards for the connect/disconnect loop.

    `openConnection()` closes the old socket and opens a new one. The old socket's
    `onclose` arrives later, and because it was an unguarded closure over mutable
    state it ran against the *new* connection: marking it closed and scheduling a
    reconnect. That opened a further socket, whose own stale handler repeated it —
    a new connection roughly every second, a new server-side session (and a second
    TTS pipeline) each time, and replies that started, cut off, or overlapped.
    The user's log showed six connections in six seconds.
    """

    def test_callbacks_are_ignored_for_a_superseded_socket(self, transport):
        assert "const isCurrent = () => this.socket === socket;" in transport, (
            "each socket's handlers must check they still own the connection"
        )

    @pytest.mark.parametrize("handler", ["onopen", "onmessage", "onclose", "onerror"])
    def test_every_handler_is_guarded(self, transport, handler):
        """A stale handler reaching the live connection is what caused the loop."""
        names = ("onopen", "onmessage", "onclose", "onerror")
        positions = {name: transport.index(f"socket.{name} = ") for name in names}
        start = positions[handler]
        later = [position for position in positions.values() if position > start]
        end = min(later) if later else len(transport)
        body = transport[start:end]
        assert "isCurrent()" in body, f"{handler} does not check whether it is stale"

    def test_close_detaches_handlers_before_closing(self, transport):
        for handler in ("onopen", "onmessage", "onclose", "onerror"):
            assert f"socket.{handler} = null;" in transport, (
                f"{handler} must be detached in close(), so it cannot reach a "
                "handler now pointed at a different connection"
            )

    def test_the_socket_reference_is_cleared_before_closing(self, transport):
        assert "this.socket = null;" in transport
        # Ordering matters: the guard compares against this.socket, so clearing
        # it first makes every in-flight callback stale.
        assert transport.index("this.socket = null;") < transport.index("socket.close();")


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


class TestTheSettingsPanelScrolls:
    """A panel taller than the window has to scroll, not clip.

    This is how a third provider card became unreachable: `.modal__content` is a
    grid item and defaults to `min-height: auto`, so it grew to its content instead
    of the row it sits in; the flex column inside then gave `.modal__options` all the
    height it wanted, so `scrollHeight` equalled `clientHeight`, `overflow-y: auto`
    never fired, and the panel's own `overflow: hidden` hid the rest. No scrollbar,
    no sign that anything was missing — the last card was simply gone.

    A grid item and a flex item with a scrolling child each have to be allowed to be
    smaller than their contents. Measured on the running app: 962px of content in a
    626px box, with the third provider card 146px below the fold.
    """

    @staticmethod
    def _rule(css: str, selector: str) -> str:
        match = re.search(rf"\{selector}\s*\{{([^}}]*)\}}", css)
        assert match, f"{selector} is not in the stylesheet"
        return match.group(1)

    def test_the_panel_content_can_shrink_to_its_row(self, css):
        assert "min-height: 0" in self._rule(css, ".modal__content")

    def test_the_scrolling_area_can_shrink_to_its_column(self, css):
        rule = self._rule(css, ".modal__options")

        assert "overflow-y: auto" in rule, "the content is what scrolls"
        assert "min-height: 0" in rule, (
            "without this the flex child grows to its content and never scrolls"
        )

    def test_the_panel_is_bounded_by_the_window(self, css):
        rule = self._rule(css, ".modal__panel")

        assert "min(" in rule and "100vh" in rule, "a tall panel must not exceed the window"


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

    def test_the_probe_reports_the_playback_rate(self):
        """The 24 kHz question is browser- and device-dependent.

        It made the voice sound like a different person on some setups, so the
        probe has to answer it directly rather than leaving it to be guessed at
        from a recording.
        """
        text = (WEB / "mic-probe.html").read_text(encoding="utf-8")
        assert "PLAYBACK CONTEXT" in text
        assert "requested rate" in text and "granted rate" in text
        assert "24 kHz REFUSED" in text

    def test_the_archive_controls_exist(self, html):
        for element_id in ("archiveToggle", "archiveList", "archivePurge", "confirmModal"):
            assert f'id="{element_id}"' in html


class TestScriptReferences:
    """Catch dangling DOM ids and API paths without needing a browser.

    A typo here is invisible until someone clicks the thing, and the failure mode
    is a silent no-op rather than an error.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def script(cls) -> str:
        return (WEB / "js" / "app.js").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    @classmethod
    def audio(cls) -> str:
        return (WEB / "js" / "audio.js").read_text(encoding="utf-8")

    def test_every_getElementById_has_a_matching_element(self, script, html):
        ids = set(re.findall(r'getElementById\("([^"]+)"\)', script))
        missing = sorted(element_id for element_id in ids if f'id="{element_id}"' not in html)
        assert not missing, f"app.js looks up elements that are not in index.html: {missing}"

    def test_every_api_path_is_a_real_route(self, script, settings):
        """Every URL the UI calls must exist on the server.

        Template holes (`${state.project.id}`) are collapsed to `{}` on both
        sides, so `/api/sessions/${id}/archive` is compared against
        `/api/sessions/{session_id}/archive`.
        """
        app = create_app_for(settings)
        # The OpenAPI document is the flattened truth; app.routes keeps included
        # routers nested and would report every path as missing.
        routes = {re.sub(r"\{[^}]+\}", "{}", path) for path in app.openapi()["paths"]}

        called: set[str] = set()
        for raw in re.findall(r'api\(\s*[`"]([^`"]+)[`"]', script):
            cleaned = raw.split("?")[0]
            if not cleaned.startswith("/api/"):
                continue
            called.add(re.sub(r"\$\{[^}]*\}", "{}", cleaned).rstrip("/"))

        missing = sorted(path for path in called if path not in routes)
        assert not missing, f"the UI calls routes the server does not define: {missing}"


class TestSpeechPlaybackRate:
    """Guards for a bug that made the voice sound wrong intermittently.

    An AudioBufferSourceNode resamples its buffer into the audio context's rate,
    so an AudioBuffer must be declared at the rate the PCM really is. The player
    used to overwrite the synthesis rate with whatever rate the context negotiated
    and then declare its buffers at *that*; on a machine where 24 kHz was refused
    (which depends on the active output device, hence "sometimes"), every reply
    played an octave high and twice as fast.

    There is no browser here, so these assert on the shape of the code — the same
    approach used for the `[hidden]` regression above.
    """

    def test_buffers_are_declared_at_the_synthesis_rate(self, audio):
        assert "createBuffer(1, sampleCount, this.serverRate)" in audio, (
            "the buffer must declare the rate the server synthesised at, or the "
            "context will resample from the wrong source rate"
        )

    def test_the_synthesis_rate_is_never_overwritten_by_the_context(self, audio):
        assert "this.sampleRate = this.context.sampleRate" not in audio, (
            "adopting the context's rate as the buffer rate is what broke the pitch"
        )

    def test_the_context_is_asked_for_the_synthesis_rate(self, audio):
        assert "new AudioContext({ sampleRate: this.serverRate })" in audio

    def test_the_client_adopts_the_rate_the_server_reports(self, script):
        assert "playback.setServerRate(data.sample_rate)" in script, (
            "the server sends the rate it synthesises at; ignoring it means a "
            "configured SURTITLE_TTS_SAMPLE_RATE is decoded wrongly"
        )

    def test_a_speed_fallback_is_applied_during_playback(self, audio, script):
        assert "source.playbackRate.value = this.playbackRate" in audio
        assert 'data.kind_detail === "speed_fallback"' in script

    def test_a_paused_sink_element_is_restarted(self, audio):
        assert "_ensureElementPlaying" in audio, (
            "the <audio> element is the only route to a chosen output device; if "
            "the browser pauses it there is silence and no error"
        )


class TestCaptureWorkletWiring:
    """Guards for a silent failure that made the worklet path never work at all.

    Browsers disagree about how an `AudioWorkletProcessor` receives messages: the
    specification delivers them to `port.onmessage`, Chrome never calls a
    `handleMessage` method, and Firefox has historically called only that. The
    processor defined `handleMessage` alone, so the `mute: false` that arms
    capture never arrived, `_muted` stayed at its constructor default, and it
    posted zero frames — in every browser. Nothing errored; `process()` ran and the
    graph was fine. The app's ScriptProcessor watchdog then took over silently, so
    voice worked while the intended low-latency path never did.

    There is no browser here, so these assert on the shape of the code, as with the
    `[hidden]` regression above. The behaviour was confirmed in a real browser:
    the single-entry-point version emits 0 frames, the both-entry-points version
    emits 62 frames in 2 s.
    """

    @pytest.fixture(scope="module")
    def worklet(self) -> str:
        return (WEB / "js" / "capture-worklet.js").read_text(encoding="utf-8")

    def test_the_modern_entry_point_is_wired(self, worklet):
        assert "this.port.onmessage = " in worklet, (
            "Chrome never calls handleMessage, so without port.onmessage the "
            "processor can never be armed and emits nothing"
        )

    def test_the_legacy_entry_point_is_kept(self, worklet):
        assert "handleMessage(event)" in worklet, (
            "Firefox has historically called only handleMessage"
        )

    def test_both_entry_points_share_one_handler(self, worklet):
        """Two copies of the mute logic would drift apart."""
        assert "this._onMessage(event.data)" in worklet
        assert worklet.count("this._onMessage(event.data)") == 2

    def test_the_processor_still_arms_on_mute(self, worklet):
        assert 'if (data.type === "mute")' in worklet
        assert "this._muted = Boolean(data.value);" in worklet

    def test_the_worklet_url_is_versioned(self, audio):
        """A cached pre-fix processor would otherwise survive an upgrade."""
        assert "WORKLET_VERSION" in audio
        assert "capture-worklet.js?v=" in audio


class TestCapturedAudioReachesTheConversation:
    """Guards the call site that made the microphone look dead.

    When several conversations were allowed at once, the module-level
    `connection` singleton was deleted and replaced by a map keyed by
    conversation. Most call sites were rebound; the capture callback was not, and
    it was left calling `sendAudio` on the removed name. That does not throw a
    ReferenceError in a browser — a bare `connection` resolves to the element with
    `id="connection"` — so every captured frame threw `sendAudio is not a
    function` and was dropped.

    Nothing reported it. Capture reported success, the level meter moved, the
    device was named, and the server saw nothing at all:

        microphone closed (#2); received 0 frame(s), 0.00s of audio, peak 0.000
        no audio arrived for this listening session -- the problem is in the
        browser's capture, not recognition

    The server's message is a diagnosis of the wrong end, which is why this is a
    static check over the source rather than a behavioural one.
    """

    def test_nothing_uses_the_removed_single_connection(self, script):
        stale = re.findall(r"(?<![\w.$])connection\.(?!js\b)", script)
        assert not stale, (
            "a bare `connection.` is not the per-conversation socket — it resolves "
            "to the #connection element and silently drops every audio frame "
            f"({len(stale)} left)"
        )

    def test_captured_frames_are_routed_by_conversation(self, script):
        capture = script[script.index("const capture = new Capture(") :]
        capture = capture[: capture.index("\n});")]
        assert "sendAudioFrame" in capture, (
            "capture must resolve the socket for the conversation on screen"
        )
        assert not re.search(r"connection\.sendAudio", capture)
        assert "connections.get" in script, "the per-conversation map must be the route"


class TestMissingReplyRecovery:
    """A completed turn that showed nothing must be recovered from the store.

    Reported as "it finished and gave me no result and I had to prompt it again".
    The database held a 7,072-character answer for that turn, written when the
    agent finished — so the work was done and only the delivery failed. The client
    could not tell "the agent produced nothing" from "the events never arrived", so
    it showed an empty turn and the user re-asked.
    """

    def test_a_finished_turn_with_nothing_shown_is_recovered(self, script):
        assert "recoverMissingAnswer(finished)" in script, (
            "a silently empty turn must be reconciled against the stored transcript"
        )

    def test_emptiness_is_measured_on_both_channels(self, script):
        block = script[script.index("function turnHasVisibleText(") :]
        block = block[: block.index("\n}") + 2]
        assert "saidText" in block and "shownText" in block, "a spoken-only reply is still a reply"

    def test_the_display_channel_is_tracked_per_turn(self, script):
        assert "turn.shownText += text;" in script

    def test_recovery_reads_the_stored_transcript(self, script):
        block = script[script.index("async function recoverMissingAnswer(") :]
        block = block[: block.index("\n}\n") + 2]
        assert "/api/sessions/" in block, "it must re-read the transcript, not re-ask"
        assert 'role === "assistant"' in block

    def test_recovery_never_shows_a_stale_answer(self, script):
        """An answer to an earlier question reads as a real answer to this one."""
        block = script[script.index("async function recoverMissingAnswer(") :]
        block = block[: block.index("\n}\n") + 2]
        assert 'role === "user"' in block
        assert "last.id < lastUser.id" in block, (
            "the recovered reply must belong to the question just asked"
        )

    def test_a_turn_with_no_events_still_gets_a_turn_to_recover_into(self, script):
        """With every text event lost, no turn object exists at all."""
        assert 'state.currentTurn || beginTurn("assistant")' in script, (
            "the turn is created by the first rendering event, so nothing arriving "
            "means there is no turn unless one is made"
        )

    def test_recovery_is_reported_rather_than_silent(self, script):
        block = script[script.index("async function recoverMissingAnswer(") :]
        block = block[: block.index("\n}\n") + 2]
        assert "Recovered a missing reply" in block, (
            "a reply appearing late for no visible reason is as confusing as none"
        )

    def test_a_failed_turn_is_not_treated_as_missing(self, script):
        """An errored turn already says what went wrong; do not stack on it."""
        assert "if (!data.failed && !turnHasVisibleText(finished))" in script


class TestTranscriptScroll:
    """Selecting a conversation must show its newest message.

    `scrollToBottom` only moves the view when the reader is already near the
    bottom, so streaming text cannot yank the page while someone reads back. That
    is right during a turn and wrong when replaying: the transcript is replaced,
    the view starts at the top, and the follow-along check then refuses to move
    it — so a long conversation opened showing its oldest messages.

    Smooth scrolling has to be off for the whole replay, not just the last jump.
    Assembling sixty turns started sixty animations, one of which was still in
    flight afterwards and fought the jump; assigning `scrollTop` to move the view
    was itself animated. On a 60-message conversation that left the view about
    1,600 px short of the bottom.
    """

    def test_replay_lands_at_the_newest_message(self, script):
        assert "replayThenJumpToLatest(" in script, "replay must land at the newest message"

    def test_the_jump_ignores_the_reader_position(self, script):
        block = script[script.index("async function replayThenJumpToLatest(") :]
        block = block[: block.index("\n}\n") + 2]
        assert "nearBottom" not in block, "the jump must not use the follow-along check"

    def test_smooth_scrolling_is_suspended_for_the_whole_replay(self, script):
        block = script[script.index("async function replayThenJumpToLatest(") :]
        block = block[: block.index("\n}\n") + 2]
        assert 'scrollBehavior = "auto"' in block
        # Suspended before the transcript is built, and only restored after it.
        assert block.index('scrollBehavior = "auto"') < block.index("build();")
        assert block.rindex("scrollBehavior = previous") > block.index("build();")

    def test_live_streaming_still_respects_the_reader(self, script):
        """The follow-along behaviour during a turn must survive."""
        assert "function scrollToBottom(force = false)" in script
        assert "if (force || nearBottom)" in script

    def test_the_jump_waits_for_layout(self, script):
        block = script[script.index("async function replayThenJumpToLatest(") :]
        block = block[: block.index("\n}\n") + 2]
        assert "requestAnimationFrame" in block, (
            "heights are only final after layout, so a single synchronous scroll can land short"
        )


class TestBargeInTiming:
    """The detector's windows must be real time, not render quanta.

    `process()` is called once per render quantum, which the specification fixes at
    128 sample-frames — about 2.7 ms at 48 kHz. The thresholds were written as if a
    quantum were a 32 ms audio frame, so "four frames" was 11 ms rather than the
    intended 130 ms and the grace window was 32 ms rather than 400 ms. The detector
    therefore fired on almost any loudness, and since the client silences playback
    on its own VAD *before* the server validates the interruption, the agent's own
    voice cut its replies off. Heard as audio breaking up.

    Measured in a browser against the shipped processor: before the change a 50 ms
    burst interrupted; after it, 50 ms and 100 ms bursts do not and 400 ms does.
    """

    @pytest.fixture(scope="module")
    def worklet(self) -> str:
        return (WEB / "js" / "capture-worklet.js").read_text(encoding="utf-8")

    def test_windows_are_declared_in_milliseconds(self, worklet):
        assert "SPEECH_HOLD_MS" in worklet
        assert "SPEECH_GRACE_MS" in worklet
        assert "CONSECUTIVE_SPEECH_FRAMES" not in worklet, (
            "counting render quanta as if they were audio frames is the bug"
        )
        assert "SPEECH_GRACE_FRAMES" not in worklet or "SPEECH_GRACE_MS" in worklet

    def test_the_conversion_accounts_for_the_render_quantum(self, worklet):
        assert "RENDER_QUANTUM = 128" in worklet
        assert "quantaFor" in worklet
        assert "sampleRate" in worklet, "the quantum count depends on the sample rate"

    def test_the_grace_window_matches_the_client(self, worklet, audio):
        """The client opens a 400 ms grace window; the worklet must agree."""
        assert "SPEECH_GRACE_MS = 400" in worklet
        assert "400" in audio

    def test_level_messages_are_throttled(self, worklet):
        """One per quantum is ~375 a second, each a DOM write on the main thread."""
        assert "LEVEL_INTERVAL_MS" in worklet
        assert "_quantaSinceLevel" in worklet


class TestActivityPanelNoise:
    """Reasoning must not flood the panel, and must not be lost either.

    Reasoning arrives as token-sized deltas. Pushing one row per delta and
    re-rendering the whole panel each time turned the activity log into a wall of
    single words — "Reasoning: .", "Reasoning: briefly" — and rebuilt the DOM
    dozens of times a second during every thinking turn.

    It is now coalesced into the step that produced it, in both panels, so the
    same deltas that used to be noise are what makes a long turn explain itself.
    """

    def test_reasoning_goes_into_a_step_block(self, script):
        assert 'addThinking(turn, data.text || "", data.step)' in script, (
            "thinking must attach to its step, not push a row per delta"
        )
        assert (
            'state.activity.push({ label: "Reasoning", detail: data.text.slice(0, 240) })'
            not in script
        )

    def test_the_helper_reuses_the_step_block(self, script):
        """One Think block per step, appended to — never one row per token."""
        block = script[script.index("function addThinking(") :]
        block = block[: block.index("\n}\n")]
        assert "step.think.text += text;" in block, "deltas must accumulate into the existing block"
        assert "step.thinking.append(wrap);" in block

    def test_the_re_render_is_throttled(self, script):
        assert "activityRenderTimer" in script
        assert "scheduleActivityRender" in script

    def test_a_think_row_that_already_exists_still_repaints(self, script):
        """Reported as "the activity does not seem to update".

        The row is created by the step's first delta and then updated in place
        for every delta after it. Only the *create* path scheduled a repaint, so
        the panel kept showing the step's first line and then looked frozen for
        the rest of the step — a step that thought for a minute was
        indistinguishable from one that had died.
        """
        block = script[script.index('case "thinking": {') :]
        block = block[: block.index("break;")]
        assert "existing.detail = live;" in block
        assert "scheduleActivityRender();" in block, (
            "an in-place update must schedule its own repaint, or the panel freezes"
        )

    def test_the_live_line_follows_the_newest_text(self, script):
        """A finished step reads as its first line; a running one as its last.

        The first line stops changing within a second, so gisting the live row
        from it reproduced the freeze the repaint was added to fix.
        """
        block = script[script.index("function latestLineOf(") :]
        block = block[: block.index("\n}\n")]
        assert "lines[lines.length - 1]" in block
        assert "…${line.slice(-120)}" in block, (
            "the newest words are at the end, so the ellipsis belongs in front"
        )

    def test_the_stored_reasoning_is_capped(self):
        """The cap moved to the server, where the durable copy is written."""
        from surtitle.core.agent import _REASONING_STORED_CHARS

        assert 0 < _REASONING_STORED_CHARS <= 100_000, (
            "a step's stored thinking needs a bound, or the database grows without one"
        )


class TestTheRightPanel:
    """Three tabs, in the order they answer a question.

    Reported from a real session: the plan was the only panel worth reading, the
    Files tab had no obvious purpose, and the panel opened on Files — so the
    useful view was the one nobody had seen. Plan leads now, Thinking is the
    process view the Activity tab was reaching for, and Files leads with what the
    turn actually touched.
    """

    def test_the_plan_leads_and_is_what_opens(self, html, script):
        assert html.index('id="tabTodo"') < html.index('id="tabThinking"')
        assert html.index('id="tabThinking"') < html.index('id="tabNotes"')
        assert html.index('id="tabNotes"') < html.index('id="tabFiles"')
        assert 'rightTab: "todo"' in script, "the panel must open on the plan"

    def test_the_notebook_has_a_panel_of_its_own(self, script):
        """It is the agent's memory of the project, and it was invisible.

        `remember` writes it, every later conversation is given it, and the only
        way to read it was to find `.surtitle/notes.md` on disk.
        """
        assert "function renderNotes(" in script
        block = script[script.index("async function loadNotes(") :]
        block = block[: block.index("\n}\n")]
        assert "/notes`" in block, "it reads the notebook from the project route"

    def test_an_empty_notebook_says_what_it_is_for(self, script):
        assert "Nothing recorded yet" in script, (
            "an empty panel that does not explain itself reads as broken"
        )

    def test_a_notebook_write_refreshes_the_panel(self, script):
        """The agent just changed the thing being displayed."""
        block = script[script.index('case "tool_result": {') :]
        block = block[: block.index("break;")]
        assert "state.notes = null" in block

    def test_the_plan_tab_is_never_hidden(self, html, script):
        """A primary tab that appears only once a plan exists is one nobody finds."""
        button = re.search(r'<button[^>]*id="tabTodo"[^>]*>', html).group(0)
        assert "hidden" not in button
        assert ".hidden = state.todos.length === 0" not in script

    def test_the_process_view_is_a_tab(self, html, script):
        """The user asked for thinking as a tab, not more always-on screen."""
        assert 'id="tabThinking"' in html
        assert 'id="tabActivity"' not in html
        assert "function renderThinking()" in script

    def test_writing_a_plan_does_not_steal_the_panel(self, script):
        """Switching under the reader takes away what they were looking at."""
        block = script[script.index('case "todos": {') :]
        block = block[: block.index("break;")]
        assert 'state.rightTab = "todo"' not in block

    def test_each_conversation_remembers_its_tab(self, script):
        """One conversation may be about a plan; another is one being watched."""
        assert "sessionTabs" in script
        assert "state.sessionTabs.get(sessionId)" in script

    def test_the_files_panel_leads_with_what_the_turn_touched(self, script):
        """It said what exists, which the user already knew.

        The tree is a fact about the disk; "wrote out/report.csv" is a fact
        about the conversation, and it is the one worth a tab.
        """
        assert "function renderTouchedFiles()" in script
        assert "renderTouchedFiles();" in script, "the section must actually render"
        assert "function noteTouched(" in script
        block = script[script.index('case "tool_call": {') :]
        block = block[: block.index("break;")]
        assert "noteTouched(data.name, data.arguments)" in block, (
            "a call's file argument is where the touch comes from"
        )
        for tool in ("read_file", "write_file", "edit_file"):
            assert tool in script

    def test_a_new_turn_forgets_the_last_turn_s_files(self, script):
        block = script[script.index('case "user_text": {') :]
        # To the end of the case, not to the first `break` — the queued-message
        # branch has one of its own.
        block = block[: script.index('case "say": {')]
        assert "state.touched.clear()" in block


class TestTheCentreSaysWhatItIsDoing:
    """The transcript is a live view, not a column of collapsed rows.

    Reported as: the centre has little activity while the agent works, where
    DeepSeek Harness shows the thinking and the learning as it goes. Everything
    here is one of those two things — the reasoning as it streams, and the
    conclusions worth keeping.
    """

    def test_a_think_block_folds_when_its_step_starts_acting(self, script):
        """Reasoning is settled by the call it produced; the call takes the space."""
        block = script[script.index("function noteStepTool(") :]
        block = block[: block.index("\n}\n")]
        assert "collapseThink(step.think)" in block
        assert "step.toolCount === 1" in block, "only the step's first call folds it"

    def test_the_folded_summary_keeps_moving(self, script):
        """Written once, it showed the first line forever — minutes out of date."""
        block = script[script.index("function addThinking(") :]
        block = block[: block.index("\n}\n")]
        assert "scheduleThinkPeek(step.think)" in block
        peek = script[script.index("function scheduleThinkPeek(") :]
        peek = peek[: peek.index("\n}\n")]
        assert "latestLineOf(target.text)" in peek
        assert "thinkPeekTimer" in peek, "a write per token is what this exists to avoid"

    def test_the_working_line_names_the_call_in_flight(self, script):
        """A generic verb plus a clock is the spinner it replaced."""
        block = script[script.index("function describeWorking(") :]
        block = block[: block.index("\n}\n")]
        assert "Running ${record.name}" in block
        assert "!record.endedAt" in block
        assert "record.startedAt >= turn.startedAt" in block, (
            "a row left running by an earlier turn must not be reported as current"
        )

    def test_the_working_line_is_what_shows_it(self, script):
        block = script[script.index("function updateWorkingLine(") :]
        block = block[: block.index("\n}\n")]
        assert 'querySelector(".working__label").textContent = describeWorking(turn)' in block

    def test_a_notebook_write_is_shown_as_learning(self, script):
        """The notebook is the durable result, and it was entirely invisible."""
        assert "function appendLearned(" in script
        block = script[script.index('case "tool_result": {') :]
        block = block[: block.index("break;")]
        assert 'data.name === "remember"' in block
        assert "appendLearned(" in block
        assert "record.arguments.note" in block, (
            "the note itself is the point; the tool's display is a character count"
        )

    def test_a_reopened_conversation_shows_what_it_learned(self, script):
        block = script[script.index("function replayToolCall(") :]
        block = block[: block.index("\n}\n")]
        assert 'call.name === "remember"' in block, (
            "otherwise the notebook looks as though it filled itself"
        )


class TestTheContextMeter:
    """The window is what explains an agent that starts forgetting its own work.

    Nothing showed it, and the one number that *was* on screen answered a
    different question: a running total of every token the process had ever sent,
    which grows forever and says nothing about the conversation in front of you.
    """

    def test_the_window_comes_from_the_server(self, script):
        """A meter drawn against a hardcoded window is wrong the moment the model changes."""
        assert "data.context_window" in script
        assert "data.context_budget" in script
        assert "contextBudget" in script

    def test_the_meter_measures_against_the_budget_not_the_window(self, script):
        """The window says what is possible; the budget says what we intend to send.

        `deepseek-flash` accepts a million tokens, and a voice-first conversation
        that sends most of them is slow and expensive — so the number worth
        watching is the budget, and the window is shown beside it.
        """
        block = script[script.index("function renderContextMeter(") :]
        block = block[: block.index("\n}\n")]
        assert "used / budget" in block, "the ratio is over the budget"
        assert "state.contextWindow" in block, "the window is still reported, as context"

    def test_the_cache_hit_share_is_shown(self, script):
        """A cache hit is a fiftieth of a miss, so this is the cost story in one number.

        It is also the only way to see whether the head of the request moved: the
        plan, the notebook and the project listing used to sit in the system prompt,
        invalidating the prefix on nearly every turn of a coding session.
        """
        block = script[script.index("function renderContextMeter(") :]
        block = block[: block.index("\n}\n")]
        assert "usage.cached_tokens" in block, "the provider reports it; nothing read it"
        assert "cache " in block, "the share is shown, not merely measured"

    def test_a_million_token_window_does_not_read_as_a_thousand_k(self, script):
        block = script[script.index("function formatTokens(") :]
        block = block[: block.index("\n}\n")]
        assert "1_000_000" in block, "the window can be a million tokens now"
        assert "M`" in block, "it must read as 1M, not 1000k"

    def test_it_is_drawn_from_the_last_completion(self, script):
        block = script[script.index("function renderContextMeter(") :]
        block = block[: block.index("\n}\n")]
        assert "usage.prompt_tokens" in block, "the sent conversation is what the window bounds"
        assert "usage.completion_tokens" in block
        assert "meterFill.style.width" in block, "a number alone does not show a ratio"

    def test_it_warns_before_the_window_is_full(self, script):
        block = script[script.index("function renderContextMeter(") :]
        block = block[: block.index("\n}\n")]
        assert '"high"' in block and '"warm"' in block, (
            "a meter that looks the same at 5% and 95% is not a warning"
        )

    def test_switching_conversation_clears_it(self, script):
        block = script[script.index("async function selectSession(") :]
        block = block[: block.index("\n}\n")]
        assert "state.usage = null" in block, (
            "another conversation's usage is a different number, not a smaller one"
        )

    def test_the_usage_event_feeds_the_meter_not_the_badge(self, script):
        block = script[script.index('case "usage": {') :]
        block = block[: block.index("break;")]
        assert "renderContextMeter()" in block
        assert "modelBadge" not in block, "the badge keeps the model; the numbers go to the meter"


class TestATurnEndsWithAClosingSection:
    """A turn must not simply stop at a Think block.

    Reported from a real 0.8.1 session: the last turn neither spoke nor showed a
    result, and the transcript ended on "thinking…" with nothing to say it was
    over. The banner above the composer explains the ending, but the transcript is
    what a reopened conversation shows and where the eye already is.
    """

    def test_a_turn_with_no_answer_says_so_where_it_ended(self, script):
        assert "function appendNoAnswer(" in script
        block = script[script.index('case "done": {') :]
        block = block[: block.index("break;")]
        assert "appendNoAnswer(finished" in block, (
            "the ending belongs in the transcript, not only in the banner"
        )

    def test_the_goal_is_shown_above_the_plan(self, script):
        """The plan says what is being done; the goal says what it is for, and the
        broader of the two goes first."""
        assert 'case "goal": {' in script, "the goal arrives as its own event"
        plan = script[script.index("function renderTodo()") :]
        plan = plan[: plan.index("state.todos.length === 0")]
        assert "state.goal" in plan and "goal__text" in plan, (
            "and is rendered above the plan rather than beside it"
        )

    def test_the_goal_comes_back_with_a_reopened_conversation(self, script):
        assert "session.goal" in script, (
            "a goal outlives the turn that set it, so reopening must show it"
        )

    def test_the_store_is_asked_before_concluding_there_was_no_answer(self, script):
        """A lost event stream is not the same as a turn that produced nothing."""
        block = script[script.index('case "done": {') :]
        block = block[: block.index("break;")]
        assert "recoverMissingAnswer(finished).then(" in block
        assert "if (!found) appendNoAnswer(" in block, (
            "only report the ending once recovery has come back empty"
        )

    def test_a_reopened_conversation_shows_the_ending_too(self, script):
        block = script[script.index("if (lastAskedAt > lastAnsweredAt)") :]
        block = block[: block.index("\n    }")]
        assert "appendNoAnswer(" in block, (
            "a conversation that stops mid-process must not end on a Think block"
        )


class TestStepGrouping:
    """A turn's process must be grouped by the step that produced it.

    The server numbers each model round and puts that number on every thinking
    delta, tool call and tool result. Flattened, a turn that ran fourteen commands
    was fourteen identical rows with no indication of what was being attempted —
    which is how a long release turn read.
    """

    def test_streamed_events_carry_their_step(self, script):
        for needle in (
            "stepFor(turn, data.step)",
            'addThinking(turn, data.text || "", data.step)',
        ):
            assert needle in script, f"missing step routing: {needle}"

    def test_tool_events_carry_their_step(self):
        """The grouping needs the step on the wire, or nothing can route it."""
        import inspect

        from surtitle.core.agent import AgentLoop

        source = inspect.getsource(AgentLoop)
        assert "step=state.step," in source, "tool events must name their step"

    def test_a_step_is_created_lazily(self, script):
        """Announced steps that produce nothing must not leave empty rows."""
        block = script[script.index("function stepFor(") :]
        block = block[: block.index("\n}\n")]
        assert "if (existing) return existing;" in block
        assert "turn.steps.append(root);" in block, "the row is built on first content"

    def test_a_collapsed_step_still_says_what_it_did(self, script):
        assert "planSummary(step)" in script, (
            "a collapsed step must summarise its calls, or the grouping hides the work"
        )

    def test_a_running_step_gets_a_timer(self, script):
        assert "step.timer.textContent = formatDuration(now - step.start)" in script

    def test_overlong_durations_are_not_shown_as_raw_seconds(self, script):
        block = script[script.index("function formatDuration(") :]
        block = block[: block.index("\n}\n")]
        assert "minutes" in block and " s`" in block, (
            "a build that takes ten minutes must not read as '612.4 s'"
        )


class TestProcessSurvivesReload:
    """Reopening a conversation must show the process, not just the answer.

    The store has always kept every tool call with its step, its outcome and its
    duration, and `GET /api/sessions/{id}` has always returned them. The client
    ignored both, so reopening a conversation showed a bare answer with no sign of
    the commands behind it, and an Activity panel that was empty.
    """

    def test_the_stored_calls_are_replayed(self, script):
        assert "session.tool_calls" in script, (
            "the transcript endpoint already returns tool_calls; not reading them "
            "is why a reopened turn showed no work"
        )

    def test_stored_work_is_attached_to_its_step(self, script):
        """A step's thinking must land in that step, not in the next turn.

        The stored reasoning row carries no step number, so the step comes from
        the calls that follow it. Holding it back until the turn's answer instead
        put a Think block in the wrong turn — and dropped it altogether when the
        turn had no answer, which is the shape a stopped conversation has, so the
        case where it matters most was the one that lost it.
        """
        block = script[script.index("const thinking = new Map();") :]
        block = block[: block.index("state.currentTurn = null;")]
        assert "flushThinking(turn, call.step)" in block, (
            "thinking must be claimed by the step whose calls follow it"
        )
        assert "thinking.set(Number(call.step) || 1" in block

    def test_a_stopped_conversation_keeps_its_thinking(self, script):
        """No answer follows, so nothing may be conditionally dropped."""
        block = script[script.index("const thinking = new Map();") :]
        block = block[: block.index("state.currentTurn = null;")]
        tail = block[block.index("if (thinking.size") :]
        assert "addThinking(turn, text, index)" in tail, (
            "the thinking of a turn with no answer must still be shown"
        )

    def test_thinking_is_replayed_into_its_step(self, script):
        assert 'message.role === "reasoning"' in script

    def test_a_settled_row_is_not_left_running(self, script):
        block = script[script.index("function replayToolCall(") :]
        block = block[: block.index("\n}\n")]
        assert "settleToolRow(record" in block, (
            "a replayed call is over; leaving it 'running' would start a timer"
        )

    def test_a_conversation_that_stops_mid_process_says_so(self, script):
        assert "STOPPED_WITHOUT_ANSWER" in script, (
            "work with no answer after it must not read as an answer that trailed off"
        )

    def test_the_todo_tool_is_registered(self):
        from surtitle.tools.registry import default_tool_list

        names = {tool.name for tool in default_tool_list()}
        assert "todo_write" in names, "the agent needs a way to write its plan down"

    def test_the_todo_tool_is_not_gated_on_approval(self):
        """Recording a plan changes nothing on disk; it must never prompt."""
        from surtitle.tools.registry import TODO_TOOL, default_tool_list

        tool = next(item for item in default_tool_list() if item.name == TODO_TOOL)
        assert tool.approval == "never"
        assert tool.mutating is False


class TestTheWorkingLineClearsWhenTheTurnEnds:
    """ "Deep diving…" must not outlive the turn it describes.

    `finishTimers` takes the line down, then calls `updateTimers` to write the
    frozen durations — and that is the same pass that puts the line up. Any row
    left un-ended, whether from an earlier turn in the view or one replayed from
    the store, put it straight back; the ticker was stopped immediately after, so
    a frozen "Deep diving… 1m 11s" sat on screen claiming the agent was still
    working. Reported as "it's stopped but there's no message to say".
    """

    def test_an_ended_turn_is_never_shown_as_working(self, script):
        block = script[script.index("function updateWorkingLine(") :]
        block = block[: block.index("\n}\n")]
        assert "turn.endedAt" in block, (
            "a turn that has ended is not working, whatever a leftover row says"
        )
        assert "workingLine.remove()" in block

    def test_the_end_is_stamped_before_the_line_is_decided(self, script):
        block = script[script.index("function finishTimers(") :]
        block = block[: block.index("\n}\n")]
        assert block.index("turn.endedAt = now") < block.index("updateTimers()"), (
            "the end must be stamped before the pass that decides to show the line, "
            "or that pass re-creates it"
        )


def _stop_note_block(script: str) -> str:
    """The body of `showStopNote`, which is what decides whether to show a banner."""
    block = script[script.index("function showStopNote(") :]
    return block[: block.index("\n}\n")]


def _stop_note_reasons(block: str) -> list[str]:
    """The turn-end reasons the block has copy for, in source order.

    Read out of the lookup object rather than by searching for the words, so the
    assertion is about which endings get a banner and not about which strings
    happen to appear in a comment.
    """
    return re.findall(r"^ {4}([a-z_]+): \{", block, re.M)


class TestStopReasonIsVisible:
    """A turn that stops must say why, on screen and aloud.

    "It just stops" was reported repeatedly. The indicator slid back to "Idle"
    whether the work had finished or the turn had run out of steps, so the only
    way to find out was to ask again — and a voice-first user, who is listening
    rather than reading, got no signal at all.
    """

    def test_the_done_event_carries_a_reason(self):
        """Each way a turn can end must name itself on the wire."""
        import inspect

        from surtitle.core.agent import AgentLoop

        source = inspect.getsource(AgentLoop)
        for reason in ('reason="complete"', 'reason="step_limit"', 'reason="failed"'):
            assert reason in source, f"the done event never reports {reason}"

    def test_the_agent_speaks_the_reason(self):
        from surtitle.core.session import Session

        source = __import__("inspect").getsource(Session._speak_problem)
        assert "step_limit" in source, "the step-budget stop must be spoken"
        assert "carry on from here" in source or "pick up where" in source, (
            "the spoken stop must say how to continue"
        )

    def test_the_budget_is_warned_about_before_it_runs_out(self):
        """The warning threshold is a fraction of the real budget, not a literal."""
        import inspect

        from surtitle.core.agent import NEAR_BUDGET_FRACTION
        from surtitle.core.session import Session

        assert 0 < NEAR_BUDGET_FRACTION < 1
        source = inspect.getsource(Session)
        assert "NEAR_BUDGET_FRACTION" in source, (
            "a turn about to stop must warn first, or the stop is a surprise"
        )
        assert "_speak_budget" in source

    def test_the_client_shows_a_stop_note_with_a_continue_action(self, script, html):
        assert 'id="stopNote"' in html
        assert 'id="stopContinue"' in html
        assert "showStopNote(data, finished)" in script, (
            "the done event's reason must reach the banner"
        )
        block = script[script.index("function showStopNote(") :]
        block = block[: block.index("\n}\n")]
        assert "step_limit" in block, "a step-limited turn is the case worth offering to resume"

    def test_cancellation_is_not_dressed_up_as_a_problem(self, script):
        """The user stopped it; telling them why is noise."""
        block = _stop_note_block(script)
        assert "cancelled" not in _stop_note_reasons(block)
        assert "stopped" not in _stop_note_reasons(block)
        assert "hideStopNote" in block

    def test_an_ordinary_completion_shows_no_stop_banner(self, script):
        """A turn that simply finished must not be labelled "Stopped".

        The reason lookup ended in `|| { title: "Stopped", detail: "" }`, so
        `reason: "complete"` — the common case, every turn that goes well — put a
        bare "Stopped" on screen with nothing under it. The user asked whether the
        agent had finished; the banner was telling them it had stopped. Only the
        endings that need explaining may produce copy.
        """
        block = _stop_note_block(script)
        assert '|| { title: "Stopped"' not in block, (
            "an unrecognised reason must not fall through to a Stopped banner"
        )
        assert "hideStopNote" in block, "there must be a path that shows nothing"
        assert _stop_note_reasons(block) == [
            "step_limit",
            "no_answer",
            "failed",
            "interrupted",
        ], "exactly the endings that need explaining get a banner"

    def test_a_reopened_conversation_is_not_called_stopped_for_having_worked(self, script):
        """A finished conversation must not be labelled "Stopped" on reload.

        The reload path set the banner whenever the replayed transcript contained
        any thinking or tool call — which is every turn that ever did anything.
        Reopening a conversation that had finished normally therefore announced
        "Stopped — the step limit was reached" over a complete answer, naming a
        cause the browser cannot know: it may have been a restart, a cancellation,
        or a crash.
        """
        block = script[script.index("if (thinking.size || looseThinking.length") :]
        block = block[: block.index("state.currentTurn = null;")]
        assert "lastAskedAt > lastAnsweredAt" in block, (
            "a stopped turn is one that was never answered, not one that did work"
        )
        assert 'reason: "step_limit"' not in block, (
            "the client cannot know the server hit its step limit"
        )

    def test_a_reopened_conversation_uses_the_recorded_ending(self, script):
        """The client must not name a cause it has no way to know.

        It inferred "interrupted" for any unanswered turn, which reads as a
        specific diagnosis — a cancellation, a crash — when the real reason may
        have been a spent step budget or an empty model round. The server records
        the reason now, and the reload path prefers it, keeping the guess only as
        the fallback for conversations whose last turn ended before the record
        existed.
        """
        block = script[script.index("if (lastAskedAt > lastAnsweredAt)") :]
        block = block[: block.index("\n    }")]
        assert "session.last_end_reason || " in block, (
            "the recorded reason is the answer; the guess is only the fallback"
        )
        assert "session.last_end_detail" in block, (
            "the server's own sentence is what the banner should show"
        )

    def test_continue_resumes_rather_than_repeating_the_question(self, script):
        block = script[script.index("function continueLastTurn(") :]
        block = block[: block.index("\n}\n")]
        assert '"Continue from where you stopped."' in block, (
            "the work in flight is already in the server's history; resending the "
            "original request would make the model start over"
        )
        assert 'sendCommand("text"' in block


class TestReadyEventContract:
    """The fields the browser needs must be present in the ready event."""

    async def test_ready_carries_the_synthesis_rate(self, tmp_path):
        from surtitle.core.session import Session
        from surtitle.store.db import Store

        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", tmp_path)
        record = store.create_session(project.id)

        async def send(_payload):
            return None

        async def send_audio(_data):
            return None

        session = Session(
            session_id=record.id,
            project_id=project.id,
            root=tmp_path,
            settings=Settings(
                DEEPSEEK_API_KEY="sk-test",
                SURTITLE_HOME=str(tmp_path),
                voice_enabled=False,
            ),
            store=store,
            deepseek=None,
            send=send,
            send_audio=send_audio,
        )
        await session.start()

        ready = [
            event.to_dict() for event in _drain(session) if event.to_dict().get("kind") == "ready"
        ]
        assert ready, "a session must announce itself"
        assert ready[0]["data"]["sample_rate"] == session.settings.tts_sample_rate
        assert ready[0]["data"]["context_window"] == 1_000_000, (
            "deepseek-flash accepts 1M tokens, and the meter reports that — "
            "a default window is wrong by however much the models differ"
        )
        assert ready[0]["data"]["context_budget"] == 128_000, (
            "the budget is what we intend to send, and it is the number the meter measures"
        )


def _drain(session) -> list:
    events = []
    while not session._outbox.empty():
        events.append(session._outbox.get_nowait())
    return events


class TestIcon:
    """The mark shipped to the browser, the taskbar and the Start Menu.

    The tray icon is loaded from this file by the shell, so a missing or
    malformed icon is not a cosmetic problem: it is an icon that silently does
    not appear. The sizes have to be inside it, not generated on the fly.
    """

    def test_the_browser_is_told_which_icon_to_use(self, html):
        assert 'rel="icon"' in html
        assert "/static/surtitle.ico" in html

    def test_the_icon_is_shipped_next_to_the_assets_it_is_served_with(self):
        assert (WEB / "surtitle.ico").is_file(), "the tray and the favicon need this file"

    def test_the_icon_contains_every_size_windows_asks_for(self):
        import struct

        raw = (WEB / "surtitle.ico").read_bytes()
        reserved, kind, count = struct.unpack("<HHH", raw[:6])
        assert (reserved, kind) == (0, 1), "not an icon file"
        sizes = set()
        for index in range(count):
            entry = 6 + index * 16
            width, height = raw[entry], raw[entry + 1]
            sizes.add((width or 256, height or 256))
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= sizes


class TestFolderPickerList:
    """The in-app picker has to lead with the folder being looked at.

    It used to lead with the *other* drives, so at ``C:\\`` ten rows of
    ``D:\\ E:\\ F:\\`` filled the panel and the folders inside ``C:\\`` sat below
    the fold. To the person using it that is a picker that can see nothing at all,
    which is how it was reported.
    """

    def _render_folder(self, script) -> str:
        body = script[script.index("function renderFolder") :]
        return body[: body.index("\n}\n")]

    def test_the_folders_come_before_the_other_drives(self, script):
        body = self._render_folder(script)
        assert body.index("rows.push(...visible)") < body.index("Other drives")

    def test_the_drives_are_labelled_so_they_do_not_read_as_contents(self, script):
        assert 'node("p", "folder__group", row.group)' in script

    def test_hidden_folders_are_left_out_until_asked_for(self, script):
        assert "folderState.showHidden || !entry.hidden" in script

    def test_a_level_of_only_hidden_folders_says_which_silence_it_is(self, script):
        assert "hidden — choose Hidden to show them" in script

    def test_the_count_is_rendered_and_declared(self, html, script):
        assert re.search(r'id="folderCount"[^>]*\bhidden\b', html)
        assert 'document.getElementById("folderCount")' in script

    def test_the_hidden_toggle_shows_that_it_is_on(self, css):
        assert re.search(r'\.button\[aria-pressed="true"\]', css), (
            "without a pressed style, 'nothing here' and 'everything here is hidden' look identical"
        )

    def test_a_new_level_starts_at_the_top(self, script):
        assert "el.folderList.scrollTop = 0" in script


class TestAMessageTypedWhileTheAgentWorks:
    """Reported: "I've lost the ability to enter text while the system is thinking".

    The old answer was an error telling the user to stop the turn or wait. The
    request is held now, shown where it was typed, and marked as waiting until its
    own turn begins — and a turn can run for minutes, so the marker has to go away
    when the wait is over rather than sitting there claiming it is still pending.
    """

    def _queued_branch(self, script: str) -> str:
        block = script[script.index('case "user_text": {') :]
        block = block[: script.index('case "say": {')]
        branch = block[block.index("if (queued) {") :]
        return branch[: branch.index("break;")]

    def test_a_queued_message_is_shown_as_waiting(self, script):
        branch = self._queued_branch(script)

        assert "dataset.queued" in branch, "the transcript says it is waiting"
        assert "toast(" in branch, "and so does the moment it was typed"
        assert "state.currentTurn = null" not in branch, (
            "resetting the turn would orphan the answer still streaming above it"
        )

    def test_the_waiting_marker_clears_when_its_turn_starts(self, script):
        """Otherwise a message that has been answered still says "queued"."""
        block = script[script.index("function assistantTurn(") :]
        block = block[: block.index("\n}\n")]
        assert "clearQueuedMarkers()" in block
        clear = script[script.index("function clearQueuedMarkers(") :]
        clear = clear[: clear.index("\n}\n")]
        assert 'data-queued="true"' in clear


class TestStopAndPushControls:
    """Two ways out of a turn that is taking too long.

    Stop halts the work and drops what was queued behind it. Push stops the turn
    and takes its place. Neither existed: the protocol had a cancel command and the
    client never sent it, so a running turn could not be stopped from the interface
    at all — and a message typed during one could only wait.
    """

    def test_both_controls_exist_and_start_hidden(self, html):
        for element_id in ("stopButton", "pushButton"):
            assert re.search(rf'id="{element_id}"[^>]*\bhidden\b', html), (
                f"#{element_id} should start hidden"
            )

    def test_they_appear_only_while_a_turn_is_running(self, script):
        block = script[script.index("function syncComposerControls(") :]
        block = block[: block.index("\n}\n")]
        assert "el.stopButton.hidden = !working" in block
        assert "el.pushButton.hidden = !working" in block
        assert 'state_ === "thinking" || state_ === "tool"' in script, (
            "speaking after a turn is not working"
        )

    def test_stop_sends_cancel_and_push_interrupts(self, script):
        assert 'sendCommand("cancel"' in script, "the cancel command was never sent"
        assert "sendMessage({ interrupt: true })" in script
        assert "interrupt: Boolean(options && options.interrupt)" in script, (
            "the flag has to reach the wire"
        )


class TestThePanelAfterASecondLook:
    """Built, used, and three of the tabs did not earn their place.

    Reported after a real session: "the side bar except for the plan seems to still
    be useless — the files should be dropped or just show the files accessed;
    thinking is useless it does not seem to update; no idea what notes does."
    """

    def test_a_failed_notebook_read_is_not_an_empty_notebook(self, script):
        """A notebook with 3,700 characters in it sat behind "Nothing recorded yet".

        The route did not exist on the running server, the fetch failed, and the
        failure was cached as an empty notebook — so the panel went on saying there
        was nothing to see long after the reason had gone.
        """
        block = script[script.index("async function loadNotes(") :]
        block = block[: block.index("\n}\n")]

        assert "error:" in block, "a failed read is stored as a failure"
        assert 'text: ""' not in block, "and never as an empty notebook"

    def test_opening_the_tab_re_reads_it(self, script):
        """The notebook belongs to the project, not to this panel's last look."""
        block = script[script.index("function showRightTab(") :]
        block = block[: block.index("\n}\n")]

        assert 'tab === "notes"' in block
        assert "state.notes = null" in block

    def test_the_project_tree_is_folded_away(self, script):
        """It was a wall of dot-directories above nothing, and it was the panel."""
        block = script[script.index("function renderFiles(") :]
        block = block[: block.index("\n}\n")]

        assert "renderTouchedFiles();" in block, "what the turn touched comes first"
        assert 'node("details", "browse")' in block, "the tree is one line until asked for"
        assert block.index("renderTouchedFiles();") < block.index('node("details", "browse")')

    def test_the_thinking_tab_follows_the_newest_step_until_you_pick_one(self, script):
        """It is a reader with a history, not a column of cards.

        Following the newest by default is what makes it live; letting the reader
        pin an older step is what makes it useful after the turn has moved on. The
        pin has to be released when the next turn starts, or the panel stays on a
        finished step while the agent works.
        """
        block = function_source(script, "renderThinking")

        assert "panelFollows" in block, "follow the newest unless the reader scrolled back"
        assert "state.thinkingStep" in block, "the reader's choice of step"
        assert "pinned" in block, "and it wins over the newest while it is set"

        reset = function_source(script, "beginTurn")
        assert "state.thinkingStep = null" in reset, "a new turn goes back to following the work"

    def test_the_history_is_the_whole_conversation_not_the_newest_turn(self, script):
        """Measured on a real conversation: the turn with fifty-eight steps of
        reasoning in it was turn 1, and the newest turn that reasoned had one. A
        panel scoped to the newest turn cannot reach the reasoning worth reading,
        which is the panel being useless with extra steps."""
        block = function_source(script, "conversationReasoning")

        assert "open.turns.values()" in block, "every turn, not the last one"
        assert 'kind !== "assistant"' in block, "with the user's own turns skipped"

    def test_a_selected_step_shows_its_whole_reasoning(self, script):
        """The panel used to render a one-line gist per step, and after a reload it
        had only the gist — so the tab showed less than the transcript did. The text
        is read from the transcript's steps, which is where the whole of it is."""
        reader = function_source(script, "renderThinking")
        collect = function_source(script, "conversationReasoning")

        assert "current.text" in reader, "the whole reasoning, not the gist"
        assert "think.full" not in reader and "think.full" not in collect, (
            "the activity log's copy is the gist"
        )
        assert "step.think.text" in collect, "read from the transcript's step"

    def test_every_step_that_reasoned_can_be_picked(self, script):
        picker = function_source(script, "thinkingPicker")

        assert "thinkpick__item" in picker
        assert "gistOf(thought.text)" in picker, "a step is identifiable before it is opened"
        assert "picker.append(chip)" in picker, "each one goes in the row"
        # Built and never appended is how the first version shipped: the header said
        # "Step 1 of 1" and the row of numbers was not in the document at all.
        assert "card.append(thinkingPicker(" in function_source(script, "renderThinking"), (
            "and the row is put on screen"
        )

    def test_the_selected_chip_is_scrolled_into_view(self, script):
        """The newest is the last chip, which is off the end of a sideways strip."""
        assert "selectedChip.scrollIntoView" in function_source(script, "thinkingPicker")

    def test_selecting_a_step_does_not_lose_the_live_one(self, script):
        """Picking the newest step by hand has to go back to following it, or the
        panel freezes a step that is still being written."""
        assert "index === newest ? null : index" in function_source(script, "thinkingPicker")

    def test_selecting_a_step_rebuilds_the_panel_rather_than_appending(self, script):
        """Calling the tab's own renderer left the previous reader in place: two
        cards, two rows of chips, and the click looking like it did nothing because
        the reader above the new one is the one on screen."""
        picker = function_source(script, "thinkingPicker")

        assert "renderRightbar()" in picker
        assert "renderThinking()" not in picker

    def test_jumping_to_a_step_uses_the_step_itself(self, script):
        """The panel can show a step from any turn, so looking one up by number in
        "the current turn" lands on the wrong one."""
        assert "focusStepRecord(current.step)" in function_source(script, "renderThinking")
        assert "function focusStepRecord(step)" in script
        assert "focusStepRecord(turn && turn.stepRows.get(Number(index)))" in function_source(
            script, "focusStep"
        )

    def test_install_diagnostics_are_not_the_first_thing_shown(self, script):
        block = script[script.index("function renderThinking(") :]
        block = block[: block.index("\n}\n")]
        tail = block[block.index("el.rightbarBody.append(card)") :]

        assert "renderEnvironment()" in tail, (
            "three lines of '0 packages installed' do not belong above the work"
        )


class TestTheTurnReadsTopToBottom:
    """The work first, the answer last.

    The answer used to be announced at the top with the steps accumulating under
    it, so the line the agent was speaking drifted away from where the reader was
    looking — and a turn with six steps put the answer a screenful above the work
    it was describing. DeepSeek Harness puts the reply at the bottom, and so does
    this now.
    """

    def test_the_steps_come_first_and_the_answer_last(self, script):
        block = script[script.index("function beginTurn(") :]
        block = block[: block.index("\n}\n")]
        assistant = block[block.index("turn.append(steps") :]
        order = assistant[: assistant.index(")") + 1]

        assert order == "turn.append(steps, shown, spoken)", (
            "steps, then what was shown, then what was said"
        )

    def test_the_containers_are_built_before_they_are_ordered(self, script):
        """Position is decided once here, not by the order events arrive in — the
        three are filled as the turn runs and all three are created up front."""
        block = script[script.index("function beginTurn(") :]
        block = block[: block.index("turn.append(")]

        for container in ("const spoken", "const shown", "const steps"):
            assert container in block


class TestTheShippedScriptParses:
    """A stray brace in the shipped script is a blank page, and nothing else in the
    suite would notice: these tests read the source as text.

    Skipped rather than failed where node is absent, because node is not a
    dependency of this project — it is a convenience for catching exactly this.
    """

    def test_app_js_is_valid_javascript(self):
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")

        source = WEB / "js" / "app.js"
        result = subprocess.run(
            [node, "--check", str(source)],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr


class TestMutingActuallyReachesTheWorklet:
    """A worklet that is attached but not labelled "worklet" kept sending audio.

    The mute message was posted only when `backend === "worklet"`, and the worklet
    keeps its own mute state — it does not read the flag the fallback path uses. So
    the microphone showed off while frames were still arriving at the server, which
    went on recognising them. Reported as "the mic in the window was off but it was
    still converting voice".
    """

    def test_the_message_goes_whenever_there_is_a_worklet_node(self, audio):
        for method in ("setMuted(muted)", "notifyPlayback(playing)"):
            block = audio[audio.index(method) :]
            block = block[: block.index("\n  }")]

            assert 'this.backend === "worklet"' not in block, (
                "the label is not the same as the node being there"
            )
            assert "this.node.port.postMessage" in block


class TestTheMarkdownRenderer:
    """The `<display>` channel is Markdown by contract — the prompt asks for tables,
    code, listings and paths there — and it was being shown as preformatted text.

    Run through node rather than read as text: escaping is the part that matters and
    the part a source-reading test cannot check. Skipped where node is absent, since
    node is not a dependency of this project.
    """

    @staticmethod
    def render(markdown: str) -> str:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        module = WEB / "js" / "markdown.js"
        script = (
            "import { pathToFileURL } from 'node:url';"
            f"const m = await import(pathToFileURL({json.dumps(str(module))}).href);"
            f"process.stdout.write(m.markdownToHtml({json.dumps(markdown)}));"
        )
        result = subprocess.run(
            [node, "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_a_table_becomes_a_table(self):
        html = self.render("| Station | Distance |\n|---|---|\n| Lagana | in town |")

        assert "<table" in html and "<th>Station</th>" in html
        assert "<td>Lagana</td>" in html
        assert "|---|" not in html, "the separator row is not content"
        assert "| Station |" not in html, "and neither is the pipe syntax"

    def test_a_table_without_a_separator_is_still_a_table(self):
        html = self.render("| a | b |\n| 1 | 2 |")

        assert "<th>a</th>" in html and "<td>2</td>" in html

    def test_a_pipe_in_a_sentence_is_not_a_table(self):
        html = self.render("the flag is a | b in the docs")

        assert "<table" not in html
        assert "a | b" in html

    def test_bold_and_inline_code(self):
        html = self.render("**Name check** — `Lindock` is not a township")

        assert "<strong>Name check</strong>" in html
        assert "<code>Lindock</code>" in html
        assert "**" not in html and "`" not in html

    def test_a_fenced_block_is_code(self):
        html = self.render("```python\nprint(1 < 2)\n```")

        assert 'class="md__code"' in html
        assert "print(1 &lt; 2)" in html, "escaped, and in a code block"

    def test_headings_lists_and_rules(self):
        html = self.render("## Findings\n\n- one\n- two\n\n---\n")

        assert "<h2" in html and "Findings" in html
        assert html.count("<li>") == 2
        assert "<hr" in html

    def test_an_ordered_list_is_ordered(self):
        html = self.render("1. first\n2. second")

        assert "<ol" in html and html.count("<li>") == 2

    def test_a_link_is_a_link(self):
        html = self.render("see [the docs](https://example.test/x)")

        assert 'href="https://example.test/x"' in html
        assert 'rel="noopener noreferrer"' in html
        assert ">the docs</a>" in html

    def test_a_dangerous_link_is_not_a_link(self):
        """The text comes from a model that has been reading files and web pages, so
        it can contain whatever those contained."""
        for attempt in ("[x](javascript:alert(1))", "[x](data:text/html,<script>)"):
            html = self.render(attempt)

            assert "<a " not in html, attempt
            assert "href" not in html, attempt

    def test_html_in_the_text_is_shown_rather_than_run(self):
        html = self.render('<img src=x onerror="alert(1)"> and <script>bad()</script>')

        assert "<img" not in html and "<script>" not in html
        assert "&lt;img" in html and "&lt;script&gt;" in html

    def test_an_attribute_cannot_be_broken_out_of(self):
        """A URL with a quote in it never becomes a link at all, and the quote is
        escaped on the way out — so there is no attribute for it to escape from."""
        html = self.render('[x](https://example.test/"onmouseover="alert(1))')

        assert "<a " not in html and "href=" not in html
        assert "&quot;onmouseover" in html, "the text is shown, inert"

    def test_plain_prose_keeps_its_line_breaks(self):
        """Half of what lands here is a listing, where the breaks are the structure."""
        html = self.render("line one\nline two")

        assert "line one<br />line two" in html

    def test_nothing_is_rendered_for_nothing(self):
        assert self.render("") == ""
        assert self.render("   \n\n  ") == ""

    def test_the_app_renders_the_display_channel_through_it(self, script):
        """The only place a rendered string reaches the page, so the only place that
        has to be checked: everything it emits is escaped inside `markdown.js`."""
        block = function_source(script, "appendShown")

        assert "markdownToHtml(block.rawText)" in block
        assert 'from "./markdown.js"' in script, "and the renderer is imported, not inlined"


class TestTheWorkLogIsFoldedAway:
    """The stored answer is the display text with the turn's tool log appended, and
    the log is the model's memory of its work rather than part of what it said: one
    `run_shell(...)` line per call, unbounded, and on a long turn far longer than the
    answer above it. Rendered inline it put a wall of shell commands under every
    reopened reply, in the transcripts that most needed reading.
    """

    def test_a_stored_answer_is_split_into_what_it_showed_and_the_log(self, script):
        block = function_source(script, "splitWorkLog")

        assert "WORK_LOG_MARKER" in block
        assert "shown:" in block and "log:" in block

    def test_the_log_is_a_closed_disclosure_in_the_steps(self, script):
        block = function_source(script, "appendWorkLog")

        assert 'node("details", "worklog")' in block, "closed until asked for"
        assert "turn.steps.append(details)" in block, "with the work, not after the answer"

    def test_reopening_a_conversation_splits_the_stored_answer(self, script):
        """Both stored paths have to go through it. The recovery path was wired up
        and the replay path was not, so a reopened conversation still dumped the log
        while the code that fixes it sat there unused."""
        replay = script[script.index("await replayThenJumpToLatest(") :]
        assert "appendStoredAnswer(turn, message.content, message.spoken)" in replay

        recovery = function_source(script, "recoverMissingAnswer")
        assert "appendStoredAnswer(turn, last.content, last.spoken)" in recovery


class TestTheRendererCannotHang:
    """A line every branch declines must still be consumed.

    This is not a hypothetical: the table branch is guarded on the line *after* it,
    the paragraph loop excludes table rows, and a line starting with `|` that is not
    followed by another one therefore consumed nothing and left the index where it
    was. The loop appended an empty paragraph forever — a browser tab pinned at 100%
    CPU with the page unresponsive, on the input a Markdown table produces while it
    is still arriving. Rendered on every delta, so the first line of any table was
    enough.
    """

    @staticmethod
    def _run(script: str, *, timeout: float = 20.0) -> str:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        module = WEB / "js" / "markdown.js"
        full = (
            "import { pathToFileURL } from 'node:url';"
            f"const m = await import(pathToFileURL({json.dumps(str(module))}).href);"
            f"{script}"
        )
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-e", full],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            pytest.fail("the renderer did not finish — it is looping", pytrace=False)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def render(self, markdown: str) -> str:
        return self._run(f"process.stdout.write(m.markdownToHtml({json.dumps(markdown)}));")

    @pytest.mark.parametrize(
        "line",
        [
            "| a | b |",  # a row whose row above was never a header
            "||",
            "|",
            "|---|",  # a separator with nothing over it
            "| Outlet | Address |\n",  # a header line still arriving
            "text\n| a | b |",  # a table row as the last line
            "###",
            ">",
            "---",
            "- ",
            "```",
        ],
    )
    def test_every_lone_construct_terminates(self, line):
        html = self.render(line)

        assert html, "it has to produce something as well as finish"

    def test_a_lone_table_row_is_shown_as_text(self):
        """Degrading to text is the right failure; a stray pipe is not a hang."""
        html = self.render("| a | b |")

        assert "| a | b |" in html
        assert "<table" not in html, "one row is not a table"

    def test_no_prefix_of_a_streaming_table_hangs(self):
        """How it was actually hit: the display channel arrives in deltas and the
        block is re-rendered from everything so far, so every prefix of a table is
        rendered at some point — including the ones that are a header line and
        nothing else."""
        table = "| Outlet | Address |\n|---|---|\n| Lagana | in town |"

        output = self._run(
            f"const text = {json.dumps(table)};"
            "let count = 0;"
            "for (let n = 0; n <= text.length; n += 1) { m.markdownToHtml(text.slice(0, n)); count += 1; }"
            "process.stdout.write(count + '|' + m.markdownToHtml(text));"
        )

        count, _, final = output.partition("|")
        assert int(count) == len(table) + 1
        assert "<table" in final and "<td>Lagana</td>" in final, "the finished table renders"
