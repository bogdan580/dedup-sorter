"""Static check over the source text of the engine: every `docs/...md` anchor
points to an existing file, and the retired plan vocabulary does not come
back. Scope is tidy/, dedup.py and requirements*.txt -- deliberately not
tests/, where a forbidden string is sometimes data
(docs/technical/development/documentation.md).
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SOURCE_SUFFIXES = {".py", ".js", ".html", ".css"}

DOC_PATH = re.compile(r"docs/[A-Za-z0-9_./-]+?\.md")

FORBIDDEN = [
    re.compile(re.escape("PLAN.md")),
    re.compile(re.escape("Faza ")),
    re.compile(re.escape("plan_compression")),
    re.compile(re.escape("plan_gpu")),
    re.compile(r"plan section", re.IGNORECASE),
]


def _engine_sources():
    files = [REPO_ROOT / "dedup.py"]
    files += sorted(REPO_ROOT.glob("requirements*.txt"))
    for path in sorted((REPO_ROOT / "tidy").rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if path.is_file() and path.suffix in SOURCE_SUFFIXES:
            files.append(path)
    return files


def _read(path):
    return path.read_text(encoding="utf-8")


class TestEngineSourcesAreDiscovered:
    def test_finds_entry_point_and_package_sources(self):
        names = {p.relative_to(REPO_ROOT).as_posix() for p in _engine_sources()}
        assert "dedup.py" in names
        assert "tidy/config.py" in names
        assert "requirements.txt" in names
        assert any(n.endswith(".js") for n in names)
        assert any(n.endswith(".html") for n in names)


class TestDocAnchorsResolve:
    def test_every_docs_path_in_engine_sources_exists(self):
        missing = []
        for path in _engine_sources():
            for match in DOC_PATH.finditer(_read(path)):
                target = REPO_ROOT / match.group(0)
                if not target.is_file():
                    missing.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}: {match.group(0)}"
                    )
        assert not missing, "anchors to missing docs:\n" + "\n".join(missing)

    def test_anchor_pattern_ignores_a_fragment(self):
        match = DOC_PATH.search("# See: docs/business/rules/version-selection.md#tabela")
        assert match is not None
        assert match.group(0) == "docs/business/rules/version-selection.md"

    def test_anchors_exist_at_all(self):
        found = [
            p for p in _engine_sources() if DOC_PATH.search(_read(p))
        ]
        assert found, "no docs anchors found -- the pattern has stopped matching"


class TestRetiredVocabularyIsGone:
    def test_no_forbidden_string_in_engine_sources(self):
        hits = []
        for path in _engine_sources():
            text = _read(path)
            for pattern in FORBIDDEN:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    hits.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: "
                        f"{match.group(0)!r}"
                    )
        assert not hits, "retired plan vocabulary:\n" + "\n".join(hits)

    def test_patterns_catch_what_they_promise(self):
        samples = ["see PLAN.md", "Faza 4B", "plan_compression.md", "plan_gpu.md",
                   "Plan Section 7"]
        for sample in samples:
            assert any(p.search(sample) for p in FORBIDDEN), sample
