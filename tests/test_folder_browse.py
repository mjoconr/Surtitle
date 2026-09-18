"""The server-side directory listing behind the in-app folder picker.

The picker has to answer "where is the project folder" on machines the native
chooser cannot serve, and the shapes that matter are the ones a user hits
constantly: the parent link, the breadcrumb chain, hidden folders, a folder with
an unreadable child, and a name that is not a name.

Two hazards get their own tests. A path that is not fully qualified must be
refused rather than resolved against the server process's working directory — a
relative path would otherwise list a folder nobody chose. And Windows accepts
``\\Users`` as absolute even though it means "on whatever drive is current", which
is exactly the sort of path a caller must not be able to smuggle in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from surtitle import folder_browse


@pytest.fixture
def tree(tmp_path):
    """A small tree: two visible folders, one hidden, one file."""
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    return tmp_path


@pytest.fixture
def symlinks(tmp_path):
    """Symlinks need a privilege Windows does not grant by default.

    Creating one there raises WinError 1314, so the test would fail for a reason
    that has nothing to do with the listing.
    """
    probe = tmp_path / "probe-link"
    try:
        probe.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks without elevation")
    probe.unlink()


class TestFullyQualified:
    def test_a_posix_absolute_path_is_qualified(self):
        assert folder_browse.fully_qualified("/home/mike", platform="linux")
        assert folder_browse.fully_qualified("/", platform="linux")

    def test_a_relative_path_is_not(self):
        assert not folder_browse.fully_qualified("projects", platform="linux")
        assert not folder_browse.fully_qualified("./projects", platform="linux")

    def test_a_windows_drive_path_is_qualified(self):
        assert folder_browse.fully_qualified(r"C:\Users\mike", platform="win32")
        assert folder_browse.fully_qualified("C:/Users/mike", platform="win32")

    def test_a_rooted_drive_less_windows_path_is_not(self):
        """ "\\Users" resolves against the current drive, so it names no one place."""
        assert not folder_browse.fully_qualified(r"\Users\mike", platform="win32")

    def test_a_complete_unc_path_is_qualified(self):
        assert folder_browse.fully_qualified(r"\\server\share\folder", platform="win32")

    def test_an_incomplete_unc_path_is_not(self):
        assert not folder_browse.fully_qualified(r"\\server", platform="win32")


class TestListing:
    def test_only_directories_are_listed(self, tree):
        """A project folder is a directory; files would be noise."""
        names = [entry.name for entry in folder_browse.listing(tree).entries]
        assert names == [".hidden", "alpha", "beta"]
        assert "notes.txt" not in names

    def test_entries_are_sorted_case_insensitively(self, tmp_path):
        for name in ("Zebra", "apple", "Mango"):
            (tmp_path / name).mkdir()
        names = [entry.name for entry in folder_browse.listing(tmp_path).entries]
        assert names == ["apple", "Mango", "Zebra"]

    def test_dot_folders_are_flagged_hidden(self, tree):
        by_name = {entry.name: entry for entry in folder_browse.listing(tree).entries}
        assert by_name[".hidden"].hidden is True
        assert by_name["alpha"].hidden is False

    def test_the_windows_hidden_attribute_is_honoured(self):
        """Dot-prefixing is not how Windows marks a hidden folder."""
        assert folder_browse._is_hidden("node_modules", 0x2) is True
        assert folder_browse._is_hidden("node_modules", 0) is False

    def test_the_parent_is_offered(self, tree):
        level = folder_browse.listing(tree)
        assert level.parent == str(tree.parent)

    def test_a_filesystem_root_has_no_parent(self):
        level = folder_browse.listing("/", platform="linux")
        assert level.parent is None

    def test_the_breadcrumb_chain_runs_from_the_root(self):
        level = folder_browse.listing("/usr/share", platform="linux")
        paths = [crumb["path"] for crumb in level.crumbs]
        assert paths == ["/", "/usr", "/usr/share"]

    def test_the_root_crumb_is_labelled_by_its_path(self):
        """An empty name for "/" would be an unlabelled jump target."""
        level = folder_browse.listing("/", platform="linux")
        assert level.crumbs == [{"name": "/", "path": "/"}]

    def test_an_existing_path_is_reported_as_an_error(self, tmp_path):
        with pytest.raises(folder_browse.BrowserError) as caught:
            folder_browse.listing(tmp_path / "nope")
        assert caught.value.code == "unreadable"

    def test_a_file_is_not_a_folder(self, tree):
        with pytest.raises(folder_browse.BrowserError):
            folder_browse.listing(tree / "notes.txt")

    def test_a_relative_path_is_refused(self):
        with pytest.raises(folder_browse.BrowserError) as caught:
            folder_browse.listing("somewhere/relative")
        assert caught.value.code == "not-fully-qualified"

    def test_a_dir_less_windows_path_is_refused(self):
        with pytest.raises(folder_browse.BrowserError) as caught:
            folder_browse.listing(r"\Users", platform="win32")
        assert caught.value.code == "not-fully-qualified"

    def test_an_unreadable_child_does_not_lose_the_level(self, tree):
        """One bad entry is not worth the whole listing."""
        locked = tree / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        try:
            level = folder_browse.listing(tree)
        finally:
            os.chmod(locked, 0o755)
        assert "alpha" in [entry.name for entry in level.entries]

    def test_a_symlink_to_a_directory_is_followed(self, tree, symlinks):
        link = tree / "shortcut"
        link.symlink_to(tree / "alpha", target_is_directory=True)
        names = [entry.name for entry in folder_browse.listing(tree).entries]
        assert "shortcut" in names

    def test_a_broken_symlink_is_skipped(self, tree, symlinks):
        (tree / "dangling").symlink_to(tree / "gone", target_is_directory=True)
        names = [entry.name for entry in folder_browse.listing(tree).entries]
        assert "dangling" not in names

    def test_a_large_level_is_truncated_rather_than_stalled(self, tmp_path):
        for index in range(12):
            (tmp_path / f"dir-{index:02d}").mkdir()
        level = folder_browse.listing(tmp_path, max_entries=5)
        assert len(level.entries) == 5
        assert level.truncated is True
        # The head of the sorted level, so the answer is still useful.
        assert [entry.name for entry in level.entries] == [
            "dir-00",
            "dir-01",
            "dir-02",
            "dir-03",
            "dir-04",
        ]

    def test_a_complete_level_is_not_marked_truncated(self, tree):
        assert folder_browse.listing(tree).truncated is False

    def test_the_home_directory_is_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        level = folder_browse.listing()
        assert level.path == str(tmp_path)
        assert level.home == str(tmp_path)

    def test_the_answer_serialises_to_json_shapes(self, tree):
        payload = folder_browse.listing(tree).to_dict()
        assert set(payload) >= {"path", "parent", "crumbs", "entries", "roots", "home", "truncated"}
        assert payload["entries"][0]["path"].startswith(str(tree))


class TestRoots:
    def test_posix_has_one_root(self):
        assert folder_browse.roots(platform="linux") == ["/"]

    def test_windows_drive_letters_come_from_the_bitmask(self):
        """Bit 2 is C:, bit 3 is D:; the mapping is the whole risk here."""
        assert folder_browse._letters_from_mask(0b1100) == ["C:\\", "D:\\"]

    def test_an_empty_bitmask_is_no_drives(self):
        assert folder_browse._letters_from_mask(0) == []


class TestCreateDirectory:
    def test_a_child_is_created(self, tree):
        made = folder_browse.create_directory(tree, "gamma")
        assert made.is_dir()
        assert made.name == "gamma"

    def test_surrounding_whitespace_is_trimmed(self, tree):
        assert folder_browse.create_directory(tree, "  gamma  ").name == "gamma"

    def test_an_existing_name_is_refused(self, tree):
        with pytest.raises(folder_browse.BrowserError) as caught:
            folder_browse.create_directory(tree, "alpha")
        assert caught.value.code == "exists"

    def test_a_separator_is_refused(self, tree):
        """Otherwise a "name" could create a tree, or escape the parent."""
        with pytest.raises(folder_browse.BrowserError) as caught:
            folder_browse.create_directory(tree, "a/b")
        assert caught.value.code == "bad-name"
        assert not (tree / "a").exists()

    def test_a_backslash_is_refused(self, tree):
        with pytest.raises(folder_browse.BrowserError):
            folder_browse.create_directory(tree, "a\\b")

    def test_dot_dot_is_refused(self, tree):
        with pytest.raises(folder_browse.BrowserError):
            folder_browse.create_directory(tree, "..")

    def test_an_empty_name_is_refused(self, tree):
        with pytest.raises(folder_browse.BrowserError):
            folder_browse.create_directory(tree, "   ")

    def test_windows_illegal_characters_are_refused_there(self, tmp_path):
        with pytest.raises(folder_browse.BrowserError) as caught:
            folder_browse.create_directory(tmp_path, "a:b", platform="win32")
        assert caught.value.code == "bad-name"

    def test_a_trailing_dot_is_refused_on_windows(self, tmp_path):
        """Windows silently strips it, so the folder would not be named as asked."""
        with pytest.raises(folder_browse.BrowserError):
            folder_browse.create_directory(tmp_path, "gamma.", platform="win32")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="a colon is not a legal character in a Windows file name",
    )
    def test_a_colon_is_allowed_on_posix(self, tmp_path):
        assert folder_browse.create_directory(tmp_path, "a:b", platform="linux").is_dir()

    def test_a_missing_parent_is_a_failure_not_a_tree(self, tree):
        with pytest.raises(folder_browse.BrowserError) as caught:
            folder_browse.create_directory(tree / "gone" / "deeper", "gamma")
        assert caught.value.code == "unreadable"
