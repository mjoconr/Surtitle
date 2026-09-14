"""Tests for file attachments.

Uploads are a security boundary: a filename arrives from the client and is written
to disk inside the user's project. These tests treat the name as hostile and assert
that nothing escapes the upload directory, that a failed upload leaves no partial
file, and that limits actually bite.
"""

from __future__ import annotations

import io

import httpx
import pytest

from surtitle.config import Settings
from surtitle.server import MAX_UPLOAD_BYTES, _safe_filename, create_app_for


@pytest.fixture
def app_client(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    settings = Settings(
        DEEPSEEK_API_KEY="sk-test",
        SURTITLE_HOME=str(home),
        voice_enabled=False,
    )
    app = create_app_for(settings)
    root = tmp_path / "proj"
    root.mkdir()
    project = app.state.app_state.store.create_project("P", root)
    session = app.state.app_state.store.create_session(project.id)
    transport = httpx.ASGITransport(app=app)
    return app, transport, project, session, root


class TestFilenameSanitising:
    """A filename never becomes a path."""

    @pytest.mark.parametrize(
        ("hostile", "must_not_contain"),
        [
            ("../../etc/passwd", ".."),
            ("/absolute/path.txt", "/"),
            ("..\\..\\windows\\system32\\cmd.exe", ".."),
            ("nested/deep/file.txt", "/"),
            ("....//escape.txt", ".."),
        ],
    )
    def test_traversal_is_stripped(self, hostile, must_not_contain):
        cleaned = _safe_filename(hostile)
        assert must_not_contain not in cleaned
        assert "/" not in cleaned and "\\" not in cleaned

    def test_a_normal_name_is_preserved(self):
        assert _safe_filename("Q3 report.pdf") == "Q3 report.pdf"

    def test_exotic_characters_are_replaced(self):
        cleaned = _safe_filename("re;port$(whoami).pdf")
        assert ";" not in cleaned and "$" not in cleaned and "(" not in cleaned
        assert cleaned.endswith(".pdf")

    @pytest.mark.parametrize("name", ["", ".", "..", "...", "   "])
    def test_degenerate_names_get_a_fallback(self, name):
        assert _safe_filename(name) == "upload"

    @pytest.mark.parametrize("reserved", ["CON.txt", "PRN", "aux.log", "COM1.txt"])
    def test_windows_reserved_names_are_escaped(self, reserved):
        """These are invalid to create on Windows and would fail confusingly."""
        cleaned = _safe_filename(reserved)
        stem = cleaned.split(".")[0]
        assert stem.upper() not in {"CON", "PRN", "AUX", "NUL", "COM1"}

    def test_a_very_long_name_is_truncated(self):
        cleaned = _safe_filename("x" * 400 + ".pdf")
        assert len(cleaned) <= 150


class TestUploadEndpoint:
    async def test_uploads_a_file_inside_the_project(self, app_client):
        app, transport, project, _session, root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/projects/{project.id}/uploads",
                files={"files": ("notes.txt", io.BytesIO(b"hello world"), "text/plain")},
            )
            assert response.status_code == 200, response.text
            saved = response.json()["files"]
            assert len(saved) == 1
            assert saved[0]["bytes"] == 11

            # Stored inside the project, in the hidden env directory.
            stored = root / saved[0]["path"]
            assert stored.is_file()
            assert stored.read_text() == "hello world"
            assert saved[0]["path"].startswith("uploads/")
        await app.state.app_state.aclose()

    async def test_a_hostile_filename_cannot_escape(self, app_client):
        app, transport, project, _session, root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/projects/{project.id}/uploads",
                files={"files": ("../../escaped.txt", io.BytesIO(b"x"), "text/plain")},
            )
            assert response.status_code == 200
            path = response.json()["files"][0]["path"]
            assert ".." not in path
            # Nothing was written above the project root.
            assert not (root.parent / "escaped.txt").exists()
            assert not (root.parent.parent / "escaped.txt").exists()
        await app.state.app_state.aclose()

    async def test_multiple_files_in_one_request(self, app_client):
        app, transport, project, _session, _root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/projects/{project.id}/uploads",
                files=[
                    ("files", ("a.txt", io.BytesIO(b"aaa"), "text/plain")),
                    ("files", ("b.txt", io.BytesIO(b"bbbbb"), "text/plain")),
                ],
            )
            assert response.status_code == 200
            saved = response.json()["files"]
            assert [item["name"] for item in saved] == ["a.txt", "b.txt"]
            assert [item["bytes"] for item in saved] == [3, 5]
        await app.state.app_state.aclose()

    async def test_a_repeated_name_does_not_overwrite(self, app_client):
        """Two files called notes.txt must both survive."""
        app, transport, project, _session, root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for content in (b"first", b"second"):
                response = await client.post(
                    f"/api/projects/{project.id}/uploads",
                    files={"files": ("notes.txt", io.BytesIO(content), "text/plain")},
                )
                assert response.status_code == 200

            upload_dir = root / "uploads"
            contents = sorted(path.read_text() for path in upload_dir.iterdir())
            assert contents == ["first", "second"]
        await app.state.app_state.aclose()

    async def test_an_oversized_file_is_rejected_and_leaves_nothing_behind(self, app_client):
        app, transport, project, _session, root = app_client
        oversized = b"x" * (MAX_UPLOAD_BYTES + 1024)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/projects/{project.id}/uploads",
                files={"files": ("big.bin", io.BytesIO(oversized), "application/octet-stream")},
            )
            assert response.status_code == 413
            # A partially written file must not remain.
            upload_dir = root / "uploads"
            leftovers = list(upload_dir.iterdir()) if upload_dir.is_dir() else []
            assert leftovers == [], f"partial upload left behind: {leftovers}"
        await app.state.app_state.aclose()

    async def test_an_unknown_project_is_rejected(self, app_client):
        app, transport, _project, _session, _root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/projects/nope/uploads",
                files={"files": ("a.txt", io.BytesIO(b"x"), "text/plain")},
            )
            assert response.status_code == 404
        await app.state.app_state.aclose()

    async def test_the_upload_is_readable_by_the_file_endpoint(self, app_client):
        """The point of storing it: the agent reads it like any project file."""
        app, transport, project, _session, _root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            up = await client.post(
                f"/api/projects/{project.id}/uploads",
                files={"files": ("data.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
            )
            path = up.json()["files"][0]["path"]
            fetched = await client.get(f"/api/projects/{project.id}/file", params={"path": path})
            assert fetched.status_code == 200
            assert "a,b" in fetched.text
        await app.state.app_state.aclose()

    async def test_listing_reports_previous_uploads(self, app_client):
        app, transport, project, _session, _root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            empty = await client.get(f"/api/projects/{project.id}/uploads")
            assert empty.json()["files"] == []

            await client.post(
                f"/api/projects/{project.id}/uploads",
                files={"files": ("one.txt", io.BytesIO(b"1"), "text/plain")},
            )
            listed = await client.get(f"/api/projects/{project.id}/uploads")
            assert [item["name"] for item in listed.json()["files"]] == ["one.txt"]
        await app.state.app_state.aclose()

    async def test_an_upload_can_be_deleted(self, app_client):
        app, transport, project, _session, root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            up = await client.post(
                f"/api/projects/{project.id}/uploads",
                files={"files": ("gone.txt", io.BytesIO(b"x"), "text/plain")},
            )
            path = up.json()["files"][0]["path"]
            name = path.rsplit("/", 1)[-1]

            deleted = await client.delete(f"/api/projects/{project.id}/uploads/{name}")
            assert deleted.status_code == 200
            assert not (root / path).exists()
        await app.state.app_state.aclose()

    async def test_deleting_a_traversal_name_is_refused(self, app_client):
        app, transport, project, _session, _root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete(
                f"/api/projects/{project.id}/uploads/..%2F..%2Fsettings.json"
            )
            assert response.status_code in (403, 404)
        await app.state.app_state.aclose()


class TestAttachmentPrompting:
    async def test_the_stored_path_is_recorded_for_the_session(self, app_client):
        """The agent needs the path, and the transcript needs a record."""
        app, transport, project, session, _root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                f"/api/projects/{project.id}/uploads",
                data={"session_id": session.id},
                files={"files": ("q3.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            )
        messages = app.state.app_state.store.list_messages(session.id)
        assert any("q3.pdf" in message.content for message in messages), (
            "the session should record which files were attached"
        )
        await app.state.app_state.aclose()


class TestUploadsArePartOfTheProject:
    """Attachments live in the project, not in hidden tooling state.

    `.surtitle/` is excluded from directory listings and content searches so
    tooling does not pollute them. Storing attachments there made uploaded
    documents invisible in the user's own file manager *and* undiscoverable by the
    agent's `search_files`, which is wrong for files that are usually the substance
    of the work.
    """

    async def test_uploads_land_in_a_visible_project_folder(self, app_client):
        app, transport, project, _session, root = app_client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/projects/{project.id}/uploads",
                files={"files": ("q3.csv", io.BytesIO(b"a,b\n"), "text/csv")},
            )
            path = response.json()["files"][0]["path"]

        assert path == "uploads/q3.csv"
        # Visible in the project root, not hidden under a dot-directory.
        assert (root / "uploads" / "q3.csv").is_file()
        assert not any(part.startswith(".") for part in path.split("/"))
        await app.state.app_state.aclose()

    async def test_an_upload_is_discoverable_by_list_dir(self, app_client):
        app, transport, project, _session, root = app_client
        from surtitle.tools.fs_tools import ToolContext, list_dir, search_files

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                f"/api/projects/{project.id}/uploads",
                files={
                    "files": ("throughput.csv", io.BytesIO(b"machine,rate\nA,412\n"), "text/csv")
                },
            )

        ctx = ToolContext(root=root)
        entries = list_dir(ctx, "uploads")
        assert entries.ok, entries.error
        assert {entry["name"] for entry in entries.data["entries"]} == {"throughput.csv"}

        # And a content search finds it, which is the point of moving it out of
        # the hidden tooling directory.
        found = search_files(ctx, "412")
        assert found.ok
        assert found.data["match_count"] >= 1, "an uploaded file should be searchable"
        await app.state.app_state.aclose()

    async def test_an_upload_is_readable_by_read_file(self, app_client):
        app, transport, project, _session, root = app_client
        from surtitle.tools.fs_tools import ToolContext, read_file

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/projects/{project.id}/uploads",
                files={
                    "files": ("notes.md", io.BytesIO(b"# Finding\nRate is 412.\n"), "text/markdown")
                },
            )
            path = response.json()["files"][0]["path"]

        result = read_file(ToolContext(root=root), path)
        assert result.ok, result.error
        assert "412" in result.data["content"]
        await app.state.app_state.aclose()

    async def test_an_upload_survives_a_directory_listing_of_the_root(self, app_client):
        """It should appear to the agent when it looks at the project."""
        app, transport, project, _session, root = app_client
        from surtitle.tools.fs_tools import ToolContext, list_dir

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                f"/api/projects/{project.id}/uploads",
                files={"files": ("report.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            )

        root_listing = list_dir(ToolContext(root=root), ".")
        names = {entry["name"] for entry in root_listing.data["entries"]}
        assert "uploads" in names, "the uploads folder should be visible in a listing"
        await app.state.app_state.aclose()

    async def test_deleting_with_a_traversal_name_cannot_reach_outside(self, app_client):
        """`name` comes from the URL, so it must not be able to name a parent."""
        app, transport, project, _session, root = app_client
        secret = root / "important.txt"
        secret.write_text("keep me", encoding="utf-8")

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete(f"/api/projects/{project.id}/uploads/..%2Fimportant.txt")
            assert response.status_code in (403, 404)

        assert secret.read_text(encoding="utf-8") == "keep me", "a traversal delete removed a file"
        await app.state.app_state.aclose()
