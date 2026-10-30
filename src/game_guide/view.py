"""The spoiler-safe Game View: give it a game, a spoiler level and the notes folder, and it returns only what the
player may see. Behind this interface sit the rules every adapter once had to repeat: find the current chapter
before redacting, pick maps from redacted text only, and strip the notes' `# <Game name>` heading. The web UI, TUI,
share export and CLI are adapters over it; URLs and rendering stay with them."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .notes import (
    IMAGE_EXTS,
    LEVELS,
    Game,
    Video,
    chapters,
    current_chapter,
    games_dir,
    images,
    read_notes,
    read_walkthrough,
    redact,
    videos,
)

_MAP = re.compile(r"(?:^|[^a-z0-9])(?:map|maps|minimap|mini-map|atlas)(?:$|[^a-z0-9])", re.I)
_COVER = re.compile(r"(?:^|[^a-z0-9])(?:cover|artwork|boxart|keyart|poster|banner|hero)(?:$|[^a-z0-9])", re.I)

Image = tuple[str, Path]  # (label, absolute path), as notes.images gives it


@dataclass(frozen=True)
class Chapter:
    index: int  # 0-based
    title: str
    body: str  # redacted, starting with its `## ` heading
    current: bool
    videos: tuple[Video, ...]
    images: tuple[Image, ...]


@dataclass(frozen=True)
class MapImage:
    label: str
    path: Path
    chapter: str  # "Notes" for a map in the notes
    chapter_index: int | None


@dataclass(frozen=True)
class GameView:
    game: Game
    spoiler: str
    notes: str  # redacted, without the `# <Game name>` heading
    preamble: str  # redacted walkthrough text before the first chapter, e.g. the cover embed
    chapters: tuple[Chapter, ...]
    current: int | None  # index of the chapter naming the player's spot
    maps: tuple[MapImage, ...]
    media: tuple[Video | Image, ...]  # visible videos, then images, from the notes and every chapter


@dataclass(frozen=True)
class Progress:
    chapters: int
    current: int | None  # 1-based chapter number
    percent: int


def game_view(game: Game, spoiler: str, directory=None) -> GameView:
    """What the player may see of `game` at `spoiler` (anything unknown means "hint"); a cut level leaves notes.HIDDEN_NOTE."""
    spoiler = spoiler if spoiler in LEVELS else "hint"
    base = games_dir(directory)
    notes = re.sub(rf"\A#\s+{re.escape(game.name)}\s*\n", "", redact(read_notes(game, base), spoiler))
    walkthrough = read_walkthrough(game, base)
    sections = chapters(walkthrough)
    here = current_chapter(sections, game.spot)  # before redacting: the spot may be named only in a hidden level
    shown = []
    for i, (title, body) in enumerate(sections):
        body = redact(body, spoiler)
        shown.append(Chapter(i, title, body, i == here, tuple(videos(body)), tuple(images(body, base))))
    # chapters() keeps every line from the first heading on, so whatever comes before them is the preamble.
    preamble = redact(walkthrough[:len(walkthrough) - sum(len(body) for _, body in sections)], spoiler)
    visible = "\n".join([notes, *(c.body for c in shown)])
    return GameView(
        game=game,
        spoiler=spoiler,
        notes=notes,
        preamble=preamble,
        chapters=tuple(shown),
        current=here,
        maps=tuple(_maps(notes, base, "Notes", None) + [m for c in shown for m in _maps(c.body, base, c.title, c.index)]),
        media=(*videos(visible), *images(visible, base)),
    )


@dataclass(frozen=True)
class Hit:
    game: Game
    chapter_index: int | None  # None for the game's notes
    place: str  # the chapter title, or "Notes"
    snippet: str  # plain text around the first match


def search(query: str, games: list[Game], spoiler: str, directory=None, limit: int = 50) -> list[Hit]:
    """Notes and chapters of `games` whose visible text holds every word of `query`, chapter titles first. Searching
    each game's view means a level hidden at `spoiler` can never match, nor show up in a snippet."""
    terms = query.casefold().split()
    if not terms:
        return []
    titled, rest = [], []
    for game in games:
        view = game_view(game, spoiler, directory)
        for index, place, body in [(None, "Notes", view.notes), *((c.index, c.title, c.body) for c in view.chapters)]:
            text = _plain(body)
            if all(t in f"{place}\n{text}".casefold() for t in terms):
                hit = Hit(game, index, place, _snippet(text, terms))
                (titled if any(t in place.casefold() for t in terms) else rest).append(hit)
    return (titled + rest)[:limit]


def _plain(markdown: str) -> str:
    """Roughly the text a reader sees: no headings markers, emphasis, image embeds or link targets."""
    text = re.sub(r"!\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)", " ", markdown)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*|[*_`>]+", " ", text, flags=re.M)
    return " ".join(text.split())


def _snippet(text: str, terms: list[str], width: int = 80) -> str:
    folded = text.casefold()
    at = min((i for i in (folded.find(t) for t in terms) if i >= 0), default=0)
    start, end = max(0, at - width), min(len(text), at + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def progress(game: Game, directory=None) -> Progress:
    """How far through its walkthrough `game` is; a Beaten game whose spot names no chapter counts as finished."""
    sections = chapters(read_walkthrough(game, directory))
    here = current_chapter(sections, game.spot)
    beaten, count = game.status.lower() == "beaten", len(sections)
    current = here + 1 if here is not None else (count if beaten and count else None)
    return Progress(count, current, round(current / count * 100) if current and count else (100 if beaten else 0))


def _maps(text: str, base: Path, chapter: str, chapter_index: int | None) -> list[MapImage]:
    """Saved map images that redacted `text` links to, in order: only existing files under <base>/images, never covers."""
    root = (base / "images").resolve()
    found, seen = [], set()
    for label, path in images(text, base):
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
            continue
        if path in seen or _COVER.search(path.stem) or not _MAP.search(f"{label} {path.stem}"):
            continue
        seen.add(path)
        if label == path.name:
            label = re.sub(r"[-_]+", " ", path.stem).strip().capitalize()
        found.append(MapImage(label, path, chapter, chapter_index))
    return found
