"""Portable git and svn: getting them, reading them, and committing with them.

Nothing here touches the network or a real repository. The downloads are replaced
by an archive built in the test, and the tools by scripted runners, because the
behaviours worth pinning are the decisions: which copy of git is used, what
happens when a checksum does not match, what is refused, and what must never
reach a commit.

The policy tests matter as much as the mechanics. The agent is told to ask before
committing, and the tool is the last place that can enforce it: an empty change is
refused rather than committed, and ``.surtitle/`` is left out even when the agent
asked for everything.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from surtitle.config import Settings
from surtitle.tools.fs_tools import ToolContext
from surtitle.vcs import commit as vcs_commit
from surtitle.vcs import guide as vcs_guide
from surtitle.vcs import provision, repo


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Settings(
        DEEPSEEK_API_KEY="sk-test-deepseek-1234567890",
        SURTITLE_HOME=str(home),
        voice_enabled=False,
    )


def make_portable(settings: Settings, name: str) -> Path:
    """Fabricate an unpacked portable tool, as ``install`` would leave it."""
    tool = provision.CATALOG[name]
    root = provision.tools_dir(settings) / name
    program = root / tool.executable
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_text("fake", encoding="utf-8")
    return root


class TestCatalog:
    def test_every_entry_is_pinned(self):
        """A floating version would make the checksum impossible to keep true."""
        for name, tool in provision.CATALOG.items():
            assert tool.name == name
            assert tool.url.startswith("https://")
            assert tool.version in tool.url or tool.version in tool.url.replace("-", ".")
            assert len(tool.sha256) == 64
            assert tool.size > 0
            assert tool.executable

    def test_the_digest_is_a_hex_string(self):
        for tool in provision.CATALOG.values():
            int(tool.sha256, 16)

    def test_git_is_the_minimum_build_that_still_has_the_client(self):
        """MinGit, not PortableGit: no installer, and the whole CLI."""
        git = provision.CATALOG["git"]
        assert git.executable == "cmd/git.exe"
        assert git.path_entries == ("cmd",)

    def test_svn_unpacks_with_its_bin_directory(self):
        svn = provision.CATALOG["svn"]
        assert svn.executable == "bin/svn.exe"
        assert svn.path_entries == ("bin",)


class TestLocate:
    def test_a_portable_copy_is_preferred_over_path(self, settings, monkeypatch):
        """The copy the user installed from the tray is the copy the app promised."""
        make_portable(settings, "git")
        monkeypatch.setattr(provision.shutil, "which", lambda name: "/usr/bin/git")

        found = provision.executable(settings, "git")

        assert found == provision.tools_dir(settings) / "git" / "cmd" / "git.exe"

    def test_a_system_copy_is_used_when_nothing_is_unpacked(self, settings, monkeypatch):
        monkeypatch.setattr(
            provision.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None
        )
        assert provision.executable(settings, "git") == Path("/usr/bin/git")

    def test_nothing_installed_is_none(self, settings, monkeypatch):
        monkeypatch.setattr(provision.shutil, "which", lambda name: None)
        assert provision.executable(settings, "git") is None

    def test_the_source_says_which_copy_it_is(self, settings, monkeypatch):
        make_portable(settings, "svn")
        monkeypatch.setattr(provision, "_reported_version", lambda found, name, **kw: "1.14.5")
        location = provision.locate("svn", settings)
        assert location is not None
        assert location.source == "portable"
        assert location.version == "1.14.5"

    def test_a_cheap_lookup_does_not_run_the_program(self, settings, monkeypatch):
        """The prompt is rebuilt every turn; it must not spawn processes."""
        make_portable(settings, "git")
        calls: list[str] = []
        monkeypatch.setattr(
            provision, "_reported_version", lambda found, name, **kw: calls.append(name) or "x"
        )
        location = provision.locate("git", settings, verify=False)
        assert calls == []
        assert location is not None and location.version == provision.CATALOG["git"].version


class TestPathEntries:
    def test_only_unpacked_tools_contribute(self, settings):
        assert provision.path_entries(settings) == []
        make_portable(settings, "git")
        entries = provision.path_entries(settings)
        assert entries == [str(provision.tools_dir(settings) / "git" / "cmd")]

    def test_activate_prepends_once(self, settings, monkeypatch):
        make_portable(settings, "git")
        monkeypatch.setenv("PATH", "/usr/bin")
        first = provision.activate(settings)
        second = provision.activate(settings)
        assert len(first) == 1
        assert second == []
        assert provision.path_entries(settings)[0] in provision.os.environ["PATH"]

    def test_the_child_environment_carries_them_without_activating(self, settings):
        make_portable(settings, "svn")
        env = provision.child_env(settings, base={"PATH": "/usr/bin"})
        assert env["PATH"].startswith(str(provision.tools_dir(settings) / "svn" / "bin"))
        assert env["PATH"].endswith("/usr/bin")

    def test_nothing_installed_leaves_the_environment_alone(self, settings):
        env = provision.child_env(settings, base={"PATH": "/usr/bin"})
        assert env["PATH"] == "/usr/bin"


class TestStatus:
    def test_reports_what_is_missing_and_what_to_do(self, settings, monkeypatch):
        monkeypatch.setattr(provision.shutil, "which", lambda name: None)
        rows = provision.status(settings, platform="win32", verify=False)
        assert [row.name for row in rows] == ["git", "svn"]
        assert all(not row.available for row in rows)
        assert "tray" in rows[0].hint

    def test_elsewhere_it_points_at_the_package_manager(self, settings, monkeypatch):
        monkeypatch.setattr(provision.shutil, "which", lambda name: None)
        rows = provision.status(settings, platform="linux", verify=False)
        assert "package manager" in rows[0].hint

    def test_an_installed_tool_reports_its_path(self, settings, monkeypatch):
        make_portable(settings, "git")
        monkeypatch.setattr(provision.shutil, "which", lambda name: None)
        row = next(
            r for r in provision.status(settings, platform="win32", verify=False) if r.name == "git"
        )
        assert row.available is True
        assert row.source == "portable"
        assert row.path.endswith("git.exe")


def _fake_archive(destination: Path, members: dict[str, str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as bundle:
        for name, content in members.items():
            bundle.writestr(name, content)


class TestInstall:
    def test_a_non_windows_machine_is_told_the_truth(self, settings):
        result = provision.install(settings, platform="linux")
        assert result.installed == []
        assert result.ok
        assert "package manager" in result.detail

    def test_the_archive_is_verified_unpacked_and_run(self, settings, monkeypatch):
        def download(url, destination, expected, report):
            _fake_archive(destination, {"cmd/git.exe": "fake"})

        monkeypatch.setattr(provision, "_download", download)
        monkeypatch.setattr(provision, "_sha256_of", lambda path: provision.CATALOG["git"].sha256)
        monkeypatch.setattr(
            provision, "_reported_version", lambda found, name, **kw: "git version 2.51.0.windows.1"
        )

        result = provision.install(settings, ["git"], platform="win32")

        assert result.installed == ["git"]
        assert (provision.tools_dir(settings) / "git" / "cmd" / "git.exe").is_file()

    def test_a_bad_checksum_leaves_nothing_behind(self, settings, monkeypatch):
        """A corrupt download is common; a half-unpacked tree that gets run is not."""

        def download(url, destination, expected, report):
            _fake_archive(destination, {"cmd/git.exe": "fake"})

        monkeypatch.setattr(provision, "_download", download)
        monkeypatch.setattr(provision, "_sha256_of", lambda path: "0" * 64)

        result = provision.install(settings, ["git"], platform="win32")

        assert result.failed == ["git"]
        assert not (provision.tools_dir(settings) / "git").exists()

    def test_a_binary_that_does_not_run_is_a_failure(self, settings, monkeypatch):
        """Unpacking is not installing: the program has to actually start."""

        def download(url, destination, expected, report):
            _fake_archive(destination, {"cmd/git.exe": "fake"})

        monkeypatch.setattr(provision, "_download", download)
        monkeypatch.setattr(provision, "_sha256_of", lambda path: provision.CATALOG["git"].sha256)
        monkeypatch.setattr(provision, "_reported_version", lambda found, name, **kw: "")

        result = provision.install(settings, ["git"], platform="win32")

        assert result.failed == ["git"]
        assert not (provision.tools_dir(settings) / "git").exists()

    def test_an_already_installed_tool_is_not_downloaded_again(self, settings, monkeypatch):
        make_portable(settings, "git")
        calls: list[str] = []

        def download(url, destination, expected, report):
            calls.append(url)

        monkeypatch.setattr(provision, "_download", download)
        result = provision.install(settings, ["git"], platform="win32")

        assert result.skipped == ["git"]
        assert calls == []

    def test_progress_is_reported(self, settings, monkeypatch):
        messages: list[str] = []
        monkeypatch.setattr(
            provision,
            "_download",
            lambda url, dest, size, report: _fake_archive(dest, {"cmd/git.exe": "x"}),
        )
        monkeypatch.setattr(provision, "_sha256_of", lambda path: provision.CATALOG["git"].sha256)
        monkeypatch.setattr(provision, "_reported_version", lambda found, name, **kw: "2.51.0")

        provision.install(
            settings,
            ["git"],
            platform="win32",
            progress=lambda _percent, message: messages.append(message),
        )

        assert any("Downloading git" in message for message in messages)
        assert any("installed" in message for message in messages)

    def test_an_archive_may_not_escape_its_destination(self, settings, monkeypatch):
        def download(url, destination, expected, report):
            _fake_archive(destination, {"../escape.txt": "no"})

        monkeypatch.setattr(provision, "_download", download)
        monkeypatch.setattr(provision, "_sha256_of", lambda path: provision.CATALOG["git"].sha256)

        result = provision.install(settings, ["git"], platform="win32")

        assert result.failed == ["git"]
        assert not (settings.data_dir / "tools" / "escape.txt").exists()
        assert not (settings.data_dir / "tools" / "git").exists()

    def test_the_job_reports_its_outcome(self, settings, monkeypatch):
        monkeypatch.setattr(
            provision,
            "_download",
            lambda url, dest, size, report: _fake_archive(dest, {"cmd/git.exe": "x"}),
        )
        monkeypatch.setattr(provision, "_sha256_of", lambda path: provision.CATALOG["git"].sha256)
        monkeypatch.setattr(provision, "_reported_version", lambda found, name, **kw: "2.51.0")

        job = provision.InstallJob(settings=settings)
        assert job.start(["git"]) is True
        assert job.wait(30) is True
        assert job.snapshot()["ok"] is True


class TestRepoDetect:
    def _git_runner(self, overrides=None):
        overrides = overrides or {}
        answers = {
            ("git", "rev-parse", "--show-toplevel"): (0, "/work/project\n"),
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n"),
            ("git", "rev-parse", "--short", "HEAD"): (0, "abc1234\n"),
            ("git", "config", "--get", "remote.origin.url"): (0, "git@github.com:x/y.git\n"),
        }

        def run(argv, cwd):
            key = tuple(argv)
            if key in overrides:
                return overrides[key]
            return answers.get(key, (1, ""))

        return run

    def test_a_git_checkout_is_recognised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            provision, "executable", lambda settings, name="git": Path("/usr/bin/git")
        )
        run = self._git_runner(
            {
                ("git", "status", "--porcelain=v1", "--branch"): (
                    0,
                    "## main...origin/main [ahead 2, behind 1]\n M a.py\n?? b.txt\n?? c.txt\n",
                )
            }
        )
        state = repo.detect(tmp_path, run=run, check_svn=False)

        assert state.system == "git"
        assert state.branch == "main"
        assert state.revision == "abc1234"
        assert state.changed == 1
        assert state.untracked == 2
        assert state.ahead == 2 and state.behind == 1
        assert state.dirty is True
        assert state.remote.endswith("x/y.git")

    def test_a_clean_checkout_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            provision, "executable", lambda settings, name="git": Path("/usr/bin/git")
        )
        run = self._git_runner({("git", "status", "--porcelain=v1", "--branch"): (0, "## main\n")})
        state = repo.detect(tmp_path, run=run, check_svn=False)
        assert state.dirty is False
        assert "nothing uncommitted" in state.describe()

    def test_a_folder_with_no_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            provision, "executable", lambda settings, name="git": Path("/usr/bin/git")
        )
        state = repo.detect(
            tmp_path, run=lambda argv, cwd: (128, "fatal: not a git repository"), check_svn=False
        )
        assert state.system == "none"
        assert "not under version control" in state.describe()

    def test_an_svn_working_copy_is_recognised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(provision, "executable", lambda settings, name="git": None)
        info = (
            "Path: .\n"
            "Working Copy Root Path: /work/project\n"
            "URL: https://svn.example.com/repo/trunk\n"
            "Relative URL: ^/trunk\n"
            "Revision: 4211\n"
        )

        def run(argv, cwd):
            if argv[:2] == ["svn", "info"]:
                return 0, info
            if argv[:2] == ["svn", "status"]:
                return 0, "M       a.py\n?       b.txt\n"
            return 1, ""

        state = repo.detect(tmp_path, run=run, check_svn=True)

        assert state.system == "svn"
        assert state.root == "/work/project"
        assert state.branch == "^/trunk"
        assert state.revision == "4211"
        assert state.changed == 1 and state.untracked == 1
        assert "r4211" in state.describe()

    def test_svn_is_not_asked_when_it_is_not_installed(self, tmp_path, monkeypatch):
        """A missing binary fails in a way that looks like "not a working copy"."""
        monkeypatch.setattr(provision, "executable", lambda settings, name="git": None)
        asked: list[str] = []

        def run(argv, cwd):
            asked.append(argv[0])
            return 1, "svn: command not found"

        state = repo.detect(tmp_path, run=run)
        assert asked == []
        assert state.system == "none"


class TestCommitGit:
    def _runner(self, transcript, *, staged=("a.py",), commit_code=0, push_code=0):
        def run(argv, cwd):
            transcript.append(list(argv))
            if argv[:2] == ["git", "add"]:
                return 0, "", ""
            if argv[:3] == ["git", "diff", "--cached"]:
                return 0, "".join(f"{name}\n" for name in staged), ""
            if argv[:2] == ["git", "restore"]:
                return 0, "", ""
            if argv[:2] == ["git", "commit"]:
                return commit_code, "[main abc1234] subject", ""
            if argv[:2] == ["git", "rev-parse"]:
                return 0, "abc1234\n", ""
            if argv[:2] == ["git", "push"]:
                return push_code, "To github.com:x/y.git", "rejected" if push_code else ""
            return 1, "", ""

        return run

    def test_the_message_and_subject_reach_git(self, tmp_path):
        transcript: list[list[str]] = []
        result = vcs_commit.commit(
            tmp_path,
            system="git",
            message="Add retry\n\nBecause the uploader dropped.",
            run=self._runner(transcript),
        )
        assert result.ok is True
        assert result.revision == "abc1234"
        commit_call = next(call for call in transcript if call[:2] == ["git", "commit"])
        assert commit_call[3] == "Add retry\n\nBecause the uploader dropped."

    def test_pushing_is_a_separate_step_the_user_asks_for(self, tmp_path):
        transcript: list[list[str]] = []
        without = vcs_commit.commit(tmp_path, system="git", message="x", run=self._runner([]))
        transcript.clear()
        with_push = vcs_commit.commit(
            tmp_path, system="git", message="x", push=True, run=self._runner(transcript)
        )
        assert without.pushed is False
        assert with_push.pushed is True
        assert any(call[:2] == ["git", "push"] for call in transcript)

    def test_a_failed_push_is_reported_as_a_failure(self, tmp_path):
        """The commit happened, and saying so plainly is the point."""
        result = vcs_commit.commit(
            tmp_path, system="git", message="x", push=True, run=self._runner([], push_code=1)
        )
        assert result.ok is False
        assert "committed" in result.error and "push failed" in result.error

    def test_an_empty_change_is_refused(self, tmp_path):
        result = vcs_commit.commit(
            tmp_path, system="git", message="x", run=self._runner([], staged=())
        )
        assert result.ok is False
        assert "nothing to commit" in result.error

    def test_surtitle_state_is_left_out_and_reported(self, tmp_path):
        transcript: list[list[str]] = []
        result = vcs_commit.commit(
            tmp_path,
            system="git",
            message="x",
            run=self._runner(transcript, staged=(".surtitle/notes.md", "a.py")),
        )
        unstaged = next(call for call in transcript if call[:2] == ["git", "restore"])
        assert ".surtitle/notes.md" in unstaged
        assert result.excluded == [".surtitle/notes.md"]

    def test_specific_paths_are_staged_without_add_all(self, tmp_path):
        transcript: list[list[str]] = []
        vcs_commit.commit(
            tmp_path,
            system="git",
            message="x",
            paths=["a.py", "b.py"],
            include_all=False,
            run=self._runner(transcript),
        )
        add = next(call for call in transcript if call[:2] == ["git", "add"])
        assert "-A" not in add
        assert add[-2:] == ["a.py", "b.py"]

    def test_a_message_is_required(self, tmp_path):
        result = vcs_commit.commit(tmp_path, system="git", message="   ", run=self._runner([]))
        assert result.ok is False
        assert "message" in result.error

    def test_a_missing_tool_says_how_to_get_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(provision, "executable", lambda settings, name="git": None)
        result = vcs_commit.commit(tmp_path, system="git", message="x", run=self._runner([]))
        assert result.ok is False
        assert "tools install" in result.error


class TestCommitSvn:
    def _runner(self, transcript, *, status="?       new.txt\n", commit_code=0):
        def run(argv, cwd):
            transcript.append(list(argv))
            if argv[:2] == ["svn", "status"]:
                return 0, status, ""
            if argv[:2] == ["svn", "commit"]:
                if commit_code:
                    return commit_code, "", "svn: E155011: File is out of date"
                return 0, "Committed revision 42.", ""
            return 0, "", ""

        return run

    def test_new_files_are_versioned_before_the_commit(self, tmp_path):
        """A commit silently omits anything unversioned: the classic false success."""
        transcript: list[list[str]] = []
        result = vcs_commit.commit(
            tmp_path, system="svn", message="Add the thing", run=self._runner(transcript)
        )

        assert result.ok is True
        assert result.revision == "42"
        assert any(call[:2] == ["svn", "add"] for call in transcript)

    def test_surtitle_state_is_not_added(self, tmp_path):
        transcript: list[list[str]] = []
        result = vcs_commit.commit(
            tmp_path,
            system="svn",
            message="x",
            run=self._runner(transcript, status="?       .surtitle\n?       new.txt\n"),
        )
        added = [call for call in transcript if call[:2] == ["svn", "add"]]
        assert all(".surtitle" not in call for call in added)
        assert result.excluded == [".surtitle"]

    def test_a_rejected_commit_is_reported(self, tmp_path):
        result = vcs_commit.commit(
            tmp_path, system="svn", message="x", run=self._runner([], commit_code=1)
        )
        assert result.ok is False
        assert "out of date" in result.error

    def test_publishing_is_explained_rather_than_implied(self, tmp_path):
        """There is no separate push in svn; the result says so instead of faking one."""
        result = vcs_commit.commit(
            tmp_path, system="svn", message="x", push=True, run=self._runner([])
        )
        assert result.pushed is False
        assert "publishes on commit" in result.output


class TestOwnStateDetection:
    """The one path that must never reach a commit, in every form it arrives in."""

    @pytest.mark.parametrize(
        "path",
        [
            ".surtitle/notes.md",
            ".surtitle",
            "./.surtitle/notes.md",
            ".surtitle\\notes.md",
            "  .surtitle/notes.md  ",
            ".surtitle/env/venv/pyvenv.cfg",
        ],
    )
    def test_recognised_as_surtitle_state(self, path):
        assert vcs_commit._is_own_state(path) is True

    @pytest.mark.parametrize(
        "path",
        ["surtitle/notes.md", "a/.surtitle/notes.md", "notes.md", "", ".surtitleX/y"],
    )
    def test_everything_else_is_not(self, path):
        assert vcs_commit._is_own_state(path) is False


class TestGuide:
    def test_both_systems_have_a_guide(self):
        assert "git" in vcs_guide.guide_for("git").lower()
        assert "Subversion" in vcs_guide.guide_for("svn")

    def test_an_unknown_system_gets_both(self):
        text = vcs_guide.guide_for("")
        assert "git, as this environment expects it" in text
        assert "svn, as this environment expects it" in text

    def test_the_levels_are_the_ones_the_prompt_promises(self):
        assert set(vcs_guide.DETAIL_LEVELS) == {"one-line", "summary", "detailed"}
        assert "one-line" in vcs_guide.detail_menu()

    def test_the_guide_names_what_must_never_be_committed(self):
        for text in (vcs_guide.GIT_GUIDE, vcs_guide.SVN_GUIDE):
            assert ".surtitle/" in text
            assert "secret" in text.lower()

    def test_it_warns_about_the_irreversible_commands(self):
        assert "svn revert" in vcs_guide.SVN_GUIDE
        assert "reset --hard" in vcs_guide.GIT_GUIDE
        assert "force" in vcs_guide.GIT_GUIDE.lower()


class TestAgentTools:
    """The three tools the agent gets: two readers that never ask, one that does."""

    def test_the_readers_never_ask_and_the_writer_always_does(self):
        from surtitle.tools.registry import default_registry

        registry = default_registry()
        assert registry.get("vcs_status").approval == "never"
        assert registry.get("vcs_guide").approval == "never"
        assert registry.get("vcs_commit").approval == "ask"
        assert registry.get("vcs_commit").mutating is True

    def test_the_commit_tool_demands_the_level_of_detail(self):
        """It is how the user's answer reaches the call, so it cannot be omitted."""
        from surtitle.tools.registry import default_registry

        schema = default_registry().get("vcs_commit").parameters
        assert schema["required"] == ["message", "detail"]
        assert set(schema["properties"]["detail"]["enum"]) == set(vcs_guide.DETAIL_LEVELS)

    async def test_status_reports_the_tools_and_the_repository(self, tmp_path, monkeypatch):
        from surtitle.tools import registry as reg

        monkeypatch.setattr(
            provision,
            "status",
            lambda *a, **k: [
                provision.ToolStatus(
                    name="git", available=True, source="portable", path="/t/git", version="2.51.0"
                )
            ],
        )
        monkeypatch.setattr(
            repo, "detect", lambda root, **k: repo.RepoState(system="git", branch="main", changed=2)
        )

        result = await reg._vcs_status_handler(ToolContext(root=tmp_path))

        assert result.ok is True
        assert result.data["repository"]["branch"] == "main"
        assert result.data["tools"][0]["source"] == "portable"
        assert "main" in result.display

    async def test_the_guide_can_be_asked_for_by_name(self, tmp_path):
        from surtitle.tools import registry as reg

        result = await reg._vcs_guide_handler(ToolContext(root=tmp_path), "svn")

        assert result.ok is True
        assert result.data["system"] == "svn"
        assert "Subversion" in result.data["guide"]

    async def test_the_guide_defaults_to_this_project_s_system(self, tmp_path, monkeypatch):
        from surtitle.tools import registry as reg

        monkeypatch.setattr(repo, "detect", lambda root, **k: repo.RepoState(system="git"))
        result = await reg._vcs_guide_handler(ToolContext(root=tmp_path))

        assert result.data["system"] == "git"
        assert "git, as this environment expects it" in result.data["guide"]

    async def test_a_one_line_message_may_not_have_a_body(self, tmp_path):
        """The user asked for one line; a body is the agent not having listened."""
        from surtitle.tools import registry as reg

        result = await reg._vcs_commit_handler(
            ToolContext(root=tmp_path), "Add retry\n\nBecause the uploader dropped.", "one-line"
        )

        assert result.ok is False
        assert "one-line" in result.error

    async def test_an_unknown_level_is_refused(self, tmp_path):
        from surtitle.tools import registry as reg

        result = await reg._vcs_commit_handler(ToolContext(root=tmp_path), "Add retry", "verbose")
        assert result.ok is False
        assert "one-line" in result.error

    async def test_committing_outside_a_repository_is_refused(self, tmp_path, monkeypatch):
        from surtitle.tools import registry as reg

        monkeypatch.setattr(repo, "detect", lambda root, **k: repo.RepoState(system="none"))
        result = await reg._vcs_commit_handler(ToolContext(root=tmp_path), "Add retry", "summary")

        assert result.ok is False
        assert "not under version control" in result.error

    async def test_a_successful_commit_reports_the_revision_and_the_level(
        self, tmp_path, monkeypatch
    ):
        from surtitle.tools import registry as reg

        monkeypatch.setattr(repo, "detect", lambda root, **k: repo.RepoState(system="git"))
        monkeypatch.setattr(
            vcs_commit,
            "commit",
            lambda *a, **k: vcs_commit.CommitResult(ok=True, system="git", revision="abc1234"),
        )

        result = await reg._vcs_commit_handler(
            ToolContext(root=tmp_path), "Add retry\n\nWhy.", "summary", push=True
        )

        assert result.ok is True
        assert result.data["revision"] == "abc1234"
        assert result.data["detail"] == "summary"
        assert "abc1234" in result.display

    async def test_a_failed_commit_is_returned_as_data(self, tmp_path, monkeypatch):
        from surtitle.tools import registry as reg

        monkeypatch.setattr(repo, "detect", lambda root, **k: repo.RepoState(system="git"))
        monkeypatch.setattr(
            vcs_commit,
            "commit",
            lambda *a, **k: vcs_commit.CommitResult(ok=False, system="git", error="push failed"),
        )

        result = await reg._vcs_commit_handler(ToolContext(root=tmp_path), "x", "summary")

        assert result.ok is False
        assert result.error == "push failed"


def _session(tmp_path, settings):
    """A real Session, so the prompt is built the way the app builds it."""
    from surtitle.core.session import Session
    from surtitle.store.db import Store

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
        settings=settings,
        store=store,
        deepseek=None,
        send=send,
        send_audio=send_audio,
    )


class TestPrimer:
    """The agent has to be told to reach for these, and told to ask first."""

    def test_the_prompt_states_the_ask_before_saving_rule(self):
        from surtitle.core.agent import build_system_prompt

        text = build_system_prompt("project")

        assert "## Version control" in text
        assert "Never commit, tag, push or `svn commit` unless the user has asked" in text
        assert "one line" in text and "detailed" in text

    def test_the_prompt_says_when_to_ask(self):
        """Done means the work mostly works — not that it started or stopped."""
        from surtitle.core.agent import build_system_prompt

        text = build_system_prompt("project")
        assert "When a piece of work is done, ask whether to save it" in text
        assert "never treat an earlier yes as covering later work" in text

    def test_the_session_says_what_is_installed_here(self, tmp_path, settings, monkeypatch):
        make_portable(settings, "git")
        monkeypatch.setattr(
            repo, "detect", lambda root, **k: repo.RepoState(system="git", branch="main")
        )
        session = _session(tmp_path, settings)

        section = session._version_control_section()

        assert "git" in section and "portable" in section
        assert "main" in section
        assert "vcs_guide" in section

    def test_a_missing_tool_is_named_rather_than_silent(self, tmp_path, settings, monkeypatch):
        monkeypatch.setattr(provision.shutil, "which", lambda name: None)
        monkeypatch.setattr(repo, "detect", lambda root, **k: repo.RepoState(system="none"))
        session = _session(tmp_path, settings)

        section = session._version_control_section()

        assert "Not installed on this machine: git, svn" in section
        assert "not under version control" in section

    def test_it_speaks_up_when_a_tool_exists_but_nothing_is_versioned(
        self, tmp_path, settings, monkeypatch
    ):
        make_portable(settings, "git")
        monkeypatch.setattr(repo, "detect", lambda root, **k: repo.RepoState(system="none"))
        session = _session(tmp_path, settings)

        assert "Nothing here is versioned yet" in session._version_control_section()

    def test_the_prompt_carries_the_section(self, tmp_path, settings, monkeypatch):
        make_portable(settings, "git")
        monkeypatch.setattr(
            repo, "detect", lambda root, **k: repo.RepoState(system="git", branch="dev")
        )
        session = _session(tmp_path, settings)

        assert "## Version control" in session._system_prompt()

    def test_it_is_derived_once_not_once_per_turn(self, tmp_path, settings, monkeypatch):
        """The prompt is rebuilt every turn, and this runs subprocesses."""
        make_portable(settings, "git")
        calls: list[int] = []
        monkeypatch.setattr(repo, "detect", lambda root, **k: calls.append(1) or repo.RepoState())
        session = _session(tmp_path, settings)

        session._version_control_section()
        session._version_control_section()
        session._version_control_section()

        assert len(calls) == 1

    def test_a_failure_to_read_it_never_costs_the_session(self, tmp_path, settings, monkeypatch):
        def explode(*_args, **_kwargs):
            raise OSError("no tools here")

        monkeypatch.setattr(provision, "status", explode)
        session = _session(tmp_path, settings)

        assert session._version_control_section() == ""
        assert session._system_prompt()
