# Install or update GameGuider on Windows 10/11 (PowerShell):
#
#   irm https://github.com/matischneider082/gameguider/releases/latest/download/install.ps1 | iex
#
# Installs uv if it's missing, the `game-guide` command with uv (into ~/.local/bin), the yt-dlp video downloader, and a
# GameGuider shortcut in the Start Menu. Then `game-guide setup` checks what else this machine needs and walks you
# through connecting your AI (Claude Code or the Codex CLI) if none is ready. On macOS and Linux, use install.sh.
# Run it again to update. GAMEGUIDER_VERSION=0.2.0 pins a version; GAMEGUIDER_BASE_URL downloads the release files
# from somewhere else (a mirror, or a local test server).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo = 'matischneider082/gameguider'
$Version = $env:GAMEGUIDER_VERSION
if (-not $Version) { $Version = '__VERSION__' }  # the release workflow stamps the version here

function Say($msg) { Write-Host "`n==> $msg" }
function Fail($msg) { Write-Host "`nGameGuider was not installed: $msg"; exit 1 }

if ($Version -match '^__') {  # unstamped (a copy from the repository): ask GitHub for the latest release
  Say 'Finding the latest release on GitHub'
  try {
    $Version = (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest").tag_name
  } catch {
    Fail 'could not find the latest release on GitHub. Check your connection and try again.'
  }
}
$Version = $Version -replace '^v', ''
$Base = if ($env:GAMEGUIDER_BASE_URL) { $env:GAMEGUIDER_BASE_URL } else { "https://github.com/$Repo/releases/download/v$Version" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say 'Installing uv, which installs GameGuider and its Python'
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
}
$env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Fail "uv was installed but isn't on PATH yet. Open a new terminal and run this again."
}

Say "Installing GameGuider $Version"
uv tool install --force "game-guide @ $Base/game_guide-$Version-py3-none-any.whl"
if ($LASTEXITCODE -ne 0) { Fail "could not install the game-guide command (uv exited $LASTEXITCODE)." }
$bin = (& uv tool dir --bin)

if (-not (Get-Command yt-dlp -ErrorAction SilentlyContinue)) {
  Say 'Installing yt-dlp for video playback and still frames'
  uv tool install yt-dlp
  if ($LASTEXITCODE -ne 0) { Write-Host 'Skipped yt-dlp; videos still open on YouTube.' }
}

Say 'Adding GameGuider to the Start Menu'
try {
  $iconDir = "$env:LOCALAPPDATA\GameGuider"
  New-Item -ItemType Directory -Force $iconDir | Out-Null
  Invoke-WebRequest "$Base/icon.ico" -OutFile "$iconDir\gameguider.ico"
  $programs = [Environment]::GetFolderPath('Programs')
  $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut("$programs\GameGuider.lnk")
  $lnk.TargetPath = (Join-Path $bin 'game-guide.exe')
  $lnk.Arguments = 'web'
  $lnk.IconLocation = "$iconDir\gameguider.ico"
  $lnk.Description = 'A game shelf and spoiler-aware walkthrough companion'
  $lnk.Save()
} catch {
  Write-Host "Skipped the Start Menu shortcut; start GameGuider with: game-guide web"
}

Say 'Checking what else GameGuider needs'
& (Join-Path $bin 'game-guide.exe') setup
if ($LASTEXITCODE -ne 0) { Write-Host 'Setup hit a problem; run `game-guide setup` to see it again.' }

Say "GameGuider $Version is installed"
Write-Host 'Open GameGuider from the Start Menu, or run: game-guide web'
Write-Host 'To update later, run the same install command again.'
