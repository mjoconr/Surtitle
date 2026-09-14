"""Tests for project-root confinement.

These are the security boundary tests: if any of them regress, the agent can
read or write outside the directory the user attached it to.
"""

from __future__ import annotations

import os

import pytest

from surtitle.tools.path_guard import (
    PathEscapeError,
    is_probably_binary,
    resolve_in_root,
)


@pytest.fixture
def root(tmp_path):
    """A project root with a file and a nested directory."""
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("deep", encoding="utf-8")
    return tmp_path


class TestAllowedPaths:
    @pytest.mark.parametrize(
        "requested",
        ["notes.txt", "./notes.txt", "sub/deep.txt", "sub/../notes.txt", "sub", "."],
    )
    def test_paths_inside_root_resolve(self, root, requested):
        resolved = resolve_in_root(root, requested)
        assert resolved.absolute == (root / requested).resolve() or resolved.relative in {
            "",
            "notes.txt",
            "sub",
            "sub/deep.txt",
        }

    def test_root_itself_is_allowed(self, root):
        assert resolve_in_root(root, ".").relative == ""

    def test_new_file_does_not_need_to_exist(self, root):
        resolved = resolve_in_root(root, "reports/q3.pdf")
        assert resolved.relative == "reports/q3.pdf"
        assert not resolved.absolute.exists()

    def test_relative_is_posix_style(self, root):
        assert resolve_in_root(root, "sub/deep.txt").relative == "sub/deep.txt"

    def test_backslash_separators_are_normalised(self, root):
        # Windows-style input must resolve identically, not be treated literally.
        assert resolve_in_root(root, "sub\\deep.txt").relative == "sub/deep.txt"

    def test_result_is_str_friendly_for_the_model(self, root):
        assert str(resolve_in_root(root, "notes.txt")) == "notes.txt"


class TestTraversalRejection:
    @pytest.mark.parametrize(
        "requested",
        [
            "../outside.txt",
            "../../etc/passwd",
            "sub/../../outside.txt",
            "..",
            "../",
            "sub/../..",
            "sub\\..\\..\\outside.txt",
        ],
    )
    def test_traversal_is_refused(self, root, requested):
        with pytest.raises(PathEscapeError):
            resolve_in_root(root, requested)

    def test_traversal_that_returns_to_root_is_allowed(self, root):
        """Climbing out and back in is harmless; only the endpoint matters."""
        assert resolve_in_root(root, "sub/../notes.txt").relative == "notes.txt"

    def test_odd_but_contained_names_are_allowed(self, root):
        """`....` is a legal directory name, not traversal, and stays in root."""
        assert resolve_in_root(root, "....//outside.txt").relative == "..../outside.txt"

    @pytest.mark.parametrize(
        "requested",
        [
            "/etc/passwd",
            "/",
            "//server/share/file.txt",
            "C:/Windows/System32/config",
            "C:\\Windows\\System32\\config",
            "c:/windows/system32",
            r"\\?\C:\Windows",
            "~/.ssh/id_rsa",
            "~/secrets.txt",
        ],
    )
    def test_absolute_and_device_paths_are_refused(self, root, requested):
        with pytest.raises(PathEscapeError):
            resolve_in_root(root, requested)

    def test_empty_path_is_refused(self, root):
        with pytest.raises(PathEscapeError):
            resolve_in_root(root, "")

    def test_error_message_names_the_path_and_reason(self, root):
        with pytest.raises(PathEscapeError) as excinfo:
            resolve_in_root(root, "../secrets.txt")
        message = str(excinfo.value)
        assert "../secrets.txt" in message
        assert "outside the project" in message


class TestSymlinkRejection:
    def test_symlink_to_file_outside_root_is_refused(self, root, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside")
        secret = outside / "secret.txt"
        secret.write_text("classified", encoding="utf-8")

        link = root / "shortcut.txt"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not permitted on this platform/account")

        with pytest.raises(PathEscapeError):
            resolve_in_root(root, "shortcut.txt")

    def test_symlink_to_directory_outside_root_is_refused(self, root, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside")
        (outside / "loot.txt").write_text("x", encoding="utf-8")

        link = root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not permitted on this platform/account")

        with pytest.raises(PathEscapeError):
            resolve_in_root(root, "escape/loot.txt")

    def test_symlink_within_root_is_allowed(self, root):
        link = root / "alias.txt"
        try:
            link.symlink_to(root / "notes.txt")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not permitted on this platform/account")

        resolved = resolve_in_root(root, "alias.txt")
        assert resolved.relative == "notes.txt"  # resolved to the real target


class TestRootContainment:
    def test_sibling_directory_with_shared_prefix_is_refused(self, tmp_path):
        """`/tmp/abc-evil` must not be treated as inside `/tmp/abc`."""
        root = tmp_path / "abc"
        root.mkdir()
        sibling = tmp_path / "abc-evil"
        sibling.mkdir()
        (sibling / "x.txt").write_text("nope", encoding="utf-8")

        with pytest.raises(PathEscapeError):
            resolve_in_root(root, "../abc-evil/x.txt")

    def test_root_itself_passes(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        assert resolve_in_root(root, ".").absolute == root.resolve()

    def test_nested_root_is_enforced(self, tmp_path):
        root = tmp_path / "proj" / "inner"
        root.mkdir(parents=True)
        with pytest.raises(PathEscapeError):
            resolve_in_root(root, "../outer.txt")


class TestBinaryDetection:
    def test_text_file_is_not_binary(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("plain ascii text\n", encoding="utf-8")
        assert is_probably_binary(path) is False

    def test_file_with_nul_bytes_is_binary(self, tmp_path):
        path = tmp_path / "a.bin"
        path.write_bytes(b"\x00\x01\x02binary")
        assert is_probably_binary(path) is True

    def test_invalid_utf8_is_binary(self, tmp_path):
        path = tmp_path / "a.dat"
        path.write_bytes(b"\xff\xfe\x00\x00garbage")
        assert is_probably_binary(path) is True

    def test_empty_file_is_not_binary(self, tmp_path):
        path = tmp_path / "empty.txt"
        path.write_bytes(b"")
        assert is_probably_binary(path) is False

    def test_missing_file_is_treated_as_binary(self, tmp_path):
        assert is_probably_binary(tmp_path / "nope.txt") is True

    def test_utf8_text_with_accents_is_not_binary(self, tmp_path):
        path = tmp_path / "unicode.txt"
        path.write_text("héllo wörld — 日本語\n", encoding="utf-8")
        assert is_probably_binary(path) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific path handling")
class TestWindowsSpecifics:
    def test_drive_relative_path_is_refused(self, root):
        with pytest.raises(PathEscapeError):
            resolve_in_root(root, "C:file.txt")

    def test_unc_path_is_refused(self, root):
        with pytest.raises(PathEscapeError):
            resolve_in_root(root, r"\\server\share\file.txt")
