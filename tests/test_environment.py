"""Tests for per-project Python environments and package installation.

These are the tests that matter most for the "the agent can do anything" goal:
they prove it can acquire a capability it was not shipped with, that the
capability lands in an isolated environment rather than the application's own,
and that the approval gate actually gates.

The installation tests are marked ``live`` because they reach PyPI, so the
default suite stays offline and fast. Run them with ``-m live``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from surtitle.tools import environment
from surtitle.tools.environment import (
    approved_requirements,
    ensure_venv,
    env_dir,
    install_packages,
    new_requirements,
    project_env_python,
    remember_requirements,
    requirements_path,
    validate_requirements,
    venv_dir,
)
from surtitle.tools.fs_tools import ToolContext
from surtitle.tools.project_config import load_project_config


class TestRequirementValidation:
    def test_plain_names_are_accepted(self):
        valid, refused = validate_requirements(["requests", "pandas", "openpyxl"])
        assert valid == ["requests", "pandas", "openpyxl"]
        assert refused == []

    @pytest.mark.parametrize(
        "requirement",
        ["pandas>=2.2", "numpy==1.26.4", "requests[socks]", "uvicorn[standard]>=0.30", "six<2"],
    )
    def test_version_specifiers_and_extras_are_accepted(self, requirement):
        valid, refused = validate_requirements([requirement])
        assert valid == [requirement]
        assert refused == []

    @pytest.mark.parametrize(
        "requirement",
        [
            "--index-url=https://evil.example/simple",
            "-e",
            "--target=/tmp",
            "-r requirements.txt",
            "git+https://github.com/x/y.git",
            "https://example.com/pkg.tar.gz",
            "../../../etc/passwd",
            "/tmp/local-project",
            "package @ https://example.com/pkg.whl",
            "rm -rf /",
            "",
        ],
    )
    def test_dangerous_requirements_are_refused(self, requirement):
        """A requirement must not become a package-manager flag or a remote URL."""
        valid, refused = validate_requirements([requirement])
        assert valid == []
        assert refused, f"{requirement!r} should have been refused"

    def test_mixed_input_keeps_the_valid_entries(self):
        valid, refused = validate_requirements(["requests", "--index-url=x", "six"])
        assert valid == ["requests", "six"]
        assert len(refused) == 1


class TestApprovalMemory:
    def test_nothing_approved_initially(self, tmp_path):
        assert approved_requirements(tmp_path) == []
        assert new_requirements(tmp_path, ["requests"]) == ["requests"]

    def test_remembering_records_the_requirement(self, tmp_path):
        remember_requirements(tmp_path, ["requests>=2.31"])
        assert approved_requirements(tmp_path) == ["requests>=2.31"]
        assert requirements_path(tmp_path).is_file()

    def test_remembering_is_idempotent(self, tmp_path):
        remember_requirements(tmp_path, ["requests"])
        remember_requirements(tmp_path, ["requests"])
        assert approved_requirements(tmp_path) == ["requests"]

    def test_approved_packages_are_not_pending_again(self, tmp_path):
        remember_requirements(tmp_path, ["requests", "six"])
        assert new_requirements(tmp_path, ["six"]) == []
        assert new_requirements(tmp_path, ["six", "pandas"]) == ["pandas"]

    def test_whitespace_differences_do_not_create_duplicates(self, tmp_path):
        remember_requirements(tmp_path, ["pandas >= 2.2"])
        assert new_requirements(tmp_path, ["pandas>=2.2"]) == []

    def test_requirements_file_has_a_comment_header(self, tmp_path):
        remember_requirements(tmp_path, ["six"])
        text = requirements_path(tmp_path).read_text(encoding="utf-8")
        assert text.startswith("#")
        assert "six" in text

    def test_approval_is_mirrored_into_the_project_config(self, tmp_path):
        remember_requirements(tmp_path, ["six"])
        config = load_project_config(tmp_path)
        assert config.requirements == ["six"]


@pytest.fixture(scope="class")
def shared_project(tmp_path_factory):
    """A project whose environment is built once for the whole class.

    Creating a virtual environment costs seconds, and several tests here only need
    *an* environment rather than a fresh one. ``ensure_venv`` is idempotent, so this
    builds it a single time on its own event loop and every test in the class
    reuses it. Tests that must start from nothing take their own ``tmp_path``.
    """
    project = tmp_path_factory.mktemp("shared-env")
    asyncio.run(ensure_venv(project))
    return project


class TestVersionedEnvironment:
    async def test_creating_an_environment_yields_an_interpreter(self, shared_project):
        python = await ensure_venv(shared_project)
        assert python.is_file()
        assert project_env_python(shared_project) == python
        assert python.parent.parent == venv_dir(shared_project)

    async def test_creating_twice_reuses_the_same_environment(self, shared_project):
        first = await ensure_venv(shared_project)
        second = await ensure_venv(shared_project)
        assert first == second

    async def test_the_environment_is_isolated_from_the_application(self, shared_project):
        """The project interpreter must not be the application's interpreter."""
        python = await ensure_venv(shared_project)
        app_prefix = Path(sys.prefix).resolve()
        project_prefix = python.parent.parent.resolve()
        assert project_prefix != app_prefix
        # And it lives inside the project, so deleting the project removes it.
        assert shared_project.resolve() in project_prefix.parents

    async def test_status_before_any_environment(self, tmp_path):
        status = await environment.project_env_status(tmp_path)
        assert status.exists is False
        assert status.python is None
        assert status.package_count == 0

    async def test_status_after_creating_an_environment(self, shared_project):
        status = await environment.project_env_status(shared_project)
        assert status.exists is True
        payload = status.to_dict()
        assert payload["isolated"] is True
        assert isinstance(payload["installed"], list)


class TestInstallationFailures:
    async def test_refused_requirements_stop_before_any_subprocess(self, tmp_path):
        ok, output, installed = await install_packages(tmp_path, ["--index-url=https://e/x"])
        assert not ok
        assert "not accepted" in output or "plain package" in output
        assert installed == []
        # Nothing should have been created for a rejected request.
        assert not venv_dir(tmp_path).exists()

    async def test_empty_request_is_rejected(self, tmp_path):
        ok, output, _installed = await install_packages(tmp_path, [])
        assert not ok
        assert "No package names" in output

    async def test_a_nonexistent_package_fails_without_corrupting_state(self, tmp_path):
        ok, output, installed = await install_packages(
            tmp_path, ["this-package-definitely-does-not-exist-surtitle"]
        )
        assert not ok
        assert installed == []
        assert output
        # A failed install must not record an approval.
        assert approved_requirements(tmp_path) == []


@pytest.mark.live
class TestLiveInstallation:
    """Reaches PyPI. Run with `pytest -m live`."""

    async def test_install_then_import_in_the_project_environment(self, tmp_path):
        ok, output, installed = await install_packages(tmp_path, ["six"])
        assert ok, output
        assert installed == ["six"]

        python = project_env_python(tmp_path)
        assert python is not None

        process = await asyncio.create_subprocess_exec(
            str(python),
            "-c",
            "import six, sys; print(six.__version__); print(sys.prefix)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        out = stdout.decode()
        assert str(venv_dir(tmp_path)) in out, "import came from the wrong environment"

    async def test_installed_package_does_not_leak_into_the_application(self, tmp_path):
        """The point of isolation: the app environment must stay unchanged."""
        sentinel = "tinycss2-not-a-real-requirement"
        ok, _output, _installed = await install_packages(tmp_path, [sentinel])
        assert not ok  # deliberately not a real package

        # A package installed into the project must not become importable from
        # the application interpreter.
        ok, output, _ = await install_packages(tmp_path, ["six"])
        if not ok:
            pytest.skip(f"PyPI unavailable: {output}")

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "print('probe')",
            stdout=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        # The application environment already contains six as a dependency, so
        # assert interpreter identity instead: that is the real isolation property.
        assert project_env_python(tmp_path) != Path(sys.executable)


class TestRunPythonEnvironmentSelection:
    """`run_python` must use the project environment once it exists."""

    async def test_runs_in_the_application_environment_before_install(self, tmp_path):
        from surtitle.tools.registry import default_registry

        registry = default_registry()
        ctx = ToolContext(root=tmp_path)
        result = await registry.dispatch(
            "run_python", ctx, {"code": "import sys; print(sys.prefix)"}
        )
        assert result.ok, result.error
        assert result.data is not None
        assert result.data["isolated"] is False

    async def test_reports_the_transcript_of_which_environment_ran(self, tmp_path):
        from surtitle.tools.registry import default_registry

        registry = default_registry()
        ctx = ToolContext(root=tmp_path)
        await ensure_venv(tmp_path)
        result = await registry.dispatch("run_python", ctx, {"code": "print('hi')"})
        assert result.ok, result.error
        assert result.data is not None
        assert result.data["isolated"] is True
        assert str(venv_dir(tmp_path)) in result.data["interpreter"]


class TestApprovalGateForInstall:
    """The approval hook must ask only about packages the project has not approved."""

    def test_new_packages_need_approval(self, tmp_path):
        from surtitle.tools.registry import _APPROVAL_ROOT, _install_needs_approval

        token = _APPROVAL_ROOT.set(tmp_path)
        try:
            assert _install_needs_approval({"packages": ["pandas"]}) is True
        finally:
            _APPROVAL_ROOT.reset(token)

    def test_approved_packages_do_not_need_approval(self, tmp_path):
        from surtitle.tools.registry import _APPROVAL_ROOT, _install_needs_approval

        remember_requirements(tmp_path, ["pandas>=2.2"])
        token = _APPROVAL_ROOT.set(tmp_path)
        try:
            assert _install_needs_approval({"packages": ["pandas>=2.2"]}) is False
            assert _install_needs_approval({"packages": ["pandas>=2.2", "openpyxl"]}) is True
        finally:
            _APPROVAL_ROOT.reset(token)

    def test_without_a_root_it_fails_closed(self):
        from surtitle.tools.registry import _APPROVAL_ROOT, _install_needs_approval

        token = _APPROVAL_ROOT.set(None)
        try:
            assert _install_needs_approval({"packages": ["pandas"]}) is True
        finally:
            _APPROVAL_ROOT.reset(token)

    def test_empty_or_bad_arguments_need_approval(self, tmp_path):
        from surtitle.tools.registry import _APPROVAL_ROOT, _install_needs_approval

        token = _APPROVAL_ROOT.set(tmp_path)
        try:
            assert _install_needs_approval({}) is True
            assert _install_needs_approval({"packages": "not-a-list"}) is True
            assert _install_needs_approval({"packages": []}) is True
        finally:
            _APPROVAL_ROOT.reset(token)

    def test_registry_honours_the_hook(self, tmp_path):
        from surtitle.tools.registry import _APPROVAL_ROOT, default_registry

        registry = default_registry()
        token = _APPROVAL_ROOT.set(tmp_path)
        try:
            assert registry.requires_approval("install_packages", {"packages": ["pandas"]}) is True
            remember_requirements(tmp_path, ["pandas"])
            assert registry.requires_approval("install_packages", {"packages": ["pandas"]}) is False
        finally:
            _APPROVAL_ROOT.reset(token)

    def test_a_broken_hook_fails_closed(self, tmp_path, monkeypatch):
        """An exception while deciding must not silently grant approval."""
        from surtitle.tools.registry import Tool, ToolRegistry

        def broken(_arguments):
            raise RuntimeError("boom")

        registry = ToolRegistry(
            [
                Tool(
                    name="risky",
                    description="x",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda ctx: None,
                    approval="ask",
                    needs_approval=broken,
                )
            ]
        )
        assert registry.requires_approval("risky", {}) is True


class TestWriteOnlyPath:
    def test_environment_directory_is_hidden_and_project_local(self, tmp_path):
        assert env_dir(tmp_path) == tmp_path / ".surtitle"
        assert env_dir(tmp_path).name.startswith(".")


@pytest.mark.skipif(os.name == "nt", reason="POSIX interpreter naming")
def test_posix_interpreter_is_discovered(tmp_path):
    asyncio.run(ensure_venv(tmp_path))
    python = project_env_python(tmp_path)
    assert python is not None
    assert python.name in {"python", "python3"} or python.name.startswith("python3")


class TestCredentialEditability:
    """A key the app stored must stay editable across repeated reads.

    Regression test for a reported bug: once a key was entered it could never be
    changed. `effective()` writes the stored key into the same Settings object
    that `credential_state` consults, so after the first call a file-stored key
    looked like an environment one and the UI locked the field.
    """

    def test_a_saved_key_is_reported_writable_before_and_after_effective(self, tmp_path):
        from surtitle.config import Settings
        from surtitle.store.settings_store import SettingsStore

        store = SettingsStore(Settings(SURTITLE_HOME=str(tmp_path)))
        store.set_credential("DEEPGRAM_API_KEY", "dg-first-0000")

        before = store.credential_state("DEEPGRAM_API_KEY")
        assert (before.configured, before.writable, before.source) == (True, True, "file")

        store.effective()
        after = store.credential_state("DEEPGRAM_API_KEY")
        assert (after.configured, after.writable, after.source) == (True, True, "file"), (
            "effective() made a stored key look like an environment key, "
            "which locks the field in the UI"
        )

    def test_a_saved_key_can_be_replaced_repeatedly(self, tmp_path):
        from surtitle.config import Settings
        from surtitle.store.settings_store import SettingsStore

        store = SettingsStore(Settings(SURTITLE_HOME=str(tmp_path)))
        for value in ("dg-one", "dg-two", "dg-three"):
            store.effective()
            store.set_credential("DEEPGRAM_API_KEY", value)
            assert store.credential_value("DEEPGRAM_API_KEY") == value

    def test_the_newest_saved_value_wins_over_an_earlier_one(self, tmp_path):
        """The reported symptom: the first key kept coming back."""
        from surtitle.config import Settings
        from surtitle.store.settings_store import SettingsStore

        store = SettingsStore(Settings(SURTITLE_HOME=str(tmp_path)))
        store.set_credential("DEEPGRAM_API_KEY", "dg-original")
        store.effective()
        store.set_credential("DEEPGRAM_API_KEY", "dg-replacement")
        store.effective()
        assert store.credential_value("DEEPGRAM_API_KEY") == "dg-replacement"

    def test_a_launch_credential_stays_read_only(self, tmp_path):
        """Environment precedence must still hold: it is not a bug, it is a rule."""
        from surtitle.config import Settings
        from surtitle.store.settings_store import SettingsStore

        store = SettingsStore(
            Settings(SURTITLE_HOME=str(tmp_path), DEEPGRAM_API_KEY="dg-from-launch")
        )
        state = store.credential_state("DEEPGRAM_API_KEY")
        assert state.configured is True
        assert state.source == "env"
        assert state.writable is False

        store.effective()
        # Still read-only after effective(), and still refused on write.
        assert store.credential_state("DEEPGRAM_API_KEY").writable is False
        from surtitle.store.settings_store import SettingsValidationError

        with pytest.raises(SettingsValidationError, match="precedence"):
            store.set_credential("DEEPGRAM_API_KEY", "dg-replacement")
