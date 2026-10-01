---
tags: [dedup, usage]
---

# Installation and Getting Started

## Requirements

- **Python 3.10 or newer** available in the system path — verify with `python --version`.
  If you don't have Python, download the installer from [python.org/downloads](https://www.python.org/downloads/)
  (remember to check the **"Add python.exe to PATH"** option) or install via terminal:
  `winget install Python.Python.3.12`.
- **Windows 10/11 only.** The file engine relies on Win32: paths longer than 260
  characters, junctions, hard links, cloud placeholder detection. Linux and macOS
  are not supported.
- **Git**, to clone the repository.

## Installation

For running the tool itself, clone the repository into a `.dedup` folder inside the
archive and install the dependencies:

```
cd D:\MyArchive
git clone https://github.com/bogdan580/dedup-sorter.git .dedup
cd .dedup
python -m pip install -r requirements.txt
```

Optionally copy the config template (without `config.json` the defaults apply):

```
copy config.example.json config.json
```

For inbox triage create the `!NEW` folder in the archive root (`mkdir ..\!NEW`). The
launchers (`run.cmd`, `run.ps1`, `run.sh` from Git Bash) abort when it is missing and do
not create it; the CLI creates any missing `!NEW`, `!MOVES` and `!REMOVE` itself.

For developing the code — the same plus the test engine:

```
python -m pip install -r requirements-dev.txt
```

The separation is intentional: the runtime dependencies file does not include testing tools. What
each dependency provides and what stops working without it:
[technical/dependencies.md](../../technical/dependencies.md).

Video compression does not require installing anything extra: on first launch, `run.cmd`
(or `run.ps1` / `run.sh`) downloads a pinned FFmpeg build (~200 MB, from the BtbN GitHub
releases) into `bin\ffmpeg\` and verifies its SHA-256. Later launches only re-check the
hashes, offline. If the download fails, the review UI still starts and only the compression
tab is unavailable — [technical/components/compression-mechanism.md](../../technical/components/compression-mechanism.md#bundled-ffmpeg-binary).
To fetch it without starting the UI: `powershell -ExecutionPolicy Bypass -File tools\fetch-ffmpeg.ps1`.

## Where the Archive Lives

The tool resides in the `.dedup` folder **inside** the archive it organizes.
The archive folder is always the parent directory — automatically detected based on
the tool's own location. Nothing needs to be configured and it works
regardless of where and under what name the archive resides.

## First Run

```
python .dedup\dedup.py
```

Without arguments, it scans the entire archive and **does not move anything**. Output: a CSV
report in `.dedup\.reports\` and a summary in the console.

**Run from the archive directory, not from `.dedup`.** Relative paths
passed in `--roots` resolve relative to the process's working directory,
not relative to the detected archive.

## Review UI

```
python .dedup\dedup.py --review
```

Starts a local server and opens a browser tab. The bundled launcher `run.cmd`
starts the same on a fixed port — just double-click it.

## What is Created on Disk

Everything the tool generates goes into dot-prefixed directories
inside `.dedup`:

| Directory | Contents |
|---|---|
| `.reports\` | CSV and HTML reports |
| `.db\` | hash cache database |
| `.thumbs\` | thumbnails |
| `.compress\` | video encoding working directory |

None of these are committed to the repository and any of them can be deleted — they will be recreated
on the next run. Deleting `.db` means recalculating hashes
**and losing recorded skips**.

## Console and Non-ASCII Characters

> [!warning]
> If the archive contains directories with non-ASCII characters (e.g. Cyrillic), switch
> your console to UTF-8 before running:
>
> ```
> chcp 65001
> ```
>
> Without this, the scan may terminate with an encoding error **before** reaching
> change application. Symptom details:
> [business/instructions/limitations.md](limitations.md).
