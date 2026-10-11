# Rebuilds the two PyInstaller-frozen executables the desktop app bundles
# (lucid-backend.exe, rip.exe) and places them where Tauri expects them.
# Run this after any change to backend/app/** or backend/requirements.txt,
# before `npm run tauri build` / `npm run tauri dev`.
#
# Prerequisites: Python 3.11+ on PATH, and a real ffmpeg.exe somewhere on
# this machine (point -FfmpegPath at it, or edit the default below).

param(
    [string]$FfmpegPath = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$binaries = Join-Path $PSScriptRoot "src-tauri\binaries"
$venv = Join-Path $root ".build-venv"

if (-not (Test-Path $venv)) {
    Write-Host "Creating build venv..."
    python -m venv $venv
    & "$venv\Scripts\pip" install --quiet -r "$backend\requirements.txt" pyinstaller
}

Push-Location $backend
try {
    & "$venv\Scripts\pyinstaller" --noconfirm --onefile --name lucid-backend `
        --collect-all streamrip --collect-all mutagen --collect-all certifi `
        desktop_main.py

    & "$venv\Scripts\pyinstaller" --noconfirm --onefile --name rip `
        --collect-all streamrip --collect-all mutagen --collect-all certifi `
        rip_stub.py
} finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $binaries | Out-Null
Copy-Item "$backend\dist\lucid-backend.exe" "$binaries\lucid-backend-x86_64-pc-windows-msvc.exe" -Force
Copy-Item "$backend\dist\rip.exe" "$binaries\rip.exe" -Force

if (Test-Path $FfmpegPath) {
    Copy-Item $FfmpegPath "$binaries\ffmpeg.exe" -Force
    Write-Host "Copied ffmpeg from $FfmpegPath"
} else {
    Write-Warning "ffmpeg.exe not found at $FfmpegPath -- pass -FfmpegPath, or audio conversion/muxing (needed for some SoundCloud tracks) will fail at runtime."
}

Write-Host "Done. Binaries are in $binaries"
