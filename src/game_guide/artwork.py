"""The artwork module: finds a game's cover art in images/<slug>/, fetches it from Wikipedia or Steam when missing,
and embeds it in the walkthrough preamble. Built on the notes module's read side and the library's atomic write."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from .library import write_atomic
from .notes import IMAGE, IMAGE_EXTS, Game, games_dir, images, read_walkthrough, slugify, walkthrough_path

COVER_STEMS = ("cover", "artwork", "boxart", "keyart", "poster", "banner", "hero")
ARTWORK_LIMIT = 10 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def game_artwork_path(game: Game | str, directory=None) -> Path | None:
    """Return the absolute path to the game's cover/hero artwork in images/<slug>/, or None."""
    base = games_dir(directory).resolve()
    slug = game.slug if isinstance(game, Game) else slugify(str(game))
    if not slug:
        return None
    images_root = (base / "images").resolve()
    game_img_dir = (images_root / slug).resolve()
    if not game_img_dir.is_relative_to(images_root):
        return None

    # 1. Explicit cover/artwork/boxart/keyart/poster/banner/hero file in images/<slug>/
    if game_img_dir.is_dir():
        for stem in COVER_STEMS:
            for ext in IMAGE_EXTS:
                candidate = game_img_dir / f"{stem}{ext}"
                if candidate.is_file():
                    return candidate
        # Also match files starting with cover- or artwork- or boxart-
        for p in sorted(game_img_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                lower_stem = p.stem.lower()
                if any(lower_stem.startswith(f"{s}-") or lower_stem.endswith(f"-{s}") for s in COVER_STEMS):
                    return p

    # 2. Image embedded in the walkthrough preamble (before the first `## ` chapter heading)
    walk_text = read_walkthrough(Game(slug), base)
    if walk_text:
        preamble = walk_text.split("\n## ", 1)[0]
        for _, abs_path in images(preamble, base):
            if abs_path.is_file() and abs_path.is_relative_to(images_root):
                return abs_path

    # 3. Fallback to the first saved image in the walkthrough or images/<slug>/ directory
    if walk_text:
        for _, abs_path in images(walk_text, base):
            if abs_path.is_file() and abs_path.is_relative_to(images_root):
                return abs_path
    if game_img_dir.is_dir():
        for p in sorted(game_img_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                return p
    return None


def _download_https_image(url: str, dest: Path, images_root: Path) -> Path | None:
    if not url or not url.startswith("https://"):
        return None
    target = dest.resolve()
    if target == images_root or not target.is_relative_to(images_root):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if not resp.geturl().startswith("https://"):
                return None
            ctype = (resp.headers.get_content_type() or "").lower()
            if not ctype.startswith("image/"):
                return None
            data = resp.read(ARTWORK_LIMIT + 1)
        if not data or len(data) > ARTWORK_LIMIT:
            return None
        ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(ctype)
        if ext is None:
            return None
        final_target = target.with_suffix(ext)
        final_target.parent.mkdir(parents=True, exist_ok=True)
        final_target.write_bytes(data)
        if final_target.stem.lower() in COVER_STEMS:
            for old_ext in IMAGE_EXTS:
                stale = final_target.with_suffix(old_ext)
                if stale != final_target and stale.is_file():
                    stale.unlink()
        return final_target
    except Exception:
        return None


def fetch_game_artwork(game: Game | str, directory=None, force: bool = False) -> Path | None:
    """Ensure a dedicated cover artwork file (`images/<slug>/cover.<ext>`) exists for `game`.

    Checks existing cover files first (unless `force=True`), then searches Wikipedia (og:image / pageimages)
    and Steam Store Search API for official cover/key art, downloading it safely over HTTPS.
    Also embeds the cover image into the `<slug>.walkthrough.md` preamble if a walkthrough exists and lacks one.
    """
    base = games_dir(directory).resolve()
    game_obj = game if isinstance(game, Game) else Game(str(game))
    slug = game_obj.slug
    if not slug:
        return None
    images_root = (base / "images").resolve()
    game_img_dir = (images_root / slug).resolve()

    if not force and game_img_dir.is_dir():
        for stem in COVER_STEMS:
            for ext in IMAGE_EXTS:
                cand = game_img_dir / f"{stem}{ext}"
                if cand.is_file():
                    _ensure_walkthrough_preamble_artwork(game_obj, cand, base)
                    return cand

    candidate_urls: list[str] = []

    # 1. Wikipedia direct page & search og:image (high-res official box art for Switch/PS5/PC games)
    wiki_titles = [game_obj.name, f"{game_obj.name} (video game)"]
    try:
        search_q = urllib.parse.quote(f"{game_obj.name} video game")
        search_api = (
            f"https://en.wikipedia.org/w/api.php?action=query&list=search"
            f"&srsearch={search_q}&srlimit=2&format=json"
        )
        req = urllib.request.Request(search_api, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            sdata = json.loads(resp.read().decode("utf-8", errors="replace"))
            for hit in (sdata.get("query") or {}).get("search") or []:
                t = hit.get("title")
                if t and t not in wiki_titles:
                    wiki_titles.append(t)
    except Exception:
        pass

    og_re = re.compile(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https://[^"\']+)["\']'
        r'|<meta[^>]+content=["\'](https://[^"\']+)["\'][^>]+property=["\']og:image["\']',
        re.IGNORECASE,
    )
    for wtitle in wiki_titles[:3]:
        try:
            wurl = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(wtitle.replace(' ', '_'))}"
            req = urllib.request.Request(wurl, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read(250_000).decode("utf-8", errors="replace")
            m = og_re.search(html)
            if m:
                img_url = (m.group(1) or m.group(2) or "").replace("&amp;", "&").split("?")[0]
                if img_url.startswith("https://") and "wikipedia_en" not in img_url.lower():
                    candidate_urls.append(img_url)
                    break
        except Exception:
            continue

    # 2. Steam Store search API (600x900 library cover & header capsule)
    try:
        steam_url = (
            f"https://store.steampowered.com/api/storesearch/"
            f"?term={urllib.parse.quote(game_obj.name)}&l=english&cc=US"
        )
        req = urllib.request.Request(steam_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            sdata = json.loads(resp.read().decode("utf-8", errors="replace"))
        items = sdata.get("items") or []
        if items:
            appid = items[0].get("id")
            if appid:
                candidate_urls.append(f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_600x900.jpg")
                candidate_urls.append(f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg")
            tiny = items[0].get("tiny_image") or ""
            if tiny.startswith("https://"):
                candidate_urls.append(tiny)
    except Exception:
        pass

    for url in candidate_urls:
        saved = _download_https_image(url, game_img_dir / "cover.jpg", images_root)
        if saved is not None:
            _ensure_walkthrough_preamble_artwork(game_obj, saved, base)
            return saved

    return ensure_walkthrough_artwork(game_obj, base)


def ensure_walkthrough_artwork(game: Game | str, directory=None) -> Path | None:
    """Embed existing game artwork into the walkthrough preamble without any network access."""
    base = games_dir(directory).resolve()
    game_obj = game if isinstance(game, Game) else Game(str(game))
    artwork = game_artwork_path(game_obj, base)
    if artwork is not None:
        _ensure_walkthrough_preamble_artwork(game_obj, artwork, base)
    return artwork


def _ensure_walkthrough_preamble_artwork(game_obj: Game, cover_path: Path, base_dir: Path) -> None:
    """If `<slug>.walkthrough.md` exists and has no image in its preamble, insert `![<Game> Cover Artwork](images/<slug>/<file>)`."""
    wpath = walkthrough_path(game_obj, base_dir)
    if not wpath.is_file():
        return
    try:
        text = wpath.read_text(encoding="utf-8")
        parts = text.split("\n## ", 1)
        preamble = parts[0]
        if IMAGE.search(preamble):
            return
        rel = cover_path.resolve().relative_to(base_dir.resolve()).as_posix()
        embed_line = f"![{game_obj.name} Cover Artwork]({rel})"
        lines = preamble.splitlines()
        if lines and lines[0].startswith("# "):
            new_preamble = "\n".join([lines[0], "", embed_line, *lines[1:]]).rstrip()
        else:
            new_preamble = f"{embed_line}\n\n{preamble}".rstrip()
        updated = f"{new_preamble}\n\n## {parts[1]}" if len(parts) > 1 else f"{new_preamble}\n"
        write_atomic(wpath, updated)
    except Exception:
        pass
