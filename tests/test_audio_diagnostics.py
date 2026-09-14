"""Diagnostics that attribute a missing transcript to the right layer.

The symptom this exists for: the microphone indicated it was listening, frames
were demonstrably arriving from the browser, and no text appeared. The log
recorded only a frame count, which cannot distinguish

* the browser sending **silence**, so capture is the problem, from
* the browser sending real audio that recognition then **failed to transcribe**.

Without that distinction the failure is unattributable, and it was being guessed
at instead. The log line now carries the peak amplitude of the audio received.
"""

from __future__ import annotations

import array

import pytest

from surtitle.config import Settings
from surtitle.core.session import Session, _peak_level
from surtitle.store.db import Store


def pcm16(values: list[int]) -> bytes:
    return array.array("h", values).tobytes()


class TestPeakLevel:
    def test_silence_is_zero(self):
        assert _peak_level(pcm16([0] * 512)) == 0.0

    def test_full_scale_is_one(self):
        assert _peak_level(pcm16([-32768, 32767])) == 1.0

    def test_a_negative_peak_is_measured_by_magnitude(self):
        assert _peak_level(pcm16([-32768, 0])) == 1.0

    def test_half_scale(self):
        assert _peak_level(pcm16([16384] * 64)) == pytest.approx(0.5)

    def test_an_odd_length_frame_is_truncated_not_rejected(self):
        """Diagnostics must never be the thing that breaks a session."""
        assert _peak_level(b"\x01\x02\x03") > 0

    def test_an_empty_frame_is_zero(self):
        assert _peak_level(b"") == 0.0


@pytest.fixture
def session(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("P", tmp_path)
    record = store.create_session(project.id)

    async def send(_payload):
        return None

    async def send_audio(_data):
        return None

    return Session(
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


class TestListeningSessionReport:
    async def test_the_report_carries_the_peak_amplitude(self, session, caplog):
        with caplog.at_level("INFO"):
            await session.handle_mic(True)
            await session.handle_audio(pcm16([12000] * 512))
            await session.handle_mic(False)

        assert "peak 0.366" in caplog.text

    async def test_silent_audio_is_called_out_as_a_capture_problem(self, session, caplog):
        """Frames arriving with no signal is a capture fault, not a recognition one."""
        with caplog.at_level("INFO"):
            await session.handle_mic(True)
            for _ in range(10):
                await session.handle_audio(pcm16([0] * 512))
            await session.handle_mic(False)

        assert "the browser sent silence" in caplog.text
        assert "capture is running but delivering no signal" in caplog.text

    async def test_real_audio_is_not_called_silent(self, session, caplog):
        with caplog.at_level("INFO"):
            await session.handle_mic(True)
            await session.handle_audio(pcm16([8000, -8000] * 256))
            await session.handle_mic(False)

        assert "sent silence" not in caplog.text

    async def test_no_frames_at_all_is_reported_separately(self, session, caplog):
        with caplog.at_level("INFO"):
            await session.handle_mic(True)
            await session.handle_mic(False)

        assert "no audio arrived for this listening session" in caplog.text
        assert "sent silence" not in caplog.text, "no frames is not the same as silence"

    async def test_each_listening_session_reports_its_own_peak(self, session, caplog):
        """A loud first cycle must not make a silent second cycle look healthy."""
        with caplog.at_level("INFO"):
            await session.handle_mic(True)
            await session.handle_audio(pcm16([30000] * 512))
            await session.handle_mic(False)
            await session.handle_mic(True)
            await session.handle_audio(pcm16([0] * 512))
            await session.handle_mic(False)

        assert "peak 0.916" in caplog.text
        assert "sent silence" in caplog.text, "the second cycle's peak was not reset"

    async def test_the_frame_report_describes_one_session_not_the_total(self, session, caplog):
        with caplog.at_level("INFO"):
            await session.handle_mic(True)
            for _ in range(3):
                await session.handle_audio(pcm16([1000] * 512))
            await session.handle_mic(False)
            await session.handle_mic(True)
            await session.handle_audio(pcm16([1000] * 512))
            await session.handle_mic(False)

        assert "received 1 frame(s)" in caplog.text
