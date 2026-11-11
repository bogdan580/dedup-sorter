🌐 **English** | [Polski](README.pl.md) | [Українська](README.uk.md)

---

# Dedup sorter

![Dedup Sorter](docs/assets/dedup_sorter.jpg)

A tool for organizing photo and video archives: exact duplicate detection, identifying series and visually similar photos, grouping into trips/events, inbox triage, and recompressing abnormally large video files.

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy_Me_A_Coffee-FFDD00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/mbohdan)

**By default, nothing is deleted.** The tool operates in a dry-run mode — until you explicitly request it, it only reports what it proposes to do. Even when applying changes, it does not permanently delete files from your drive: it moves them to a `!REMOVE` trash folder along with a manifest, making everything fully reversible. Permanent deletion must always be done manually.

**Windows 10/11 only.** The tool relies on Win32 APIs (long paths, junctions, hard links); Linux and macOS are not supported.

**Where to place the project?**
The `.dedup` repository directory must be placed **directly inside the root directory of your archive** that you want to manage. The tool processes its parent folder.
On every run, the tool creates any of these special directories that are missing in your archive:
- `!NEW` – an inbox for newly dumped photos waiting to be sorted
- `!MOVES` – history of manifests and move operations
- `!REMOVE` – a safe trash bin for rejected duplicates and files you marked for removal in the review UI (they are never permanently deleted by the program)

The `run.cmd` / `run.ps1` / `run.sh` launchers are stricter: they abort if `!NEW` does not exist yet, so create it first (step 4 below).

## Quick Start

Requires Windows 10/11, Python 3.10 or newer, and Git.

> **Installing Python on Windows:**
> If you don't have Python installed yet:
> 1. Download the installer from [python.org/downloads](https://www.python.org/downloads/) or run in PowerShell: `winget install Python.Python.3.12`
> 2. **Important:** In the installer, make sure to check **"Add python.exe to PATH"** before clicking Install.
> 3. Verify the installation in a new terminal window: `python --version`

1. Go to the folder of your archive (the one you want to tidy):
   ```bat
   cd D:\MyArchive
   ```
2. Clone the repository into a `.dedup` subfolder and install the dependencies:
   ```bat
   git clone https://github.com/bogdan580/dedup-sorter.git .dedup
   cd .dedup
   python -m pip install -r requirements.txt
   ```
3. Optional: copy the config template and edit it (without `config.json` the defaults apply):
   ```bat
   copy config.example.json config.json
   ```
4. Optional, for inbox triage: create the `!NEW` folder in the archive root (and put new photos there):
   ```bat
   mkdir ..\!NEW
   ```
5. Double-click `run.cmd` (or run `.\run.ps1` in PowerShell, or `./run.sh` in Git Bash). The review UI opens in your browser.

The **first launch downloads ffmpeg** (~200 MB, SHA-256 verified) for video compression. This is the only network access of the project; later launches are fully offline, and `dedup.py` itself never goes online. If the download fails, the review UI still starts, just without compression. Details: [bundled ffmpeg binary](docs/technical/components/compression-mechanism.md#bundled-ffmpeg-binary).

CLI commands are run from the archive root (not from inside `.dedup`), because relative `--roots` resolve against the current directory:

```bat
cd D:\MyArchive
python .dedup\dedup.py --review
```

| Command | Description |
|---|---|
| `python .dedup\dedup.py` | scans the entire archive, no changes to disk |
| `python .dedup\dedup.py --review` | opens the web review UI — this is where you make decisions |
| `python .dedup\dedup.py --apply` | moves rejected duplicates to the trash |
| `python .dedup\dedup.py --restore MANIFEST` | reverts an entire batch of operations |
| `python .dedup\dedup.py --inbox` | `!NEW` inbox triage, generates an HTML report |
| `python .dedup\dedup.py --compress` | indicates videos worth re-encoding, no changes to disk |

![Story Book](docs/assets/story_book.jpg)

## Documentation

The tool features an extensive architecture and a robust set of business rules. The full documentation is located in the [`docs/`](docs/index.md) directory — it serves as the single source of truth.

| Looking for | Go to |
|---|---|
| Can this delete my photos? | [safety guarantees](docs/business/guarantees.md) |
| How to install | [installation](docs/business/instructions/installation.md) |
| What does a specific flag do? | [CLI reference](docs/business/instructions/CLI.md) |
| Typical workflows | [workflows](docs/business/instructions/workflows.md) |
| Which duplicate is kept and why? | [keeper selection](docs/business/rules/version-selection.md) |
| Which video is worth compressing? | [compression rules](docs/business/rules/compression.md) |
| Something isn't working (known bugs) | [limitations](docs/business/instructions/limitations.md) |
| How is it built under the hood? | [architecture](docs/technical/architecture.md) |

## Video Compression

Compression uses ffmpeg, which is not part of the repository: the launchers download a pinned, checksum-verified build on first launch (see Quick Start), so no manual installation is needed. Details: [compression mechanism](docs/technical/components/compression-mechanism.md).

## Testing and Development

For those who wish to contribute code (including AI agents — see [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Coding conventions, styling, and test suite state: [tests and conventions](docs/technical/development/conventions-and-tests.md). The codebase avoids extensive comments — business logic details are kept in the documentation, and the code only contains "anchors" pointing to them (e.g., `# See: docs/business/rules/...`).

## License

This project is open-sourced under the [MIT](LICENSE) License. ffmpeg is downloaded separately (GPLv3, BtbN builds) and is not part of this repository.
