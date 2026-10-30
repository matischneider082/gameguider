#!/bin/sh
# Install or update GameGuider on macOS or Linux:
#
#   curl -LsSf https://github.com/matischneider082/gameguider/releases/latest/download/install.sh | sh
#
# Installs uv if it's missing, the `game-guide` command with uv (into ~/.local), the yt-dlp video downloader, and on
# macOS the GameGuider app in ~/Applications, pinned to the Dock. Then `game-guide setup` checks what else this
# machine needs and walks you through connecting your AI (Claude Code or the Codex CLI) if none is ready.
# Run it again to update. GAMEGUIDER_VERSION=0.2.0 pins a version; GAMEGUIDER_NO_DOCK=1 leaves the Dock alone;
# GAMEGUIDER_BASE_URL downloads the release files from somewhere else (a mirror, or a local test server).
set -eu

REPO="matischneider082/gameguider"
VERSION="${GAMEGUIDER_VERSION:-__VERSION__}"  # the release workflow stamps the version here

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nGameGuider was not installed: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) os=macos ;;
  Linux) os=linux ;;
  *) fail "this installer supports macOS and Linux. On Windows, see the README." ;;
esac
command -v curl >/dev/null 2>&1 || fail "curl is required."

case "$VERSION" in __*)  # unstamped (a copy from the repository): ask GitHub for the latest release
  VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
  [ -n "$VERSION" ] || fail "could not find the latest release on GitHub. Check your connection and try again." ;;
esac
VERSION=${VERSION#v}
BASE="${GAMEGUIDER_BASE_URL:-https://github.com/$REPO/releases/download/v$VERSION}"
USER_PATH="$PATH"

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv, which installs GameGuider and its Python"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export PATH
command -v uv >/dev/null 2>&1 || fail "uv was installed but isn't on PATH yet. Open a new terminal and run this again."

say "Installing GameGuider $VERSION"
uv tool install --force "game-guide @ $BASE/game_guide-$VERSION-py3-none-any.whl"
BIN=$(uv tool dir --bin)

if ! command -v yt-dlp >/dev/null 2>&1; then
  say "Installing yt-dlp for video playback and still frames"
  uv tool install yt-dlp || echo "Skipped yt-dlp; videos still open on YouTube."
fi

if [ "$os" = macos ]; then
  say "Installing the GameGuider app"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL "$BASE/GameGuider-macOS.zip" -o "$tmp/GameGuider.zip" || fail "could not download the app."
  ditto -x -k "$tmp/GameGuider.zip" "$tmp"
  if [ -n "${GAMEGUIDER_NO_DOCK:-}" ]; then
    "$BIN/game-guide" install-app --app "$tmp/GameGuider.app" --dest-dir "$HOME/Applications" --no-dock
  else
    "$BIN/game-guide" install-app --app "$tmp/GameGuider.app" --dest-dir "$HOME/Applications"
  fi
fi

say "Checking what else GameGuider needs"
if (exec </dev/tty) 2>/dev/null; then
  "$BIN/game-guide" setup </dev/tty || true  # a terminal: setup can walk you through connecting your AI
else
  "$BIN/game-guide" setup || true
fi

say "GameGuider $VERSION is installed"
if [ "$os" = macos ]; then
  echo "Open GameGuider from the Dock or ~/Applications."
else
  echo "Run: game-guide web"
fi
case ":$USER_PATH:" in *":$BIN:"*) ;; *) echo "To use the game-guide command in new terminals, run: uv tool update-shell" ;; esac
echo "To update later, run the same install command again."
