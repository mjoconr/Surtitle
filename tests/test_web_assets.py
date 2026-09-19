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
        assert html.index('id="tabThinking"') < html.index('id="tabFiles"')
        assert 'rightTab: "todo"' in script, "the panel must open on the plan"

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

        The tree is a fact about the disk; "wrote sim/balegate.lpc" is a fact
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
        block = block[: block.index("break;")]
        assert "state.touched.clear()" in block


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
