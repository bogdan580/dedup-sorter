"""Safety-net test for requirement 13: the engine is dry-run only. No module
under .dedup\\ may call a delete/move/rename operation. This is a static
grep-style check over the source text of the engine modules (not the test
files themselves, which legitimately reference these names as strings).
"""
from pathlib import Path

import pytest

FORBIDDEN_SUBSTRINGS = [
    "os.remove(",
    "os.unlink(",
    "os.rmdir(",
    "os.removedirs(",
    "shutil.move(",
    "shutil.rmtree(",
    "os.rename(",
    "os.replace(",
    "Path.unlink(",
    ".unlink(",
    ".rmdir(",
    ".rename(",
]

# Only the engine modules -- deliberately excludes tests/ (which contains
# this very list of forbidden substrings as data).
#
# platform_win.py is deliberately EXCLUDED: it legitimately contains the
# real os.rename()/os.makedirs()-equivalent calls backing
# FileSystem.move()/makedirs() (see its module docstring) -- that's by
# design, gated entirely behind --apply/--restore at the orchestration
# layer (tidy/moves/applier), not a regression of the dry-run guarantee. Every
# other module must still only ever reach the filesystem through the
# injected `fs` seam.
ENGINE_MODULES = [
    "tidy/config.py",
    "tidy/fs/fsmodel.py",
    "tidy/duplicates/scanner.py",
    "tidy/duplicates/hashing.py",
    "tidy/duplicates/tiers.py",
    "tidy/store/cache.py",
    "tidy/duplicates/report.py",
    "dedup.py",
    "tidy/cli/__init__.py",
    "tidy/cli/args.py",
    "tidy/cli/common.py",
    "tidy/cli/main.py",
    "tidy/cli/run_compress.py",
    "tidy/cli/run_dedup.py",
    "tidy/cli/run_events.py",
    "tidy/cli/run_inbox.py",
    "tidy/cli/run_restore.py",
    "tidy/cli/run_review.py",
    "tidy/cli/run_series.py",
    "tidy/duplicates/keeper.py",
    "tidy/moves/manifest.py",
    "tidy/moves/applier/__init__.py",
    "tidy/moves/applier/_paths.py",
    "tidy/moves/applier/remove.py",
    "tidy/moves/applier/move.py",
    "tidy/moves/applier/compress.py",
    "tidy/moves/applier/restore.py",
    "tidy/moves/applier/verify.py",
    "tidy/compression/probe.py",
    "tidy/compression/compress.py",
    "tidy/compression/hwdetect.py",
]


def _dedup_dir() -> Path:
    return Path(__file__).resolve().parent.parent


class TestNoDestructiveCalls:
    @pytest.mark.parametrize("module_name", ENGINE_MODULES)
    def test_module_contains_no_destructive_filesystem_calls(self, module_name):
        path = _dedup_dir() / module_name
        assert path.exists(), f"expected engine module missing: {path}"
        text = path.read_text(encoding="utf-8")
        offenders = [s for s in FORBIDDEN_SUBSTRINGS if s in text]
        assert offenders == [], (
            f"{module_name} contains destructive call(s): {offenders} -- "
            "the engine must be dry-run only."
        )
