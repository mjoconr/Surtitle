"""Tests for voice engine selection.

``build_voice`` decides which engine each direction uses, and it is the one place
where a configuration mistake becomes "the microphone does not work". These tests
pin the matrix, including the case that matters most for a partial install: one
direction working while the other cannot, which must not disable both.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.test_local_engines import install_fake_models, make_settings

from surtitle.config import Settings
from surtitle.voice.engine import VoiceUnavailable, build_voice


async def noop(*_args, **_kwargs) -> None:
    return


def callbacks() -> dict:
    return {
        "on_transcript": noop,
        "on_audio": noop,
        "on_started": noop,
        "on_finished": noop,
        "on_error": noop,
        "on_speed_fallback": noop,
    }


class TestSelectionMatrix:
    def test_both_hosted_with_a_key(self):
        settings = Settings(DEEPSEEK_API_KEY="k", DEEPGRAM_API_KEY="d")
        bundle = build_voice(settings, **callbacks())
        assert bundle.stt is not None and bundle.tts is not None
        assert bundle.fully_enabled
        assert bundle.problems == []

    def test_both_hosted_without_a_key_explains_itself(self):
        settings = Settings(DEEPSEEK_API_KEY="k", DEEPGRAM_API_KEY=None)
        bundle = build_voice(settings, **callbacks())
        assert bundle.stt is None and bundle.tts is None
        assert not bundle.enabled
        assert bundle.problem is not None
        assert "DEEPGRAM_API_KEY" in bundle.problem.reason
        assert bundle.problem.fix

    def test_voice_disabled_builds_nothing(self):
        settings = Settings(DEEPSEEK_API_KEY="k", DEEPGRAM_API_KEY="d", SURTITLE_VOICE="false")
        bundle = build_voice(settings, **callbacks())
        assert bundle.stt is None and bundle.tts is None
        assert bundle.problems == []

    def test_local_backends_need_no_deepgram_key(self, tmp_path):
        settings = make_settings(
            tmp_path,
            SURTITLE_STT_BACKEND="local",
            SURTITLE_TTS_BACKEND="local",
        )
        install_fake_models(tmp_path, settings)
        bundle = build_voice(settings, **callbacks())
        assert bundle.fully_enabled, bundle.problems

    def test_hybrid_local_ears_and_hosted_voice(self, tmp_path):
        settings = make_settings(
            tmp_path,
            DEEPGRAM_API_KEY="d",
            SURTITLE_STT_BACKEND="local",
        )
        install_fake_models(tmp_path, settings)
        bundle = build_voice(settings, **callbacks())
        assert bundle.stt is not None, "local recognition should have been built"
        assert bundle.tts is not None
        assert bundle.fully_enabled

    def test_hybrid_hosted_ears_and_local_voice_needs_no_key(self, tmp_path):
        settings = make_settings(tmp_path, SURTITLE_TTS_BACKEND="local")
        install_fake_models(tmp_path, settings)
        bundle = build_voice(settings, **callbacks())
        # Recognition is hosted and has no key, so it is absent and *reported*;
        # the local voice must survive that, which is the whole point of building
        # the two directions independently.
        assert bundle.tts is not None, "a local voice must not be lost to a missing key"
        assert bundle.stt is None
        assert any("DEEPGRAM_API_KEY" in p.reason for p in bundle.problems)


class TestMissingLocalModels:
    def test_a_missing_model_is_reported_with_the_fix(self, tmp_path):
        settings = make_settings(
            tmp_path,
            SURTITLE_STT_BACKEND="local",
            SURTITLE_TTS_BACKEND="local",
        )
        bundle = build_voice(settings, **callbacks())
        assert not bundle.enabled
        problems = " ".join(p.reason for p in bundle.problems)
        assert settings.local_stt_model in problems
        assert settings.local_tts_model in problems
        assert all(p.fix and "models download" in p.fix for p in bundle.problems)

    def test_a_missing_local_tts_model_does_not_remove_a_hosted_stt(self, tmp_path):
        """Half a pipeline is strictly better than none."""
        settings = make_settings(
            tmp_path,
            DEEPGRAM_API_KEY="d",
            SURTITLE_TTS_BACKEND="local",
        )
        bundle = build_voice(settings, **callbacks())
        assert bundle.stt is not None
        assert bundle.tts is None
        assert not bundle.fully_enabled
        assert bundle.enabled


class TestCredentialRequirement:
    """A local-only configuration must not demand a Deepgram key."""

    def test_local_only_needs_no_deepgram_key(self, tmp_path):
        settings = make_settings(
            tmp_path,
            SURTITLE_STT_BACKEND="local",
            SURTITLE_TTS_BACKEND="local",
        )
        assert settings.missing_credentials() == []
        settings.require_credentials()  # must not raise

    def test_hybrid_still_needs_the_key(self, tmp_path):
        settings = make_settings(tmp_path, SURTITLE_STT_BACKEND="local")
        assert settings.missing_credentials() == ["DEEPGRAM_API_KEY"]

    def test_text_only_never_needs_it(self, tmp_path):
        settings = make_settings(tmp_path, SURTITLE_VOICE="false")
        assert settings.missing_credentials() == []

    def test_backend_names_are_validated(self):
        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="k", SURTITLE_STT_BACKEND="whisper")


def test_voice_unavailable_carries_a_reason_and_fix():
    error = VoiceUnavailable("no model", fix="download it", backend="local")
    assert error.reason == "no model"
    assert error.fix == "download it"
    assert error.backend == "local"


class TestNativeLibraryConflictCheck:
    """A competing DLL is worth *reporting*, not worth failing over.

    `sherpa-onnx` bundles its own ONNX runtime, and modern Windows ships its own at
    `C:\\Windows\\System32\\onnxruntime.dll`. Windows keeps one native library per
    name per process, so a copy loaded earlier can win. The observed consequence —
    an un-catchable C++ abort — was seen once and never reproduced, so this stays a
    warning with evidence rather than presenting itself as a diagnosis.
    """

    def test_reports_a_competing_dll_on_path(self, tmp_path, monkeypatch):
        from surtitle import doctor

        library_dir = tmp_path / "somewhere"
        library_dir.mkdir()
        (library_dir / "onnxruntime.dll").write_bytes(b"stale")
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setenv("PATH", str(library_dir))

        check = doctor._check_native_conflicts()
        assert check.status is doctor.CheckStatus.WARN
        assert "onnxruntime.dll" in check.detail
        # It must not claim the engines are broken: this is information, and most
        # machines with a System32 copy work fine.
        assert check.ok, "a competing DLL must not fail the doctor run"
        assert check.fix and "uninstall" in check.fix

    def test_clean_windows_environment_passes(self, tmp_path, monkeypatch):
        from surtitle import doctor

        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setenv("PATH", str(empty))
        check = doctor._check_native_conflicts()
        assert check.status is doctor.CheckStatus.OK

    def test_not_applicable_elsewhere(self, monkeypatch):
        from surtitle import doctor

        monkeypatch.setattr("sys.platform", "darwin")
        check = doctor._check_native_conflicts()
        assert check.status is doctor.CheckStatus.SKIP


class TestLocalVoiceCheckIsNeverSilent:
    """A check that did not run must say so, or it reads as a pass.

    Observed: local voice would not start, and `surtitle doctor` reported a clean
    run. The check only fires while a local engine is selected, and it used to
    return nothing at all otherwise, so switching back to a hosted engine to get
    unblocked removed the evidence and the clean report became the reason the real
    cause was never examined.
    """

    def test_a_hosted_selection_reports_that_it_was_skipped(self, tmp_path):
        from surtitle import doctor

        settings = make_settings(
            tmp_path,
            SURTITLE_STT_BACKEND="deepgram",
            SURTITLE_TTS_BACKEND="deepgram",
        )
        checks = doctor._check_local_voice(settings)
        assert checks, "the local voice check vanished from the report instead of skipping"

        check = checks[0]
        assert check.status is doctor.CheckStatus.SKIP
        assert "deepgram" in check.detail, "the reason must name what is selected"
        assert "not checked" in check.detail
        assert check.ok, "a check that did not run must not fail the report"

    def test_voice_switched_off_says_so(self, tmp_path):
        from surtitle import doctor

        settings = make_settings(tmp_path, SURTITLE_VOICE="false")
        checks = doctor._check_local_voice(settings)
        assert checks and checks[0].status is doctor.CheckStatus.SKIP
        assert "off" in checks[0].detail

    def test_a_local_selection_still_fails_without_the_extra(self, tmp_path, monkeypatch):
        """The skip must not have displaced the failure it was hiding."""
        from surtitle import doctor

        settings = make_settings(
            tmp_path,
            SURTITLE_STT_BACKEND="local",
            SURTITLE_TTS_BACKEND="local",
        )
        monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda _name: None)

        checks = doctor._check_local_voice(settings)
        extra = [check for check in checks if check.name == "Local voice extra"]
        assert extra, f"a local selection must be checked, got {[c.name for c in checks]}"
        assert extra[0].status is doctor.CheckStatus.FAIL
        assert "voice-local" in (extra[0].fix or ""), "the failure must carry the fix"
