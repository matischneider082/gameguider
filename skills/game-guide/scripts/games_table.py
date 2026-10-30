#!/usr/bin/env python3
"""Print GAMES.md for the skill to load. The folder is resolved here, because
Claude Code refuses shell variables in a skill's !`command`."""
import json
import os
from pathlib import Path


def notes_folder():
    """$GAME_GUIDE_DIR, else the folder saved in game-guide's config.json, else ~/Documents/GameGuider."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "game-guide" / "config.json"
    try:
        saved = json.loads(config.read_text(encoding="utf-8")).get("notes_dir")
    except (OSError, ValueError, AttributeError):
        saved = None
    return Path(os.environ.get("GAME_GUIDE_DIR") or (saved if isinstance(saved, str) and saved else "~/Documents/GameGuider")).expanduser().resolve()


folder = notes_folder()
index = folder / "GAMES.md"
print(f"Notes folder: {folder}\n")
print(index.read_text(encoding="utf-8") if index.exists() else "No games tracked yet.")
