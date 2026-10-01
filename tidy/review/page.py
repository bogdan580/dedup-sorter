"""Assembles the review UI into one HTML document (CSS and JS inlined).

Layout and marker rules: tidy/review/static/index.html.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Concatenation order is source order; a marker resolves only against this tuple.
INLINE_FILES: tuple[str, ...] = (
    "css/app.css",
    "js/common.js",
    "js/cards.js",
    "js/dup.js",
    "js/tuning.js",
    "js/screens.js",
    "js/skips.js",
    "js/series.js",
    "js/placement.js",
    "js/compress.js",
    "js/main.js",
    "js/restore.js",
    "js/listeners.js",
    "js/hotkeys.js",
)

_MARKER = re.compile(r"^(?:/\*@inline (\S+)\*/|//@inline (\S+))$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def assemble(static_dir: Path) -> str:
    """Replace every marker line of index.html with its file's content.

    An unknown marker or a missing file raises; nothing is skipped silently.
    """
    static_dir = Path(static_dir)
    parts = {name: _read(static_dir / name) for name in INLINE_FILES}
    out = []
    for line in _read(static_dir / "index.html").split("\n"):
        m = _MARKER.match(line)
        if m is None:
            out.append(line)
            continue
        name = m.group(1) or m.group(2)
        if name not in parts:
            raise ValueError(f"unknown inline marker: {name!r}")
        out.append(parts[name].removesuffix("\n"))
    return "\n".join(out)


INDEX_HTML = assemble(STATIC_DIR)
