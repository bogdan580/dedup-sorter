---
tags: [dedup, business, rules]
---

# The `!NEW` Inbox

`!NEW` is where fresh dumps arrive — individual files and entire
folders — before they find their permanent place in the archive.

The inbox **is not** excluded from scanning, unlike the trash. On the contrary:
it is the primary source of duplicates, so it must be scanned. However, it holds
a distinct status in each mode.

## Distinct Status

| Mode | Behavior regarding `!NEW` |
|---|---|
| Exact duplicates | scanned normally, but Rule 0 applies — the inbox copy always loses |
| Review | distinct filter "show only `!NEW`" |
| Series | computed **within** the inbox and **across** the inbox–archive boundary |
| Grouping suggestions | the inbox is the source, archive is the target |

## Triage — Three Steps in Enforced Order

A single command executes three operations, always in this order:

**1. Dedup against the entire archive.** If an inbox file is a byte-for-byte
duplicate of something already in the archive, Rule 0 mandates keeping the archive
instance, proposing the inbox copy for removal.

**2. Series within the inbox** — evaluated only on files that survived step 1.

**3. Grouping suggestions** — evaluated on what remains after step 1.

> [!warning]
> **This order cannot be inverted, and this is intentional.** If sorting
> preceded dedup, the tool would propose archive destinations for files slated
> for the trash anyway — forcing the user to make decisions about content that
> will soon be gone.

## Untouchable Files and Folders

**Non-media files** — everything other than photos and videos, including RAW, archives,
documents — remain untouched and are routed to a separate list "skipped, decision required".
The tool has no basis for suggesting a destination for them.

**Empty folders** are reported only. Two cases are distinguished:
folders already empty, and folders that will become empty once step 1 suggestions
are accepted. Neither is deleted.

Reporting instead of deleting is not just caution. On this drive,
an emptied directory has been observed to disappear **without user intervention** —
the suspect is a background cloud sync client. The report therefore displays the current state
rather than assuming an empty directory survives until read.

## Output

The command line generates a single HTML report with three sections and **does not move
any file**. Execution takes place in the review UI:

- duplicates and series → to the trash,
- grouping suggestions → deeper into the archive (cards marked red → to the trash), rules
  in [business/rules/version-selection.md](version-selection.md).


### From code: `tidy/analysis/inbox.py`
```python
Names meaning "this is a camera-roll dump", not "this is a trip". They
do two things, both the opposite of what a meaningful name does:

  1. never a whole-folder move, however coherent the contents look. The
     content-based check alone is not enough -- `Camera` comes out "mixed"
     only because it really does span 5.5 years, but a `Baseus` dumped
     after one weekend would come out "coherent" and be offered as a
     single folder named after the phone that took the photos.
  2. never a naming hint -- `#Home\2024 Gdansk` is useful,
     `#Baseus\2024 Baseus` is not.

Overridable via config.json so a new phone brand needs no code change.
```


### From code: `tidy/analysis/inbox.py`
```python
    The counts left the folder NAME (where they went stale the moment
    one more photo landed) and moved to the review screen, so they have to
    travel with the suggestion. Taken straight off the Event that produced
    it -- computing them again in the view layer would be a fourth
    independent copy of the photo/video extension rule.
```


### From code: `tidy/analysis/inbox.py`
```python
    A dump name is worth exactly nothing as a place name, and a dump
    folder is never offered as one move. Loose files have no folder name
    at all, so they are in the same position.
```


### From code: `tidy/analysis/inbox.py`
```python
    Files scanner.py excluded from duplicate-candidacy entirely (zero-byte,
    offline/cloud placeholder, hardlink-of, inventory-only like Thumbs.db)
    never get a placement suggestion -- routed straight to
    skipped_non_media instead of silently vanishing from every section of
    the report. Never opened (no series/events call on them): opening an
    offline placeholder would trigger a cloud download, which scanner.py's
    own contract forbids.
```


### From code: `tidy/analysis/inbox.py`
```python
    Folder-emptiness must reflect real disk contents, independent of
    --min-size/--ext -- built from every !NEW file (not just filtered
    candidates), with only the dedup-discarded paths removed.
```
