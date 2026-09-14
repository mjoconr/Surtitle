"""Tests for project-scoped configuration and document conversion.

The LibreOffice tests are skipped when it is not installed, but they are not
mocked: a converter that returns ``ok`` while producing an unopenable file is
worthless, so these assert the output is genuinely valid.
"""

from __future__ import annotations

import json

import pytest

from surtitle.tools.fs_tools import ToolContext, read_file
from surtitle.tools.project_config import (
    CONFIG_FILENAME,
    McpServerConfig,
    ProjectConfig,
    find_soffice,
    load_project_config,
    save_project_config,
)


class TestProjectConfig:
    def test_missing_file_yields_defaults(self, tmp_path):
        config = load_project_config(tmp_path)
        assert config.soffice_path is None
        assert config.mcp_servers == []
        assert config.enabled_mcp_servers == []

    def test_round_trip(self, tmp_path):
        config = ProjectConfig(
            soffice_path="/opt/libreoffice/soffice",
            trusted_tools=["write_file", "run_python", "write_file"],
            instructions="Prefer the sampling-line naming convention.",
            ignore=["scratch"],
            mcp_servers=[
                McpServerConfig(
                    name="fusion",
                    command="fusion-mcp",
                    args=["--stdio"],
                    namespace="fusion360",
                    trusted_tools=["get_design"],
                )
            ],
        )
        save_project_config(tmp_path, config)
        loaded = load_project_config(tmp_path)

        assert loaded.soffice_path == "/opt/libreoffice/soffice"
        assert loaded.trusted_tools == ["run_python", "write_file"]  # de-duplicated
        assert loaded.instructions.startswith("Prefer the sampling")
        assert loaded.ignore == ["scratch"]
        assert len(loaded.mcp_servers) == 1
        server = loaded.mcp_servers[0]
        assert server.name == "fusion"
        assert server.command == "fusion-mcp"
        assert server.args == ["--stdio"]
        assert server.tool_prefix == "fusion360"
        assert server.trusted_tools == ["get_design"]

    def test_written_file_is_valid_json(self, tmp_path):
        save_project_config(tmp_path, ProjectConfig(trusted_tools=["read_file"]))
        raw = json.loads((tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert raw["trusted_tools"] == ["read_file"]
        # Absent values must not be written as nulls.
        assert "soffice_path" not in raw
        assert "mcp_servers" not in raw

    def test_malformed_json_degrades_to_defaults(self, tmp_path):
        (tmp_path / CONFIG_FILENAME).write_text("{not json", encoding="utf-8")
        assert load_project_config(tmp_path).mcp_servers == []

    def test_non_object_json_degrades_to_defaults(self, tmp_path):
        (tmp_path / CONFIG_FILENAME).write_text("[1,2,3]", encoding="utf-8")
        assert load_project_config(tmp_path).trusted_tools == []

    def test_server_without_command_is_skipped(self, tmp_path):
        (tmp_path / CONFIG_FILENAME).write_text(
            json.dumps({"mcp_servers": [{"name": "broken"}, {"command": "x"}, {}]}),
            encoding="utf-8",
        )
        assert load_project_config(tmp_path).mcp_servers == []

    def test_disabled_server_is_excluded(self, tmp_path):
        save_project_config(
            tmp_path,
            ProjectConfig(
                mcp_servers=[
                    McpServerConfig(name="on", command="a"),
                    McpServerConfig(name="off", command="b", enabled=False),
                ]
            ),
        )
        enabled = load_project_config(tmp_path).enabled_mcp_servers
        assert [s.name for s in enabled] == ["on"]

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Fusion 360", "fusion_360"),
            ("my-rust-mcp", "my_rust_mcp"),
            ("  spaced  ", "spaced"),
            ("!!!", "mcp"),
        ],
    )
    def test_namespace_sanitisation(self, name, expected):
        assert McpServerConfig(name=name, command="x").tool_prefix == expected

    def test_explicit_namespace_wins(self):
        server = McpServerConfig(name="a", command="x", namespace="Custom Name")
        assert server.tool_prefix == "custom_name"


class TestSofficeDiscovery:
    def test_explicit_path_is_preferred(self, tmp_path):
        fake = tmp_path / "soffice"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o755)
        assert find_soffice(str(fake)) == fake

    def test_nonexistent_explicit_path_is_ignored(self, tmp_path):
        # Should fall through to real discovery rather than returning a bad path.
        result = find_soffice(str(tmp_path / "nope"))
        assert result is None or result.is_file()

    def test_env_variable_is_honoured(self, tmp_path, monkeypatch):
        fake = tmp_path / "soffice"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("SURTITLE_SOFFICE", str(fake))
        assert find_soffice() == fake

    def test_returns_none_when_nothing_present(self, monkeypatch):
        monkeypatch.delenv("SURTITLE_SOFFICE", raising=False)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(
            "surtitle.tools.project_config._SOFFICE_CANDIDATES", {}, raising=False
        )
        monkeypatch.setattr(
            "surtitle.tools.project_config._SOFFICE_LINUX", ("/nope/soffice",), raising=False
        )
        # Discovery must return None rather than raising when LibreOffice is absent.
        assert find_soffice() is None or find_soffice().is_file()


# --------------------------------------------------------------------------- #
# LibreOffice conversion
# --------------------------------------------------------------------------- #

SOFFICE = find_soffice()
requires_soffice = pytest.mark.skipif(SOFFICE is None, reason="LibreOffice is not installed")


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "note.txt").write_text(
        "Sampling Line Report\nRevenue grew eight percent.\n", encoding="utf-8"
    )
    return ToolContext(root=tmp_path)


@requires_soffice
class TestConvertDocument:
    async def test_txt_to_pdf_produces_a_valid_pdf(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "note.txt", target="pdf")
        assert result.ok, result.error
        assert result.data["path"] == "note.pdf"
        assert result.artifacts == ["note.pdf"]

        # Re-parse it: the point is a file the user can actually open.
        from pypdf import PdfReader

        text = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(str(ctx.root / "note.pdf")).pages
        )
        assert "Sampling Line Report" in text

    async def test_office_round_trip_via_docx(self, ctx):
        from surtitle.tools.documents import convert_document

        to_docx = await convert_document(ctx, "note.txt", target="docx")
        assert to_docx.ok, to_docx.error

        back = await convert_document(ctx, "note.docx", target="txt")
        assert back.ok, back.error
        content = (ctx.root / "note.txt").read_text(encoding="utf-8")
        assert "Sampling Line Report" in content

    async def test_explicit_output_path(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(
            ctx, "note.txt", target="pdf", output="reports/summary.pdf"
        )
        assert result.ok, result.error
        assert result.data["path"] == "reports/summary.pdf"
        assert (ctx.root / "reports" / "summary.pdf").is_file()

    async def test_unknown_target_lists_the_options(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "note.txt", target="quux")
        assert not result.ok
        assert "pdf" in result.error and "docs" not in result.error

    async def test_missing_source_is_reported(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "missing.txt", target="pdf")
        assert not result.ok
        assert "not found" in result.error.lower()

    async def test_escaping_source_is_blocked(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "../../../etc/hosts", target="pdf")
        assert not result.ok

    async def test_escaping_output_is_blocked(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(
            ctx, "note.txt", target="pdf", output="../../evil.pdf"
        )
        assert not result.ok
        assert not (ctx.root.parent.parent / "evil.pdf").exists()

    async def test_same_source_and_destination_is_refused(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "note.txt", target="txt", output="note.txt")
        assert not result.ok
        assert "overwrite the source" in result.error.lower()

    async def test_overwrite_false_refuses(self, ctx):
        from surtitle.tools.documents import convert_document

        assert (await convert_document(ctx, "note.txt", target="pdf")).ok
        result = await convert_document(ctx, "note.txt", target="pdf", overwrite=False)
        assert not result.ok
        assert "already exists" in result.error.lower()

    async def test_converted_pdf_is_readable_by_read_file(self, ctx):
        """The round trip that matters: convert an Office file, then read it."""
        from surtitle.tools.documents import convert_document

        assert (await convert_document(ctx, "note.txt", target="docx")).ok
        assert (await convert_document(ctx, "note.docx", target="pdf")).ok

        read = read_file(ctx, "note.pdf")
        assert read.ok, read.error
        assert "eight percent" in read.data["content"]
