---
tags: [dedup, business, rules]
---

# Events and Trips

An event is a collection of files created around the same time and place — a trip,
an occasion, a day. Event partitioning forms the basis for suggestions on where
files belong.

## Time is the Foundation, Location is an Enrichment

The initial concept was grouping purely by location. Archive measurements
disproved it: only 31% of photos have GPS, while 87% have dates. An algorithm based
solely on location would cover one-third of the collection, leaving the rest without suggestions.

Therefore, the sequence is counter-intuitive:

**1. Partitioning by time.** A gap longer than 8 hours starts a new
event. Works for all files with any date, including old
cameras without GPS.

**2. Partitioning by location.** Within a single event, a jump greater than
25 km splits it into two locations. An eight-hour event can span two
cities — a day trip during an extended stay is a distinct
location, even if photos are taken minutes apart.

The split point falls at the **temporal midpoint** between the two GPS readings
where the jump occurred. As a result, photos without location lying
between them land unambiguously on one side or the other.

**3. Location inheritance.** A photo without GPS inherits coordinates from its
temporally closest neighbor in the same, already partitioned event. This is how
DSLR photos acquire a location from a smartphone present on the
same trip.

**4. Location naming.** Resolved offline from a local geographical names database.
No request ever leaves the computer.

Both thresholds are configurable: [business/instructions/CLI.md](../instructions/CLI.md).

## Camera Clock

The silent prerequisite for steps 1 and 2. DSLRs without GPS notoriously have
clocks drifted by hours, and after battery removal reset to factory defaults.

The consequence of clock drift is more severe than it appears: a photo lands
in someone else's event, **inherits someone else's location**, and receives a suggestion
to move into a folder from an entirely different trip.

The safeguard is two-tiered:

- **Suspicious date** — before 1995 or in the future. Such a file is placed in a separate
  report section and **excluded from location inheritance**: it can neither give nor
  inherit coordinates, as its own timeline is unreliable.
- **Per-camera-model offset** — configured by the user in `config.json`,
  for example `"NIKON D3100": "+2h15m"`. Applied **strictly in memory**,
  prior to event partitioning. Never modifies files on disk.

A model with suspicious dates and no configured offset receives a distinct
warning in the summary. The tool **does not compute** the offset automatically —
it warns, leaving the value for the user to determine.

> [!warning]
> Clock warnings appear at the very top of the grouping suggestions screen.
> Hiding them would conceal the fact that some photos could land in
> another trip's folder.

## Destination Directory Name

Generated according to conventions detected from the archive — `YYYY Location` within
a top-level directory, e.g. `#Home\2024 Gdańsk`. The convention is described
in [business/overview.md](../overview.md).

Two rules:

**Existing top-level directory takes precedence.** If `#Home`
already exists in the archive, suggestions target its interior rather than creating
a new directory alongside. Matching is **exact**, not fuzzy — fuzzy matching risks
landing in the wrong one of two similarly named folders.

**File count does not enter directory names.** The name `2024-08 Kraków
(37 photos, 4 videos)` goes stale the moment a single file is added,
breaking archive conventions. Counts are displayed in the UI next to the group.

## Output

This mode **plans**. The CLI generates a report and touches no files.
Execution takes place in the review UI on the grouping suggestions tab —
rules (including the move / remove card states) in
[business/rules/version-selection.md](version-selection.md#card-states).


---

---
tags: [dedup, business, rules]
---

# Series and Visual Similarity

A series is a group of photos that are **not** byte-identical, but depict
the same scene: camera burst, recompression of the same frame,
format conversion, multiple attempts at one shot. Exact deduplication cannot detect them —
that requires a different algorithm and a different level of certainty.

## Difference from Duplicates — and Consequences

In a duplicate group, every discarded file has a byte-identical copy
elsewhere. **In a series, it does not.** Every photo is unique; discarding means losing
content that exists nowhere else.

Hence three behavioral differences:

- **More than one file may be retained.** A series group does not have a single keeper —
  the user selects all shots worth keeping.
- **A group must retain at least one file.** Attempting to discard all
  is rejected before anything moves.
- **Thumbnail review is mandatory.** Series are never applied
  automatically; the command line only reports them.

## When Two Photos Land in One Group

They must satisfy both conditions simultaneously:

**Temporal proximity.** Only photos from the same time window are compared —
otherwise the tool would have to compare every photo with every other photo.
Window scale dictates interpretation:

| Interval | Meaning |
|---|---|
| up to 3 seconds | camera burst |
| up to 30 seconds | same shot |
| up to 5 minutes | same scene — default value |

**Image similarity.** Perceptual image hashes of both photos must differ by no
more than the threshold — default 6 out of 64 bits. The hash is resistant to
recompression and resizing, catching conversions that byte comparison misses.

Both thresholds are configurable: [business/instructions/CLI.md](../instructions/CLI.md).

### Adjusting Thresholds in the UI

Both thresholds can also be changed from the Series tab without restarting the tool — two
dropdowns visible **only in developer mode**
([technical/components/review-ui.md](../../technical/components/review-ui.md)).

> [!note]
> The change **does not take effect immediately**. It applies only upon the next
> **Rescan**, until which time the control is marked as pending. This is not
> a technical limitation, but a consequence of what these thresholds represent: groups on
> screen were generated with specific values, and showing them under a different number
> would misrepresent them. Rescan re-evaluates the disk from scratch anyway, so files
> previously moved to `!REMOVE` do not reappear as proposals.

The time window is computationally more expensive than the similarity threshold: it is the sole
limit on the number of comparisons within a cluster, so widening it extends
Rescan duration more than file count alone would suggest. The upper limit is one hour;
an out-of-range value is **rejected**, never silently clamped — otherwise the
control would display a number the engine did not use.

Timestamps come from EXIF, or when missing — from filename patterns, and as a last
resort from modification dates. This ensures photos with stripped EXIF also participate
in grouping.

## Which Copy is Suggested

Order of criteria until first resolution:

1. **Original prior to conversion** — see original rule below.
2. **Larger file size.**
3. **Higher resolution.**
4. **Higher sharpness** — measured via Laplacian variance, detects motion blur.
5. **Presence of EXIF.**
6. **Path** — for reproducible results.

### The Original Rule

**A copy in the source format, with complete camera EXIF, wins
regardless of file size.**

The rule exists for a specific reason: at the same resolution, a JPG can be
**larger** than the HEIC it was created from. A size-only criterion would therefore
favor the converted export and discard the phone original — exactly the opposite of what
the user wants. There are 18 such pairs in the archive.

Hence, the original sits **before** size in the chain, even though size is
generally more significant than resolution or sharpness.

## Skipping a Group

Checking "skip this group" is a **permanent** decision — the group disappears from the tab
on all subsequent runs.

The decision is recorded **by the file paths in the group**, never by group number.
Group numbers are positions in a single run's output and represent different things
after each recalculation.

Consequences are deliberate:

- a file is **removed** from a skipped group → the remainder **stays** hidden, as the decision
  pertained to it as well;
- a new file is **added** to a skipped group → the group **reappears**, because it contains
  something the user has never reviewed.

The second case is the only way this feature could ever lose a photo —
hence the bias toward re-displaying.

The same asymmetry governs threshold adjustments (above). Relaxing thresholds merges groups,
so a skipped group ceases to be a subset of the recorded decision and **reappears** —
it now contains a file the user has not seen. Tightening thresholds splits a group
into smaller ones, each remaining a subset, so it **stays hidden**. Both behaviors
stem from the same rule, not separate exceptions.

Skips **survive recalculations and algorithm changes** — this is human judgment,
not a calculated value. Only deleting the cache database clears them. A way back always
exists: "Show skipped groups" with a hidden count.

Skipping works **strictly in the review UI**. The CSV report continues to list
all discovered groups — it is a raw data export, not a decision screen.

## Scope

Video, audio, RAW, and editing files are excluded from this mode — reasons
in [business/rules/duplicates.md](duplicates.md). Video is still detected in exact
deduplication and events.


### From code: `tidy/analysis/dateindex.py`
```python
How far apart two days may be and still count as "the same trip". One day
absorbs the ordinary causes of a near miss -- a timezone offset, a photo
taken either side of midnight, a camera clock minutes out -- without
letting a week-long gap look like a match.
```


### From code: `tidy/analysis/dateindex.py`
```python
    Every day within tolerance of a source day, mapped back to which
    source day it vouches for, so overlap_days counts the SOURCE's days
    rather than the folder's.
```


### From code: `tidy/analysis/dateindex.py`
```python
    Exact days first, tolerated ones only as a tie-breaker. Without that
    split the tolerance eats the signal it exists to protect: for a
    two-day trip, a folder holding ONE of the days is "within a day" of
    the other as well, so it scores the same coverage as a folder holding
    both -- and then wins on file count, which is the wrong answer.
```


### From code: `tidy/analysis/events.py`
```python
Files dated before this year, or after "now", have their EXIF/filename
timestamp treated as untrustworthy -- a plausible symptom of a camera
whose clock reset to its factory default after a dead battery, seen in
this archive's own Nikon/Canon bodies.
```


### From code: `tidy/analysis/events.py`
```python
Archive top-level folders are conventionally named after a country in
English ("#Home", "#Trip-A") except for a handful of pre-existing
city-named exceptions
("#Trip-C", "#Trip-D", "#Trip-E") this heuristic can't invent on its own --
only the countries this archive's own travel history has actually
touched are listed; an unlisted country code falls back to its raw ISO
code rather than guessing a display name.
```


### From code: `tidy/analysis/events.py`
```python
                             DateTimeOriginal/GPSInfo which live in
                             nested SubIFDs (verified empirically, same
                             way timeline.py's SubIFD fix was).
```


### From code: `tidy/analysis/events.py`
```python
    One representative coordinate per group (its own or inherited GPS,
    whichever is available first) -- collected up front so geocoding
    happens in a single batched call (see _geocode_all).
```


### From code: `tidy/analysis/events.py`
```python
        The drop-folder name outranks the geocoder for BOTH halves of
        the suggestion -- the label and the top-level folder to match
        against. A user who wrote "Gdansk" on the folder gets "#Home\
        2024 Gdansk" even when the only GPS in the batch geocodes to a
        neighbouring village.
```


### From code: `tidy/analysis/events.py`
```python
        "RRRR Miejsce", matching what is actually on disk
        (#Home\2024 Gdansk, #Travel\2023 Mountains). The file counts moved
        to the review screen: baked into a directory name they go stale the
        moment one more photo lands there.
```


### From code: `tidy/analysis/series.py`
```python
Outer time-clustering window (the widest tier, "same scene"): dHash is
only ever computed for members that already fall within this window of
at least one other candidate -- what keeps the pairwise Hamming
comparison cheap at archive scale instead of comparing every photo
against every other photo.
```


### From code: `tidy/analysis/series.py`
```python
Descriptive-only sub-tiers: labels attached to a finished group from its
own max internal time gap, purely for display in the review UI --
unlike DEFAULT_SERIES_WINDOW_SECONDS, these don't gate clustering or
similarity grouping.
```


### From code: `tidy/analysis/series.py`
```python
Same-base-name pairs where the first extension is the camera/source
format and the second is a converted export of it -- the source always
wins the keeper suggestion regardless of resolution/sharpness/size.
".arw" is listed for completeness even though thumbnails.supports()
can't decode RAW today -- such a pair can never actually reach this
code path yet, since only Pillow-decodable files become SeriesMembers
at all.
```


### From code: `tidy/analysis/series.py`
```python
    Paths that looked like photos (thumbnails.supports() said yes) but
    failed to decode when computing similarity metrics -- a single bad
    file must not drop the rest of its time cluster, only itself.
```


### From code: `tidy/analysis/series.py`
```python
    force_recompute (--rescan) intentionally skips only the is_hit()
    freshness check, not the cache object itself -- _put_dhash() below
    still reads the real cache row to preserve any existing
    partial_hash/full_hash tiers.py wrote for this same path. Wrapping
    `cache` in something like tidy/cli/common.py's _BypassReadCache (whose get()
    always returns None) would defeat that merge and null those columns
    out in the shared --db on every --rescan --series run.
```


### From code: `tidy/analysis/series.py`
```python
        0 = wins: a same-base-name sibling in this group is a converted
        export of this file's source format, so this file must win
        regardless of every other criterion below. 1 = no such pairing
        -- fall through to the rest of the chain.
```


### From code: `tidy/analysis/series.py`
```python
    thumbnails.kind() == "image", NOT thumbnails.supports(): supports()
    also returns True for video (when ffmpeg is resolvable), but video
    stays explicitly out of scope here (see module docstring) --
    dHash/sharpness below call Image.open() directly, which can't decode
    a video container at all.
```


### From code: `tidy/analysis/series.py`
```python
        Covers phase 1 (timestamp extraction, every photo candidate)
        fully; phase 2's scope (only clustered candidates get a dHash)
        isn't known until clustering below finishes, so the total grows
        via add_total() at that point rather than being guessed here.
```


### From code: `tidy/media/gps.py`
```python
GPS IFD tag ids (EXIF spec): ref tags are single-character strings
("N"/"S"/"E"/"W"), coordinate tags are (degrees, minutes, seconds) tuples
of rationals.
```


### From code: `tidy/media/sharpness.py`
```python
Downscaling before convolving keeps this cheap for multi-megapixel
archive photos -- sharpness is a coarse signal, full resolution buys
nothing here but a slower convolution.
```


### From code: `tidy/media/timeline.py`
```python
EXIF tag 36867 = DateTimeOriginal, stored as "YYYY:MM:DD HH:MM:SS". Real
cameras/phones store this in the nested "Exif SubIFD" (pointed to by
IFD0 tag 0x8769/34665), not in the top-level IFD0 dict image.getexif()
returns directly -- Image.Exif.get_ifd(ExifTags.IFD.Exif) is what
resolves that pointer. A tag written the "obvious" way (exif[36867] = ...)
round-trips through the top level and would make this look correct in a
naive test, but a real photo's DateTimeOriginal is invisible to plain
getexif().get(36867).
```


### From code: `tidy/media/timeline.py`
```python
The separator is captured once per half and back-referenced, so
"2021-02-15" matches but "2021-0215" does not -- a mixed-separator string
is far more likely to be an id than a date. Negative lookaround at both
ends keeps this from matching inside a longer digit run. The millisecond
tail is consumed explicitly rather than by loosening the trailing guard.
Observed name forms: docs/business/overview.md
```


### From code: `tidy/media/timeline.py`
```python
A four-digit run only reads as a year inside this range; without it,
epoch-millisecond names like "1780592864140.png" parse as year 1780. The
upper bound is deliberately well ahead of now: a clock drifted into the
future is flagged by the events layer rather than silently discarded here.
```


### From code: `tidy/media/timeline.py`
```python
                Real cameras/phones write DateTimeOriginal into the Exif
                SubIFD, not IFD0 -- try that first. A handful of tools
                write it non-standardly at the top level, so that's kept
                as a fallback rather than the primary source.
```


### From code: `tidy/analysis/series.py`
```python
--- parallel dHash resolution for one time cluster ---

Deliberately a SEPARATE function from _get_dhash() above, not a rewrite of
it: _get_dhash() is tested directly (tests/test_series.py) and stays the
sequential fallback (max_workers<=1, or a single-member cluster where
spinning up a thread pool would cost more than it saves). This function
produces byte-for-byte the same SeriesGroup output as calling _get_dhash()
once per member in cluster order -- only the WORK is parallelized, never
the cache access.
```
