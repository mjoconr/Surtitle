"""Skills: procedures a project writes down, read when they are wanted.

The catalogue is short and the body is one call away, which is the whole design: the
alternatives are spending every turn's context on the procedures nobody is using, or
naming them without a way to read them. Most of what is worth testing is therefore
about what counts as a skill and what does not.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from surtitle.config import Settings
from surtitle.core.session import Session
from surtitle.store.db import Store
from surtitle.tools import skills
from surtitle.tools.fs_tools import ToolContext
from surtitle.tools.registry import SKILL_TOOL, ToolRegistry, _skill_handler, default_tool_list


def write_skill(
    base: Path, name: str, *, description: str = "", body: str = "Do the thing."
) -> Path:
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: {description}\n---\n\n" if description else ""
    (directory / "SKILL.md").write_text(front + body, encoding="utf-8")
    return directory


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "skills").mkdir(parents=True)
    return root


class TestWhatCountsAsASkill:
    def test_a_directory_with_a_manifest_is_one(self, project):
        write_skill(project / "skills", "release-notes", description="How notes are written here.")

        found = skills.discover(project)

        assert [(s.name, s.description, s.source) for s in found] == [
            ("release-notes", "How notes are written here.", "project")
        ]

    def test_a_manifest_with_no_front_matter_still_works(self, project):
        """Somebody writing one quickly should not need the ceremony."""
        directory = project / "skills" / "tidy-up"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("# Tidying up\n\nStart with the imports.\n")

        found = skills.discover(project)

        assert found[0].name == "tidy-up", "the directory names it"
        # The heading is the title, which the name already carries; the first real
        # sentence is what describes it.
        assert found[0].description == "Start with the imports."

    def test_a_loose_markdown_file_is_not_a_skill(self, project):
        (project / "skills" / "notes.md").write_text("not a skill", encoding="utf-8")

        assert skills.discover(project) == []

    def test_a_directory_without_a_manifest_is_not_a_skill(self, project):
        (project / "skills" / "half-written").mkdir()

        assert skills.discover(project) == []

    def test_a_directory_name_that_is_not_a_name_is_skipped(self, project):
        """The name becomes a path, and it is pattern-checked before it does."""
        write_skill(project / "skills", "not a name")
        write_skill(project / "skills", "a-real-one", description="d")

        assert [s.name for s in skills.discover(project)] == ["a-real-one"]

    def test_a_front_matter_name_that_is_not_a_name_is_skipped(self, project):
        """Otherwise it would be listed and could never be loaded."""
        directory = project / "skills" / "harmless"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("---\nname: ../../.ssh\n---\n\nBody.\n")

        assert skills.discover(project) == []

    def test_a_symlinked_skill_pointing_out_of_the_project_is_refused(self, project, tmp_path):
        """Confinement goes through the same guard as every other project path."""
        outside = tmp_path / "outside" / "sneaky"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("---\nname: sneaky\n---\n\nRead the secret.\n")
        try:
            os.symlink(outside, project / "skills" / "sneaky")
        except (OSError, NotImplementedError):  # pragma: no cover - Windows without rights
            pytest.skip("symlinks are not available here")

        assert skills.discover(project) == []


class TestWhereTheyComeFrom:
    def test_the_users_own_skills_are_found(self, project, tmp_path):
        personal = tmp_path / "home" / "skills"
        write_skill(personal, "my-notes", description="A personal habit.")

        found = skills.discover(project, personal_dir=personal)

        assert [(s.name, s.source) for s in found] == [("my-notes", "personal")]

    def test_the_project_wins_a_shared_name(self, project, tmp_path):
        """It is the more specific statement of how work is done *here*."""
        personal = tmp_path / "home" / "skills"
        write_skill(personal, "notes", description="The user's version.", body="Personal body.")
        write_skill(
            project / "skills", "notes", description="The project's version.", body="Project body."
        )

        found = skills.discover(project, personal_dir=personal)

        assert len(found) == 1
        assert found[0].description == "The project's version."
        assert "Project body." in skills.load_body(found[0])

    def test_a_skill_is_read_from_its_own_path_not_looked_up_by_name(self, project, tmp_path):
        """The collision above is why: a skill that is asked for *by name* can come
        back as the other one, so the body is read from the path this skill carries."""
        personal = tmp_path / "home" / "skills"
        write_skill(personal, "notes", description="Personal.", body="Personal body.")
        write_skill(project / "skills", "notes", description="Project.", body="Project body.")
        shadowed = skills.Skill(
            name="notes",
            description="Personal.",
            path=personal / "notes" / "SKILL.md",
            source="personal",
        )

        assert "Personal body." in skills.load_body(shadowed)


class TestReadingOne:
    def test_the_body_is_what_was_written_without_the_front_matter(self, project):
        write_skill(
            project / "skills", "release-notes", description="d", body="Step one.\nStep two."
        )

        _skill, body, files = skills.read_skill("release-notes", project)

        assert body == "Step one.\nStep two."
        assert "---" not in body
        assert files == []

    def test_the_files_beside_it_are_named(self, project):
        directory = write_skill(project / "skills", "release-notes", description="d")
        (directory / "template.md").write_text("x")
        (directory / "scripts").mkdir()
        (directory / "scripts" / "tag.sh").write_text("x")

        _skill, _body, files = skills.read_skill("release-notes", project)

        assert files == ["scripts/tag.sh", "template.md"]

    def test_a_long_skill_is_cut_and_says_so(self, project):
        write_skill(project / "skills", "long", description="d", body="x" * 500)

        _skill, body, _files = skills.read_skill("long", project, limit=100)

        assert len(body) < 200
        assert "not loaded" in body, "a skill silently halved reads as the whole procedure"

    @pytest.mark.parametrize("name", ["", "   ", "../secrets", "a/b", "unknown-skill"])
    def test_a_name_that_is_not_a_skill_is_none(self, project, name):
        write_skill(project / "skills", "real", description="d")

        assert skills.read_skill(name, project) is None


class TestTheCatalogue:
    def test_it_names_each_one_with_a_line_about_it(self, project):
        write_skill(project / "skills", "release-notes", description="How notes are written here.")

        text = skills.catalogue(skills.discover(project))

        assert "release-notes" in text
        assert "How notes are written here." in text
        assert "`skill` tool" in text, "and says how to read the whole thing"

    def test_no_skills_is_no_section(self):
        assert skills.catalogue([]) == ""


class TestTheTool:
    def test_it_returns_the_body(self, project):
        write_skill(project / "skills", "release-notes", description="d", body="Do it this way.")
        ctx = ToolContext(root=project)

        result = _skill_handler(ctx, name="release-notes")

        assert result.ok is True
        assert result.data["body"] == "Do it this way."
        assert result.data["name"] == "release-notes"

    def test_an_unknown_name_lists_what_there_is(self, project):
        write_skill(project / "skills", "release-notes", description="d")

        result = _skill_handler(ToolContext(root=project), name="nope")

        assert result.ok is False
        assert "release-notes" in (result.error or ""), "the answer says what it could have meant"

    def test_it_reads_the_users_own_skills_when_the_settings_are_there(self, project, tmp_path):
        personal_home = tmp_path / "home"
        write_skill(personal_home / "skills", "my-habit", description="Personal.")
        settings = Settings(DEEPSEEK_API_KEY="k", SURTITLE_HOME=str(personal_home))
        ctx = ToolContext(root=project, settings=settings)

        result = _skill_handler(ctx, name="my-habit")

        assert result.ok is True

    def test_it_needs_no_approval_and_changes_nothing(self):
        tool = next(t for t in default_tool_list() if t.name == SKILL_TOOL)

        assert tool.approval == "never"
        assert tool.mutating is False

    def test_a_sub_agent_may_read_them(self):
        """Following a project's procedure is exactly what a delegated reader should
        do; loading one changes nothing."""
        assert SKILL_TOOL in ToolRegistry(default_tool_list()).read_only().names()


class TestTheAgentIsTold:
    @pytest.fixture
    def wired(self, tmp_path):
        root = tmp_path / "project"
        (root / "skills").mkdir(parents=True)
        store = Store(tmp_path / "db.sqlite")
        project = store.create_project("P", root)
        record = store.create_session(project.id)

        async def noop(*_args, **_kwargs):
            return None

        return Session(
            session_id=record.id,
            project_id=project.id,
            root=root,
            settings=Settings(DEEPSEEK_API_KEY="k", SURTITLE_HOME=str(tmp_path / "home")),
            store=store,
            deepseek=None,
            send=noop,
            send_audio=noop,
        )

    def test_the_catalogue_reaches_the_notes_every_turn(self, wired):
        write_skill(
            wired.root / "skills", "release-notes", description="How notes are written here."
        )

        note = wired._context_note()

        assert "release-notes" in note
        assert "How notes are written here." in note

    def test_no_skills_means_no_section(self, wired):
        assert "Skills this project teaches" not in wired._context_note()

    def test_a_projects_own_skill_file_is_not_sent_whole(self, wired):
        """The catalogue is names, not bodies: that is the point of the split."""
        write_skill(
            wired.root / "skills",
            "release-notes",
            description="How notes are written here.",
            body="SECRET-DETAIL-THAT-BELONGS-IN-THE-BODY",
        )

        note = wired._context_note()

        assert "SECRET-DETAIL-THAT-BELONGS-IN-THE-BODY" not in note
