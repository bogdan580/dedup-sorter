"""Keeper selection + `config.json` loading.

Pure and OS-agnostic like scanner.py/tiers.py: everything here works on
plain path strings and a caller-supplied path->mtime_ns mapping (never on
StatInfo or the FileSystem seam directly), because `tiers.DuplicateGroup`
carries only paths, no stat info. Callers build the mtime mapping from
`ScanResult.files` before calling `select_keeper`.

No filesystem writes/moves/deletes happen anywhere in this module --
selection and reporting only, still fully dry-run. Building the move
manifest is tidy/moves/applier's job.
"""
# See: docs/business/rules/version-selection.md
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from tidy import config

# See: docs/technical/components/duplicates-mechanism.md
REASON_RULE_0 = "rule-0"
REASON_DUP_EXACT = "dup-exact"
REASON_DUP_VISUAL = "dup-visual"
REASON_EVENT_SORT = "event-sort"
REASON_EVENT_REMOVE = "event-remove"
REASON_COMPRESS_REMOVE = "compress-remove-original"

# --- config.json schema + defaults ------------------------------------------

# No built-in priority folders; users set them in config.json (see config.example.json).
DEFAULT_PRIORITY_DIRS: Tuple[str, ...] = ()
DEFAULT_COPY_NAME_PATTERNS: Tuple[str, ...] = ("(1)", "(2)", "- kopia", "_1", "- Copy")
DEFAULT_REPORT_TOP_N = 20


@dataclass(frozen=True)
class KeeperConfig:
    priority_dirs: Tuple[str, ...] = DEFAULT_PRIORITY_DIRS
    copy_name_patterns: Tuple[str, ...] = DEFAULT_COPY_NAME_PATTERNS
    report_top_n: int = DEFAULT_REPORT_TOP_N


def _warn_bad_config(path: Optional[str], message: str) -> None:
# See: docs/technical/components/duplicates-mechanism.md
    location = path if path is not None else "<config.json>"
    print(f"Warning: {location}: {message} -- using the default value.")


def _coerce_str_tuple(
    data: dict, key: str, default: Tuple[str, ...], path: Optional[str]
) -> Tuple[str, ...]:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    _warn_bad_config(path, f"'{key}' should be a list of strings")
    return default


def _coerce_int(data: dict, key: str, default: int, path: Optional[str]) -> int:
    if key not in data:
        return default
    value = data[key]
    # bool is a subclass of int in Python -- exclude it explicitly so
    # {"report_top_n": true} doesn't silently become report_top_n=1.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    _warn_bad_config(path, f"'{key}' should be an integer")
    return default


def keeper_config_from_dict(data: dict, path: Optional[str] = None) -> KeeperConfig:
    """Build a KeeperConfig from a parsed JSON object, field by field. Each
    field is validated and coerced independently -- a wrong-typed single
    field (e.g. "priority_dirs": "#Home" instead of ["#Home"], a very
    plausible hand-editing typo) falls back to that field's default rather
    than raising out of here, matching load_keeper_config()'s promise that
    a broken config.json degrades gracefully instead of crashing the scan."""
    return KeeperConfig(
        priority_dirs=_coerce_str_tuple(data, "priority_dirs", DEFAULT_PRIORITY_DIRS, path),
        copy_name_patterns=_coerce_str_tuple(
            data, "copy_name_patterns", DEFAULT_COPY_NAME_PATTERNS, path
        ),
        report_top_n=_coerce_int(data, "report_top_n", DEFAULT_REPORT_TOP_N, path),
    )


def load_keeper_config(path: Optional[str]) -> KeeperConfig:
    """Load config.json from `path`. Missing file, unreadable file,
    malformed JSON, non-object JSON, or a well-formed object with
    wrong-typed fields all fall back to built-in defaults (per-field, where
    possible) -- config.json is meant to be hand-edited by the archive
    owner, so a typo in it must never crash a dry-run scan. Every fallback
    prints a one-line warning so a corrupted edit isn't silently ignored."""
    if path is None:
        return KeeperConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return KeeperConfig()
    except ValueError:
        _warn_bad_config(path, "invalid JSON")
        return KeeperConfig()
    if not isinstance(data, dict):
        _warn_bad_config(path, "expected a JSON object ({...})")
        return KeeperConfig()
    return keeper_config_from_dict(data, path=path)


# --- path-segment matching (mirrors scanner._is_remove_path) ---------------

def _path_segments(path: str) -> List[str]:
    normalized = path.replace("\\", "/")
    return [seg for seg in normalized.split("/") if seg]


def _has_segment(path: str, name: str) -> bool:
    name_cf = name.casefold()
    return any(seg.casefold() == name_cf for seg in _path_segments(path))


def is_new_path(path: str) -> bool:
    """True if any path segment casefold-matches config.NEW_DIR_NAME
    ("!NEW"), mirroring scanner._is_remove_path's approach for
    config.REMOVE_DIR_NAME."""
    return _has_segment(path, config.NEW_DIR_NAME)


def _priority_rank(path: str, priority_dirs: Tuple[str, ...]) -> int:
    """Lower is better. A path matching an earlier entry in priority_dirs
    outranks one matching a later entry; a path matching none of them gets
    a rank equal to len(priority_dirs) -- worse than any real match, but
    equal among themselves so later tie-break criteria decide."""
    for idx, name in enumerate(priority_dirs):
        if _has_segment(path, name):
            return idx
    return len(priority_dirs)


def _basename(path: str) -> str:
    segments = _path_segments(path)
    return segments[-1] if segments else path


def _looks_like_copy(path: str, patterns: Tuple[str, ...]) -> bool:
    name_cf = _basename(path).casefold()
    return any(pattern.casefold() in name_cf for pattern in patterns)


def _tie_break_key(path: str, mtimes: Dict[str, int], cfg: KeeperConfig):
    return (
        _priority_rank(path, cfg.priority_dirs),
        1 if _looks_like_copy(path, cfg.copy_name_patterns) else 0,
        len(path),
        mtimes.get(path, 0),
        path,
    )


# --- keeper decision ---------------------------------------------------------

@dataclass(frozen=True)
class Discard:
    path: str
    reason: str  # REASON_RULE_0 | REASON_DUP_EXACT | REASON_DUP_VISUAL
# See: docs/technical/components/duplicates-mechanism.md
    size: Optional[int] = None
    hash: str = ""


@dataclass(frozen=True)
class KeeperDecision:
    keeper: str
    discarded: Tuple[Discard, ...]


def select_keeper(
    paths: Iterable[str],
    mtimes: Dict[str, int],
    cfg: Optional[KeeperConfig] = None,
) -> KeeperDecision:
    """Select which of one duplicate group's paths survives.

    See: docs/business/rules/version-selection.md

    Rule 0 (hard-coded, always applied first): if the group has at least
    one path under !NEW and at least one path outside it, every !NEW path
    is discarded (reason=rule-0) regardless of any other criterion, and the
    keeper is chosen only among the non-!NEW paths. If all paths are inside
    !NEW, or all are outside it, rule 0 doesn't eliminate anything and the
    tie-break chain runs over the full set.

    Tie-break chain among whichever candidate set rule 0 left standing (in
    order, first difference wins): priority directory rank, then "doesn't
    look like a copy name", then shorter path, then older mtime, then
    alphabetically smaller path (final tiebreak for total reproducibility).
    """
    cfg = cfg or KeeperConfig()
    all_paths = list(paths)
    if not all_paths:
        raise ValueError("select_keeper() requires at least one path")

    new_paths = [p for p in all_paths if is_new_path(p)]
    archive_paths = [p for p in all_paths if not is_new_path(p)]

    if new_paths and archive_paths:
        candidates = archive_paths
        rule0_discarded = new_paths
    else:
        candidates = all_paths
        rule0_discarded = []

    keeper_path = min(candidates, key=lambda p: _tie_break_key(p, mtimes, cfg))

    discarded: List[Discard] = [
        Discard(path=p, reason=REASON_RULE_0) for p in sorted(rule0_discarded)
    ]
    discarded.extend(
        Discard(path=p, reason=REASON_DUP_EXACT)
        for p in sorted(candidates)
        if p != keeper_path
    )

    return KeeperDecision(keeper=keeper_path, discarded=tuple(discarded))
