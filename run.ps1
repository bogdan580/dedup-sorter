<#
.SYNOPSIS
Review interface launcher: starts dedup.py --review on fixed port 8765 with 8 decoding workers.
Additional arguments passed to run.ps1 go unmodified to dedup.py (e.g. --no-browser).
#>
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -Path $ScriptDir

if (!(Get-Command "python" -ErrorAction SilentlyContinue)) {
    Write-Host '"python" not found in PATH. Install Python and try again.' -ForegroundColor Red
    Read-Host -Prompt "Press Enter to exit"
    exit 1
}

# Ensure the pinned ffmpeg build is present (downloads it on first run).
# A failure only disables the compression tab, so the review still starts.
& (Join-Path $ScriptDir "tools\fetch-ffmpeg.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: ffmpeg is not available -- the compression tab will not work.`n" -ForegroundColor Yellow
}

$ArchiveRoot = Split-Path -Parent $ScriptDir
$NewDir = Join-Path -Path $ArchiveRoot -ChildPath "!NEW"

if (!(Test-Path -LiteralPath $NewDir -PathType Container)) {
    Write-Host 'Main "!NEW" directory not found in the archive:' -ForegroundColor Red
    Write-Host "    $NewDir"
    Write-Host "`nCreate it first, e.g.:  mkdir `"$NewDir`""
    Write-Host 'or run "python .dedup\dedup.py" from the archive root for a report'
    Write-Host "without reviewing !NEW. Aborting."
    Read-Host -Prompt "Press Enter to exit"
    exit 1
}

$ItemsInNew = Get-ChildItem -LiteralPath $NewDir -Force
if ($ItemsInNew.Count -eq 0) {
    Write-Host 'WARNING: "!NEW" directory exists, but is empty:' -ForegroundColor Yellow
    Write-Host "    $NewDir"
    Write-Host "Review will start, but Inbox tab will not show anything new.`n"
}

Write-Host "!NEW directory: $NewDir" -ForegroundColor Cyan
Write-Host "Review started: http://127.0.0.1:8765/`n" -ForegroundColor Cyan

$ArgsList = @("--review", "--port", "8765", "--workers", "8", "-y") + $args
python dedup.py @ArgsList

Write-Host "`n"
Read-Host -Prompt "Press Enter to exit"
