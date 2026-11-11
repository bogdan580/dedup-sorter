"""Selection validators and destination-folder sanitising for the apply endpoints."""
from __future__ import annotations

from typing import List, Mapping, Optional, Set, Tuple

from tidy.moves import applier
from tidy.duplicates.tiers import DuplicateGroup
from tidy.duplicates.keeper import (
    REASON_DUP_VISUAL, REASON_EVENT_REMOVE, REASON_EVENT_SORT, Discard, KeeperDecision,
)


# See: docs/technical/security.md

_WINDOWS_INVALID_PATH_CHARS = frozenset('<>:"|?*')


# See: docs/technical/security.md
_WINDOWS_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)


def sanitize_dest_folder(raw: object) -> Tuple[Optional[str], Optional[str]]:
    """Validate a user-edited destination folder. Returns
    (normalised_relative_path, None) or (None, error_message).

    Accepts a relative, backslash- or forward-slash-separated path of one or
    more segments (`#Home`, `#Home\\2024 Gdansk`) -- suggestions land in
    existing archive folders, and the user may retarget them, including
    into a new subfolder.

    Rejected, each for a concrete reason rather than one blanket pattern:
      - empty/non-string, or more than 8 segments deep (a real archive path
        here is 1-2 deep; anything longer is a mistake or an attempt);
      - absolute paths and drive letters (`C:\\...`, `\\\\server\\share`) --
        the only legal base is the archive root, supplied server-side;
      - `..` anywhere -- the one way a relative path can escape its base;
      - characters Windows cannot put in a path, and control characters;
      - a segment that is only dots/spaces, or ends in a dot or space
        (Windows silently strips those, so `foo.` and `foo` would collide as
        two names for one directory);
      - reserved DOS device names.
    """
    if not isinstance(raw, str):
        return None, "destination folder must be a string"
    text = raw.strip()
    if not text:
        return None, "destination folder must not be empty"
    if len(text) > 240:
        return None, "destination folder is too long"
    # Checked before splitting: "C:" and "\\\\server" matter to Windows path
    # resolution in ways a per-segment check would miss.
    if text.startswith(("/", "\\")) or (len(text) >= 2 and text[1] == ":"):
        return None, "destination folder must be relative to the archive root"

    segments = [s for s in text.replace("\\", "/").split("/") if s != ""]
    if not segments:
        return None, "destination folder must not be empty"
    if len(segments) > 8:
        return None, "destination folder is nested too deeply"

    for segment in segments:
        if segment in ("..", "."):
            return None, "destination folder must not contain '..' or '.'"
        if any(ch in _WINDOWS_INVALID_PATH_CHARS for ch in segment):
            return None, f"folder name {segment!r} contains characters Windows does not allow"
        if any(ord(ch) < 32 for ch in segment):
            return None, f"folder name {segment!r} contains control characters"
        if segment != segment.rstrip(". "):
            return None, f"folder name {segment!r} must not end with a dot or space"
        if not segment.strip(". "):
            return None, "folder name must not be only dots or spaces"
        stem = segment.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED_NAMES:
            return None, f"folder name {segment!r} is a reserved Windows device name"

    return "\\".join(segments), None


# See: docs/technical/security.md

def _join_path(a: str, b: str) -> str:
    """Mirror of applier._join(): forward slash, both sides stripped. The
    project builds internal paths with '/' regardless of platform (see
    applier's module docstring) and platform_win.py normalises on the way
    out, so this must not "helpfully" switch to a backslash on Windows."""
    return f"{a.rstrip('/' + chr(92))}/{b.lstrip('/' + chr(92))}"


def _basename_of(path: str) -> str:
    idx = max(path.rfind("/"), path.rfind("\\"))
    return path[idx + 1:] if idx >= 0 else path


def _validate_placement_selections(
    selections: object,
    placement_registry: Mapping[str, dict],
    archive_root: str,
) -> Tuple[List["applier.MovePlan"], Optional[str]]:
    if not isinstance(selections, list) or not selections:
        return [], "selections must be a non-empty list"

    seen_group_ids: Set[str] = set()
    plans: List[applier.MovePlan] = []

    for selection in selections:
        if not isinstance(selection, dict):
            return [], "each selection must be a JSON object"

        group_id = selection.get("group_id")
        file_ids = selection.get("file_ids")
        remove_ids = selection.get("remove_ids", [])
        dest_raw = selection.get("dest")

        if not isinstance(group_id, str) or not group_id:
            return [], "each selection must have a group_id"
        if group_id in seen_group_ids:
            return [], f"group {group_id!r}: submitted more than once"
        seen_group_ids.add(group_id)

        if not isinstance(file_ids, list) or not all(isinstance(f, str) for f in file_ids):
            return [], f"group {group_id!r}: file_ids must be a list of id strings"
        if not isinstance(remove_ids, list) or not all(isinstance(r, str) for r in remove_ids):
            return [], f"group {group_id!r}: remove_ids must be a list of id strings"
        if not file_ids and not remove_ids:
            return [], f"group {group_id!r}: file_ids must not be empty"
        all_ids = [*file_ids, *remove_ids]
        if len(set(all_ids)) != len(all_ids):
            return [], f"group {group_id!r}: duplicate ids within selection"

        for file_id in all_ids:
            entry = placement_registry.get(file_id)
            if entry is None:
                return [], f"unknown id: {file_id!r}"
            if entry["group_id"] != group_id:
                return [], f"id {file_id!r} does not belong to group {group_id!r}"

        # A removal-only selection moves nothing, so it needs no destination.
        if not file_ids:
            continue
        dest_rel, dest_error = sanitize_dest_folder(dest_raw)
        if dest_error is not None:
            return [], f"group {group_id!r}: {dest_error}"

        dest_abs = _join_path(archive_root, dest_rel.replace("\\", "/"))
        for file_id in file_ids:
            entry = placement_registry[file_id]
            plans.append(applier.MovePlan(
                src_abs=entry["path"],
                dst_abs=_join_path(dest_abs, _basename_of(entry["path"])),
                group_id=group_id,
                reason=REASON_EVENT_SORT,
                size=entry["size"],
            ))

    return plans, None


def _placement_removals(
    selections: list,
    placement_registry: Mapping[str, dict],
) -> Tuple[List[DuplicateGroup], List[KeeperDecision]]:
    """The `remove_ids` half of selections already accepted by
    _validate_placement_selections(), as apply_batch() input (op=remove,
    keeper left empty: nothing in the group is kept on its behalf).
    See: docs/business/rules/events-and-series.md"""
    groups: List[DuplicateGroup] = []
    decisions: List[KeeperDecision] = []
    for selection in selections:
        remove_ids = selection.get("remove_ids") or []
        if not remove_ids:
            continue
        discarded = tuple(
            Discard(
                path=placement_registry[rid]["path"],
                reason=REASON_EVENT_REMOVE,
                size=placement_registry[rid]["size"],
            )
            for rid in remove_ids
        )
        groups.append(DuplicateGroup(size=0, full_hash="", paths=tuple(d.path for d in discarded)))
        decisions.append(KeeperDecision(keeper="", discarded=discarded))
    return groups, decisions


# See: docs/technical/security.md

def _validate_compress_selections(
    selections: object,
    compress_registry: Mapping[str, dict],
) -> Tuple[List[dict], Optional[str]]:
    """Returns a flat list of {"file_id", "group_id", "path", "size"}
    entries resolved through compress_registry, or ([], error). The
    caller (POST /api/apply-compress) turns each entry into a CompressJob
    once it also knows the request's profile -- kept out of this function
    so it stays a pure resolve-and-validate step, mirroring
    _validate_placement_selections()'s shape."""
    if not isinstance(selections, list) or not selections:
        return [], "selections must be a non-empty list"

    seen_group_ids: Set[str] = set()
    entries: List[dict] = []

    for selection in selections:
        if not isinstance(selection, dict):
            return [], "each selection must be a JSON object"

        group_id = selection.get("group_id")
        file_ids = selection.get("file_ids")

        if not isinstance(group_id, str) or not group_id:
            return [], "each selection must have a group_id"
        if group_id in seen_group_ids:
            return [], f"group {group_id!r}: submitted more than once"
        seen_group_ids.add(group_id)

        if not isinstance(file_ids, list) or not all(isinstance(f, str) for f in file_ids):
            return [], f"group {group_id!r}: file_ids must be a list of id strings"
        if not file_ids:
            return [], f"group {group_id!r}: file_ids must not be empty"
        if len(set(file_ids)) != len(file_ids):
            return [], f"group {group_id!r}: duplicate ids within selection"

        for file_id in file_ids:
            entry = compress_registry.get(file_id)
            if entry is None:
                return [], f"unknown id: {file_id!r}"
            if entry["group_id"] != group_id:
                return [], f"id {file_id!r} does not belong to group {group_id!r}"

        for file_id in file_ids:
            entry = compress_registry[file_id]
            entries.append({
                "file_id": file_id, "group_id": group_id,
                "path": entry["path"], "size": entry["size"],
            })

    return entries, None


# See: docs/technical/security.md

def _validate_compress_remove_selections(
    selections: object,
    compress_registry: Mapping[str, dict],
) -> Tuple[List["applier.CompressRemoval"], Optional[str]]:
    if not isinstance(selections, list) or not selections:
        return [], "selections must be a non-empty list"

    seen_group_ids: Set[str] = set()
    removals: List[applier.CompressRemoval] = []

    for selection in selections:
        if not isinstance(selection, dict):
            return [], "each selection must be a JSON object"

        group_id = selection.get("group_id")
        file_ids = selection.get("file_ids")

        if not isinstance(group_id, str) or not group_id:
            return [], "each selection must have a group_id"
        if group_id in seen_group_ids:
            return [], f"group {group_id!r}: submitted more than once"
        seen_group_ids.add(group_id)

        if not isinstance(file_ids, list) or not all(isinstance(f, str) for f in file_ids):
            return [], f"group {group_id!r}: file_ids must be a list of id strings"
        if not file_ids:
            return [], f"group {group_id!r}: file_ids must not be empty"
        if len(set(file_ids)) != len(file_ids):
            return [], f"group {group_id!r}: duplicate ids within selection"

        for file_id in file_ids:
            entry = compress_registry.get(file_id)
            if entry is None:
                return [], f"unknown id: {file_id!r}"
            if entry["group_id"] != group_id:
                return [], f"id {file_id!r} does not belong to group {group_id!r}"
            if "compressed_path" not in entry:
                return [], f"id {file_id!r} has no verified compressed result to remove"

        for file_id in file_ids:
            entry = compress_registry[file_id]
            removals.append(applier.CompressRemoval(
                original_abs=entry["path"],
                compressed_abs=entry["compressed_path"],
                group_id=group_id,
            ))

    return removals, None


# See: docs/technical/security.md
def _validate_selections(
    selections: object,
    file_registry: Mapping[str, dict],
) -> Tuple[List[DuplicateGroup], List[KeeperDecision], Optional[str]]:
    if not isinstance(selections, list) or not selections:
        return [], [], "selections must be a non-empty list"

    seen_group_ids: Set[str] = set()
    groups: List[DuplicateGroup] = []
    decisions: List[KeeperDecision] = []

    for selection in selections:
        if not isinstance(selection, dict):
            return [], [], "each selection must be a JSON object"

        group_id = selection.get("group_id")
        keeper_id = selection.get("keeper_id")
        discard_ids = selection.get("discard_ids")

        if not isinstance(group_id, str) or not group_id:
            return [], [], "each selection must have a group_id"
        if group_id in seen_group_ids:
            return [], [], f"group {group_id!r}: submitted more than once"
        seen_group_ids.add(group_id)

        if not isinstance(keeper_id, str) or not keeper_id:
            return [], [], f"group {group_id!r}: must have exactly one keeper_id"
        if not isinstance(discard_ids, list) or not all(isinstance(d, str) for d in discard_ids):
            return [], [], f"group {group_id!r}: discard_ids must be a list of id strings"

        all_ids = [keeper_id, *discard_ids]
        if len(set(all_ids)) != len(all_ids):
            return [], [], (
                f"group {group_id!r}: duplicate ids within selection "
                "(the keeper cannot also be listed as a discard)"
            )

        for file_id in all_ids:
            entry = file_registry.get(file_id)
            if entry is None:
                return [], [], f"unknown id: {file_id!r}"
            if entry["group_id"] != group_id:
                return [], [], f"id {file_id!r} does not belong to group {group_id!r}"

        keeper_entry = file_registry[keeper_id]
        discarded = tuple(
            Discard(
                path=file_registry[discard_id]["path"],
                reason=file_registry[discard_id]["reason"] or "user-selected",
            )
            for discard_id in discard_ids
        )
        groups.append(DuplicateGroup(
            size=keeper_entry["size"],
            full_hash=keeper_entry["hash"],
            paths=(keeper_entry["path"],) + tuple(d.path for d in discarded),
        ))
        decisions.append(KeeperDecision(keeper=keeper_entry["path"], discarded=discarded))

    return groups, decisions, None


# See: docs/technical/security.md
def _find_already_applied_group_id(
    selections: object,
    applied_group_ids: Set[str],
) -> Optional[str]:
    if not isinstance(selections, list):
        return None  # not this function's job -- _validate_selections() reports the shape error
    for selection in selections:
        if not isinstance(selection, dict):
            continue
        group_id = selection.get("group_id")
        if isinstance(group_id, str) and group_id in applied_group_ids:
            return group_id
    return None


# See: docs/technical/security.md
def _validate_series_selections(
    selections: object,
    series_registry: Mapping[str, dict],
) -> Tuple[List[DuplicateGroup], List[KeeperDecision], Optional[str]]:
    if not isinstance(selections, list) or not selections:
        return [], [], "selections must be a non-empty list"

    seen_group_ids: Set[str] = set()
    groups: List[DuplicateGroup] = []
    decisions: List[KeeperDecision] = []

    for selection in selections:
        if not isinstance(selection, dict):
            return [], [], "each selection must be a JSON object"

        group_id = selection.get("group_id")
        keep_ids = selection.get("keep_ids")
        discard_ids = selection.get("discard_ids")

        if not isinstance(group_id, str) or not group_id:
            return [], [], "each selection must have a group_id"
        if group_id in seen_group_ids:
            return [], [], f"group {group_id!r}: submitted more than once"
        seen_group_ids.add(group_id)

        if not isinstance(keep_ids, list) or not all(isinstance(k, str) for k in keep_ids):
            return [], [], f"group {group_id!r}: keep_ids must be a list of id strings"
        if not keep_ids:
            return [], [], f"group {group_id!r}: keep_ids must not be empty"
        if not isinstance(discard_ids, list) or not all(isinstance(d, str) for d in discard_ids):
            return [], [], f"group {group_id!r}: discard_ids must be a list of id strings"

        all_ids = [*keep_ids, *discard_ids]
        if len(set(all_ids)) != len(all_ids):
            return [], [], (
                f"group {group_id!r}: duplicate ids within selection "
                "(an id cannot be both kept and discarded)"
            )

        for file_id in all_ids:
            entry = series_registry.get(file_id)
            if entry is None:
                return [], [], f"unknown id: {file_id!r}"
            if entry["group_id"] != group_id:
                return [], [], f"id {file_id!r} does not belong to group {group_id!r}"

        keeper_entry = series_registry[keep_ids[0]]
        discarded = tuple(
            Discard(
                path=series_registry[discard_id]["path"],
                reason=series_registry[discard_id]["reason"] or REASON_DUP_VISUAL,
                size=series_registry[discard_id]["size"],
                hash=series_registry[discard_id]["hash"],
            )
            for discard_id in discard_ids
        )
        groups.append(DuplicateGroup(
            size=keeper_entry["size"],
            full_hash=keeper_entry["hash"],
            paths=tuple(series_registry[k]["path"] for k in keep_ids) + tuple(d.path for d in discarded),
        ))
        decisions.append(KeeperDecision(keeper=keeper_entry["path"], discarded=discarded))

    return groups, decisions, None
