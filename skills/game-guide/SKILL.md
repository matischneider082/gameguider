---
name: game-guide
description: Track the video games the user is playing and research guides for them. Use when the user mentions a game they started, finished or are stuck in, asks how to beat a boss, level or puzzle, or asks for a walkthrough. Searches written guides, wikis and YouTube, then distills them into the short info needed to progress, without unrequested spoilers.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/games_table.py)
---

# Game Guide

Keep a log of the user's games, and turn long guides and videos into the few facts they need right now.

## Current games

!`python3 ${CLAUDE_SKILL_DIR}/scripts/games_table.py`

The table above was loaded with this skill. Use it to work out which game and spot the user means. If they don't name a game and only one is `Playing`, assume that one. Don't read `GAMES.md` again in this turn unless you've written to it.

## Where things live

The notes folder is the `Notes folder:` path printed above. All the files below live there. The `game-guide` Web UI and TUI read these files, so keep them in exactly this format:

- `GAMES.md` is the index: one Markdown table with exactly these columns, one row per game:

  ```markdown
  | Game | Platform | Genre | Status | Spot | Updated |
  |---|---|---|---|---|---|
  | Elden Ring | PS5 | soulslike | Playing | Malenia | 2026-10-23 |
  ```

  Status is `Playing`, `Backlog`, `Paused`, `Beaten` or `Dropped`. Genre is a key from the genre table below. Updated is a `YYYY-MM-DD` date.
- `<game-slug>.md` holds one game's notes. To make the slug from the game name: lowercase it; strip accents (Unicode NFKD, then drop the combining marks); delete apostrophes (`'` and `’`); replace each run of other characters outside `a-z` and `0-9` with one `-`; trim `-` from both ends. So `Elden Ring` becomes `elden-ring.md`, `Pokémon Scarlet` becomes `pokemon-scarlet.md` and `Marvel's Spider-Man 2` becomes `marvels-spider-man-2.md`. It starts with a **Character** block: level, build or class, key gear and items, difficulty, and the game version or patch if known. After that come the spot sections in story order, in the format given in step 6.

Create these files the first time they are needed. Never overwrite the user's own notes; append or edit in place.

## Tracking

- **Start a game:** when the user starts one, add a row with status `Playing` and set its genre from the genre table below. Use the closest match, and confirm it with one WebSearch if you're unsure.
- **Backlog a game:** when the user adds a game to their backlog or "want to play" list, add a row with status `Backlog`.
- **Update a game:** when they report progress or ask about a spot ("I'm at the Ornstein fight"), update its current spot and date.
- **Update the character:** when they mention their level, build, gear or difficulty, update the Character block. Missing fields are fine. Ask for one only when the answer depends on it, such as melee or magic for a boss.
- **List games:** when they ask what they're playing or what is in their backlog, read the index and answer from it. Don't guess.

## Researching a spot

1. **Pin down the spot.** You need the game, the platform if it matters, and exactly where they are. Ask one question if it's unclear. The spoiler level is `hint`, `strategy` or `answer`. The Web UI, TUI and CLI always state it in the question (`Spoiler level: …`); ask for it only if no level was given.
2. **Check the saved notes.** Read `<game-slug>.md`. If a section already covers this spot and has a subheading for the requested level, answer from that subheading, cite its sources, and stop. If it has only lower levels (say `#### Hint` is saved and the user now wants the strategy), research as below and add the missing level subheadings to that section; keep the saved ones. Search again from scratch only if the user says the saved advice didn't work or the patch has changed since. When you do, replace that section rather than adding a second one.
3. **Search written sources.** Use WebSearch for `"<game>" <boss/level/puzzle> guide`. Prefer game wikis (Fandom, Fextralife, wiki.gg), GameFAQs, Steam guides, IGN and Game8; `references/source_strategies.md` in this skill's base directory lists which sites suit which genre and what to do when results are thin. Read a page with the text-guide script rather than WebFetch: it strips ads, keeps puzzle tables, ASCII maps, walkthrough images (`![alt](https://...)`) and embedded YouTube links, and lets you read only the section you need. List the page's sections first, then pull the one for the spot:

   ```bash
   uv run --no-config <this skill's base directory>/scripts/fetch_text_guide.py "<url>" --toc
   uv run --no-config <this skill's base directory>/scripts/fetch_text_guide.py "<url>" --section "<heading keyword>"
   ```

   Read two or three sources. Use WebFetch only for a page the script can't parse or that refuses it (a 403 or 429); if WebFetch is blocked as well, move on to another site.
4. **Use YouTube for timestamped ad-free video clips and visual moments.** Read videos only through the walkthrough script; never run `yt-dlp` yourself. It prints the chapters, the description, the top comments (where players post exact codes and routes) and the spoken transcript, without downloading the video:

   ```bash
   uv run --no-config <this skill's base directory>/scripts/fetch_yt_walkthrough.py --search "<game> <spot> guide" --section "<spot>"
   uv run --no-config <this skill's base directory>/scripts/fetch_yt_walkthrough.py "<youtube-url>" --time-range 04:30 08:15
   ```

   Narrow a long video with `--section "<chapter keyword>"`, `--time-range START END` or `--grep "<word>"`. When the moment is visual (a no-commentary video, a route, a puzzle layout), add `--save-storyboards /tmp/game-guide/sb` and open the saved grid images with Read; each one is a 3x3 grid of frames whose timestamps the script prints. Transcript lines start with `[mm:ss]`; use them for the source links.

   If the script finds no transcript, `captions.py` reads the auto-captions instead (`python3 <this skill's base directory>/scripts/captions.py text "<youtube-url>" --from <seconds> --to <seconds>`). If a script says `yt-dlp` or `uv` is missing, tell the user they install with `mise use -g yt-dlp uv`, and ask before installing them.
5. **Distill.** Write only what's needed to get past this spot. Use the sections for the game's genre from the table below, fitted to the Character block: compare the prep with what the character has, say what's missing, and skip advice for other builds. Every genre ends with:
   - **Watch out for:** missables, points of no return, and common mistakes.
   - **Sources:** URLs, with timestamps for videos (`https://youtu.be/<id>?t=<seconds>`).

   When a game mixes genres, such as a puzzle room inside an action RPG, use the template for the kind of spot the user is stuck on, not the game's genre.

   | Genre | Sections | Hint level means |
   |---|---|---|
   | `soulslike` / `action` (boss or hard fight) | **Prep** (level, weapon, upgrades, resistances, summons) · **Phases** (per phase: telegraphs → what to do, the punish window) · **Cheese** (only if asked) | the one attack that kills them most and how to read it |
   | `rpg` (quest, build, story progress) | **Where to go** · **Requirements** (items, flags, level, party) · **Steps** · **Choices** (what each option locks out, no plot spoilers) | the next place to go |
   | `puzzle` | **What to notice** · **Nudge** · **Solution** steps | only "What to notice", then stop and offer the nudge |
   | `metroidvania` / `open-world` (stuck, can't reach somewhere) | **What you need** (ability or item) · **Where it is** · **Route** | the name of the ability, not where it is |
   | `strategy` / `management` | **Goal** · **Opening or build order** · **Counters** · **Economy and timing** | the key priority, like "rush X" |
   | `shooter` / `platformer` / `racing` (execution) | **Setup** (loadout, settings) · **Route or line** · **Hard part** with the input or timing | where the hard part is and what goes wrong there |
   | `collectible` hunt | **Count** (have/total if known) · **List of locations**, closest first · **Missable ones** first | the area to search |

   For a genre not in the table, use Prep, then How to do it.

   Respect the spoiler level they asked for: `hint` is the table's last column, `strategy` is the genre's sections without the outright solution, and `answer` adds the solution (a puzzle's Solution steps, the exact route or input). Never mention plot twists, later bosses or endings unless they asked. When sources disagree, say so and give both versions.
6. **Save.** Append the distilled section to `<game-slug>.md` and update the spot in `GAMES.md`. The Web UI and TUI hide the levels above the one the user picked, so a spot section is its `## <spot>` heading followed only by level subheadings named exactly `#### Hint`, `#### Strategy` and `#### Answer`, in that order. Put everything under them, including **Watch out for** and **Sources**, and nothing between the spot heading and the first subheading. Write each level up to the one requested and none above it:

   ```markdown
   ## Malenia

   #### Hint
   <the hint>
   - <short label>: https://youtu.be/<id>?t=<seconds>

   #### Strategy
   **Prep:** …
   ![arena map](images/elden-ring/malenia-arena.png)
   ```

   Write every YouTube source as `https://youtu.be/<id>?t=<seconds>` on its own dedicated bullet line, starting with a short label (`- Phase 2 waterfowl dodge: https://youtu.be/…`): the Web UI and TUI render each YouTube bullet as an interactive video card with a timestamp-cropped thumbnail, stream and play those clips locally with zero ads, and extract still frames around the timestamp. Whenever a walkthrough page has a helpful map, puzzle diagram or route screenshot, save it with the script below and link it under the level it belongs to as `![what it shows](images/<game-slug>/<file>)`; the Web UI and TUI display local images inline. The script accepts only an https image of 10 MB or less, and a destination inside `images/` in the notes folder.

   ```bash
   python3 <this skill's base directory>/scripts/save_image.py "<https-image-url>" images/<game-slug>/<file>
   ```

   Reply with the distilled section only, not the raw research. When the caller asks for structured output (the `game-guide` Web UI, TUI and CLI do), fill `summary` with the hint, or for `strategy` and `answer` the one-line plan; put the genre's sections into `prep`, `steps` and `watch_out`, and each source into `sources` with `at_seconds` for a video timestamp. Otherwise reply in Markdown as above.

## Full walkthrough

When the user asks for the whole game ("full walkthrough", "how do I beat the entire game"), write `<game-slug>.walkthrough.md` in the notes folder. The Web UI and TUI Walkthrough tab lists its chapters and lets the user page through them with inline pictures, chapter visual strips, and ad-free videos with timestamp thumbnails. **Use subagents in parallel** to make building full walkthroughs fast, in-depth, and visually rich:

1. **Step 1 (Outline & Game Cover Artwork):** Search for `"<game>" walkthrough` and choose one or two complete walkthroughs (Game8, IGN, RPG Site, Fextralife, GameFAQs or a wiki). Fetch the walkthrough table of contents (`uv run --no-config <this skill's base directory>/scripts/fetch_text_guide.py "<url>" --toc`) and the main YouTube walkthrough video chapters (`uv run --no-config <this skill's base directory>/scripts/fetch_yt_walkthrough.py --search "<game> full walkthrough"`).
   - **Save Game Cover / Key Artwork (`images/<game-slug>/cover.<ext>`):** Download the game's official box art, cover artwork, or hero banner (`fetch_text_guide.py --toc` prints `- **Cover / Hero Artwork**: ![Cover Artwork](https://...)`, or grab the cover image from Wikipedia / the walkthrough hub page) using `python3 <this skill's base directory>/scripts/save_image.py "<https-cover-url>" images/<game-slug>/cover.jpg` (or `.png` / `.webp`). The Web UI displays `images/<game-slug>/cover.*` on the sidebar game card, backlog list, and header!
   - Use the main-story order for your chapters, usually 8 to 25 of them. Leave out side content unless it's needed to finish the game or it's missable. If a site blocks the script, use WebFetch or try another walkthrough site. Never fill chapters with "watch the video" or "no source found": a chapter needs in-depth written steps from a source you read, or a video transcript read with the walkthrough script.
2. **Step 2 (Parallel Subagents per Chapter Batch):** Spawn subagents in parallel (e.g., 3–4 chapters per subagent using `Agent` / `Task`) to research each batch concurrently. Pass each subagent the skill base directory, notes folder, walkthrough URL(s), main YouTube walkthrough URL(s), game slug, and Character block. Each subagent:
   - Runs `uv run --no-config <skill_dir>/scripts/fetch_text_guide.py "<url>" --section "<chapter>"` to extract ad-free steps, flowchart steps, puzzle tables, ASCII maps, and image URLs (`![...](https://...)`), cross-checking individual chapter pages when the hub page is brief.
   - **Pictures (`images/<game-slug>/`):** Downloads at least 1–3 key battle maps, puzzle diagrams, or route/boss screenshots per chapter using `python3 <skill_dir>/scripts/save_image.py "<https-image-url>" images/<game-slug>/<chapter-slug>-<name>.<ext>` and embeds `![caption](images/<game-slug>/<chapter-slug>-<name>.<ext>)` under `#### Strategy` and/or `#### Answer`. Never leave a walkthrough with zero saved images when the source pages have maps or step screenshots.
   - **Ad-free videos (`https://youtu.be/<id>?t=<seconds>`):** Runs `uv run --no-config <skill_dir>/scripts/fetch_yt_walkthrough.py` for chapter/boss timestamps and adds dedicated `- <short label>: https://youtu.be/<id>?t=<seconds>` bullet lines so the Web UI and TUI display timestamp-cropped video thumbnails, stream the exact moment locally with no ads, and extract still frames.
   - Returns the finished `## <Chapter>` markdown sections with `#### Hint`, `#### Strategy`, and `#### Answer`.
3. **Step 3 (Assemble & Save):** Combine all chapter sections in story order into `<game-slug>.walkthrough.md`. Start it with `# <game> walkthrough`, embed the saved game artwork (`![<game> Cover Artwork](images/<game-slug>/cover.jpg)`), and add a line naming the main source and patch. Name each chapter after its area or act (`## Act 2: Greenpath`), never after a boss, a twist or an ending, because the user sees every title. Put three level subheadings in each chapter, because the Web UI and TUI hide levels above the selected spoiler level:
   - `#### Hint`: the goal and where to go, in one or two sentences, plus a timestamped chapter-start video bullet (`- Chapter start: https://youtu.be/<id>?t=<seconds>`) when available so even Hint view has a video thumbnail card.
   - `#### Strategy`: in-depth route, key fights with the genre sections from the table above (recommended classes/weapons/items, turn limits, victory/defeat conditions, unit positioning, counters, chest/economy pickups), saved battle/route maps (`![...](images/<game-slug>/...)`), timestamped video links, and **Watch out for** with missables, recruits, and points of no return.
   - `#### Answer`: numbered step-by-step walkthrough, puzzle solutions, solution diagrams/screenshots (`![...](images/<game-slug>/...)`), exact routes, sidequests/unlocks, and timestamped solution video links.

   Put the chapter's sources and media under the level they support, in the same format as in step 6. Rewrite the whole file only if the user asks for a rebuild. Otherwise fill in missing chapters and keep any edits the user made.
4. **Reply** with a one-line summary: how many chapters you wrote, how many images were saved (including the game cover artwork), and the sources you used. For structured output, use `kind: tracked`.

## Rules

- Every tip must come from a source you actually read in this session. If you couldn't find one, say so instead of answering from memory. Patches change bosses and builds.
- Note the game version or patch when a source mentions it.
- Keep spot replies focused; make full walkthrough chapters comprehensive and visual.
- A `hint` reply is at most 3 sentences, plus its sources.
