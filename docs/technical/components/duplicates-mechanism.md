---
tags: [dedup, technical]
---

# Duplicate Detection

Problem: determining which of over 17 thousand files are identical, without
reading 314 GB. The solution consists of three tiers of increasing cost — a file
advances to the next tier only if the previous one did not rule it out.

## Three Tiers

| Tier | Action | Disk Read |
|---|---|---|
| 1 | grouping by size | metadata only |
| 2 | hash of first and last 64 KB | approximately 128 KB per file |
| 3 | hash of entire file | actual candidates only |

**Tier 1** is free: a unique size guarantees no duplicate, without reading a
single byte.

**Tier 2** filters out files of the same size that have different content. The
window covers the beginning and end, as multimedia files typically differ in
headers or trailing metadata.

**Tier 3** computes the full hash. A file **no larger than two windows** already
had its entire content covered by the Tier 2 read — its full hash is then the
partial hash reused, without a second read.

The algorithm used is BLAKE2b. Collision risk is negligible, and file size is
compared additionally.

## Confirmation Before Costly Stage

Before Tier 2 and 3, an estimate of data volume to be read is calculated and
presented for confirmation. Refusal skips all hashing. Non-interactive calls
pass an auto-approving confirmation callback — the estimate is still computed
and available in the result.

This guards against accidentally starting hours of disk reads by mistake.

## Role of the Cache Database

A cache hit — matching path, size, and modification time, with the required
hash column populated — skips reading the file entirely. Thanks to this,
subsequent runs take seconds instead of hours.

Details: [persistence.md](manifest-and-db.md).

## What This Mechanism Does Not Do

It does not decide **which** copy is kept — that is the task of a separate
layer described in [../business/rules/keeper.md](../../business/rules/version-selection.md).
Nor does it recognize visual similarity: two photos differing by a single byte
are completely different files to it. That is handled by
[series](../../business/rules/events-and-series.md).


### From code: `tidy/duplicates/keeper.py`
```python
Manifest reason vocabulary: "rule-0" marks a !NEW copy
discarded unconditionally by rule 0; "dup-exact" marks an ordinary
byte-identical-duplicate discard decided by the tie-break chain;
"dup-visual" marks a series discard.
```


### From code: `tidy/duplicates/keeper.py`
```python
"event-sort" marks an op=move row: a file relocated *inside* the archive
by the placement suggestions, not discarded at all. It is the
only reason value that never appears on an op=remove row.
```


### From code: `tidy/duplicates/keeper.py`
```python
"compress-remove-original" marks an op=remove row created by
applier.apply_compress_removals(): the original file whose already-moved
_compressed result replaces it (docs/technical/components/manifest-and-db.md).
```


### From code: `tidy/duplicates/keeper.py`
```python
    A silent fallback would mean a hand-editing typo quietly degrades
    which archive copy gets kept, with nothing telling the user their
    edit never took effect -- so every fallback prints, even though it
    never raises.
```


### From code: `tidy/duplicates/keeper.py`
```python
    Per-file size/hash override for apply_batch()'s manifest row.
    None/"" (the default) means "use the group's shared size/hash",
    exactly today's behaviour for byte-identical duplicate groups. Series
    discards set both explicitly, because series members have no
    shared size or byte hash -- see tidy/moves/applier's apply_batch().
```


### From code: `tidy/duplicates/report.py`
```python
    Only for type hints -- report.py stays decoupled from tiers.py/
    keeper.py at import time (duck-typed at runtime: build_grouped_report
    only accesses .size / .keeper / .discarded[].path).
```


### From code: `tidy/duplicates/scanner.py`
```python
            A per-entry stat() failure (locked file, transient I/O error,
            AV lock, cloud-sync race) must only drop THIS entry, never the
            rest of the directory listing -- see fsmodel.DirEntryInfo.
```


### From code: `tidy/duplicates/scanner.py`
```python
    Defense-in-depth backstop: even after root de-duplication, collapse
    any remaining exact-path duplicates (e.g. a future caller passing
    overlapping roots by some other route) before they ever reach tier
    duplicate-detection, where a self-paired "duplicate group" would be
    a false positive worse than any false negative.
```


### From code: `tidy/duplicates/tiers.py`
```python
    Cache.put() is INSERT OR REPLACE over every column, so a write here
    must carry forward whatever dhash series.py may already
    have cached for this same path -- otherwise an ordinary run sharing
    the same --db as an earlier --series run would silently null it back
    out, forcing series.py to redecode the image next time for no reason.
```
