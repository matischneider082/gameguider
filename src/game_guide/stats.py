"""Progress overview for the notes folder: status counts, the backlog queue, recently touched games, and how far
each saved walkthrough has gotten. It only reads GAMES.md plus each game's notes and walkthrough, on every call, so
a change shows up the next time the panel asks. Nothing here leaves the machine."""

from __future__ import annotations

from datetime import datetime, timezone

from .library import read_games
from .notes import STATUSES, chapters, current_chapter, games_dir, notes_path, read_walkthrough, walkthrough_path
from .view import progress as walk_progress

_CANON = {status.casefold(): status for status in STATUSES}
_STEPS = (
    (365 * 24 * 3600, "year"),
    (30 * 24 * 3600, "month"),
    (7 * 24 * 3600, "week"),
    (24 * 3600, "day"),
    (3600, "hour"),
    (60, "minute"),
)


def relative_ago(seconds: float) -> str:
    """A short age such as "2 days ago". Under a minute, or a clock that sits ahead of the file, is "just now"."""
    remaining = max(0, int(seconds))
    for size, name in _STEPS:
        count = remaining // size
        if count >= 1:
            return f"{count} {name} ago" if count == 1 else f"{count} {name}s ago"
    return "just now"


def _status(raw: str) -> str:
    text = (raw or "").strip()
    return _CANON.get(text.casefold(), text or "Playing")


def _mtime(path) -> float | None:
    try:
        return path.stat().st_mtime if path.is_file() else None
    except OSError:
        return None


def _chapters(game, base):
    """Walkthrough progress, or None when the game has no readable chapter headings.

    The numbers come from view.progress (a Beaten game whose spot names no chapter counts as finished). The chapter
    title is included only when the spot actually matches a heading, so finishing a game does not surface a later title.
    """
    try:
        report = walk_progress(game, base)
        if not report.chapters:
            return None
        sections = chapters(read_walkthrough(game, base))
    except (OSError, UnicodeDecodeError):
        return None
    here = current_chapter(sections, game.spot)
    title = sections[here][0] if here is not None and here < len(sections) else None
    return report, title


def overview(directory=None, now: datetime | None = None) -> dict:
    """Counts, backlog queue, recent file changes, and per-game walkthrough progress for tracked games."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    base = games_dir(directory)
    counts = {status: 0 for status in STATUSES}
    backlog, recent, progressed = [], [], []
    position = 0
    for game in read_games(base):
        status = _status(game.status)
        counts[status] = counts.get(status, 0) + 1
        if status == "Backlog":
            position += 1
            backlog.append({
                "name": game.name,
                "slug": game.slug,
                "platform": game.platform,
                "spot": game.spot,
                "position": position,
            })
        touched = [stamp for stamp in (_mtime(notes_path(game, base)), _mtime(walkthrough_path(game, base))) if stamp is not None]
        if touched:
            when = max(touched)
            recent.append((when, {
                "name": game.name,
                "slug": game.slug,
                "status": status,
                "ago": relative_ago(moment.timestamp() - when),
            }))
        found = _chapters(game, base)
        if found:
            report, title = found
            progressed.append({
                "name": game.name,
                "slug": game.slug,
                "status": status,
                "spot": game.spot,
                "chapters": report.chapters,
                "current": report.current,
                "percent": report.percent,
                "chapter": title,
            })
    recent.sort(key=lambda item: item[0], reverse=True)
    return {"counts": counts, "backlog": backlog, "recent": [item[1] for item in recent], "progress": progressed}
