"""Fixtures shared by the review tests."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
import pytest
from tidy.fs import platform_win
from tidy.review import server as review_server
from fakes import FakeFileSystem
from tidy.fs.fsmodel import StatInfo
from tidy.duplicates.keeper import Discard, KeeperDecision
from tidy.duplicates.scanner import ScannedFile
from tidy.duplicates.tiers import DuplicateGroup

from .helpers import (
    _FFMPEG_EXE,
    _HAS_FFMPEG,
    _jpeg_bytes,
    _publish_series_result,
    _series_group,
    _series_member,
)


# --- real HTTP server, real socket ---------------------------------------

@pytest.fixture
def running_server(tmp_path, real_archive_root):
    fs = FakeFileSystem()
    fs.add_dir(real_archive_root)
    fs.add_file(f"{real_archive_root}/a.jpg", _jpeg_bytes(), volume_id=1, file_index=1)
    fs.add_file(f"{real_archive_root}/b.jpg", _jpeg_bytes(), volume_id=1, file_index=2)
    group = DuplicateGroup(
        size=100, full_hash="hash1",
        paths=(f"{real_archive_root}/a.jpg", f"{real_archive_root}/b.jpg"),
    )
    decision = KeeperDecision(
        keeper=f"{real_archive_root}/a.jpg",
        discarded=(Discard(path=f"{real_archive_root}/b.jpg", reason="dup-exact"),),
    )

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root=real_archive_root,
        duplicate_groups=[group],
        decisions=[decision],
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
    )
    try:
        yield httpd, state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def running_server_with_candidates(tmp_path):
    # Unlike `running_server` above (which leaves
    # db_path/candidates at their defaults, so /api/series and /api/events
    # short-circuit straight to "ready" with an empty list), this fixture
    # passes a real db_path (a fresh sqlite file Cache() can open) and one
    # candidate -- so the lazy computation actually reaches
    # series.find_series()/events.find_events(), which tests that
    # monkeypatch those functions need in order to observe "computing" and
    # count real invocations.
    fs = FakeFileSystem()
    fs.add_dir("D:/Archive")
    fs.add_file("D:/Archive/a.jpg", _jpeg_bytes(), volume_id=1, file_index=1)
    fs.add_file("D:/Archive/b.jpg", _jpeg_bytes(), volume_id=1, file_index=2)
    group = DuplicateGroup(size=100, full_hash="hash1", paths=("D:/Archive/a.jpg", "D:/Archive/b.jpg"))
    decision = KeeperDecision(
        keeper="D:/Archive/a.jpg",
        discarded=(Discard(path="D:/Archive/b.jpg", reason="dup-exact"),),
    )
    candidate = ScannedFile(
        path="D:/Archive/a.jpg",
        stat=StatInfo(
            size=100, mtime_ns=0, volume_id=1, file_index=1, nlink=1,
            attributes=0, is_dir=False, is_reparse_point=False, reparse_tag=None,
        ),
        is_duplicate_candidate=True,
    )

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root="D:/Archive",
        duplicate_groups=[group],
        decisions=[decision],
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
        candidates=[candidate],
        db_path=str(tmp_path / "cache.db"),
    )
    try:
        yield httpd, state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def running_server_many_groups(tmp_path):
    # 120 synthetic groups, deliberately using an unsupported extension
    # (.bin) so no thumbnail decoding/file I/O is needed -- this fixture
    # exists purely to exercise client-side pagination, not thumbnails or
    # the keeper/discard file layer.
    fs = FakeFileSystem()
    fs.add_dir("D:/Archive")
    groups = []
    decisions = []
    for i in range(120):
        keeper = f"D:/Archive/g{i}/a.bin"
        discard = f"D:/Archive/g{i}/b.bin"
        groups.append(DuplicateGroup(size=10, full_hash=f"hash{i}", paths=(keeper, discard)))
        decisions.append(
            KeeperDecision(keeper=keeper, discarded=(Discard(path=discard, reason="dup-exact"),))
        )

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root="D:/Archive",
        duplicate_groups=groups,
        decisions=decisions,
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
    )
    try:
        yield httpd, state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def running_server_multi(tmp_path):
    # Two real groups, one of them with two
    # discards -- exercises selective apply (a subset of groups, and a
    # subset of discard_ids within a single group) and the id-registry
    # validation paths (unknown id / wrong-group id / missing keeper).
    # Uses a tmp_path-based archive_root (not the "D:/Archive" fixture
    # string other fixtures in this module use) so manifest.py's real,
    # non-fs-abstracted disk writes land in pytest's own sandboxed temp
    # dir, never anywhere near a real archive.
    archive_root = str(tmp_path / "Archive").replace("\\", "/")
    fs = FakeFileSystem()
    fs.add_dir(archive_root)
    fs.add_dir(f"{archive_root}/g1")
    fs.add_dir(f"{archive_root}/g2")
    fs.add_file(f"{archive_root}/g1/a.jpg", _jpeg_bytes(), volume_id=1, file_index=1)
    fs.add_file(f"{archive_root}/g1/b.jpg", _jpeg_bytes(), volume_id=1, file_index=2)
    fs.add_file(f"{archive_root}/g1/c.jpg", _jpeg_bytes(), volume_id=1, file_index=3)
    fs.add_file(f"{archive_root}/g2/a.jpg", _jpeg_bytes(), volume_id=1, file_index=4)
    fs.add_file(f"{archive_root}/g2/b.jpg", _jpeg_bytes(), volume_id=1, file_index=5)

    groups = [
        DuplicateGroup(
            size=100, full_hash="hashg1",
            paths=(f"{archive_root}/g1/a.jpg", f"{archive_root}/g1/b.jpg", f"{archive_root}/g1/c.jpg"),
        ),
        DuplicateGroup(
            size=100, full_hash="hashg2",
            paths=(f"{archive_root}/g2/a.jpg", f"{archive_root}/g2/b.jpg"),
        ),
    ]
    decisions = [
        KeeperDecision(
            keeper=f"{archive_root}/g1/a.jpg",
            discarded=(
                Discard(path=f"{archive_root}/g1/b.jpg", reason="dup-exact"),
                Discard(path=f"{archive_root}/g1/c.jpg", reason="dup-exact"),
            ),
        ),
        KeeperDecision(
            keeper=f"{archive_root}/g2/a.jpg",
            discarded=(Discard(path=f"{archive_root}/g2/b.jpg", reason="dup-exact"),),
        ),
    ]

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root=archive_root,
        duplicate_groups=groups,
        decisions=decisions,
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
    )
    try:
        yield httpd, state, fs
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def running_server_four_groups(tmp_path):
    # Four real groups on a tmp_path-backed
    # archive_root (manifest.py does real disk I/O independent of the fs
    # seam -- same reasoning as running_server_multi above, a fake
    # "D:/Archive" root like running_server_many_groups uses would make
    # apply_batch()'s real manifest.csv write fail). Four disjoint
    # single-discard groups are enough to simulate two "pages" worth of
    # selective, independent applies (group 1-2 vs 3-4) without this
    # fixture needing to know anything about pagination itself -- that
    # arithmetic is client-side only and never reaches the server.
    archive_root = str(tmp_path / "Archive").replace("\\", "/")
    fs = FakeFileSystem()
    fs.add_dir(archive_root)
    groups = []
    decisions = []
    for i in range(1, 5):
        fs.add_dir(f"{archive_root}/g{i}")
        keeper = f"{archive_root}/g{i}/a.jpg"
        discard = f"{archive_root}/g{i}/b.jpg"
        fs.add_file(keeper, _jpeg_bytes(), volume_id=1, file_index=i * 2 - 1)
        fs.add_file(discard, _jpeg_bytes(), volume_id=1, file_index=i * 2)
        groups.append(DuplicateGroup(size=100, full_hash=f"hash{i}", paths=(keeper, discard)))
        decisions.append(
            KeeperDecision(keeper=keeper, discarded=(Discard(path=discard, reason="dup-exact"),))
        )

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root=archive_root,
        duplicate_groups=groups,
        decisions=decisions,
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
    )
    try:
        yield httpd, state, fs
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def running_server_seven_groups(tmp_path):
    # Full-session fixture: 7 groups is enough
    # to span three client-side pages at PAGE_SIZE=3 (3 + 3 + 1) below, with
    # group 3 carrying TWO discards so a per-file exclude checkbox can be
    # exercised (leaving one discard in place) alongside a whole-group skip
    # (group 5) and a keeper swap (group 2) in the same session. Real
    # tmp_path-backed archive_root, same reasoning as running_server_multi/
    # running_server_four_groups above: manifest.py writes real CSV files
    # straight to disk, independent of the fs seam.
    archive_root = str(tmp_path / "Archive").replace("\\", "/")
    fs = FakeFileSystem()
    fs.add_dir(archive_root)
    groups = []
    decisions = []
    for i in range(1, 8):
        group_dir = f"{archive_root}/g{i}"
        fs.add_dir(group_dir)
        keeper = f"{group_dir}/a.jpg"
        fs.add_file(keeper, _jpeg_bytes())
        if i == 3:
            b, c = f"{group_dir}/b.jpg", f"{group_dir}/c.jpg"
            fs.add_file(b, _jpeg_bytes())
            fs.add_file(c, _jpeg_bytes())
            groups.append(DuplicateGroup(size=100, full_hash=f"hash{i}", paths=(keeper, b, c)))
            decisions.append(KeeperDecision(
                keeper=keeper,
                discarded=(
                    Discard(path=b, reason="dup-exact"),
                    Discard(path=c, reason="dup-exact"),
                ),
            ))
        else:
            b = f"{group_dir}/b.jpg"
            fs.add_file(b, _jpeg_bytes())
            groups.append(DuplicateGroup(size=100, full_hash=f"hash{i}", paths=(keeper, b)))
            decisions.append(
                KeeperDecision(keeper=keeper, discarded=(Discard(path=b, reason="dup-exact"),))
            )

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root=archive_root,
        duplicate_groups=groups,
        decisions=decisions,
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
    )
    try:
        yield httpd, state, fs
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def running_server_path_scenarios(tmp_path):
    # Four independent groups, one
    # per acceptance scenario. Deliberately .bin (an unsupported thumbnail
    # extension, same trick as running_server_many_groups above) so no real
    # file content or thumbnail decoding is needed -- only entry.path is
    # under test here.
    fs = FakeFileSystem()
    fs.add_dir("D:/Archive")
    groups = [
        # 1: shared prefix "D:", "Archive", "2019"; a single diverging
        # directory ("Lato" vs "Lato_kopia"); identical filename.
        DuplicateGroup(
            size=10, full_hash="hash1",
            paths=("D:/Archive/2019/Lato/IMG.bin", "D:/Archive/2019/Lato_kopia/IMG.bin"),
        ),
        # 2: three files, three different directories -- the rule must
        # hold for N > 2 members, not just a pair.
        DuplicateGroup(
            size=10, full_hash="hash2",
            paths=("D:/Archive/A/x.bin", "D:/Archive/B/x.bin", "D:/Archive/C/x.bin"),
        ),
        # 3: identical directories, filenames differ -- nothing to
        # highlight (there is no folder difference to point at).
        DuplicateGroup(
            size=10, full_hash="hash3",
            paths=("D:/Archive/2020/a.bin", "D:/Archive/2020/b.bin"),
        ),
        # 4: "&" and "<" inside a directory name -- must reach the client
        # literally: no HTML-entity encoding, and (pinned separately below)
        # no innerHTML anywhere near it.
        DuplicateGroup(
            size=10, full_hash="hash4",
            paths=("D:/Archive/A&B/<x>/img.bin", "D:/Archive/A&B/other/img.bin"),
        ),
    ]
    decisions = [
        KeeperDecision(
            keeper="D:/Archive/2019/Lato/IMG.bin",
            discarded=(Discard(path="D:/Archive/2019/Lato_kopia/IMG.bin", reason="dup-exact"),),
        ),
        KeeperDecision(
            keeper="D:/Archive/A/x.bin",
            discarded=(
                Discard(path="D:/Archive/B/x.bin", reason="dup-exact"),
                Discard(path="D:/Archive/C/x.bin", reason="dup-exact"),
            ),
        ),
        KeeperDecision(
            keeper="D:/Archive/2020/a.bin",
            discarded=(Discard(path="D:/Archive/2020/b.bin", reason="dup-exact"),),
        ),
        KeeperDecision(
            keeper="D:/Archive/A&B/<x>/img.bin",
            discarded=(Discard(path="D:/Archive/A&B/other/img.bin", reason="dup-exact"),),
        ),
    ]

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root="D:/Archive",
        duplicate_groups=groups,
        decisions=decisions,
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
    )
    try:
        yield httpd, state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def placement_server(tmp_path, real_archive_root):
    """An archive shaped like the real one mid-workflow: an existing
    #Home\\2024 Gdansk, and two photos dropped into !NEW\\Gdansk a minute
    apart (so events analysis sees exactly one event -- a coherent folder)."""
    fs = FakeFileSystem()
    fs.add_dir(real_archive_root)
    fs.add_dir(f"{real_archive_root}/#Home")
    fs.add_dir(f"{real_archive_root}/#Home/2024 Gdansk")
    fs.add_dir(f"{real_archive_root}/!NEW")
    fs.add_dir(f"{real_archive_root}/!NEW/Gdansk")

    base = int(datetime(2024, 6, 1, 10, 0).timestamp() * 1_000_000_000)
    candidates = []
    # The existing folder's own contents, taken the same day as the files
    # being placed, and part of the candidate pool exactly like on a real
    # archive-wide scan -- the archive date index is built from that pool,
    # so a fixture that left them out could not exercise it at all.
    for i in range(2):
        old_path = f"{real_archive_root}/#Home/2024 Gdansk/old{i}.jpg"
        fs.add_file(old_path, _jpeg_bytes(), volume_id=1, file_index=9 + i, mtime_ns=base)
        candidates.append(
            ScannedFile(path=old_path, stat=fs.stat(old_path), is_duplicate_candidate=True)
        )

    paths = [f"{real_archive_root}/!NEW/Gdansk/a.jpg", f"{real_archive_root}/!NEW/Gdansk/b.jpg"]
    for i, path in enumerate(paths):
        fs.add_file(path, _jpeg_bytes(), volume_id=1, file_index=100 + i,
                    mtime_ns=base + i * 60 * 1_000_000_000)
        candidates.append(ScannedFile(path=path, stat=fs.stat(path), is_duplicate_candidate=True))

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root=real_archive_root,
        duplicate_groups=[],
        decisions=[],
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
        candidates=candidates,
        db_path=str(tmp_path / "cache.sqlite3"),
    )
    try:
        yield httpd, state, fs
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def series_skip_server(tmp_path):
    """A review server with a REAL db_path and a finished series computation
    holding two groups, plus a `restart()` that brings a brand-new server up
    over the SAME database -- the only way to test "nawet przy nastepnym
    uruchomieniu" for real rather than by inspecting state."""
    archive_root = str(tmp_path / "Archive").replace("\\", "/")
    db_path = str(tmp_path / "cache.sqlite3")
    fs = FakeFileSystem()
    fs.add_dir(archive_root)
    names = ("a.jpg", "b.jpg", "c.jpg", "d.jpg")
    for i, name in enumerate(names, start=1):
        fs.add_file(f"{archive_root}/{name}", _jpeg_bytes(), volume_id=1, file_index=i)

    groups = [
        _series_group(
            [_series_member(f"{archive_root}/a.jpg"), _series_member(f"{archive_root}/b.jpg")],
            suggested_keeper=f"{archive_root}/a.jpg",
        ),
        _series_group(
            [_series_member(f"{archive_root}/c.jpg"), _series_member(f"{archive_root}/d.jpg")],
            suggested_keeper=f"{archive_root}/c.jpg",
        ),
    ]

    running = []

    def start():
        httpd, thread, state = review_server.start_server(
            fs=fs,
            archive_root=archive_root,
            duplicate_groups=[],
            decisions=[],
            thumbs_dir=str(tmp_path / "thumbs"),
            host="127.0.0.1",
            port=0,
            token="test-token",
            db_path=db_path,
        )
        _publish_series_result(state, groups)
        running.append((httpd, thread))
        return state

    try:
        yield start, fs, db_path
    finally:
        for httpd, thread in running:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


@pytest.fixture
def dup_skip_server(tmp_path):
    """A review server with a REAL db_path and two exact-duplicate groups
    ("1": a/b, "2": c/d), plus a `start()` that brings a brand-new server up
    over the SAME database -- the dup-tab twin of series_skip_server."""
    archive_root = str(tmp_path / "Archive").replace("\\", "/")
    db_path = str(tmp_path / "cache.sqlite3")
    fs = FakeFileSystem()
    fs.add_dir(archive_root)
    for i, name in enumerate(("a.bin", "b.bin", "c.bin", "d.bin"), start=1):
        fs.add_file(f"{archive_root}/{name}", b"x" * 10, volume_id=1, file_index=i)

    def group(keeper, discard, full_hash):
        return (
            DuplicateGroup(
                size=10, full_hash=full_hash,
                paths=(f"{archive_root}/{keeper}", f"{archive_root}/{discard}"),
            ),
            KeeperDecision(
                keeper=f"{archive_root}/{keeper}",
                discarded=(Discard(path=f"{archive_root}/{discard}", reason="dup-exact"),),
            ),
        )

    pairs = [group("a.bin", "b.bin", "h1"), group("c.bin", "d.bin", "h2")]
    running = []

    def start():
        httpd, thread, state = review_server.start_server(
            fs=fs,
            archive_root=archive_root,
            duplicate_groups=[g for g, _ in pairs],
            decisions=[d for _, d in pairs],
            thumbs_dir=str(tmp_path / "thumbs"),
            host="127.0.0.1",
            port=0,
            token="test-token",
            db_path=db_path,
        )
        running.append((httpd, thread))
        return state

    try:
        yield start, fs, db_path
    finally:
        for httpd, thread in running:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


@pytest.fixture
def compress_server(tmp_path):
    """A real, on-disk archive holding one real, tiny video ffprobe can
    actually read, plus a FakeFileSystem shadow of the same tree for
    everything else in the pipeline (scanner.scan(), list_archive_dirs()).
    Skips itself when the bundled ffmpeg is not resolvable -- practically
    only on a fresh clone before run.cmd fetched bin/ffmpeg/ (docs/technical/components/compression-mechanism.md),
    but the same escape hatch test_encoder.py's own integration fixture
    uses.

    Deliberately `tmp_path`, NOT the `real_archive_root` fixture other
    real-disk tests in this module use: that fixture lives under
    `.dedup/.tests/<name>/Archive`, and compress.py's own excluded-zone
    check (rule 4, `_is_in_excluded_zone()`) matches `.dedup` as ANY path
    segment, ancestor or descendant -- correct for production, where
    `.dedup` is only ever a child of the archive root (CLAUDE.md), but it
    would flag every file under `real_archive_root` as excluded, since
    `.dedup` sits ABOVE it there. This fixture exercises no manifest.py
    real write (no apply endpoint in this step), so real_archive_root's
    actual guarantee is not needed here; `tmp_path` is still a real,
    isolated directory, never the literal archive.

    The clip is mjpeg (config.INTRA_CODECS): compress.py's heuristic rule 7
    marks an intra codec worth compressing unconditionally, so the test
    does not depend on ffprobe reporting any particular bitrate."""
    if not _HAS_FFMPEG:
        pytest.skip("ffmpeg not resolvable")

    archive_root = str(tmp_path / "Archive").replace("\\", "/")
    fs = FakeFileSystem()
    fs.add_dir(archive_root)
    fs.add_dir(f"{archive_root}/!NEW")

    os.makedirs(f"{archive_root}/!NEW", exist_ok=True)
    clip_path = f"{archive_root}/!NEW/clip.avi"
    subprocess.run([
        _FFMPEG_EXE, "-y", "-v", "error", "-f", "lavfi",
        "-i", "testsrc=duration=2.5:size=160x120:rate=10",
        "-c:v", "mjpeg", "-q:v", "3", clip_path,
    ], check=True, timeout=30)
    disk_stat = os.stat(clip_path)
    fs.add_file(
        clip_path, b"", size=disk_stat.st_size, mtime_ns=disk_stat.st_mtime_ns,
        volume_id=1, file_index=1,
    )

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root=archive_root,
        duplicate_groups=[],
        decisions=[],
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
        db_path=str(tmp_path / "cache.sqlite3"),
    )
    try:
        yield httpd, state, fs
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def compress_queue_server(tmp_path):
    """Real WindowsFileSystem + a real archive on disk -- deliberately NOT
    FakeFileSystem, unlike compress_server above (GET /api/compress's
    scan-only tests). The endpoints tested here
    (POST /api/apply-compress and its neighbours) call fs.move()/
    fs.stat()/fs.open_read() on files a real ffmpeg process actually wrote
    to real disk (encoder.py's staging directory is unconditionally real,
    per its own module docstring) -- FakeFileSystem's in-memory model has
    no way to discover those without being told about them by hand
    mid-test, and the background queue thread makes that race-prone. The
    real FileSystem implementation needs no such bridging: staging and
    archive are both real disk, exactly like production.

    tmp_path, not real_archive_root, for the same reason compress_server's
    own fixture gives (see its docstring): compress.py's excluded-zone
    check matches ".dedup" as any path segment, and real_archive_root
    lives under `.dedup/.tests/...`, above tmp_path's own tree.

    Two identical, tiny mjpeg clips (compress.py's heuristic rule 7 marks
    an intra codec worth compressing unconditionally, so this does not
    depend on ffprobe reporting any particular bitrate) -- most tests
    select just one; a few (cancellation, "queue already running") need
    two distinct candidates."""
    if not _HAS_FFMPEG:
        pytest.skip("ffmpeg not resolvable")

    archive_root = str(tmp_path / "Archive").replace("\\", "/")
    new_dir = f"{archive_root}/!NEW"
    os.makedirs(new_dir, exist_ok=True)
    clip_path = f"{new_dir}/clip.avi"
    clip_path2 = f"{new_dir}/clip2.avi"
    for path in (clip_path, clip_path2):
        subprocess.run([
            _FFMPEG_EXE, "-y", "-v", "error", "-f", "lavfi",
            "-i", "testsrc=duration=2.5:size=160x120:rate=10",
            "-c:v", "mjpeg", "-q:v", "3", path,
        ], check=True, timeout=30)

    fs = platform_win.WindowsFileSystem()
    staging_dir = str(tmp_path / ".compress")

    httpd, thread, state = review_server.start_server(
        fs=fs,
        archive_root=archive_root,
        duplicate_groups=[],
        decisions=[],
        thumbs_dir=str(tmp_path / "thumbs"),
        host="127.0.0.1",
        port=0,
        token="test-token",
        db_path=str(tmp_path / "cache.sqlite3"),
        compress_staging_dir=staging_dir,
    )
    try:
        yield httpd, state, archive_root, clip_path, clip_path2
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
