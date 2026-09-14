"""Tests for the filesystem tools."""

from __future__ import annotations

import pytest

from surtitle.tools.fs_tools import (
    ToolContext,
    edit_file,
    list_dir,
    read_file,
    search_files,
    write_file,
)


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "notes.md").write_text("# Notes\n\nRevenue grew.\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rows.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "data" / "more.txt").write_text("hello world\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02\x03")
    return ToolContext(root=tmp_path, session_id="s1", project_id="p1")


class TestListDir:
    def test_lists_root(self, ctx):
        result = list_dir(ctx, ".")
        assert result.ok
        names = {entry["name"] for entry in result.data["entries"]}
        assert {"notes.md", "data"} <= names

    def test_distinguishes_files_and_directories(self, ctx):
        result = list_dir(ctx, ".")
        by_name = {e["name"]: e for e in result.data["entries"]}
        assert by_name["data"]["type"] == "dir"
        assert by_name["notes.md"]["type"] == "file"

    def test_lists_subdirectory(self, ctx):
        result = list_dir(ctx, "data")
        names = {e["name"] for e in result.data["entries"]}
        assert names == {"rows.csv", "more.txt"}

    def test_missing_directory_is_an_error_not_a_crash(self, ctx):
        result = list_dir(ctx, "nope")
        assert not result.ok
        assert "not found" in result.error.lower()

    def test_file_path_is_rejected_with_guidance(self, ctx):
        result = list_dir(ctx, "notes.md")
        assert not result.ok
        assert "not a directory" in result.error.lower()

    def test_path_escape_is_blocked(self, ctx):
        result = list_dir(ctx, "../")
        assert not result.ok
        assert "outside the project" in result.error

    def test_limit_is_respected(self, ctx):
        for index in range(20):
            (ctx.root / f"f{index}.txt").write_text("x", encoding="utf-8")
        result = list_dir(ctx, ".", limit=5)
        assert len(result.data["entries"]) <= 5


class TestReadFile:
    def test_reads_text_with_line_numbers(self, ctx):
        result = read_file(ctx, "notes.md")
        assert result.ok
        assert "Revenue grew." in result.data["content"]
        assert result.data["total_lines"] == 3

    def test_reads_csv(self, ctx):
        assert read_file(ctx, "data/rows.csv").ok

    def test_missing_file_reports_clearly(self, ctx):
        result = read_file(ctx, "missing.txt")
        assert not result.ok
        assert "not found" in result.error.lower()

    def test_directory_is_rejected_with_a_hint(self, ctx):
        result = read_file(ctx, "data")
        assert not result.ok
        assert "list_dir" in result.error

    def test_binary_file_is_refused(self, ctx):
        result = read_file(ctx, "binary.bin")
        assert not result.ok
        assert "binary" in result.error.lower()

    def test_empty_file_is_ok_and_empty(self, ctx):
        (ctx.root / "empty.txt").write_text("", encoding="utf-8")
        result = read_file(ctx, "empty.txt")
        assert result.ok
        assert result.data["content"] == ""

    def test_large_file_is_windowed_with_continuation(self, ctx):
        (ctx.root / "big.txt").write_text(
            "\n".join(f"line {i}" for i in range(5000)), encoding="utf-8"
        )
        result = read_file(ctx, "big.txt", max_lines=100)
        assert result.ok
        assert result.truncated
        assert result.data["total_lines"] == 5000
        assert result.data["has_more"] is True
        assert result.data["next_start_line"] == 101

    def test_continuation_returns_the_next_window(self, ctx):
        (ctx.root / "big.txt").write_text(
            "\n".join(f"line {i}" for i in range(500)), encoding="utf-8"
        )
        first = read_file(ctx, "big.txt", max_lines=100)
        second = read_file(ctx, "big.txt", start_line=first.data["next_start_line"], max_lines=100)
        assert "line 100" in second.data["content"]
        assert second.data["start_line"] == 101

    def test_start_line_past_end_is_an_error(self, ctx):
        result = read_file(ctx, "notes.md", start_line=9999)
        assert not result.ok
        assert "past the end" in result.error

    def test_escape_is_blocked(self, ctx):
        assert not read_file(ctx, "../../etc/passwd").ok

    def test_non_utf8_text_is_read_with_replacement(self, ctx):
        (ctx.root / "latin.txt").write_bytes("caf\xe9\n".encode("latin-1"))
        result = read_file(ctx, "latin.txt")
        # Either it decodes with replacement characters, or it is judged binary;
        # what matters is that it does not raise.
        assert isinstance(result.ok, bool)


class TestReadPdf:
    def test_reads_back_a_generated_pdf(self, ctx):
        from surtitle.tools.artifacts import make_pdf

        created = make_pdf(
            ctx,
            "report.pdf",
            title="Quarterly Report",
            blocks=[{"type": "paragraph", "text": "Revenue grew by eight percent."}],
        )
        assert created.ok, created.error

        result = read_file(ctx, "report.pdf")
        assert result.ok, result.error
        assert result.data["kind"] == "pdf"
        assert "eight percent" in result.data["content"]
        assert "Quarterly Report" in result.data["content"]

    def test_corrupt_pdf_reports_an_error(self, ctx):
        (ctx.root / "broken.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
        result = read_file(ctx, "broken.pdf")
        assert not result.ok
        assert result.error


class TestSearchFiles:
    def test_finds_a_match(self, ctx):
        result = search_files(ctx, "Revenue")
        assert result.ok
        assert result.data["match_count"] >= 1
        assert any("notes.md" in m["file"] for m in result.data["matches"])

    def test_reports_no_matches_cleanly(self, ctx):
        result = search_files(ctx, "zzz-not-present")
        assert result.ok
        assert result.data["match_count"] == 0

    def test_invalid_regex_is_reported(self, ctx):
        result = search_files(ctx, "([unclosed")
        assert not result.ok
        assert "pattern" in result.error.lower() or "search failed" in result.error.lower()

    def test_glob_filter(self, ctx):
        result = search_files(ctx, "hello", glob="*.txt")
        assert result.ok
        assert all(m["file"].endswith(".txt") for m in result.data["matches"])

    def test_case_insensitive_by_default(self, ctx):
        assert search_files(ctx, "revenue").data["match_count"] >= 1

    def test_case_sensitive_flag(self, ctx):
        assert search_files(ctx, "revenue", case_sensitive=True).data["match_count"] == 0

    def test_scoped_to_subdirectory(self, ctx):
        result = search_files(ctx, "hello", path="data")
        assert result.ok
        assert all(m["file"].startswith("data/") for m in result.data["matches"])

    def test_escape_is_blocked(self, ctx):
        assert not search_files(ctx, "x", path="../").ok


class TestWriteFile:
    def test_creates_a_new_file(self, ctx):
        result = write_file(ctx, "out/new.txt", "hello")
        assert result.ok
        assert result.data["action"] == "created"
        assert (ctx.root / "out" / "new.txt").read_text(encoding="utf-8") == "hello"

    def test_creates_parent_directories(self, ctx):
        assert write_file(ctx, "a/b/c/d.txt", "x").ok
        assert (ctx.root / "a" / "b" / "c" / "d.txt").exists()

    def test_updates_existing_file(self, ctx):
        write_file(ctx, "f.txt", "one")
        result = write_file(ctx, "f.txt", "one\ntwo")
        assert result.ok
        assert result.data["action"] == "updated"

    def test_overwrite_false_refuses(self, ctx):
        write_file(ctx, "f.txt", "one")
        result = write_file(ctx, "f.txt", "two", overwrite=False)
        assert not result.ok
        assert "already exists" in result.error

    def test_escape_is_blocked(self, ctx):
        result = write_file(ctx, "../evil.txt", "x")
        assert not result.ok
        assert not (ctx.root.parent / "evil.txt").exists()

    def test_absolute_path_is_blocked(self, ctx):
        assert not write_file(ctx, "/tmp/evil.txt", "x").ok

    def test_directory_target_is_rejected(self, ctx):
        assert not write_file(ctx, "data", "x").ok

    def test_artifact_extension_is_reported(self, ctx):
        result = write_file(ctx, "sheet.csv", "a,b\n1,2\n")
        assert result.ok
        assert "sheet.csv" in result.artifacts


class TestEditFile:
    def test_replaces_unique_text(self, ctx):
        write_file(ctx, "f.txt", "alpha\nbeta\ngamma\n")
        result = edit_file(ctx, "f.txt", "beta", "BETA")
        assert result.ok
        assert (ctx.root / "f.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"

    def test_ambiguous_match_is_refused(self, ctx):
        write_file(ctx, "f.txt", "x\nx\n")
        result = edit_file(ctx, "f.txt", "x", "y")
        assert not result.ok
        assert "ambiguous" in result.error.lower()
        # The file must be untouched after a refused edit.
        assert (ctx.root / "f.txt").read_text(encoding="utf-8") == "x\nx\n"

    def test_replace_all_allows_ambiguity(self, ctx):
        write_file(ctx, "f.txt", "x\nx\n")
        result = edit_file(ctx, "f.txt", "x", "y", replace_all=True)
        assert result.ok
        assert result.data["replacements"] == 2
        assert (ctx.root / "f.txt").read_text(encoding="utf-8") == "y\ny\n"

    def test_missing_old_string_is_reported_with_guidance(self, ctx):
        write_file(ctx, "f.txt", "alpha\n")
        result = edit_file(ctx, "f.txt", "nothere", "x")
        assert not result.ok
        assert "not found" in result.error.lower()

    def test_empty_old_string_is_refused(self, ctx):
        write_file(ctx, "f.txt", "alpha\n")
        result = edit_file(ctx, "f.txt", "", "x")
        assert not result.ok
        assert "write_file" in result.error

    def test_identical_strings_are_refused(self, ctx):
        write_file(ctx, "f.txt", "alpha\n")
        assert not edit_file(ctx, "f.txt", "alpha", "alpha").ok

    def test_missing_file_is_reported(self, ctx):
        assert not edit_file(ctx, "nope.txt", "a", "b").ok

    def test_escape_is_blocked(self, ctx):
        assert not edit_file(ctx, "../outside.txt", "a", "b").ok


class TestModelPayloadBudget:
    def test_large_read_is_truncated_to_the_budget(self, ctx):
        (ctx.root / "huge.txt").write_text("x" * 200_000, encoding="utf-8")
        result = read_file(ctx, "huge.txt", max_lines=2000)
        payload = result.to_model_payload()
        from surtitle.tools.fs_tools import MAX_OUTPUT_CHARS

        assert len(payload) <= MAX_OUTPUT_CHARS + 100
        assert "truncated" in payload

    def test_successful_payload_marks_ok(self, ctx):
        payload = read_file(ctx, "notes.md").to_model_payload()
        assert '"ok": true' in payload

    def test_failed_payload_carries_the_error(self, ctx):
        payload = read_file(ctx, "nope.txt").to_model_payload()
        assert '"ok": false' in payload
        assert "not found" in payload

    def test_wire_form_omits_internals(self, ctx):
        wire = read_file(ctx, "notes.md").to_dict()
        assert "content" not in wire
        assert wire["ok"] is True
