"""Tests for fsmodel.py: StatInfo / DirEntryInfo dataclasses and the
FileSystem Protocol shape. Pure in-memory, no real filesystem access.
"""
import dataclasses

import pytest

from tidy.fs.fsmodel import StatInfo, DirEntryInfo, FileSystem


def make_stat(**overrides):
    defaults = dict(
        size=1234,
        mtime_ns=1_700_000_000_000_000_000,
        volume_id=1,
        file_index=42,
        nlink=1,
        attributes=0,
        is_dir=False,
        is_reparse_point=False,
        reparse_tag=None,
    )
    defaults.update(overrides)
    return StatInfo(**defaults)


class TestStatInfoIsFrozenDataclass:
    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(StatInfo)

    def test_is_frozen_raises_on_mutation(self):
        st = make_stat()
        with pytest.raises(dataclasses.FrozenInstanceError):
            st.size = 999

    def test_fields_roundtrip(self):
        st = make_stat(size=100, mtime_ns=5, volume_id=7, file_index=8, nlink=2,
                        attributes=0x20, is_dir=True, is_reparse_point=False,
                        reparse_tag=None)
        assert st.size == 100
        assert st.mtime_ns == 5
        assert st.volume_id == 7
        assert st.file_index == 8
        assert st.nlink == 2
        assert st.attributes == 0x20
        assert st.is_dir is True
        assert st.is_reparse_point is False
        assert st.reparse_tag is None

    def test_volume_id_and_file_index_may_be_none(self):
        st = make_stat(volume_id=None, file_index=None)
        assert st.volume_id is None
        assert st.file_index is None


class TestStatInfoIsOffline:
    @pytest.mark.parametrize("bit", [0x1000, 0x400000, 0x40000])
    def test_each_offline_bit_alone_is_offline(self, bit):
        st = make_stat(attributes=bit)
        assert st.is_offline is True

    def test_no_offline_bits_is_not_offline(self):
        st = make_stat(attributes=0x20)  # FILE_ATTRIBUTE_ARCHIVE only
        assert st.is_offline is False

    def test_zero_attributes_is_not_offline(self):
        st = make_stat(attributes=0)
        assert st.is_offline is False

    def test_offline_bit_combined_with_unrelated_bits_is_offline(self):
        st = make_stat(attributes=0x20 | 0x1000)
        assert st.is_offline is True

    def test_all_three_bits_together_is_offline(self):
        st = make_stat(attributes=0x1000 | 0x400000 | 0x40000)
        assert st.is_offline is True


class TestDirEntryInfo:
    def test_is_a_dataclass(self):
        assert dataclasses.is_dataclass(DirEntryInfo)

    def test_is_frozen(self):
        entry = DirEntryInfo(path="C:/x/y.jpg", name="y.jpg", stat=make_stat())
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.name = "z.jpg"

    def test_fields_roundtrip(self):
        st = make_stat(is_dir=True)
        entry = DirEntryInfo(path="C:/x/sub", name="sub", stat=st)
        assert entry.path == "C:/x/sub"
        assert entry.name == "sub"
        assert entry.stat is st

    def test_supports_cyrillic_and_long_paths(self):
        long_name = "a" * 300
        path = f"C:/Фото/{long_name}.jpg"
        entry = DirEntryInfo(path=path, name=f"{long_name}.jpg", stat=make_stat())
        assert "Фото" in entry.path
        assert len(entry.name) == 304


class TestFileSystemProtocolShape:
    def test_protocol_declares_scandir_stat_open_read(self):
        # FileSystem is a typing.Protocol used as the test seam; verify the
        # expected method names exist on the Protocol itself (structural
        # typing contract), not that it's instantiable.
        for method in ("scandir", "stat", "open_read"):
            assert hasattr(FileSystem, method), f"FileSystem missing {method}"

    def test_fake_filesystem_can_satisfy_protocol_structurally(self):
        class MinimalFS:
            def scandir(self, path):
                return []

            def stat(self, path):
                return make_stat()

            def open_read(self, path):
                import io
                return io.BytesIO(b"")

        fs: FileSystem = MinimalFS()
        assert list(fs.scandir("whatever")) == []
        assert fs.stat("whatever").size == 1234
