"""Settings that outlive a run, in $XDG_CONFIG_HOME/game-guide/config.json (~/.config/... by default): today the
notes folder, so the desktop app (which gets no shell environment) and the CLI agree on where the notes live, and
the color theme."""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_NOTES_DIR = "~/Documents/GameGuider"


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "game-guide" / "config.json"


def load() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def notes_dir() -> Path:
    """$GAME_GUIDE_DIR, else the saved setting, else ~/Documents/GameGuider."""
    saved = load().get("notes_dir")
    return Path(os.environ.get("GAME_GUIDE_DIR") or (saved if isinstance(saved, str) and saved else DEFAULT_NOTES_DIR)).expanduser()


def set_notes_dir(path: str) -> Path:
    """Save the notes folder, creating it. The AI CLI may write anywhere in it, so it can't hold your home folder."""
    folder = Path(str(path or "").strip()).expanduser()
    if not folder.is_absolute():
        raise ValueError("Choose a full folder path, such as ~/Documents/GameGuider.")
    folder = folder.resolve()
    if folder == Path(folder.anchor) or Path.home().resolve().is_relative_to(folder):
        raise ValueError("Choose a folder inside your home folder, not your home folder itself or one above it.")
    folder.mkdir(parents=True, exist_ok=True)
    save("notes_dir", str(folder))
    return folder


def save(key: str, value) -> None:
    """Set one setting, keeping the others; the file is replaced whole so a reader never sees half of it."""
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_text(json.dumps({**load(), key: value}, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)
