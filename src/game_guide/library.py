"""The Library module: the one owner of GAMES.md and of every write to the per-game <slug>.md notes files.

Its interface is read_games / discover_games / find_game for lookups and save_game, update_game, remove_game,
reorder_games, batch_add_games, write_notes and append_note for changes. Every write is a temp file plus os.replace,
so a reader never sees half a file, and every read-modify-write of GAMES.md holds _LOCK, so the threaded web server
can't lose an update. Reading a game's files and parsing walkthroughs stays in the notes module.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
from contextlib import suppress
from pathlib import Path

from .notes import Game, games_dir, notes_path, slugify, today_iso

# Re-entrant because save_game runs inside batch_add_games and update_game.
# The AI CLI writes GAMES.md from another process: there the rename, not this lock, is what protects readers.
_LOCK = threading.RLock()


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` with `text` through a temp file in the same folder, so readers see the old or the new file."""
    path = Path(path).resolve()  # a symlinked notes file keeps its link; the rename lands on the target
    temp = path.with_name(f".{path.name}-{secrets.token_hex(4)}.tmp")
    try:
        with open(temp, "x", encoding="utf-8") as handle:  # "x" honours the umask, unlike mkstemp's 0600
            handle.write(text)
        with suppress(FileNotFoundError):
            shutil.copymode(path, temp)  # keep a mode the user set on the file being replaced
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_games(directory=None):
    path = games_dir(directory) / "GAMES.md"
    if not path.exists():
        return []
    header, games = None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if header is None:
            header = [c.lower() for c in cells]
            continue
        row = dict(zip(header, cells))
        games.append(Game(row.get("game", ""), row.get("platform", ""), row.get("genre", ""),
                          row.get("status", ""), row.get("spot", ""), row.get("updated", "")))
    return [g for g in games if g.name]


def _walkthrough_title(path: Path) -> str:
    """The walkthrough file's `# Title` heading without a trailing "Walkthrough", or "" if it has none."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        first_line = handle.readline().splitlines()[:1]
    if first_line and first_line[0].startswith("# "):
        return re.sub(r"\s*(?:—|-)?\s*walkthrough\s*$", "", first_line[0][2:].strip(), flags=re.I)
    return ""


def _matches(game: Game, key: str) -> bool:
    return game.slug == slugify(key) or game.name.casefold() == key.strip().casefold()


def discover_games(directory=None, requested_game: str | None = None) -> list[Game]:
    """Tracked games, then walkthroughs nobody tracks, then `requested_game` if it has a notes or walkthrough file."""
    base = games_dir(directory)
    tracked = read_games(base)
    seen_slugs = {g.slug for g in tracked}
    if base.exists():
        for walk_file in sorted(base.glob("*.walkthrough.md")):
            slug = walk_file.name[: -len(".walkthrough.md")]
            if slug and slug not in seen_slugs:
                title = _walkthrough_title(walk_file) or slug.replace("-", " ").title()
                tracked.append(Game(name=title, status="Walkthrough"))
                seen_slugs.add(slugify(title))
    if requested_game:
        req_slug = slugify(requested_game)
        if req_slug and req_slug not in seen_slugs:
            walk_path = base / f"{req_slug}.walkthrough.md"
            note_path = base / f"{req_slug}.md"
            if walk_path.exists() or note_path.exists():
                tracked.append(Game(name=requested_game))
    return tracked


def find_game(key: str, directory=None) -> Game | None:
    """The tracked game or untracked walkthrough whose slug is slugify(key) or whose name is `key` ignoring case."""
    return next((g for g in discover_games(directory, key) if _matches(g, key)), None)


def _clean_cell(val: str) -> str:
    return re.sub(r"\s+", " ", (val or "").replace("|", "/").replace("\r", " ").replace("\n", " ")).strip()


def write_games(games: list[Game], directory=None) -> Path:
    base = games_dir(directory)
    base.mkdir(parents=True, exist_ok=True)
    path = base / "GAMES.md"

    with _LOCK:
        prefix_lines: list[str] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.lstrip().startswith("|"):
                    break
                prefix_lines.append(line)

        table_lines = [
            "| Game | Platform | Genre | Status | Spot | Updated |",
            "|---|---|---|---|---|---|",
        ]
        for g in games:
            if not g.name.strip():
                continue
            table_lines.append(
                f"| {_clean_cell(g.name)} | {_clean_cell(g.platform)} | {_clean_cell(g.genre)} "
                f"| {_clean_cell(g.status or 'Playing')} | {_clean_cell(g.spot)} | {_clean_cell(g.updated or today_iso())} |"
            )

        prefix = "\n".join(prefix_lines).rstrip()
        content = (f"{prefix}\n\n" if prefix else "") + "\n".join(table_lines) + "\n"
        write_atomic(path, content)
    return path


def save_game(
    name: str,
    platform: str | None = None,
    genre: str | None = None,
    status: str | None = None,
    spot: str | None = None,
    updated: str | None = None,
    notes_text: str | None = None,
    lookup_slug: str | None = None,
    directory=None,
) -> Game:
    """Insert or update a tracked game in GAMES.md and initialize <slug>.md if needed."""
    clean_name = _clean_cell(name)
    if not clean_name:
        raise ValueError("Game name is required")

    base = games_dir(directory)
    base.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        existing = read_games(base)

        target_slug = slugify(lookup_slug or clean_name)
        new_slug = slugify(clean_name)
        matched: Game | None = None
        for g in existing:
            if g.slug in (target_slug, new_slug) or g.name.lower() == clean_name.lower():
                matched = g
                break

        stamp = _clean_cell(updated) if updated else today_iso()
        if matched is not None:
            old_note_path = notes_path(matched, base)
            matched.name = clean_name
            if platform is not None:
                matched.platform = _clean_cell(platform)
            if genre is not None:
                matched.genre = _clean_cell(genre)
            if status is not None:
                matched.status = _clean_cell(status) or "Playing"
            elif not matched.status:
                matched.status = "Playing"
            if spot is not None:
                matched.spot = _clean_cell(spot)
            matched.updated = stamp
            game_obj = matched
            new_note_path = notes_path(game_obj, base)
            if old_note_path != new_note_path and old_note_path.exists() and not new_note_path.exists():
                old_note_path.rename(new_note_path)
        else:
            game_obj = Game(
                name=clean_name,
                platform=_clean_cell(platform or ""),
                genre=_clean_cell(genre or ""),
                status=_clean_cell(status or "Playing") or "Playing",
                spot=_clean_cell(spot or ""),
                updated=stamp,
            )
            existing.append(game_obj)

        write_games(existing, base)

        npath = notes_path(game_obj, base)
        if notes_text is not None:
            write_atomic(npath, notes_text.rstrip() + "\n")
        elif not npath.exists():
            spot_heading = game_obj.spot or ("Backlog Queue" if game_obj.status.lower() == "backlog" else "Current Progress")
            meta_parts = [p for p in (game_obj.platform, game_obj.genre, game_obj.status) if p]
            meta_line = " · ".join(meta_parts) if meta_parts else "Playing"
            starter = (
                f"**Character / Build:** {meta_line}\n\n"
                f"## {spot_heading}\n\n"
                "#### Hint\n"
                f"Tracking **{game_obj.name}** ({game_obj.status}). Ask a question below or build a full walkthrough!\n"
            )
            write_atomic(npath, starter)

    return game_obj


def reorder_games(ordered_slugs: list[str], status_filter: str | None = None, directory=None) -> list[Game]:
    """Reorder games in GAMES.md according to ordered_slugs (globally or within a specific status group)."""
    base = games_dir(directory)
    with _LOCK:
        existing = read_games(base)
        if not existing or not ordered_slugs:
            return existing

        slug_order = [slugify(s) for s in ordered_slugs if s]
        by_slug = {g.slug: g for g in existing}

        if status_filter and status_filter.lower() != "all":
            target_status = status_filter.lower()
            group_indices = [i for i, g in enumerate(existing) if (g.status or "playing").lower() == target_status]
            group_games = [existing[i] for i in group_indices]
            reordered_group = []
            seen = set()
            for s in slug_order:
                if s in by_slug and (by_slug[s].status or "playing").lower() == target_status and s not in seen:
                    reordered_group.append(by_slug[s])
                    seen.add(s)
            for g in group_games:
                if g.slug not in seen:
                    reordered_group.append(g)
            for idx, g in zip(group_indices, reordered_group):
                existing[idx] = g
        else:
            reordered = []
            seen = set()
            for s in slug_order:
                if s in by_slug and s not in seen:
                    reordered.append(by_slug[s])
                    seen.add(s)
            for g in existing:
                if g.slug not in seen:
                    reordered.append(g)
            existing = reordered

        write_games(existing, base)
    return existing


def batch_add_games(
    lines: list[str] | str,
    default_status: str = "Backlog",
    default_platform: str = "",
    default_genre: str = "",
    directory=None,
) -> list[Game]:
    """Add or update multiple games at once (e.g. a pasted backlog list).

    Each line can be `Game Name` or `Game Name | Platform | Genre | Spot / Notes`.
    """
    base = games_dir(directory)
    raw_lines = (
        lines.splitlines()
        if isinstance(lines, str)
        else [line for item in lines for line in str(item).splitlines()]
    )
    saved_games: list[Game] = []
    with _LOCK:
        for raw in raw_lines:
            cleaned = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", raw.strip()).strip()
            if not cleaned or cleaned.startswith("#"):
                continue
            parts = [p.strip() for p in cleaned.split("|")] if "|" in cleaned else [cleaned]
            name = parts[0]
            if not name:
                continue
            plat = parts[1] if len(parts) > 1 and parts[1] else (default_platform or None)
            genre = parts[2] if len(parts) > 2 and parts[2] else (default_genre or None)
            spot = parts[3] if len(parts) > 3 and parts[3] else None
            saved_games.append(
                save_game(
                    name=name,
                    platform=plat,
                    genre=genre,
                    status=default_status or "Backlog",
                    spot=spot,
                    directory=base,
                )
            )
    return saved_games


def update_game(
    slug_or_name: str,
    *,
    name: str | None = None,
    platform: str | None = None,
    genre: str | None = None,
    status: str | None = None,
    spot: str | None = None,
    directory=None,
) -> Game:
    """Update an existing tracked game (or promote an untracked walkthrough game into GAMES.md)."""
    base = games_dir(directory)
    with _LOCK:
        key_slug = slugify(slug_or_name)
        matched = next((g for g in read_games(base) if _matches(g, slug_or_name)), None)
        resolved_name = name.strip() if (name and name.strip()) else (matched.name if matched else slug_or_name.strip())
        if not matched and not (name and name.strip()):
            # Check if there is a walkthrough file with a nice # Title
            walk_file = base / f"{key_slug}.walkthrough.md"
            if walk_file.exists():
                resolved_name = _walkthrough_title(walk_file) or resolved_name
        return save_game(
            name=resolved_name,
            platform=platform,
            genre=genre,
            status=status,
            spot=spot,
            lookup_slug=key_slug,
            directory=base,
        )


def remove_game(slug_or_name: str, directory=None) -> bool:
    """Remove a game from GAMES.md while leaving its notes and walkthrough files untouched."""
    base = games_dir(directory)
    with _LOCK:
        existing = read_games(base)
        kept = [g for g in existing if not _matches(g, slug_or_name)]
        if len(kept) == len(existing):
            return False
        write_games(kept, base)
    return True


def write_notes(game: Game | str, content: str, directory=None) -> Path:
    base = games_dir(directory)
    base.mkdir(parents=True, exist_ok=True)
    game_obj = game if isinstance(game, Game) else Game(str(game))
    path = notes_path(game_obj, base)
    with _LOCK:
        write_atomic(path, content.rstrip() + "\n" if content.strip() else "")
        # Touch updated timestamp if tracked in GAMES.md
        existing = read_games(base)
        for g in existing:
            if g.slug == game_obj.slug:
                g.updated = today_iso()
                write_games(existing, base)
                break
    return path


def append_note(
    game: Game | str,
    entry: str,
    heading: str | None = None,
    spot: str | None = None,
    directory=None,
) -> Path:
    """Append a progress note or section to <slug>.md and optionally update the game's spot."""
    base = games_dir(directory)
    base.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        game_obj = game if isinstance(game, Game) else update_game(str(game), spot=spot, directory=base)
        if spot is not None:
            game_obj = update_game(game_obj.slug, spot=spot, directory=base)

        path = notes_path(game_obj, base)
        current = path.read_text(encoding="utf-8").rstrip() if path.exists() else ""
        clean_entry = entry.strip()
        if not clean_entry:
            return path

        section_title = (heading or spot or "").strip()
        block_lines: list[str] = []
        if section_title and f"## {section_title}" not in current:
            block_lines.append(f"## {section_title}")
            if not clean_entry.startswith("#"):
                block_lines.append("")
                block_lines.append("#### Hint")
        block_lines.append(clean_entry)
        block = "\n".join(block_lines)

        updated_text = f"{current}\n\n{block}\n" if current else f"{block}\n"
        return write_notes(game_obj, updated_text, base)
