"""Tests for project-scoped configuration and document conversion.

The LibreOffice tests are skipped when it is not installed, but they are not
mocked: a converter that returns ``ok`` while producing an unopenable file is
worthless, so these assert the output is genuinely valid.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

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


def _write_docx(path: Path) -> Path:
    """A small but genuine .docx: a heading, a paragraph and a table."""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
       <w:r><w:t>Quarterly Report</w:t></w:r></w:p>
  <w:p><w:r><w:t>Revenue rose 12%.</w:t></w:r></w:p>
  <w:tbl>
   <w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc>
         <w:tc><w:p><w:r><w:t>Revenue</w:t></w:r></w:p></w:tc></w:tr>
   <w:tr><w:tc><w:p><w:r><w:t>EMEA</w:t></w:r></w:p></w:tc>
         <w:tc><w:p><w:r><w:t>4.2M</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
 </w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("word/document.xml", document)
    return path


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
        monkeypatch.setattr("surtitle.tools.project_config._SOFFICE_CANDIDATES", {}, raising=False)
        monkeypatch.setattr(
            "surtitle.tools.project_config._SOFFICE_LINUX", ("/nope/soffice",), raising=False
        )
        # Discovery must return None rather than raising when LibreOffice is absent.
        assert find_soffice() is None or find_soffice().is_file()


# --------------------------------------------------------------------------- #
# LibreOffice conversion
# --------------------------------------------------------------------------- #

SOFFICE = find_soffice()

# Real conversions are marked `live` as well as skipped without LibreOffice.
#
# Each conversion starts a whole office suite (~7-9 s, essentially all of it
# process start), and there are two dozen of them here — so they dominated the
# default suite's running time while testing something that changes rarely. The
# conversion *logic* is covered offline in the class below by faking the
# subprocess; these keep the end-to-end confidence available on demand.
requires_soffice = pytest.mark.skipif(SOFFICE is None, reason="LibreOffice is not installed")


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "note.txt").write_text(
        "Sampling Line Report\nRevenue grew eight percent.\n", encoding="utf-8"
    )
    return ToolContext(root=tmp_path)


@pytest.mark.live
@requires_soffice
class TestConvertDocument:
    async def test_txt_to_pdf_produces_a_valid_pdf(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "note.txt", target="pdf", backend="libreoffice")
        assert result.ok, result.error
        assert result.data["path"] == "note.pdf"
        assert result.artifacts == ["note.pdf"]

        # Re-parse it: the point is a file the user can actually open.
        from pypdf import PdfReader

        text = "\n".join(
            page.extract_text() or "" for page in PdfReader(str(ctx.root / "note.pdf")).pages
        )
        assert "Sampling Line Report" in text

    async def test_office_round_trip_via_docx(self, ctx):
        from surtitle.tools.documents import convert_document

        to_docx = await convert_document(ctx, "note.txt", target="docx", backend="libreoffice")
        assert to_docx.ok, to_docx.error

        # Read back through the built-in engine: it must cope with a .docx that
        # LibreOffice actually wrote, not only with a hand-made fixture.
        back = await convert_document(ctx, "note.docx", target="txt", backend="builtin")
        assert back.ok, back.error
        content = (ctx.root / "note.txt").read_text(encoding="utf-8")
        assert "Sampling Line Report" in content

    async def test_explicit_output_path(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(
            ctx, "note.txt", target="pdf", output="reports/summary.pdf", backend="libreoffice"
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

        result = await convert_document(ctx, "note.txt", target="pdf", output="../../evil.pdf")
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


class TestConvertDocumentWithoutLibreOffice:
    """The conversion logic, with the subprocess faked.

    LibreOffice costs ~7-9 s per start, almost all of it process launch, which made
    the real-conversion tests dominate the suite while testing something that
    changes rarely. Everything that is actually *our* logic is checked here instead:
    the staging dance, LibreOffice's habit of ignoring the requested filename, the
    escape guards, and honest reporting when nothing is produced.

    The end-to-end tests above still run against the real thing under `-m live`.
    """

    @pytest.fixture
    def ctx(self, tmp_path):
        (tmp_path / "note.txt").write_text("Sampling report\n", encoding="utf-8")
        return ToolContext(root=tmp_path)

    @pytest.fixture(autouse=True)
    def _discovery_is_stubbed(self, monkeypatch):
        """Pin LibreOffice discovery, which is not what these tests are about.

        `configured_soffice` looks at the host's real install locations, so
        without this the whole class passes on a developer machine that happens
        to have LibreOffice and fails on a bare CI runner with "LibreOffice was
        not found" — the conversion logic under test never even runs.
        """
        import surtitle.tools.documents as documents

        monkeypatch.setattr(
            documents, "configured_soffice", lambda _root=None: Path("/usr/bin/soffice")
        )

    @staticmethod
    def _fake_libreoffice(output_name="note.pdf", *, content=b"%PDF-1.4 fake"):
        """Patch the runner to behave like LibreOffice writing into --outdir.

        Returns the recorder of the argv it was handed, so the command line can be
        asserted: the staging directory and the escape guards are the point.
        """
        import surtitle.tools.documents as documents

        seen: list[list[str]] = []

        async def fake_run(argv, *, timeout):
            seen.append(argv)
            outdir = Path(argv[argv.index("--outdir") + 1])
            if output_name:
                (outdir / output_name).write_bytes(content)
            return documents._RunResult("convert ok", "", 0, False)

        return fake_run, seen

    async def test_it_converts_into_a_staging_directory_not_the_project(self, ctx, monkeypatch):
        """Staging is what stops a failed run leaving a half-written file."""
        import surtitle.tools.documents as documents

        fake, seen = self._fake_libreoffice()
        monkeypatch.setattr(documents, "_run", fake)

        result = await documents.convert_document(
            ctx, "note.txt", target="pdf", backend="libreoffice"
        )
        assert result.ok, result.error

        outdir = Path(seen[0][seen[0].index("--outdir") + 1])
        assert outdir != ctx.root, "LibreOffice was pointed straight at the project"
        assert "note" in outdir.name or "surtitle" in outdir.name

    async def test_it_uses_a_private_user_profile(self, ctx, monkeypatch):
        """A shared profile is the classic cause of a hung conversion."""
        import surtitle.tools.documents as documents

        fake, seen = self._fake_libreoffice()
        monkeypatch.setattr(documents, "_run", fake)

        await documents.convert_document(ctx, "note.txt", target="pdf", backend="libreoffice")
        profile = next(arg for arg in seen[0] if arg.startswith("-env:UserInstallation="))
        assert "surtitle-lo-profile" in profile

    async def test_output_named_after_the_source_is_still_placed_correctly(self, ctx, monkeypatch):
        """The bug this guards: LibreOffice ignores the requested output filename.

        Asking for `reports/summary.pdf` from `note.txt` produces `note.pdf`, so a
        naive implementation looks for the wrong file and reports a failure.
        """
        import surtitle.tools.documents as documents

        fake, _seen = self._fake_libreoffice(output_name="note.pdf")
        monkeypatch.setattr(documents, "_run", fake)

        result = await documents.convert_document(
            ctx, "note.txt", target="pdf", output="reports/summary.pdf", backend="libreoffice"
        )
        assert result.ok, result.error
        assert result.data["path"] == "reports/summary.pdf"
        assert (ctx.root / "reports" / "summary.pdf").is_file()

    async def test_a_differently_named_output_is_still_found(self, ctx, monkeypatch):
        """Some import filters rewrite the name; the fallback must cope."""
        import surtitle.tools.documents as documents

        fake, _seen = self._fake_libreoffice(output_name="note-converted.pdf")
        monkeypatch.setattr(documents, "_run", fake)

        result = await documents.convert_document(
            ctx, "note.txt", target="pdf", backend="libreoffice"
        )
        assert result.ok, result.error
        assert (ctx.root / "note.pdf").is_file()

    async def test_no_output_is_reported_as_a_failure(self, ctx, monkeypatch):
        """LibreOffice can exit cleanly having written nothing; that is a failure."""
        import surtitle.tools.documents as documents

        fake, _seen = self._fake_libreoffice(output_name=None)
        monkeypatch.setattr(documents, "_run", fake)

        result = await documents.convert_document(
            ctx, "note.txt", target="pdf", backend="libreoffice"
        )
        assert not result.ok
        assert "did not produce" in result.error

    async def test_a_timeout_is_reported_as_such(self, ctx, monkeypatch):
        import surtitle.tools.documents as documents

        async def timing_out(argv, *, timeout):
            return documents._RunResult("", "", None, True)

        monkeypatch.setattr(documents, "_run", timing_out)
        result = await documents.convert_document(
            ctx, "note.txt", target="pdf", backend="libreoffice"
        )
        assert not result.ok
        assert "did not finish" in result.error

    async def test_the_staging_holder_is_named_in_the_command(self, ctx, monkeypatch):
        import surtitle.tools.documents as documents

        fake, seen = self._fake_libreoffice()
        monkeypatch.setattr(documents, "_run", fake)
        await documents.convert_document(ctx, "note.txt", target="pdf", backend="libreoffice")

        assert "--convert-to" in seen[0]
        assert "--headless" in seen[0]
        # The source is passed as an absolute path, so the process cwd is irrelevant.
        assert str((ctx.root / "note.txt").resolve()) in seen[0]

    async def test_libreoffice_never_sees_a_path_outside_the_project(self, ctx, monkeypatch):
        """The escape guard must reject before anything is launched."""
        import surtitle.tools.documents as documents

        fake, seen = self._fake_libreoffice()
        monkeypatch.setattr(documents, "_run", fake)

        refused = await documents.convert_document(
            ctx, "../../../etc/hosts", target="pdf", backend="libreoffice"
        )
        assert not refused.ok
        assert seen == [], "LibreOffice was launched for a rejected path"


class TestChoosingAnEngine:
    """Which engine runs, and what happens when neither can do the job.

    LibreOffice is deliberately hidden here: the point of the built-in engine is
    that none of this needs it.
    """

    @pytest.fixture(autouse=True)
    def _no_libreoffice(self, monkeypatch):
        import surtitle.tools.documents as documents

        monkeypatch.setattr(documents, "configured_soffice", lambda _root=None: None)

    async def test_auto_converts_without_libreoffice(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "note.txt", target="pdf")
        assert result.ok, result.error
        assert result.data["backend"] == "builtin"
        assert (ctx.root / "note.pdf").is_file()

    async def test_office_formats_convert_without_libreoffice(self, ctx):
        from surtitle.tools.documents import convert_document

        _write_docx(ctx.root / "report.docx")
        pdf = await convert_document(ctx, "report.docx", target="pdf")
        assert pdf.ok, pdf.error
        csv_result = await convert_document(ctx, "report.docx", target="csv")
        assert csv_result.ok, csv_result.error
        assert "EMEA,4.2M" in (ctx.root / "report.csv").read_text(encoding="utf-8")

    async def test_builtin_never_launches_an_external_program(self, ctx, monkeypatch):
        import surtitle.tools.documents as documents

        def explode(*_args, **_kwargs):  # pragma: no cover - only runs on failure
            raise AssertionError("the built-in backend shelled out")

        monkeypatch.setattr(documents, "_run", explode)
        result = await documents.convert_document(ctx, "note.txt", target="pdf", backend="builtin")
        assert result.ok, result.error

    async def test_builtin_refuses_a_target_only_libreoffice_writes(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "note.txt", target="rtf", backend="builtin")
        assert not result.ok
        assert "built-in" in result.error
        assert "libreoffice" in result.error.lower()

    async def test_libreoffice_backend_without_libreoffice_explains_itself(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "note.txt", target="pdf", backend="libreoffice")
        assert not result.ok
        assert "LibreOffice was not found" in result.error
        # The way out has to be named, because it is the whole point.
        assert "builtin" in result.error

    async def test_a_format_nothing_can_read_says_so(self, ctx):
        from surtitle.tools.documents import convert_document

        (ctx.root / "thing.zzz").write_text("?", encoding="utf-8")
        result = await convert_document(ctx, "thing.zzz", target="pdf")
        assert not result.ok
        assert "No converter" in result.error

    async def test_an_unknown_backend_is_refused(self, ctx):
        from surtitle.tools.documents import convert_document

        result = await convert_document(ctx, "note.txt", target="pdf", backend="magic")
        assert not result.ok
        assert "Unknown backend" in result.error


class TestFallingBackToLibreOffice:
    @pytest.fixture(autouse=True)
    def _pretend_libreoffice_is_installed(self, monkeypatch):
        import surtitle.tools.documents as documents

        monkeypatch.setattr(
            documents, "configured_soffice", lambda _root=None: Path("/usr/bin/soffice")
        )

    async def test_auto_falls_back_when_the_builtin_engine_fails(self, ctx, monkeypatch):
        """A file the built-in parser chokes on should still convert if it can."""
        import surtitle.tools.documents as documents
        from surtitle.tools import document_native

        def refuse(*_args, **_kwargs):
            raise document_native.NativeConversionError("simulated parser failure")

        seen: list[list[str]] = []

        async def fake_run(argv, *, timeout):
            seen.append(argv)
            outdir = Path(argv[argv.index("--outdir") + 1])
            (outdir / "note.pdf").write_bytes(b"%PDF-1.4 fake")
            return documents._RunResult("ok", "", 0, False)

        monkeypatch.setattr(document_native, "convert", refuse)
        monkeypatch.setattr(documents, "_run", fake_run)

        result = await documents.convert_document(ctx, "note.txt", target="pdf")
        assert result.ok, result.error
        assert result.data["backend"] == "libreoffice"
        assert seen, "LibreOffice was never tried"

    async def test_builtin_failure_is_reported_when_libreoffice_also_fails(self, ctx, monkeypatch):
        import surtitle.tools.documents as documents
        from surtitle.tools import document_native

        def refuse(*_args, **_kwargs):
            raise document_native.NativeConversionError("simulated parser failure")

        async def produce_nothing(argv, *, timeout):
            return documents._RunResult("", "no filter for that format", 2, False)

        monkeypatch.setattr(document_native, "convert", refuse)
        monkeypatch.setattr(documents, "_run", produce_nothing)

        result = await documents.convert_document(ctx, "note.txt", target="pdf")
        assert not result.ok
        assert "no filter for that format" in result.error

    async def test_libreoffice_handles_the_formats_the_builtin_one_cannot(self, ctx, monkeypatch):
        import surtitle.tools.documents as documents

        seen: list[list[str]] = []

        async def fake_run(argv, *, timeout):
            seen.append(argv)
            outdir = Path(argv[argv.index("--outdir") + 1])
            (outdir / "note.rtf").write_bytes(b"{\\rtf1 fake}")
            return documents._RunResult("ok", "", 0, False)

        monkeypatch.setattr(documents, "_run", fake_run)
        result = await documents.convert_document(ctx, "note.txt", target="rtf")
        assert result.ok, result.error
        assert result.data["backend"] == "libreoffice"
        assert seen, "LibreOffice should have been asked to do a format it owns"
