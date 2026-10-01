"""Tests for the additions to tests/fakes.py's FakeFileSystem:
move()/exists()/makedirs()/touch()/set_move_error(). These are tested in
their own right (like test_platform_win.py tests the real implementation)
because every tidy/moves/applier and manifest.py test in this suite depends on the fake
behaving exactly like a real FileSystem would.
"""
import pytest

from fakes import FakeFileSystem


class TestExists:
    def test_true_for_known_file(self):
        fs = FakeFileSystem().add_file("D:/A/f.jpg", b"x")
        assert fs.exists("D:/A/f.jpg") is True

    def test_true_for_known_dir(self):
        fs = FakeFileSystem().add_dir("D:/A")
        assert fs.exists("D:/A") is True

    def test_false_for_unknown_path(self):
        fs = FakeFileSystem()
        assert fs.exists("D:/nope") is False


class TestMakedirs:
    def test_creates_missing_directory(self):
        fs = FakeFileSystem()
        fs.makedirs("D:/A/B")
        assert fs.exists("D:/A/B") is True
        assert fs.exists("D:/A") is True

    def test_no_error_if_already_present(self):
        fs = FakeFileSystem().add_dir("D:/A")
        fs.makedirs("D:/A")  # must not raise
        assert fs.exists("D:/A") is True

    def test_new_dir_is_discoverable_via_scandir_of_parent(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.makedirs("D:/A/B/C")
        names = {e.name for e in fs.scandir("D:/A")}
        assert "B" in names


class TestTouch:
    def test_creates_empty_file_if_missing(self):
        fs = FakeFileSystem()
        fs.add_dir("D:/A")
        fs.touch("D:/A/.dedup-kosz")
        assert fs.exists("D:/A/.dedup-kosz") is True
        assert fs.stat("D:/A/.dedup-kosz").size == 0

    def test_noop_if_already_present(self):
        fs = FakeFileSystem().add_file("D:/A/f.jpg", b"already-here")
        fs.touch("D:/A/f.jpg")
        assert fs.stat("D:/A/f.jpg").size == len(b"already-here")

    def test_touched_file_is_discoverable_via_scandir(self):
        fs = FakeFileSystem().add_dir("D:/A")
        fs.touch("D:/A/.dedup-kosz")
        names = {e.name for e in fs.scandir("D:/A")}
        assert ".dedup-kosz" in names


class TestMove:
    def test_moves_stat_and_content_to_destination(self):
        fs = FakeFileSystem().add_dir("D:/A").add_dir("D:/B")
        fs.add_file("D:/A/f.jpg", b"bytes", size=5, mtime_ns=123)

        fs.move("D:/A/f.jpg", "D:/B/f.jpg")

        assert fs.exists("D:/A/f.jpg") is False
        assert fs.exists("D:/B/f.jpg") is True
        assert fs.stat("D:/B/f.jpg").size == 5
        assert fs.stat("D:/B/f.jpg").mtime_ns == 123
        assert fs.open_read("D:/B/f.jpg").read() == b"bytes"

    def test_moved_file_disappears_from_old_parent_scandir(self):
        fs = FakeFileSystem().add_dir("D:/A").add_dir("D:/B")
        fs.add_file("D:/A/f.jpg", b"x")

        fs.move("D:/A/f.jpg", "D:/B/f.jpg")

        old_names = {e.name for e in fs.scandir("D:/A")}
        new_names = {e.name for e in fs.scandir("D:/B")}
        assert "f.jpg" not in old_names
        assert "f.jpg" in new_names

    def test_logs_call_in_move_calls(self):
        fs = FakeFileSystem().add_dir("D:/A").add_dir("D:/B")
        fs.add_file("D:/A/f.jpg", b"x")

        fs.move("D:/A/f.jpg", "D:/B/f.jpg")

        assert fs.move_calls == [("D:/A/f.jpg", "D:/B/f.jpg")]

    def test_raises_file_exists_error_if_destination_already_present(self):
        fs = FakeFileSystem().add_dir("D:/A").add_dir("D:/B")
        fs.add_file("D:/A/f.jpg", b"src")
        fs.add_file("D:/B/f.jpg", b"already-there")

        with pytest.raises(FileExistsError):
            fs.move("D:/A/f.jpg", "D:/B/f.jpg")

        # Nothing should have been touched on a refused move.
        assert fs.exists("D:/A/f.jpg") is True
        assert fs.open_read("D:/B/f.jpg").read() == b"already-there"
        assert fs.move_calls == []

    def test_set_move_error_raised_for_matching_src(self):
        fs = FakeFileSystem().add_dir("D:/A").add_dir("D:/B")
        fs.add_file("D:/A/f.jpg", b"x")
        fs.set_move_error("D:/A/f.jpg", PermissionError("locked"))

        with pytest.raises(PermissionError):
            fs.move("D:/A/f.jpg", "D:/B/f.jpg")
        assert fs.move_calls == []
        assert fs.exists("D:/A/f.jpg") is True

    def test_set_move_error_can_inject_non_os_error_for_kill_simulation(self):
        class _SimulatedKill(BaseException):
            pass

        fs = FakeFileSystem().add_dir("D:/A").add_dir("D:/B")
        fs.add_file("D:/A/f.jpg", b"x")
        fs.set_move_error("D:/A/f.jpg", _SimulatedKill())

        with pytest.raises(_SimulatedKill):
            fs.move("D:/A/f.jpg", "D:/B/f.jpg")
