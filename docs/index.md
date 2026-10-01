---
tags: [dedup, moc]
---

# Dedup Documentation

Single source of truth for this tool. If something is not here, it means we haven't established it — not that it is described elsewhere.

## Principle

**Every fact lives in exactly one file.** Other places link to it, never repeating it. This also applies to code: a comment in a module may reference a document, but cannot summarize its content.

**We describe the current state, not the journey to get there.** Change history belongs in Git. In these files, there are no deployment dates, phase numbers, or sentences like "originally it worked differently". A rule applies today or it does not exist.

## Structure

| Directory | Answers the question | For whom |
|---|---|---|
| [`business/`](business/) | **What** the tool does, **why** these rules exist, and **how** to use it | archivist, decision-maker |
| [`technical/`](technical/) | **How** it is built under the hood and how to develop it | whoever changes the code |

The boundary between `business` / `technical` has a single test: *would this content change if we rewrote the implementation without changing the program's behavior?* No → `business`. Yes → `technical`. Details and edge cases: [technical/development/documentation.md](technical/development/documentation.md).

## Navigation

| Looking for                                                       | Go to                                                                                            |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| What this is all about, archive statistics                        | [business/overview.md](business/overview.md)                                                     |
| Can this delete my photos                                         | [business/guarantees.md](business/guarantees.md)                                                 |
| Why RAW is not a duplicate of JPG                                 | [business/rules/duplicates.md](business/rules/duplicates.md)                                     |
| Which copy stays and why                                          | [business/rules/version-selection.md](business/rules/version-selection.md)                       |
| When two photos are a single series, trip splitting               | [business/rules/events-and-series.md](business/rules/events-and-series.md)                       |
| Why this specific destination directory                           | [business/rules/version-selection.md](business/rules/version-selection.md)                       |
| How the `!NEW` inbox works                                        | [business/rules/inbox.md](business/rules/inbox.md)                                               |
| Which video is worth transcoding and what happens to the original | [business/rules/compression.md](business/rules/compression.md)                                   |
| Meaning of a word used in reports or in code                      | [glossary.md](glossary.md)                                                                       |
| How to run it                                                     | [business/instructions/installation.md](business/instructions/installation.md)                   |
| What a specific flag does                                         | [business/instructions/CLI.md](business/instructions/CLI.md)                                     |
| How to follow a typical scenario from start to finish             | [business/instructions/workflows.md](business/instructions/workflows.md)                         |
| Something is not working                                          | [business/instructions/limitations.md](business/instructions/limitations.md)                     |
| How modules are structured and filesystem operations              | [technical/architecture.md](technical/architecture.md)                                           |
| How exact duplicates are detected                                 | [technical/components/duplicates-mechanism.md](technical/components/duplicates-mechanism.md)     |
| `manifest.csv` format and what is stored in the cache database    | [technical/components/manifest-and-db.md](technical/components/manifest-and-db.md)               |
| How the review UI is built                                        | [technical/components/review-ui.md](technical/components/review-ui.md)                           |
| How video compression works and where FFmpeg comes from           | [technical/components/compression-mechanism.md](technical/components/compression-mechanism.md)   |
| Why the server checks the `Host` header                           | [technical/security.md](technical/security.md)                                                   |
| System dependencies                                               | [technical/dependencies.md](technical/dependencies.md)                                           |
| How to write tests in this repo and style rules                   | [technical/development/conventions-and-tests.md](technical/development/conventions-and-tests.md) |
| Where to write new content                                        | [technical/development/documentation.md](technical/development/documentation.md)                 |
| What was deliberately postponed                                   | [technical/development/backlog.md](technical/development/backlog.md)                             |

## Relationship with Code

The documents in this directory are the **only** source of truth. `README.md` at the repository root is the entry page and links here.

Code does not duplicate this content. In a module, what remains is the contract at the enforcement point, non-obvious invariants, gotchas regarding specific lines — and an **anchor**: a single line with the path to the document, for example

```python
# See: docs/business/rules/version-selection.md
```

Anchors lead strictly one way: code points to `docs/`, `docs/` never points to code line numbers. The boundary between "what stays in code vs what goes here" is decided by [technical/development/documentation.md](technical/development/documentation.md).

The texts do not use phase terminology (`Phase 5B`, `Step 7`) or references to the old design document. They have been replaced by capability names — listed in [glossary.md](glossary.md).
