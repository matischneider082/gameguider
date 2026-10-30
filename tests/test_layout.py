import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

GAMES = """| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Hollow Knight | Switch | metroidvania | Playing | Mantis Lords | 2026-10-23 |
| Fire Emblem: Fortune's Weave | Switch 2 | strategy | Playing | Chapter 5 | 2026-10-24 |
"""
LONG_URL = "https://ag.hyperxgaming.com/article/2552/" + "hollow-knight-how-to-beat-the-mantis-lords-" * 4


class Layout(unittest.IsolatedAsyncioTestCase):
    async def test_notes_fit_the_window_and_ctrl_t_cycles_tabs(self):
        from game_guide.app import GameGuide
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            Path(d, "GAMES.md").write_text(GAMES)
            Path(d, "hollow-knight.md").write_text(f"## Mantis Lords\n\n#### Hint\nStand in the middle.\n- HyperX guide: {LONG_URL}\n")
            app = GameGuide()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                for _ in range(50):  # a slow runner (Windows CI) lays the list out some pauses later
                    if app.query("#games ListItem")[1].region.height:
                        break
                    await pilot.pause(0.1)
                main = app.query_one("#main")
                # A long unbreakable URL used to widen the right pane past the window, cutting every line off.
                self.assertLessEqual(main.region.right, 120)
                self.assertGreater(main.region.width, 60)
                long_name = app.query("#games ListItem")[1]
                self.assertGreaterEqual(long_name.region.height, 2)  # wrapped, not cut off at "Fortune's Weav"
                tabs = []
                for _ in range(4):
                    await pilot.press("ctrl+t")
                    tabs.append(main.active)
                self.assertEqual(tabs, ["walk-tab", "answer-tab", "media-tab", "notes-tab"])


if __name__ == "__main__":
    unittest.main()
