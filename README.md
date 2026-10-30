# GameGuider

Track the games you're playing and get spoiler-aware help for the spot you're stuck on. It reads wikis, guides and YouTube transcripts, strips out ads, saves maps and screenshots, and distills them into the few facts you need.

There are four ways to use it, and all share the same notes:

- **macOS Desktop App (`GameGuider.app`):** installed by the installer below (or `game-guide install-app`) and pinned to your Dock. Guide sources and other web links open in its own ad-blocking browser window, with back, forward and **Open in Browser**; ⌘-click a link to use your default browser instead.
- **Local Browser Web UI (`game-guide web`):** opens a local web app (`127.0.0.1`) for browsing full chapter-by-chapter walkthroughs, zoomable maps/screenshots, still frames, and inline video streams downloaded via `yt-dlp`.
- **Standalone `game-guide` CLI/TUI:** its own terminal app (and press `o` at any time to open the Web UI in your browser). Both the Web UI and TUI run the `claude` or `codex` CLI you already have installed, so they use your existing Claude or ChatGPT subscription. The AI guides need no API key, and the app never touches their login tokens.
- **Claude Code plugin:** the `game-guide` skill, for use inside Claude Code.

Notes are Markdown files in the notes folder, `~/Documents/GameGuider` unless you choose another one with `game-guide setup --notes-dir PATH` (for example a folder in your Obsidian vault). The choice is saved in `~/.config/game-guide/config.json`; `$GAME_GUIDE_DIR` overrides it for one shell:

- `GAMES.md` is the index.
- Each game has its own `<game-slug>.md` with your character and its distilled guides.
- `<game-slug>.walkthrough.md` holds the full chapter-by-chapter walkthrough.
- `images/` holds saved maps, puzzle diagrams, and screenshots.

Don't put the notes folder inside a git repo: Claude Code treats writes there as sensitive and refuses them in `-p` mode.

## Install

On macOS or Linux, run this in Terminal:

```bash
curl -LsSf https://github.com/matischneider082/gameguider/releases/latest/download/install.sh | sh
```

The installer sets up [uv](https://docs.astral.sh/uv/) if you don't have it, installs the `game-guide` command (with its own Python, in `~/.local`) and the `yt-dlp` video downloader, and on macOS installs **GameGuider.app** in `~/Applications` and adds it to the Dock. Set `GAMEGUIDER_NO_DOCK=1` to leave the Dock alone. It doesn't need administrator rights.

### Connect your AI

GameGuider researches guides with an AI you already pay for, through its command-line app, so there's no API key and no extra cost:

| Your AI | Plan | App GameGuider uses |
|---|---|---|
| Claude | Claude Pro or Max, or an Anthropic account | [Claude Code](https://code.claude.com/docs/en/setup) |
| ChatGPT | ChatGPT Plus, Pro or Business | [Codex CLI](https://github.com/openai/codex) |

The installer finishes by walking you through it: pick your AI, and it runs that app's official installer and then its sign-in, which opens your browser. You can redo it any time:

- **In the app:** select **Setup** in the sidebar (it opens by itself on first launch when no AI is connected). Pick your AI, then follow the two steps; each has **Copy** and, on macOS, **Run in Terminal**. Select **Check again** when you're done.
- **In a terminal:** run `game-guide setup` (add `--connect` to switch AIs).
- **By hand:** install Claude Code with `curl -fsSL https://claude.ai/install.sh | bash` and sign in with `claude auth login`, or install the Codex CLI with `curl -fsSL https://chatgpt.com/codex/install.sh | sh` and sign in with `codex login`.

GameGuider only asks each app whether it's signed in; it never reads your account details or login tokens.

Video playback and still frames also need `ffmpeg` (`brew install ffmpeg` on macOS), and an external player needs `mpv`; without them, videos open on YouTube. The shelf, notes, search and saved walkthroughs work without any of these.

**Requirements:** macOS 13 or later (Apple silicon or Intel), or Linux, for the installer. Run `game-guide setup` at any time to check this machine; in the app, use **Setup** in the sidebar.

**Updating:** run the install command again. **Check for updates** in the sidebar tells you when a newer release exists.

**Uninstalling:** `uv tool uninstall game-guide`, then delete `~/Applications/GameGuider.app`. Your notes folder is left alone.

**Windows:** not supported natively (GameGuider stops AI and video processes with POSIX process groups). Under [WSL](https://learn.microsoft.com/windows/wsl/install), run the install command in your Linux shell, then `game-guide web` and open the printed address in your Windows browser.

If you download `GameGuider-macOS.zip` from the release page in a browser instead of using the installer, macOS quarantines it because the app is not notarized. Install the command first (the installer does this), then run `xattr -dr com.apple.quarantine GameGuider.app` once before opening the app.

### Install from source

Clone the repository and install the Python package (Python 3.12 or newer):

```bash
git clone https://github.com/matischneider082/gameguider.git
cd game-guide
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
game-guide web
```

On macOS, `game-guide package-app` builds `out/GameGuider.app` locally (this needs the Xcode command line tools for `swiftc`). `game-guide install-app` additionally copies it into Applications and adds it to the Dock. The app runs the `game-guide` command it was built with, falling back to `~/.local/bin/game-guide`; if neither exists, it shows the install command.

## Sharing a guide

Open a game and select **Share guide**. Choose a spoiler level, optionally include your personal notes, and select **Download guide**. The resulting HTML file can be sent to someone and opened in a browser without GameGuider; it contains the selected walkthrough, saved images, and links to videos. Video links need an internet connection. Personal notes are excluded unless selected; shelf progress is not included.

Spoiler filtering applies to sections labelled Hint, Strategy, and Answer; other text is included as written and may contain spoilers. Images that are missing or outside the notes folder's `images` directory are marked unavailable. The export is created locally and isn't uploaded or sent by GameGuider.

## Checking for updates

In GameGuider, select **Check for updates** in the sidebar. It doesn't download or install anything.

- **Installed with the installer (or from a wheel):** the check compares your version with the latest GitHub release. To update, run the install command again and reopen the app.
- **A source checkout:** the check compares it with the `main` branch on GitHub. For a clean checkout of `main`, quit GameGuider and run the following from the repository directory, then reopen the app:

```bash
git pull --ff-only https://github.com/matischneider082/gameguider.git main
mise run setup
mise run install-app
```

If you have local changes, are on a different branch, or the histories have diverged, review your checkout before updating. An offline connection, an inaccessible repository, or unpublished local commits can prevent a comparison.

## Setup with mise

```bash
mise install && mise run bootstrap   # runs setup, install and plugin below (safe to re-run), then lists missing optional tools
mise run install-app                 # builds GameGuider.app, installs to /Applications/GameGuider.app, and pins to the macOS Dock
```

Or step by step:

```bash
mise install          # python 3.13, uv, yt-dlp
mise run setup        # uv sync
mise run install      # puts `game-guide` on PATH (editable, so it uses this repo's skill)
mise run plugin       # adds this repo as a local marketplace and installs the Claude Code plugin
mise run package      # builds out/GameGuider.app without copying to /Applications
mise run install-app  # builds GameGuider.app, installs to /Applications, and adds it to the Dock
mise use -g yt-dlp        # yt-dlp on PATH everywhere, not just in this repo (the CLI runs from any folder)
brew install ffmpeg mpv   # still frames and video playback
```

## Use

```bash
game-guide setup                   # check what's installed and what's missing, with the fix for each
game-guide setup --notes-dir ~/Obsidian/Games   # keep your notes somewhere else
game-guide web                     # open the local browser Web UI (walkthroughs, images & video)
game-guide install-app             # build GameGuider.app, install to /Applications, and pin to the Dock
game-guide package-app             # build out/GameGuider.app bundle
game-guide                         # TUI (press `o` inside to open the Web UI)
game-guide list
game-guide backlog                         # list your Backlog in queue order
game-guide backlog "Celeste" "Hades II" --platform PC --artwork
game-guide backlog $'Celeste\nHades II | PC | Roguelike'  # paste a multiline list
game-guide track "Hollow Knight" --platform Switch --artwork
game-guide ask --spoiler hint "how do I get past the Mantis Lords?"
game-guide ask --backend codex --resume <id> "that didn't work, any other way?"
game-guide ask "full walkthrough for Hollow Knight"     # research the whole game, chapter by chapter
game-guide walkthrough "Hollow Knight"                  # list its chapters
game-guide search "mantis lords" --spoiler strategy     # find it in every saved guide and notes file
game-guide walkthrough "Hollow Knight" 3 --spoiler strategy
```

A full walkthrough is saved next to the notes as `<game-slug>.walkthrough.md`. It has one `##` section per chapter, and each chapter has hint, strategy and answer levels, plus saved maps (`images/<game-slug>/…`) and timestamped video links that play locally with zero ads. Building one also fetches the game's cover artwork, embeds it in the walkthrough preamble, and shows it on Web UI library cards and the game header. Building one takes a few minutes.

In the Web UI, search your library by title, platform, or genre and filter it by status. Typing three or more letters also searches inside every saved walkthrough and notes file; results appear under the shelf and open the matching chapter. Search only sees what the current spoiler level shows, so it never reveals a hidden answer. Use **Continue guide** to return to your saved chapter; jumping between chapters does not change your saved spot. The **Maps** tab collects available map-labelled images referenced by the visible notes and walkthrough chapters. Open a map to zoom and scroll, or jump to its chapter. Maps follow the selected spoiler level, and availability depends on the guide's saved images; they are static images rather than interactive world maps.

In the Web UI, use **Backlog List** to paste bulleted, numbered, or plain multiline lists. Select the **Backlog** filter and use the arrow buttons to reorder the queue. Each line accepts `Game | Platform | Genre | Notes`; default platform and genre are optional, and cover fetching can be enabled for the whole batch.

In the Web UI, **Setup** in the sidebar shows the same checks and lets you change the notes folder. A banner on the shelf appears when GameGuider can't research guides yet, for example when neither the Claude nor the Codex CLI is installed.

In Claude Code, say "I just started Elden Ring on PS5", or run `/game-guide:game-guide how do I beat Malenia`.

### TUI keys

| Key | What it does |
|---|---|
| ↑/↓ in the games list | switch game |
| Enter in the question box | ask, using the spoiler level and backend chosen next to it |
| `w` | build the selected game's full walkthrough (or rebuild it) |
| `o` | open the local browser Web UI for the current game |
| ↑/↓ and Enter (Walkthrough tab) | page through the chapters; Enter starts a question about the chapter. ● marks the chapter with your current spot |
| `ctrl+t` | next tab |
| `p` (Media tab) | play the selected video in an mpv window, from its timestamp |
| `f` (Media tab) | show still frames from 2 s before to 2 s after the timestamp, inline |
| `r` | reload the notes |
| `ctrl+n` | start a new conversation (stops a question that is still running, like **New Chat** in the Web UI) |
| `ctrl+q` | quit |

YouTube refuses ffmpeg and mpv reading its streams directly (403), and embedding YouTube in a browser serves ads. So the first play or frame extraction on a video downloads it in full at 720p or less via `yt-dlp` into `~/.cache/game-guide`, and the Web UI streams it directly to the `<video>` element with HTTP Range seeking and zero ads. After that, frames and replays are instant. Nothing clears that cache automatically.

Images display at full resolution in the Web UI, and in Ghostty, Kitty, WezTerm and iTerm2 in the TUI. Other terminals show coloured blocks instead.

## How the backends run

- **Claude:** `claude -p "/game-guide:game-guide …" --output-format stream-json --verbose`. Follow-ups use `--resume` and send just the question, because the session already has the skill loaded.
  - The notes folder is the working directory, and edits are allowed only there (`acceptEdits`).
  - It loads only this plugin: `--plugin-dir`, with `--setting-sources project` and `--strict-mcp-config`. Your user hooks, other plugins and MCP servers don't slow it down.
  - Fetched pages and captions are untrusted text, so a prompt injection in them must not be able to run commands. Bash may run only the skill's scripts (`captions.py`, `save_image.py`, `games_table.py`, `fetch_text_guide.py`, `fetch_yt_walkthrough.py`), and Read is limited to the notes folder, the skill folder and `/tmp/game-guide`.
- **Codex:** `codex exec --json` in the notes folder.
  - The shell runs in a `game-guide` permission profile: it can read system basics, read and write the notes folder, and read the skill, with no network access. So the skill's fetch, caption and image scripts don't run under Codex; it reads guides through its live web search instead.
  - Your Codex MCP servers, plugins, connector apps, hooks, browser and computer use are switched off for these runs. If `~/.codex/config.toml` can't be read, the question is refused rather than run with them on.
  - Codex can't load Claude plugins, so the skill text is inlined into the first prompt and follow-ups use `codex exec resume`.
  - Codex has no per-command allowlist, so the profile does that job: a prompt injection in a fetched page can't read files outside the notes folder or upload anything from the shell. The most it could leak through web search queries is your game notes. The Claude backend instead limits its shell to the skill's own scripts.

## Build distributions

```bash
uv build                         # produces a source archive and wheel in dist/
python -m pip install dist/*.whl  # install the built wheel
```

The wheel includes the plugin, its scripts, and the app icon. GitHub Actions runs the tests, builds both distributions, and uploads them as workflow artifacts on pushes and pull requests.

### Releasing

1. Set the new `version` in `pyproject.toml`, run `uv sync`, and commit.
2. Check the app locally: `uv run game-guide package-app`, then `GAME_GUIDE_APP_SMOKE=1 GAME_GUIDE_DIR=$(mktemp -d) ./out/GameGuider.app/Contents/MacOS/GameGuider` must print `SMOKE_OK`.
3. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.

The `Release` workflow then runs the tests, builds the wheel and source archive, builds the universal macOS app on a macOS runner, stamps `install.sh` with the version, and publishes a GitHub release with all four files. The one-line installer always downloads the latest release.

## Develop

```bash
mise run test
```

The skill lives in `skills/game-guide/`. Its scripts are:

- `games_table.py`, which the skill runs when it loads.
- `fetch_text_guide.py`, which extracts clean walkthrough sections, puzzle tables, ASCII maps, walkthrough images and embedded YouTube links with ads removed.
- `fetch_yt_walkthrough.py`, which extracts YouTube chapters, descriptions, top comments, transcripts and storyboard frame grids.
- `captions.py`, which reads auto-captions cleaned up by `vtt_to_text.py`.
- `save_image.py`, which downloads an image (`image/*`, at most 10 MB, https only) into the notes folder's `images/`.

In Claude Code outside the TUI/Web UI, the skill asks permission the first time it runs a script.

### Critic scores

Open a game and select **Get critic score**. GameGuider looks up the game on [Wikidata](https://www.wikidata.org/) and shows the OpenCritic critic average (or Metacritic or CriticDB when OpenCritic has none), with the number of reviews when Wikidata records it. No account or API key is needed. Scores are stored locally in the notes folder, and the title must exactly match a video game on Wikidata. When several games share a title, such as a remake, the first match wins; edit the title to pick another. Scores are game-wide, not specific to the selected console.

If `IGDB_CLIENT_ID` and `IGDB_ACCESS_TOKEN` are set in the environment that launches GameGuider, scores come from IGDB's critic aggregate instead. See the [IGDB API documentation](https://api-docs.igdb.com/) for credentials and terms.
