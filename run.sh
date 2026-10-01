#!/usr/bin/env bash
# Review interface launcher for Git Bash / MSYS2 on Windows: starts dedup.py --review
# on fixed port 8765 with 8 decoding workers. The tool itself is Windows-only.
# Additional arguments passed to run.sh go unmodified to dedup.py (e.g. --no-browser).

set -e

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *)
        echo "This tool is Windows-only (Win32 file APIs, bundled ffmpeg.exe). Aborting."
        exit 1
        ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "$SCRIPT_DIR"

if command -v python &> /dev/null; then
    PYTHON_CMD="python"
elif command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
else
    echo '"python" or "python3" not found in PATH. Install Python and try again.'
    exit 1
fi

# Ensure the pinned ffmpeg build is present (downloads it on first run).
# A failure only disables the compression tab, so the review still starts.
if ! powershell.exe -NoProfile -ExecutionPolicy Bypass -File "tools/fetch-ffmpeg.ps1"; then
    echo "WARNING: ffmpeg is not available -- the compression tab will not work."
    echo ""
fi

ARCHIVE_ROOT="$(dirname "$SCRIPT_DIR")"
NEW_DIR="$ARCHIVE_ROOT/!NEW"

if [ ! -d "$NEW_DIR" ]; then
    echo 'Main "!NEW" directory not found in the archive:'
    echo "    $NEW_DIR"
    echo ""
    echo "Create it first, e.g.:  mkdir \"$NEW_DIR\""
    echo 'or run "python .dedup/dedup.py" from the archive root for a report'
    echo "without reviewing !NEW. Aborting."
    exit 1
fi

if [ -z "$(ls -A "$NEW_DIR")" ]; then
    echo 'WARNING: "!NEW" directory exists, but is empty:'
    echo "    $NEW_DIR"
    echo "Review will start, but Inbox tab will not show anything new."
    echo ""
fi

echo "!NEW directory: $NEW_DIR"
echo "Review started: http://127.0.0.1:8765/"
echo ""

$PYTHON_CMD dedup.py --review --port 8765 --workers 8 -y "$@"
