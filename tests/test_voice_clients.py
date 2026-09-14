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
