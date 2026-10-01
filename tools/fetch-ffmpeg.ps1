<#
.SYNOPSIS
Ensures bin\ffmpeg\ffmpeg.exe and ffprobe.exe are present and verified, downloading them when they are not.

.DESCRIPTION
Called by run.cmd / run.ps1 / run.sh before the review UI starts. This is the
only network access in the project; dedup.py itself never goes online.
Exit codes: 0 = binaries ready, 2 = could not obtain them (compression is then unavailable).
Rules: docs/technical/components/compression-mechanism.md#bundled-ffmpeg-binary
#>
param([switch]$Quiet)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is many times slower with the progress bar on 5.1.

$RepoDir = Split-Path -Parent $PSScriptRoot
$BinDir = Join-Path $RepoDir "bin\ffmpeg"
$VersionFile = Join-Path $BinDir "VERSION.txt"
$InstalledFile = Join-Path $BinDir "INSTALLED.txt"
$StagingDir = Join-Path $RepoDir ".compress\ffmpeg-download"
$Binaries = @("ffmpeg.exe", "ffprobe.exe")
$AllowedUrlPrefix = "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
$RequiredEncoders = @("libx265", "libsvtav1")

function Write-Info([string]$Message) {
    if (-not $Quiet) { Write-Host $Message }
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# "ffmpeg.exe   <sha256>" lines -> @{ "ffmpeg.exe" = "<sha256>" }
function Read-BinaryHashes([string]$Path) {
    $hashes = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $hashes }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*(ffmpeg\.exe|ffprobe\.exe)\s+([0-9a-fA-F]{64})\s*$') {
            $hashes[$Matches[1]] = $Matches[2].ToLowerInvariant()
        }
    }
    return $hashes
}

function Read-Field([string]$Path, [string]$Name) {
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match ('^\s*' + [regex]::Escape($Name) + ':\s*(\S+)\s*$')) { return $Matches[1] }
    }
    return $null
}

function Test-BinariesMatch([hashtable]$Expected) {
    foreach ($name in $Binaries) {
        $path = Join-Path $BinDir $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
        if (-not $Expected.ContainsKey($name)) { return $false }
        if ((Get-Sha256 $path) -ne $Expected[$name]) { return $false }
    }
    return $true
}

function Test-Functional([string]$Dir) {
    $ffmpeg = Join-Path $Dir "ffmpeg.exe"
    $ffprobe = Join-Path $Dir "ffprobe.exe"
    $encoders = (& $ffmpeg -hide_banner -encoders 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0) { return $false }
    foreach ($encoder in $RequiredEncoders) {
        if ($encoders -notmatch ('\b' + [regex]::Escape($encoder) + '\b')) { return $false }
    }
    & $ffprobe -hide_banner -version *> $null
    return ($LASTEXITCODE -eq 0)
}

# Downloads $Url, verifies it and lands both binaries in bin\ffmpeg. With $ZipSha/$BinaryHashes
# the build must match them exactly; without them (the "latest" fallback) it must pass Test-Functional.
function Install-Build([string]$Url, [string]$ZipSha, [hashtable]$BinaryHashes) {
    if (-not $Url.StartsWith($AllowedUrlPrefix)) { throw "refusing to download from outside ${AllowedUrlPrefix}: $Url" }
    if (Test-Path -LiteralPath $StagingDir) { Remove-Item -LiteralPath $StagingDir -Recurse -Force }
    New-Item -ItemType Directory -Path $StagingDir -Force | Out-Null
    $zip = Join-Path $StagingDir "ffmpeg.zip"
    $extractDir = Join-Path $StagingDir "x"

    Write-Info "Downloading ffmpeg (~200 MB): $Url"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing

    if ($ZipSha -and ((Get-Sha256 $zip) -ne $ZipSha)) { throw "SHA-256 of the downloaded archive does not match VERSION.txt" }

    Write-Info "Extracting..."
    Expand-Archive -LiteralPath $zip -DestinationPath $extractDir -Force
    $found = Get-ChildItem -LiteralPath $extractDir -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if (-not $found) { throw "ffmpeg.exe not found in the downloaded archive" }
    $srcDir = $found.DirectoryName

    if ($BinaryHashes) {
        foreach ($name in $Binaries) {
            if ((Get-Sha256 (Join-Path $srcDir $name)) -ne $BinaryHashes[$name]) { throw "SHA-256 of $name does not match VERSION.txt" }
        }
    } elseif (-not (Test-Functional $srcDir)) {
        throw "downloaded build lacks a required encoder ($($RequiredEncoders -join ', ')) or does not run"
    }

    try {
        New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
        foreach ($name in $Binaries) {
            # Move-Item -Force does not replace an existing file on Windows PowerShell 5.1.
            $dst = Join-Path $BinDir $name
            if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force }
            Move-Item -LiteralPath (Join-Path $srcDir $name) -Destination $dst
        }
    } catch {
        # A good build that cannot be landed (e.g. ffmpeg.exe in use) must not trigger the fallback.
        throw [System.IO.IOException]::new("cannot write to ${BinDir} (is ffmpeg running?): $($_.Exception.Message)")
    }

    if ($BinaryHashes) {
        if (Test-Path -LiteralPath $InstalledFile) { Remove-Item -LiteralPath $InstalledFile -Force }
    } else {
        $lines = @(
            "ffmpeg installed by tools/fetch-ffmpeg.ps1 from the unpinned fallback (not verified against VERSION.txt).",
            "Source:    $Url",
            "Installed: $((Get-Date).ToString('yyyy-MM-dd HH:mm'))"
        )
        foreach ($name in $Binaries) { $lines += ("  {0,-12} {1}" -f $name, (Get-Sha256 (Join-Path $BinDir $name))) }
        Set-Content -LiteralPath $InstalledFile -Value $lines -Encoding ASCII
    }
    Remove-Item -LiteralPath $StagingDir -Recurse -Force
}

if (-not (Test-Path -LiteralPath $VersionFile -PathType Leaf)) {
    Write-Host "ffmpeg: $VersionFile is missing -- cannot verify or download ffmpeg." -ForegroundColor Yellow
    exit 2
}

$pinnedHashes = Read-BinaryHashes $VersionFile
if ((Test-BinariesMatch $pinnedHashes) -or (Test-BinariesMatch (Read-BinaryHashes $InstalledFile))) {
    Write-Info "ffmpeg: OK ($BinDir)"
    exit 0
}

Write-Host "ffmpeg: binaries in bin\ffmpeg are missing or do not match VERSION.txt."
$pinnedUrl = Read-Field $VersionFile "Pinned URL"
$pinnedZipSha = Read-Field $VersionFile "Pinned zip SHA-256"
$fallbackUrl = Read-Field $VersionFile "Fallback URL"

try {
    Install-Build $pinnedUrl $pinnedZipSha.ToLowerInvariant() $pinnedHashes
    Write-Host "ffmpeg: installed the pinned build (SHA-256 verified)." -ForegroundColor Green
    exit 0
} catch [System.IO.IOException] {
    Write-Host "ffmpeg: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "The review UI will still start; the compression tab may be unavailable." -ForegroundColor Yellow
    exit 2
} catch {
    Write-Host "ffmpeg: pinned build unavailable: $($_.Exception.Message)" -ForegroundColor Yellow
}

try {
    Write-Host "ffmpeg: falling back to the latest build (checked by its encoder list, not by SHA-256)."
    Install-Build $fallbackUrl $null $null
    Write-Host "ffmpeg: installed the latest build; details in $InstalledFile" -ForegroundColor Green
    exit 0
} catch {
    Write-Host "ffmpeg: could not install ffmpeg: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "The review UI will still start; the compression tab will be unavailable." -ForegroundColor Yellow
    exit 2
}
