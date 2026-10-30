#!/usr/bin/env python3
"""Download one image into the notes folder's images/ directory.
Usage: save_image.py <https-url> images/<game-slug>/<file>

The destination is relative to the notes folder (resolved as games_table.py does). Anything that
isn't an image, is over 10 MB, or would land outside images/ is refused, because the URL comes from untrusted pages.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

LIMIT = 10 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def notes_folder():
    """$GAME_GUIDE_DIR, else the folder saved in game-guide's config.json, else ~/Documents/GameGuider."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "game-guide" / "config.json"
    try:
        saved = json.loads(config.read_text(encoding="utf-8")).get("notes_dir")
    except (OSError, ValueError, AttributeError):
        saved = None
    return Path(os.environ.get("GAME_GUIDE_DIR") or (saved if isinstance(saved, str) and saved else "~/Documents/GameGuider")).expanduser().resolve()


def save(url, dest, notes):
    images = (notes / "images").resolve()
    target = (notes / dest).resolve()
    if target == images or not target.is_relative_to(images):
        sys.exit(f"refusing {dest}: the image must go inside {images}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        # main() only lets https in, but urllib follows redirects to http:// and ftp://.
        if url.startswith("https://") and not response.geturl().startswith("https://"):
            sys.exit(f"refusing {url}: it redirected to {response.geturl()}, which isn't https")
        kind = response.headers.get_content_type()
        if not kind.startswith("image/"):
            sys.exit(f"refusing {url}: it is {kind}, not an image")
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        sys.exit(f"refusing {url}: it is larger than 10 MB")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    if not argv[0].startswith("https://"):
        sys.exit(f"refusing {argv[0]}: only https:// URLs are downloaded")
    notes = notes_folder()
    print(f"saved {save(argv[0], argv[1], notes).relative_to(notes)}")


if __name__ == "__main__":
    main(sys.argv[1:])
