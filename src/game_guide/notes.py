"""The read side of the notes folder: the Game and Video types, paths to a game's files, and parsing of notes and
walkthroughs (chapters, spoiler redaction, video and image links). It never writes; GAMES.md and the <slug>.md
notes files are owned by the library module, and cover art by the artwork module, both of which build on this one."""

from __future__ import annotations

import datetime
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from . import settings

YOUTUBE = re.compile(r"https?://(?:www\.|m\.)?(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/))([\w-]{11})([^\s)>\]]*)")
OFFSET = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?")
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

STATUSES = ("Playing", "Backlog", "Paused", "Beaten", "Dropped")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def today_iso() -> str:
    return datetime.date.today().isoformat()


def games_dir(directory=None) -> Path:
    """The notes folder: `directory` when given, else the configured one (see settings.notes_dir)."""
    return Path(directory).expanduser() if directory else settings.notes_dir()


def slugify(name):
    # Accents fold to ASCII and apostrophes vanish, so "Pokémon"/"Marvel's" slug the way a model would write them.
    name = "".join(c for c in unicodedata.normalize("NFKD", name.lower()) if not unicodedata.combining(c))
    name = name.replace("'", "").replace("\u2019", "")  # remove apostrophes and right single quotation marks
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")


@dataclass
class Game:
    name: str
    platform: str = ""
    genre: str = ""
    status: str = ""
    spot: str = ""
    updated: str = ""

    @property
    def slug(self):
        return slugify(self.name)


@dataclass
class Video:
    id: str
    at: int
    label: str

    @property
    def url(self):
        return f"https://youtu.be/{self.id}?t={self.at}"


def notes_path(game, directory=None):
    return games_dir(directory) / f"{game.slug}.md"


def read_notes(game, directory=None):
    path = notes_path(game, directory)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def walkthrough_path(game, directory=None):
    return games_dir(directory) / f"{game.slug}.walkthrough.md"


def read_walkthrough(game, directory=None):
    path = walkthrough_path(game, directory)
    return path.read_text(encoding="utf-8") if path.exists() else ""


CHAPTER = re.compile(r" {0,3}##[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")
FENCE = re.compile(r" {0,3}(```|~~~)")


def chapters(text):
    """The `## ` sections of a walkthrough, in order, as (title, body including its heading)."""
    found, fenced = [], False
    for line in text.splitlines(keepends=True):
        m = None if fenced else CHAPTER.fullmatch(line.rstrip("\r\n"))
        fenced ^= bool(FENCE.match(line))  # a `## ` line inside a code block is not a chapter
        if m:
            found.append([m.group(1), line])
        elif found:
            found[-1][1] += line
    return [tuple(c) for c in found]


def current_chapter(sections, spot):
    """Index of the first chapter that names the player's spot, or None."""
    spot = (spot or "").strip().casefold()
    if spot and spot != "?":
        # Titles first: a chapter's text often names the next area before you get there.
        for part in (0, 1):
            for i, section in enumerate(sections):
                if spot in section[part].casefold():
                    return i
    return None


LEVELS = ("hint", "strategy", "answer")
HEADING = re.compile(r"(#{1,6})\s+(.*?)\s*#*\s*")
HIDDEN_NOTE = "_{level} hidden — raise the spoiler level to see it._"  # the renderer turns this line into a reveal


def redact(notes, spoiler, hidden=HIDDEN_NOTE):
    """Hide the `#### Hint|Strategy|Answer` subsections above `spoiler`; everything else is kept as written.

    Each hidden subsection keeps its heading, followed by the `hidden` note with `{level}` filled in.
    """
    shown, hiding, out = LEVELS.index(spoiler) if spoiler in LEVELS else -1, False, []
    for line in notes.splitlines():
        m = HEADING.fullmatch(line)
        if m and len(m.group(1)) <= 4:
            hiding = False
            level = m.group(2).lower() if len(m.group(1)) == 4 and m.group(2) in ("Hint", "Strategy", "Answer") else None
            if level and LEVELS.index(level) > shown:
                hiding = True
                out += [line, "", hidden.format(level=m.group(2)), ""]
                continue
        if not hiding:
            out.append(line)
    return "\n".join(out) + ("\n" if notes.endswith("\n") else "")


def start_seconds(rest):
    """Start offset in the tail of a YouTube URL: `?t=95`, `&t=95s`, `?t=1h2m3s`; trailing `.`/`,` ignored."""
    t = parse_qs(rest.lstrip("?&").rstrip(".,;:!")).get("t", [""])[0]
    parts = OFFSET.fullmatch(t)
    h, m, s = (int(g or 0) for g in parts.groups()) if parts else (0, 0, 0)
    return h * 3600 + m * 60 + s


def videos(notes):
    """YouTube sources in the notes, labelled with the rest of their line.

    Handles `- label: url` (the skill's format) and Markdown links like `[1:03](url) label`.
    """
    found = []
    for line in notes.splitlines():
        for m in YOUTUBE.finditer(line):
            found.append(Video(m.group(1), start_seconds(m.group(2)), label(line[:m.start()] + line[m.end():]) or m.group(1)))
    return found


def label(text):
    text = re.sub(r"\]\(\s*\)", "]", text)  # the link whose URL was cut out
    text = re.sub(r"\*\*|__|`|[\[\]]", "", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text)
    text = re.sub(r"^\d+:\d\d(:\d\d)?\s*", "", text.strip())  # a leading timestamp duplicates Video.at
    return re.sub(r"\s+", " ", text).strip(" :·|-\u2014")


def images(notes, directory=None):
    """Local images linked from the notes, as (alt text, absolute path); remote images are skipped."""
    base = games_dir(directory)
    return [(alt or Path(src).name, (base / src).resolve())
            for alt, md_src, embed in IMAGE.findall(notes) for src in [md_src or embed] if "://" not in src]
