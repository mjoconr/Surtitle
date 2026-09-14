"""Configuration must resolve the same way regardless of launch directory.

This is a regression test for a bug that cost a real debugging cycle: the
launcher runs from ``scripts/``, and a stray ``scripts/.env`` (a filled-in copy of
the template) silently set ``DEEPGRAM_STT_MODEL=nova-3``. Nova is not a valid
model on the Flux endpoint, so speech recognition failed with an HTTP 400 loop
that looked exactly like a code defect.

pydantic-settings resolves a relative ``env_file`` against the current working
directory, so the same code gave different answers depending on where it started.
Environment files are now absolute and anchored to the application root.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from surtitle.config import (
    app_root,
    candidate_env_files,
    get_settings,
    loaded_env_files,
    reset_settings_cache,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestEnvFileLocations:
    def test_candidates_are_absolute(self):
        for path in candidate_env_files():
            assert path.is_absolute(), f"{path} is relative and would follow the cwd"

    def test_candidates_are_anchored_to_the_application_root(self):
        root = app_root()
        paths = candidate_env_files()
        # The repo's own .env and .env.local must be among the candidates.
        assert root / ".env" in paths
        assert root / ".env.local" in paths

    def test_a_stray_env_in_a_subdirectory_is_not_a_candidate(self):
        """The exact bug: scripts/.env must never be read."""
        stray = REPO_ROOT / "scripts" / ".env"
        assert stray not in candidate_env_files()

    def test_loaded_files_are_a_subset_of_candidates(self):
        assert set(loaded_env_files()) <= set(candidate_env_files())


class TestLoadingIsCwdIndependent:
    """The real assertion: same code, different cwd, same settings."""

    @staticmethod
    def _read_settings(cwd: Path) -> str:
        """Load settings in a subprocess running from ``cwd``, return stt_model."""
        script = textwrap.dedent(
            """
            from surtitle.config import get_settings, reset_settings_cache
            reset_settings_cache()
            s = get_settings()
            print(f"{s.stt_api}|{s.stt_model}")
            """
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DEEPGRAM_STT_MODEL", "SURTITLE_STT_API", "VIRTUAL_ENV"}
        }
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_same_result_from_repo_root_and_a_subdirectory(self, tmp_path):
        from_root = self._read_settings(REPO_ROOT)
        from_scripts = self._read_settings(REPO_ROOT / "scripts")
        from_tmp = self._read_settings(tmp_path)

        assert from_root == from_scripts == from_tmp, (
            "settings differ by working directory: "
            f"root={from_root} scripts={from_scripts} tmp={from_tmp}"
        )

    def test_default_stt_configuration_is_flux(self, tmp_path):
        """The default must be a model that is valid on the default endpoint."""
        api, model = self._read_settings(tmp_path).split("|")
        assert api == "v2"
        assert model == "flux-general-en"
        # Flux requires a three-part name; nova-3 on /v2/listen is an HTTP 400.
        assert model.count("-") == 2

    def test_flux_model_parts_are_valid(self):
        """Deepgram rejects a v2 model whose name is not three hyphenated parts."""
        reset_settings_cache()
        model = get_settings().stt_model
        assert len(model.split("-")) == 3, (
            f"{model!r} is not a valid v2 model name; Deepgram requires three parts"
        )
        assert model.split("-")[0] == "flux"


class TestEnvironmentStillWins:
    """Absolute paths must not stop real environment variables from overriding."""

    def test_environment_variable_overrides_the_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SURTITLE_HOME", str(tmp_path))
        monkeypatch.setenv("DEEPGRAM_STT_MODEL", "flux-general-en")
        reset_settings_cache()
        try:
            assert get_settings().stt_model == "flux-general-en"
        finally:
            reset_settings_cache()

    def test_stt_api_is_validated(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SURTITLE_HOME", str(tmp_path))
        monkeypatch.setenv("SURTITLE_STT_API", "v9")
        reset_settings_cache()
        try:
            with pytest.raises(ValueError):
                get_settings()
        finally:
            reset_settings_cache()
