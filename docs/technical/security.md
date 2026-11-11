---
tags: [dedup, technical]
---

# Security

Two surfaces: the review interface and CSV files generated from names originating
on disk. Both share the same premise — **a file name in the archive is not trusted
content**, having arrived there via download, phone transfer, or manual renaming.

## Review Interface

The review interface displays the content of a private archive and is capable of
moving files and launching them in system applications. It is an HTTP server
running on the user's machine, so it is subject to the browser's full threat
model — even though it is "only local".

The guarantee realized by these mechanisms is described in
[../business/guarantees.md](../business/guarantees.md).

### Local-Only Listening

The server binds to `127.0.0.1`, by default on an automatically selected port.
It serves nothing from the network — no external resources, no libraries from
content delivery networks. The entire browser application is served as a single
document assembled in `tidy/review/page.py`.

Geocoding runs offline, using data bundled with the package. **No query
containing personal data ever leaves the computer.**

### `Host` Header Check on Every Request

Checked on **every** request, including read requests.

> [!warning]
> Reason: with DNS rebinding, an attacker site whose name resolves to a loopback
> address is, to the browser, **the same origin**. Without checking, it could
> read the paths of all files and the photo bytes themselves with a simple
> request. Protecting writes alone is not enough — a read leak is just as severe
> here.

The `localhost` alias is permitted.

### Token for Mutating and Side-Effecting Requests

Endpoints that move files, open a file in a system application, rotate a
thumbnail, recompute results, or undo a batch additionally require a token and
a matching `Origin` header.

**The token travels in a header, never in the URL.** A URL would end up in
browser history and proxy/intermediary logs.

Opening a file is treated as a **mutating** request even though it alters nothing
in the archive: launching an external program is a side effect, and a read request
could be triggered from a foreign page via a simple image tag.

Endpoints launching background computations run under the `Host` header check
alone. Rationale: execution is **one-shot per process**, so there is no
amplification vector, and the `Host` check already cuts off DNS rebinding.

### Identifier Instead of Path

**A path from the client is not accepted in any form.** The only bridge from a
request to a file are the registries described in
[review-ui.md](components/review-ui.md). An identifier outside the
registry is an explicit error, never a silent skip.

An exception requiring attention is source directory selection on the grouping
suggestion and compression tabs: this is the only place where the selection
originates from the client. Therefore, the list of directories is **constructed
by the server**, and the client sends a selection from that list, not an arbitrary
path. The submitted name nevertheless undergoes syntax sanitization and
membership verification against this list — membership checking alone would
suffice, but both tabs must handle paths identically, and rejection should be
precise. The destination directory name, which the user can edit, undergoes
sanitization to remove Windows-disallowed characters and validation of archive
membership **on the server side**.

File launching is performed without a system shell and without string concatenation.
The encoder is launched the same way: via an argument list, without a shell, with
paths resolved through the registry, never from a string sent by the client.

**`device_key`** (hardware device selection during compression, `POST /api/apply-compress`
and `POST /api/compress/calibrate`) applies the same pattern to hardware selection
instead of a file: the client submits a string, but it is resolved **exclusively**
by comparison against the list constructed by the server during hardware detection
(`hwdetect.detect()`), never used directly to construct commands. An unknown key,
a key belonging to an encoder other than the selected profile, or any key when
using a CPU profile results in the same explicit error, and the queue does not
start — see [compression.md](components/compression-mechanism.md).

### Response Headers

Every response (JSON, HTML and thumbnail bytes) carries `X-Frame-Options: DENY`
and `X-Content-Type-Options: nosniff`; they are added centrally in
`ResponseHandler.end_headers()`. The CSP additionally sets
`frame-ancestors 'none'`, `base-uri 'none'`, `form-action 'none'` and
`object-src 'none'`. The SPA uses no `<form>` submit and no `<base>`.

> [!warning]
> Reason: a foreign page embedding the UI in a frame could trick the user into
> clicking apply buttons (clickjacking). The token comparison uses
> `secrets.compare_digest` and treats a missing token as a rejection.

### Open-File Extension Allowlist

`POST /api/open` launches a file through the OS default handler, so it only
opens extensions in `config.PHOTO_EXTENSIONS_CASEFOLD |
config.VIDEO_EXTENSIONS_CASEFOLD` (case-insensitive). Anything else answers
400. Reason: a file name in the archive is untrusted, and `.exe`, `.lnk` or
`.bat` would execute instead of display.

### Manifest Path Validation on Restore

A manifest is editable input, so `restore_batch()`, `verify_manifest()` and the
batch listing validate each row (`applier.unsafe_row_reason()`) before touching
the disk, given an `archive_root` (the CLI and the review UI always pass it):

- `src_abs` must lie inside the archive root;
- for `op=remove`, `dst_rel` must be batch-relative: not absolute, no drive
  letter, no `..` segment;
- for `op=move` / `op=compress`, the destination must lie inside the archive root.

A violating row is reported as `refused` (restore) or as a mismatch (verify) and
is never moved. Other rows of the batch are processed as usual.

### ffmpeg / ffprobe on Untrusted Files

Every ffmpeg/ffprobe invocation that reads an archive file passes
`-protocol_whitelist file,pipe` before the input. Reason: media containers such as
HLS playlists can reference `http:`, `tcp:` or other `file:` URLs; the whitelist
stops a crafted video from making the tool read arbitrary resources or contact a
network (see invariant 6, nothing leaves the machine).

### Resilience Against Malformed Requests

- The request body size is validated; a negative value would mean reading to the
  end of a stream that the client never closes — resulting in a hung thread.
- Unknown identifiers, empty selections, spoofed headers, and reapplying the same
  group produce explicit, documented error responses.
- An exception in a worker thread becomes an error state with an on-screen
  message, never a hung "computing..." or a server response with a stack trace.

### Rendering Data from Disk

File and directory names come from disk and **are not trusted HTML content**. All
are inserted as text. The HTML report generated from the CLI escapes every string
originating from the filesystem.

## CSV Files

A spreadsheet treats a cell starting with `=`, `+`, `-`, `@`, or a tab as a
**formula**, not as text. Columns containing paths originate directly from names
outside the tool's control, so a manually renamed file or directory could
introduce such a character into the report. Therefore, before writing, such a
value is prepended with a leading apostrophe, forcing it to be read literally.

This applies to both generated files: `raport.csv` and
[manifest.csv](components/manifest-and-db.md), which sit side by side in
the batch directory and look identical.

### Manifest Reading Reverses the Protection

Here lies the crucial difference. `raport.csv` exists solely for human viewing
— the tool never reads it back, so protection is write-only.

`manifest.csv` is read back during undo and verification, and from it comes the
path passed to file movement. It must therefore satisfy **both** requirements
simultaneously: the file on disk must be just as protected as the report, and the
read path must be byte-for-byte original. Therefore, the apostrophe is stripped
upon reading — every path leaving the manifest layer is genuine and ready to be
passed to file operations.

Exception: `keeper_abs` is protected on write, but **not** unescaped on read.
This column is purely informational and never directs moves, so it follows the
report convention rather than the execution path convention.


### From code: `tidy/review/security.py`
```python
A POST /api/apply body larger than this is rejected outright without ever
being read into memory -- selections are
small JSON (a handful of short id strings per group), so a body anywhere
near this size can only be a mistake or an abuse attempt, not a real
selection payload even for an archive with many thousands of groups.
```


### From code: `tidy/review/validation.py`
```python
Reserved DOS device names: a file or directory named CON/PRN/AUX/NUL or
COM1..9/LPT1..9 (with or without an extension) cannot be created on
Windows, and the failure surfaces as a confusing OSError deep inside the
move rather than as "that name is not allowed".
```


### From code: `tidy/review/security.py`
```python
--- security checks (pure, testable without a socket) -----------------------

Host is checked on EVERY request, GET included -- not just the mutating
POST /api/apply. A GET-only check would leave a DNS-rebinding read leak
even though such a request could never reach the (correctly protected)
write path; see docs/technical/security.md. `Host` is a forbidden header
name for fetch()/XHR, so browser JS can never spoof it to match; it
always reflects the real connection target.
```


### From code: `tidy/review/validation.py`
```python
--- destination-folder sanitising -------------------------------------------

The placement screen lets the user edit the suggested folder name, which
makes this the first path in the whole server that originates with the
client. Everything below treats that string as hostile until proven
otherwise. It is validated as a *relative* path and joined onto the
archive root server-side -- the client never sends an absolute path, so
there is no absolute path to smuggle. applier.apply_moves() re-checks
containment independently; this is the first of two gates, not the only
one.
```


### From code: `tidy/review/validation.py`
```python
--- selection model for POST /api/apply-placement ---------------------------

Body: {"selections": [{"group_id", "file_ids", "remove_ids", "dest"}, ...]}.
Shaped like _validate_series_selections() and validated with the same
all-or-nothing discipline. `file_ids` (blue cards) become applier.MovePlan
objects; `remove_ids` (red cards, optional) become DuplicateGroup/
KeeperDecision pairs with an empty keeper via _placement_removals() -- a
removed placement file is not a copy of anything, so there is no keeper
to name. Both lists are resolved through placement_registry only, must be
disjoint, and may not both be empty.

`dest` is the one field that originates with the user. It is validated
only when `file_ids` is non-empty, sanitised here and re-checked for
containment inside apply_moves(); neither check is load-bearing on its
own. `remove_ids` carry no path at all -- apply_batch() derives the
!REMOVE destination itself.
```


### From code: `tidy/review/validation.py`
```python
--- selection model for POST /api/apply-compress -----------------------

Body: {"selections": [{"group_id", "file_ids"}, ...]} -- no "dest" field
(unlike placement, a compressed result always lands next to its own
original, never somewhere the user picks). Same all-or-nothing
validation as every other selection model in this module: a single
unresolvable id anywhere in the payload rejects the whole request before
any CompressJob is built.
```


### From code: `tidy/review/validation.py`
```python
--- selection model for POST /api/apply-compress-remove -----------------

Body: {"selections": [{"group_id", "file_ids"}, ...]}, resolved ONLY
through compress_registry entries that carry a "compressed_path" key --
candidate rows from GET /api/compress never have one (nothing has been
encoded yet), only rows _compress_removable_candidates() adds after a
compress batch has actually landed. Rejecting any selection whose entry
lacks the key is the structural "no checkbox for an unverified row"
contract (docs/technical/components/review-ui.md) expressed server-side, not just
left to the front end to enforce.
```


### From code: `tidy/review/validation.py`
```python
--- selection model -------------------------------------------------------

POST /api/apply body: {"selections": [{"group_id", "keeper_id",
"discard_ids"}, ...]}. Every id here is opaque to the client -- it is
looked up ONLY through `file_registry` (built by build_group_views()
above), the same allowlist pattern GET /api/thumb/<id> uses for
`thumb_sources`. A client-supplied path is never used to build a
filesystem path directly.

Validation is all-or-nothing: this function only ever returns a
([], [], error) triple or a (groups, decisions, None) triple -- callers
must not act on `groups`/`decisions` unless the error is None, and since
every selection is checked before any `DuplicateGroup`/`KeeperDecision`
is built, a single bad entry anywhere in the payload guarantees zero
files get moved for the whole request.
```


### From code: `tidy/review/validation.py`
```python
--- reject a group_id already applied --------------------------------

Deliberately separate from _validate_selections() above: that function's
every error is a 400 (malformed/unresolvable request), whereas "you
already did this" is a conflict with the server's *current state*, not a
malformed request -- callers map this one to 409, matching the status
code the old one-shot `applied_result` lock used. Checked BEFORE
_validate_selections() runs, over ALL entries in `selections` up front,
so a request mixing an already-applied group_id with brand-new ones is
rejected as a whole (same all-or-nothing principle as step 3's
validation: either every group_id in this request is still eligible, or
nothing in this request moves).
```


### From code: `tidy/review/validation.py`
```python
--- selection model for POST /api/apply-series -------------------------

POST /api/apply-series body: {"selections": [{"group_id", "keep_ids",
"discard_ids"}, ...]}. Mirrors _validate_selections() above as closely as
the underlying model allows -- id -> path resolution ONLY through
`series_registry` (never file_registry or events_registry), same
all-or-nothing validation before any file moves. Two differences from the
exact-duplicate model: `keep_ids` is a non-empty LIST rather than a single
`keeper_id` (a series can keep more than one file and must keep at least
one, since discarding every member of a group would delete content that
exists nowhere else, unlike a byte-identical duplicate), and every
discard's manifest size/hash comes straight from series_registry rather
than falling back to any group-level value, since series members share
neither a size nor a byte hash.
```
