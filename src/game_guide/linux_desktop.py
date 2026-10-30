"""The Linux application-menu entry: a .desktop file and icon, so GameGuider opens like an installed app."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .backends import PLUGIN_ROOT

ENTRY_ID = "gameguider.desktop"
ICON_NAME = "gameguider.png"

ENTRY = """\
[Desktop Entry]
Type=Application
Name=GameGuider
Comment=A game shelf and spoiler-aware walkthrough companion
Exec="{command}" web
Icon=gameguider
Terminal=false
Categories=Game;Utility;
Keywords=game;walkthrough;guide;
"""


def install(data_dir=None, bin_dir=None, icon=None) -> Path:
    """Write the menu entry and its icon under `data_dir` (XDG_DATA_HOME by default); return the entry path.

    `bin_dir` is the folder holding the `game-guide` command (the installer passes uv's tool bin); without it the
    command is looked up on PATH.
    """
    command = _command(bin_dir)
    data = Path(data_dir or os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()
    icon_src = Path(icon) if icon else PLUGIN_ROOT / "build" / "icon.png"
    icon_dst = data / "icons" / "hicolor" / "512x512" / "apps" / ICON_NAME
    icon_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(icon_src, icon_dst)
    entry = data / "applications" / ENTRY_ID
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(ENTRY.format(command=command), encoding="utf-8")
    return entry


def _command(bin_dir=None) -> Path:
    if bin_dir:
        command = Path(bin_dir) / "game-guide"
        if not command.is_file():
            raise SystemExit(f"There is no game-guide command in {bin_dir}; run the installer again, or pass --bin with its folder.")
        return command
    return _on_path()


def _on_path() -> Path:
    found = shutil.which("game-guide")
    if not found:
        raise SystemExit("game-guide isn't on PATH; run the installer again, or pass --bin with its folder.")
    return Path(found)
