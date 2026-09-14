"""Tests for the Deepgram streaming clients.

The URL-construction tests here are regression tests for a real failure: Flux
(``/v2/listen``) rejects any query parameter it does not recognise with HTTP 400,
and the first version of this code built one query string for both backends. The
result was a permanent reconnect loop with no useful message, because the
exception's HTTP response body — which named the offending parameter — was not
being reported.

The accepted parameter sets below were verified against the live endpoint, where
a bogus key still reveals parameter validation (400 = bad parameters,
401 = parameters accepted).
"""

from __future__ import annotations

import contextlib
from urllib.parse import parse_qs, urlparse

import pytest

from surtitle.config import Settings
from surtitle.voice.stt import SpeechToText


def make_settings(**overrides) -> Settings:
    values = {"DEEPSEEK_API_KEY": "k", "DEEPGRAM_API_KEY": "k"}
    values.update(overrides)
    return Settings(**values)


def params_for(settings: Settings) -> dict[str, list[str]]:
    service = SpeechToText(settings, on_transcript=lambda event: None)
    return parse_qs(urlparse(service.url).query)


class TestFluxQueryParameters:
    """These must match ``/v2/listen`` exactly. Extra parameters are fatal."""

    def test_does_not_send_the_v1_only_channels_parameter(self):
        """`channels` was the actual cause of HTTP 400 on the first real run."""
        params = params_for(make_settings(stt_api="v2"))
        assert "channels" not in params, (
            "/v2/listen rejects `channels` with HTTP 400; it would break voice entirely"
        )

    @pytest.mark.parametrize(
        "rejected",
        [
            "channels",
            "language",
            "interim_results",
            "punctuate",
            "smart_format",
            "vad_events",
            "endpointing",
            "utterance_end_ms",
            "multichannel",
        ],
    )
    def test_rejected_parameters_are_absent(self, rejected):
        params = params_for(make_settings(stt_api="v2"))
        assert rejected not in params, f"/v2/listen rejects `{rejected}`"

    def test_sends_the_required_parameters(self):
        params = params_for(make_settings(stt_api="v2"))
        assert params["model"] == ["flux-general-en"]
        assert params["encoding"] == ["linear16"]
        assert params["sample_rate"] == ["16000"]

    def test_points_at_the_v2_endpoint(self):
        settings = make_settings(stt_api="v2")
        service = SpeechToText(settings, on_transcript=lambda event: None)
        assert service.url.startswith("wss://api.deepgram.com/v2/listen?")

    def test_end_of_turn_knobs_are_omitted_by_default(self):
        """They are only sent when explicitly configured, so defaults stay plain."""
        params = params_for(make_settings(stt_api="v2"))
        assert "eot_threshold" not in params
        assert "eot_timeout_ms" not in params

    def test_end_of_turn_knobs_are_sent_when_set(self):
        params = params_for(make_settings(stt_api="v2", eot_threshold=0.7, eot_timeout_ms=5000))
        assert params["eot_threshold"] == ["0.7"]
        assert params["eot_timeout_ms"] == ["5000"]

    def test_model_is_configurable(self):
        params = params_for(make_settings(stt_api="v2", stt_model="flux-general-en"))
        assert params["model"] == ["flux-general-en"]


class TestNovaQueryParameters:
    """The v1 path keeps the richer parameter set, which it does accept."""

    def test_sends_the_parameters_nova_understands(self):
        params = params_for(make_settings(stt_api="v1", stt_model="nova-3"))
        for name in ("model", "language", "encoding", "sample_rate", "channels"):
            assert name in params, f"nova needs `{name}`"

    def test_points_at_the_v1_endpoint(self):
        service = SpeechToText(make_settings(stt_api="v1"), on_transcript=lambda e: None)
        assert service.url.startswith("wss://api.deepgram.com/v1/listen?")

    def test_endpointing_is_reflected(self):
        params = params_for(make_settings(stt_api="v1", endpointing_ms=500))
        assert params["endpointing"] == ["500"]
        # utterance_end_ms is derived, and must not precede endpointing.
        assert int(params["utterance_end_ms"][0]) >= 1000

    def test_language_defaults_to_english(self):
        assert params_for(make_settings(stt_api="v1"))["language"] == ["en"]


class TestBackendSelection:
    def test_v2_uses_flux(self):
        assert SpeechToText(make_settings(stt_api="v2"), on_transcript=lambda e: None).uses_flux

    def test_v1_does_not_use_flux(self):
        assert not SpeechToText(make_settings(stt_api="v1"), on_transcript=lambda e: None).uses_flux

    def test_invalid_api_is_rejected_at_config_load(self):
        with pytest.raises(ValueError):
            make_settings(stt_api="v3")


class TestFailureReporting:
    """An HTTP rejection must name the server's reason, not just the class name."""

    def test_http_rejection_body_is_included(self):
        from surtitle.voice.stt import _explain

        class Response:
            status_code = 400
            body = b'{"err_code":"INVALID_QUERY_PARAMETER","err_msg":"Unknown query parameters: channels"}'

        class Rejected(Exception):
            response = Response()

        message = _explain(Rejected())
        assert "HTTP 400" in message
        assert "Unknown query parameters: channels" in message

    def test_plain_exception_still_reports_itself(self):
        from surtitle.voice.stt import _explain

        message = _explain(TimeoutError("took too long"))
        assert "TimeoutError" in message

    def test_non_json_body_is_passed_through(self):
        from surtitle.voice.stt import _explain

        class Response:
            status_code = 502
            body = b"bad gateway"

        class Broken(Exception):
            response = Response()

        assert "502" in _explain(Broken())


class TestResponseShapeHandling:
    """Both envelopes should be recognised, since Flux's exact schema is unconfirmed."""

    def test_nova_envelope_is_detected(self):
        from surtitle.voice.stt import _looks_like_nova_results

        payload = {"channel": {"alternatives": [{"transcript": "hello"}]}}
        assert _looks_like_nova_results(payload) is True

    def test_list_wrapped_channel_is_detected(self):
        from surtitle.voice.stt import _looks_like_nova_results

        payload = {"channel": [{"alternatives": [{"transcript": "hello"}]}]}
        assert _looks_like_nova_results(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"channel": {}},
            {"channel": {"alternatives": []}},
            {"channel": "not-a-dict"},
            {"transcript": "flux-style", "end_of_turn": True},
        ],
    )
    def test_non_nova_payloads_are_not_detected(self, payload):
        from surtitle.voice.stt import _looks_like_nova_results

        assert _looks_like_nova_results(payload) is False


class TestSharedUrlBuilders:
    """The doctor must probe the URLs the client actually uses.

    These are exported precisely so a diagnostic cannot drift from the client. A
    duplicated parameter set in the doctor once reported a healthy configuration
    as broken, which is worse than having no diagnostic at all.
    """

    def test_the_client_and_the_helper_agree(self):
        from surtitle.voice.stt import listen_url

        settings = make_settings(stt_api="v2")
        client = SpeechToText(settings, on_transcript=lambda e: None)
        assert client.url == listen_url(settings)

    def test_tts_client_and_helper_agree(self):
        from surtitle.voice.tts import TextToSpeech, speak_url

        settings = make_settings(tts_speed=1.0)
        client = TextToSpeech(settings, on_audio=lambda a, n: None)
        assert client.url == speak_url(settings)

    def test_helper_omits_channels_for_flux(self):
        from surtitle.voice.stt import listen_url

        assert "channels" not in parse_qs(urlparse(listen_url(make_settings(stt_api="v2"))).query)

    def test_helper_includes_channels_for_nova(self):
        from surtitle.voice.stt import listen_url

        params = parse_qs(
            urlparse(listen_url(make_settings(stt_api="v1", stt_model="nova-3"))).query
        )
        assert params["channels"] == ["1"]

    def test_tts_speed_is_omitted_at_natural_rate(self):
        from surtitle.voice.tts import speak_url

        assert "speed" not in parse_qs(urlparse(speak_url(make_settings(tts_speed=1.0))).query)

    def test_tts_speed_is_sent_when_not_natural(self):
        from surtitle.voice.tts import speak_url

        params = parse_qs(urlparse(speak_url(make_settings(tts_speed=1.25))).query)
        assert params["speed"] == ["1.25"]

    def test_tts_speed_can_be_suppressed_for_a_voice_that_rejects_it(self):
        from surtitle.voice.tts import speak_url

        params = parse_qs(
            urlparse(speak_url(make_settings(tts_speed=1.25), speed_supported=False)).query
        )
        assert "speed" not in params


class TestSendLoopControlMessages:
    """Only audio may be sent on the live socket during a session.

    Regression test for a connect/disconnect loop: the send loop used to emit a
    keep-alive during quiet stretches. Nova accepts that message; Flux rejects it
    and closes the connection, with

        unknown variant `KeepAlive`, expected one of
        `CloseStream`, `ForceEndTurn`, `Configure`

    Because the helper loops roughly once a second while idle, the result was a
    reconnect every second. The safe keep-alive is silence.
    """

    @staticmethod
    def _service():
        settings = make_settings(stt_api="v2")
        return SpeechToText(settings, on_transcript=lambda event: None)

    async def test_idle_send_loop_emits_no_control_messages(self):
        import asyncio
        import json as jsonlib

        service = self._service()
        sent: list[bytes | str] = []

        class FakeSocket:
            async def send(self, payload):
                sent.append(payload)

        # One audio frame so we can prove forwarding still works...
        service._queue.put_nowait(b"\x01\x02\x03")
        task = asyncio.create_task(service._send_loop(FakeSocket()))

        # ...then sit idle past the internal timeout, which is where the
        # keep-alive used to be sent.
        await asyncio.sleep(1.4)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert sent, "the audio frame was not forwarded"
        assert sent == [b"\x01\x02\x03"], f"unexpected frames sent: {sent!r}"

        control = [item for item in sent if not isinstance(item, (bytes, bytearray))]
        assert control == [], f"control messages must not be sent mid-session: {control}"
        for item in sent:
            if isinstance(item, str):
                assert jsonlib.loads(item).get("type") != "KeepAlive"

    async def test_audio_frames_are_forwarded_verbatim(self):
        import asyncio

        service = self._service()
        sent: list[bytes | str] = []

        class FakeSocket:
            async def send(self, payload):
                sent.append(payload)

        frames = [b"first-frame", b"second-frame"]
        for frame in frames:
            service._queue.put_nowait(frame)

        task = asyncio.create_task(service._send_loop(FakeSocket()))
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert sent == frames

    async def test_a_none_sentinel_ends_the_send_loop(self):
        import asyncio

        service = self._service()
        sent: list[bytes | str] = []

        class FakeSocket:
            async def send(self, payload):
                sent.append(payload)

        service._queue.put_nowait(None)
        await asyncio.wait_for(service._send_loop(FakeSocket()), timeout=2.0)
        assert sent == []


class TestFluxResponseHandling:
    """Parsing against payloads captured from the live Flux endpoint.

    The schema was unknown when this was written, so it was guessed defensively.
    These fixtures are the real thing, recorded from a live socket streaming
    silence at 16 kHz:

        {"type": "TurnInfo", "event": "Update", "turn_index": 0,
         "audio_window_start": 0.0, "audio_window_end": 0.24,
         "transcript": "", "words": [],
         "end_of_turn_confidence": 0.0044, "sequence_id": 1}

    Note there is no Nova-style ``channel.alternatives`` envelope: the transcript
    is a top-level field.
    """

    @staticmethod
    def _collector():
        captured: list = []

        async def on_transcript(event):
            captured.append(event)

        settings = make_settings(stt_api="v2")
        return SpeechToText(settings, on_transcript=on_transcript), captured

    def _live_silence_payload(self) -> dict:
        """Verbatim from the endpoint, including the empty transcript."""
        return {
            "type": "TurnInfo",
            "request_id": "01a09f13-7ad6-71f0-a672-c9fd3861d18a",
            "event": "Update",
            "turn_index": 0,
            "audio_window_start": 0.0,
            "audio_window_end": 0.24,
            "transcript": "",
            "words": [],
            "end_of_turn_confidence": 0.0044,
            "sequence_id": 1,
        }

    async def test_silence_update_produces_no_caption(self):
        service, captured = self._collector()
        await service._handle_flux_turn(self._live_silence_payload())
        assert captured == [], "an empty transcript must not become a caption"

    async def test_in_progress_text_becomes_a_live_caption(self):
        service, captured = self._collector()
        await service._handle_flux_turn(
            {
                "type": "TurnInfo",
                "event": "Update",
                "turn_index": 0,
                "transcript": "what is the through",
                "words": [],
                "end_of_turn_confidence": 0.42,
            }
        )
        assert len(captured) == 1
        assert captured[0].text == "what is the through"
        assert captured[0].final is False
        assert captured[0].is_end_of_turn is False

    async def test_end_of_turn_starts_the_turn(self):
        service, captured = self._collector()
        await service._handle_flux_turn(
            {
                "type": "TurnInfo",
                "event": "EndOfTurn",
                "turn_index": 0,
                "transcript": "what is the throughput of the line",
                "words": [],
                "end_of_turn_confidence": 0.91,
            }
        )
        assert len(captured) == 1
        event = captured[0]
        assert event.text == "what is the throughput of the line"
        assert event.final is True
        assert event.is_end_of_turn is True
        # end_of_turn_confidence is the meaningful figure on Flux.
        assert event.confidence == pytest.approx(0.91)

    async def test_end_of_turn_with_no_text_is_still_a_boundary(self):
        service, captured = self._collector()
        await service._handle_flux_turn(
            {
                "type": "TurnInfo",
                "event": "EndOfTurn",
                "transcript": "",
                "words": [],
                "end_of_turn_confidence": 0.9,
            }
        )
        assert len(captured) == 1
        assert captured[0].text == ""
        assert captured[0].is_end_of_turn is True

    async def test_playback_suppression_drops_flux_transcripts(self):
        """The agent must not transcribe its own voice through Flux either."""
        service, captured = self._collector()
        service.set_suppression(True)
        await service._handle_flux_turn(
            {
                "type": "TurnInfo",
                "event": "Update",
                "transcript": "the agent hearing itself",
                "words": [],
            }
        )
        assert captured == []

    async def test_words_only_payload_is_rebuilt(self):
        """Some revisions carry words without a transcript field."""
        service, captured = self._collector()
        await service._handle_flux_turn(
            {
                "type": "TurnInfo",
                "event": "Update",
                "words": [{"word": "hello"}, {"word": "there"}],
            }
        )
        assert captured and captured[0].text == "hello there"

    async def test_the_confirmed_shape_is_not_mistaken_for_nova(self):
        """TurnInfo has no channel envelope, so it must route to the Flux handler."""
        from surtitle.voice.stt import _looks_like_flux_turn, _looks_like_nova_results

        payload = self._live_silence_payload()
        assert _looks_like_nova_results(payload) is False
        assert _looks_like_flux_turn(payload) is True

    async def test_turn_info_type_is_routed_by_the_receive_loop(self):
        """The dispatcher must recognise `TurnInfo`, the type the service sends."""
        from surtitle.voice.stt import _FLUX_TURN_TYPES

        assert "TurnInfo" in _FLUX_TURN_TYPES


class TestTranscriptAccumulation:
    """Spoken words must be accumulated, not overwritten.

    Regression test for a correctness bug that also explains "I can't see what I
    said": the session replaced its interim text on every update, so the model
    received only the final fragment of a sentence. Both backends are covered,
    because they report differently and the accumulator must not care which.
    """

    @staticmethod
    def _accumulate(existing: str, incoming: str) -> str:
        from surtitle.core.session import Session

        return Session._accumulate(existing, incoming)

    def test_cumulative_updates_are_not_duplicated(self):
        """Flux sends the turn so far, growing each time."""
        text = ""
        for update in ("what is", "what is the", "what is the throughput"):
            text = self._accumulate(text, update)
        assert text == "what is the throughput"

    def test_fragmented_updates_are_joined(self):
        """A word-level backend sends successive fragments."""
        text = ""
        for update in ("what is", "the throughput", "of the line"):
            text = self._accumulate(text, update)
        assert text == "what is the throughput of the line"

    def test_a_repeated_update_is_idempotent(self):
        text = self._accumulate("hello world", "hello world")
        assert text == "hello world"

    def test_a_prefix_correction_keeps_the_longer_text(self):
        text = self._accumulate("throughout", "through")
        assert text == "throughout"

    def test_empty_updates_do_not_clear_accumulated_text(self):
        text = self._accumulate("keep me", "")
        assert text == "keep me"

    def test_an_existing_fragment_is_not_appended_twice(self):
        text = self._accumulate("the throughput of the line", "throughput")
        assert text == "the throughput of the line"

    def test_no_spurious_space_before_punctuation(self):
        text = self._accumulate("hello", ", world")
        assert text == "hello, world"

    def test_first_update_becomes_the_text(self):
        assert self._accumulate("", "first words") == "first words"

    def test_accumulation_is_independent_of_word_level_updates(self):
        """The end-to-end shape: fragments then a cumulative final."""
        text = ""
        for update in ("what", "what is", "what is the throughput"):
            text = self._accumulate(text, update)
        assert text == "what is the throughput"
