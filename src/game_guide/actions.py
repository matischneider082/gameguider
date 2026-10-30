"""The web actions: everything the Web UI can ask for, callable without HTTP. Each action takes the parsed JSON body
or query values and the notes folder, resolves the game through the Library, validates its input, makes the change
and returns the JSON-able payload, usually the state built on the Game View. Refused input raises ActionError with
the HTTP status and the user-facing message. The request handler in web.py is the HTTP adapter over this interface:
it maps paths to actions and keeps only the bytes, byte ranges and the NDJSON ask stream."""

from __future__ import annotations

import re
import subprocess
import urllib.parse
from collections.abc import Iterable, Iterator
from pathlib import Path

from . import doctor, media, scores, settings, sharing, stats, themes, updates
from .artwork import fetch_game_artwork, game_artwork_path
from .backends import BACKENDS, SPOILERS
from .conversation import Conversation, Update
from .library import (
    append_note,
    batch_add_games,
    discover_games,
    find_game,
    read_games,
    remove_game,
    reorder_games,
    save_game,
    update_game,
    write_notes,
)
from .notes import STATUSES, Game, Video, games_dir, read_notes
from .render import render
from .view import Image, game_view, progress
from .view import search as search_guides

VIDEO_ID_RE = re.compile(r"[\w-]{11}")


class ActionError(Exception):
    """An action refused its input: `status` is the HTTP status, `message` what the player is told."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _text(body: dict, key: str) -> str:
    return str(body.get(key) or "")


def _optional(body: dict, key: str) -> str | None:
    """The value as text, or None when it is absent, so the Library leaves that field alone."""
    value = body.get(key)
    return None if value is None else str(value)


def _required_game(body: dict) -> str:
    key = _text(body, "game").strip()
    if not key:
        raise ActionError(400, "Game is required.")
    return key


def _game(key: str, directory: Path) -> Game:
    game = find_game(key, directory)
    if game is None:
        raise ActionError(404, "Game not found.")
    return game


def spoiler_level(value, strict: bool = False) -> str:
    """The requested spoiler level; missing means "hint", and so does an unknown one unless `strict` refuses it."""
    level = str(value or "hint")
    if level in SPOILERS:
        return level
    if strict:
        raise ActionError(400, "Invalid spoiler level.")
    return "hint"


def check_video_id(video_id: str) -> str:
    """`video_id` if it is an 11-character YouTube ID, the only form the media routes and the cache accept."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ActionError(400, "Invalid YouTube video ID")
    return video_id


def is_video_cached(video_id: str) -> bool:
    if not VIDEO_ID_RE.fullmatch(video_id) or not media.CACHE.exists():
        return False
    return any(p.name == f"{video_id}.video{p.suffix}" for p in media.CACHE.glob(f"{video_id}.video.*"))


def _artwork_url(game: Game, base_dir: Path) -> str | None:
    artwork = game_artwork_path(game, base_dir)
    if artwork is None:
        return None
    try:
        rel = artwork.resolve().relative_to((base_dir / "images").resolve()).as_posix()
    except ValueError:
        return None
    return f"/images/{urllib.parse.quote(rel)}"


def _local_image_items(found: Iterable[Image], base_dir: Path) -> list[dict]:
    items = []
    images_root = (base_dir / "images").resolve()
    for label, abs_path in found:
        if abs_path.is_relative_to(images_root):
            rel = abs_path.relative_to(images_root).as_posix()
            items.append({
                "type": "image",
                "label": label,
                "src": f"/images/{urllib.parse.quote(rel)}",
                "exists": abs_path.is_file(),
            })
    return items


def _video_items(found: Iterable[Video]) -> list[dict]:
    return [
        {
            "type": "video",
            "id": v.id,
            "at": v.at,
            "label": v.label,
            "url": v.url,
            "thumb": f"/api/thumbnail/{v.id}?at={v.at}" if v.at > 0 else f"/api/thumbnail/{v.id}",
            "cached": is_video_cached(v.id),
        }
        for v in found
    ]


def state_payload(game_query: str | None = None, spoiler: str | None = "hint", directory: Path | None = None) -> dict:
    """The shelf plus the selected game's spoiler-safe view: what every state-changing action answers with."""
    spoiler = spoiler_level(spoiler)
    base_dir = games_dir(directory).resolve()
    tracked_slugs = {g.slug for g in read_games(base_dir)}
    all_games = discover_games(base_dir, game_query)

    selected = find_game(game_query, base_dir) if game_query else None
    if selected is None and all_games:
        selected = all_games[0]

    artwork_urls = {g.slug: _artwork_url(g, base_dir) for g in all_games}
    cached_scores = scores.read_scores(base_dir)
    games_list = []
    for g in all_games:
        p = progress(g, base_dir)
        games_list.append({
            "name": g.name,
            "slug": g.slug,
            "platform": g.platform,
            "genre": g.genre,
            "status": g.status,
            "spot": g.spot,
            "updated": g.updated,
            "artwork": artwork_urls[g.slug],
            "score": scores.score_for(g, cached_scores),
            "tracked": g.slug in tracked_slugs,
            "has_walkthrough": bool(p.chapters),
            "has_notes": bool(read_notes(g, base_dir).strip()),
            "chapter_count": p.chapters,
            "current_chapter_num": p.current,
            "progress_pct": p.percent,
        })

    if not selected:
        return {
            "games": [],
            "selected": None,
            "spoiler": spoiler,
            "statuses": list(STATUSES),
            "backends": list(BACKENDS),
            "notes": "",
            "notes_html": "",
            "raw_notes": "",
            "walkthrough": [],
            "current_chapter": None,
            "media": [],
            "maps": [],
        }

    view = game_view(selected, spoiler, base_dir)
    images_root = (base_dir / "images").resolve()
    return {
        "games": games_list,
        "selected": {
            "name": selected.name,
            "slug": selected.slug,
            "platform": selected.platform,
            "genre": selected.genre,
            "status": selected.status,
            "spot": selected.spot,
            "updated": selected.updated,
            "artwork": artwork_urls.get(selected.slug),
            "score": scores.score_for(selected, cached_scores),
            "tracked": selected.slug in tracked_slugs,
        },
        "spoiler": spoiler,
        "statuses": list(STATUSES),
        "backends": list(BACKENDS),
        "notes": view.notes,
        "notes_html": render(view.notes, base_dir),
        "raw_notes": read_notes(selected, base_dir),
        "walkthrough": [{
            "index": c.index + 1,
            "title": c.title,
            "body": c.body,
            "html": render(c.body, base_dir),
            "current": c.current,
            "videos": _video_items(c.videos),
            "images": _local_image_items(c.images, base_dir),
        } for c in view.chapters],
        "current_chapter": view.current,
        "media": _video_items([m for m in view.media if isinstance(m, Video)])
        + _local_image_items([m for m in view.media if not isinstance(m, Video)], base_dir),
        "maps": [{
            "label": m.label,
            "src": f"/images/{urllib.parse.quote(m.path.relative_to(images_root).as_posix())}",
            "chapter": m.chapter,
            "chapter_index": m.chapter_index,
        } for m in view.maps],
    }


def state(query: dict, directory: Path | None = None) -> dict:
    return state_payload(query.get("game"), query.get("spoiler"), directory)


def overview(_query: dict | None = None, directory: Path | None = None) -> dict:
    """Status counts, the backlog queue, recent notes, and walkthrough progress.

    Read from the notes folder on every call, like `state`, so an edit shows up the next time the panel asks.
    """
    return stats.overview(directory)


def search(query: dict, directory: Path | None = None) -> dict:
    """Notes and chapters across the library holding every word of q, at the spoiler level (see view.search)."""
    text = str(query.get("q") or "").strip()[:200]
    base = games_dir(directory)
    hits = search_guides(text, discover_games(base), spoiler_level(query.get("spoiler")), base) if len(text) >= 2 else []
    return {"query": text, "results": [{"game": h.game.name, "slug": h.game.slug, "chapter_index": h.chapter_index,
                                        "place": h.place, "snippet": h.snippet} for h in hits]}


def share(query: dict, directory: Path | None = None) -> tuple[str, bytes]:
    """The game's portable HTML export at the requested spoiler level, and the file name to download it as."""
    spoiler = spoiler_level(query.get("spoiler"), strict=True)
    base = games_dir(directory).resolve()
    game = _game(query.get("game") or "", base)
    try:
        content = sharing.render_share(game, base, spoiler, query.get("notes") == "1")
    except (OSError, ValueError) as exc:
        raise ActionError(400, str(exc)) from exc
    return f"{game.slug}-gameguider.html", content


def setup(_query: dict | None = None) -> dict:
    """What this machine is missing (see the doctor module), and the notes folder in use."""
    return doctor.as_json()


def save_setup(body: dict) -> dict:
    """Choose the notes folder; answers with the setup checks for it."""
    try:
        settings.set_notes_dir(_text(body, "notes_dir"))
    except ValueError as exc:
        raise ActionError(400, str(exc)) from None
    except OSError:
        raise ActionError(400, "Could not create that folder. Choose another one.") from None
    return doctor.as_json()


def setup_terminal(body: dict) -> dict:
    """Open Terminal on one fixed step of the AI walkthrough (install or sign in), macOS only."""
    try:
        doctor.open_in_terminal(_text(body, "backend"), _text(body, "step"))
    except ValueError as exc:
        raise ActionError(400, str(exc)) from None
    except (OSError, subprocess.SubprocessError):
        raise ActionError(500, "Could not open Terminal. Copy the command and run it yourself.") from None
    return {"ok": True}


def save_theme(body: dict) -> dict:
    try:
        return {"theme": themes.save(_text(body, "theme"))}
    except ValueError as exc:
        raise ActionError(400, str(exc)) from None


def check_updates(_body: dict | None = None) -> dict:
    return updates.check_for_updates()


def install_update(_body: dict | None = None) -> dict:
    try:
        return updates.install_update()
    except ValueError as exc:
        raise ActionError(409, str(exc)) from None


def refresh_artwork(body: dict, directory: Path | None = None) -> dict:
    base = games_dir(directory).resolve()
    game = _game(_text(body, "game"), base)
    if not fetch_game_artwork(game, directory=base):
        raise ActionError(404, "No cover found. Try editing the title or adding cover artwork to the game's images folder.")
    return state_payload(game.slug, body.get("spoiler"), base)


def refresh_score(body: dict, directory: Path | None = None) -> dict:
    base = games_dir(directory).resolve()
    game = _game(_text(body, "game"), base)
    try:
        scores.refresh_score(game, base)
    except (ValueError, OSError) as exc:
        raise ActionError(400, str(exc)) from exc
    return state_payload(game.slug, body.get("spoiler"), base)


def add_backlog(body: dict, directory: Path | None = None) -> dict:
    """Add a pasted list of games (text or a list of lines) to the backlog; the state also lists what was `added`."""
    raw_games = body.get("games", body.get("lines"))
    if not isinstance(raw_games, (str, list)) or (
        isinstance(raw_games, list) and not all(isinstance(item, str) for item in raw_games)
    ):
        raise ActionError(400, "Provide backlog games as text or a list of titles.")
    added = batch_add_games(raw_games, default_platform=_text(body, "platform"), default_genre=_text(body, "genre"),
                            directory=directory)
    if not added:
        raise ActionError(400, "Add at least one game title.")
    if body.get("artwork"):
        for game in added:
            fetch_game_artwork(game, directory=directory)
    payload = state_payload(added[-1].slug, body.get("spoiler"), directory)
    payload["added"] = [{"name": g.name, "slug": g.slug} for g in added]
    return payload


def reorder(body: dict, directory: Path | None = None) -> dict:
    """Reorder the shelf to `ordered_slugs`, across every game or only within one `status`."""
    ordered_slugs = body.get("ordered_slugs", body.get("order"))
    if not isinstance(ordered_slugs, list) or not ordered_slugs or not all(isinstance(item, str) for item in ordered_slugs):
        raise ActionError(400, "Provide a non-empty ordered_slugs list.")
    status_filter = str(body.get("status") or body.get("status_filter") or "all")
    if status_filter.lower() != "all" and status_filter.lower() not in {s.lower() for s in STATUSES}:
        raise ActionError(400, f"Unknown status: {status_filter}")
    reorder_games(ordered_slugs, status_filter=status_filter, directory=directory)
    return state_payload(_text(body, "game").strip() or None, body.get("spoiler"), directory)


def save(body: dict, directory: Path | None = None) -> dict:
    """Add a game or edit its details; `slug` names the game being renamed, and non-blank `notes` replace its notes."""
    name = _text(body, "name").strip()
    if not name:
        raise ActionError(400, "Game name is required.")
    notes_text = _optional(body, "notes")
    try:
        saved = save_game(
            name=name,
            platform=_optional(body, "platform"),
            genre=_optional(body, "genre"),
            status=_optional(body, "status"),
            spot=_optional(body, "spot"),
            notes_text=notes_text if notes_text and notes_text.strip() else None,
            lookup_slug=_text(body, "slug").strip() or None,
            directory=directory,
        )
    except ValueError as e:
        raise ActionError(400, str(e)) from e
    return state_payload(saved.slug, body.get("spoiler"), directory)


def set_spot(body: dict, directory: Path | None = None) -> dict:
    """Move the game (slug or title) to a new spot and/or status, logging an optional `note` under the new spot."""
    game_key = _required_game(body)
    note_entry = _text(body, "note").strip()
    try:
        saved = update_game(game_key, spot=_optional(body, "spot"), status=_optional(body, "status"), directory=directory)
        if note_entry:
            append_note(saved, note_entry, heading=saved.spot or None, directory=directory)
    except ValueError as e:
        raise ActionError(400, str(e)) from e
    return state_payload(saved.slug, body.get("spoiler"), directory)


def edit_notes(body: dict, directory: Path | None = None) -> dict:
    """Replace the game's notes with `notes`, or append `entry` under `heading` (default: the spot)."""
    game_key = _required_game(body)
    spot = _optional(body, "spot")
    if body.get("notes") is not None:
        saved = update_game(game_key, spot=spot, directory=directory)
        write_notes(saved, _text(body, "notes"), directory)
    else:
        entry = _text(body, "entry").strip()
        heading = _text(body, "heading").strip() or None
        if not entry:
            raise ActionError(400, "Note content is required.")
        saved = update_game(game_key, spot=spot if spot is not None and spot.strip() else None, directory=directory)
        append_note(saved, entry, heading=heading or saved.spot or None, directory=directory)
    return state_payload(saved.slug, body.get("spoiler"), directory)


def delete(body: dict, directory: Path | None = None) -> dict:
    """Untrack the game; its notes and walkthrough stay on disk."""
    remove_game(_required_game(body), directory)
    return state_payload(None, body.get("spoiler"), directory)


def frames(video_id: str, query: dict) -> dict:
    """Still frames around `at` seconds into the video, as URLs the frame route serves."""
    check_video_id(video_id)
    try:
        at = max(0, int(float(query.get("at") or "0")))
    except (ValueError, OverflowError):
        raise ActionError(400, "Invalid timestamp") from None
    if reason := media.unavailable("frames"):
        raise ActionError(400, reason)
    try:
        frame_paths = media.frames(Video(video_id, at, video_id))
    except Exception as e:
        raise ActionError(502, f"frames failed: {media.short_error(e)}") from e
    frames_out = []
    for p in frame_paths:
        t_str = p.stem.rsplit("-", 1)[-1]
        frames_out.append({"time": int(t_str) if t_str.isdigit() else at, "url": f"/api/frame/{video_id}/{t_str}"})
    return {"frames": frames_out}


def play(body: dict) -> dict:
    """Open the video at `at` seconds in the external player."""
    video_id = check_video_id(_text(body, "id"))
    try:
        at = max(0, int(float(body.get("at") or 0)))
    except (TypeError, ValueError, OverflowError):
        at = 0
    if reason := media.unavailable("play"):
        raise ActionError(400, reason)
    try:
        media.play(Video(video_id, at, _text(body, "label") or video_id))
    except Exception as e:
        raise ActionError(502, f"play failed: {media.short_error(e)}") from e
    return {"ok": True}


def _question_target(body: dict, directory: Path | None) -> tuple[str, str, str, Game | None]:
    backend = _text(body, "backend") or "claude"
    if backend not in BACKENDS:
        raise ActionError(400, f"Unknown backend: {backend}")
    key = _text(body, "game").strip()
    game = find_game(key, directory) if key else next(iter(discover_games(directory)), None)
    return backend, spoiler_level(body.get("spoiler")), key, game


def ask(conversation: Conversation, body: dict, directory: Path | None = None) -> tuple[str, Iterator[Update]]:
    """Validate a question and hand it to the Conversation: the backend and its update stream, not yet started."""
    backend, spoiler, _, game = _question_target(body, directory)
    question = _text(body, "question").strip()
    if not question:
        raise ActionError(400, "Question is required.")
    return backend, conversation.ask(backend, question, spoiler, game)


def walkthrough(conversation: Conversation, body: dict, directory: Path | None = None) -> tuple[str, Iterator[Update]]:
    """Validate a walkthrough request for the game (tracked or just named) and hand it to the Conversation."""
    backend, spoiler, key, game = _question_target(body, directory)
    target_name = (game.name if game else key).strip()
    if not target_name:
        raise ActionError(400, "Pick or add a game first.")
    return backend, conversation.walkthrough(backend, game or Game(target_name), spoiler)
