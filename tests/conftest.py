"""Pytest configuration: make the .dedup package importable from tests
without installing it, and provide shared fixtures (FakeFileSystem etc.).

Safety: nothing in this test suite touches the real archive at
D:\\Фото\\Мій архів. All filesystem interaction goes through FakeFileSystem,
an in-memory stand-in for the FileSystem protocol in fsmodel.py -- except
manifest.py, which deliberately writes manifest.csv with plain open()
rather than through the `fs` seam (see its own module docstring). Tests
that exercise that real write need a real `archive_root` for it to land
in; see `real_archive_root` below for where that goes.
"""
import shutil
import sys
from pathlib import Path

import pytest

_DEDUP_DIR = Path(__file__).resolve().parent.parent
if str(_DEDUP_DIR) not in sys.path:
    sys.path.insert(0, str(_DEDUP_DIR))

_TESTS_DIR = _DEDUP_DIR / ".tests"


@pytest.fixture
def real_archive_root(request):
    """A real, on-disk `archive_root` for fixtures whose tests reach
    manifest.py's real, non-fs-abstracted disk write (see module docstring
    above). Lives under `.dedup/.tests/<test-name>/Archive` -- never at a
    literal "D:/Archive", which resolves to a real folder at the D: drive
    root -- and is removed again after the test."""
    test_dir = _TESTS_DIR / request.node.name
    root = test_dir / "Archive"
    if test_dir.exists():
        shutil.rmtree(test_dir)
    root.mkdir(parents=True)
    yield str(root).replace("\\", "/")
    shutil.rmtree(test_dir, ignore_errors=True)
