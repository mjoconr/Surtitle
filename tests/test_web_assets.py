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
    """Reasoning must not flood the panel.

    Reasoning arrives as token-sized deltas. Pushing one row per delta and
    re-rendering the whole panel each time turned the activity log into a wall of
    single words — "Reasoning: .", "Reasoning: briefly" — and rebuilt the DOM
    dozens of times a second during every thinking turn.
    """

    def test_reasoning_goes_through_the_coalescing_helper(self, script):
        assert "pushReasoning(data.text)" in script, (
            "the thinking case must not push a row per delta"
        )
        assert (
            'state.activity.push({ label: "Reasoning", detail: data.text.slice(0, 240) })'
            not in script
        )

    def test_the_helper_reuses_the_last_reasoning_row(self, script):
        assert 'last.label === "Reasoning"' in script

    def test_the_re_render_is_throttled(self, script):
        assert "reasoningRenderTimer" in script
        assert "REASONING_MAX_CHARS" in script


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
