"""Build a portable, self-contained view of a game walkthrough."""
from __future__ import annotations

import html
from pathlib import Path

from .notes import Game
from .render import Policy, render
from .view import game_view


def render_share(game: Game, directory: Path, spoiler: str, include_notes: bool = False) -> bytes:
    """Render only selected material; local image paths cannot escape images/."""
    base = directory.resolve()
    view = game_view(game, spoiler, base)
    walkthrough = view.preamble + "".join(c.body for c in view.chapters)
    notes = view.notes if include_notes else ""
    if not walkthrough.strip() and not notes.strip():
        raise ValueError("There is no walkthrough to share yet.")

    policy = Policy(share=True)
    parts = []
    if walkthrough.strip():
        parts.append(render(walkthrough, base, policy))
    if notes.strip():
        parts.append("<section><h2>Personal notes</h2>" + render(notes, base, policy) + "</section>")
    title = html.escape(game.name)
    document = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — GameGuider</title>
<style>
  :root {{ color-scheme: light; font-family: system-ui, sans-serif; color: #26203a; background: #f5f3fa; }}
  body {{ margin: 0; }} main {{ max-width: 76ch; margin: 0 auto; padding: 2rem 1.25rem 5rem; }}
  .brand {{ color: #6940a5; font-weight: 700; }} .notice {{ color: #575067; }}
  h1, h2, h3 {{ line-height: 1.2; }} h2 {{ margin-top: 2.5rem; }}
  p, li {{ line-height: 1.7; }} img {{ max-width: 100%; height: auto; border-radius: 8px; }}
  a {{ color: #633baa; }} pre {{ overflow: auto; padding: 1rem; background: #eae6f2; }}
  table {{ border-collapse: collapse; display: block; overflow-x: auto; }} td, th {{ border: 1px solid #ccc4da; padding: .5rem; }}
</style></head><body><main><div class="brand">GameGuider</div>
<p class="notice">Shared guide · Spoiler level: {html.escape(view.spoiler.title())}</p>
{''.join(parts)}
</main></body></html>'''
    return document.encode("utf-8")
