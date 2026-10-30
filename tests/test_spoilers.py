"""The Notes tab hides spoiler levels above the selected one."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from game_guide.notes import redact, videos

FULL = """# Persona 5

**Character:** Joker, level 12

## Palace 1

#### Hint
Look for the vent.
- hint video: https://youtu.be/aaaaaaaaaaa?t=5

#### Strategy
Use Ryuji's skills.
- strategy video: https://youtu.be/bbbbbbbbbbb?t=5

##### Detail
nested strategy detail

#### Answer
Exploit weakness.
- answer video: https://youtu.be/ccccccccccc?t=5

### Sources
Kept at every level.
"""

GAMES = """# Games

| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Persona 5 | PS5 | jrpg | Playing | Palace 1 | 2026-10-23 |
"""


class Redact(unittest.TestCase):
    def test_hint_hides_strategy_and_answer(self):
        out = redact(FULL, "hint")
        self.assertIn("Look for the vent.", out)
        self.assertNotIn("Ryuji", out)
        self.assertNotIn("nested strategy detail", out)
        self.assertNotIn("Exploit", out)
        self.assertIn("#### Strategy\n\n_Strategy hidden — raise the spoiler level to see it._\n", out)
        self.assertIn("#### Answer\n\n_Answer hidden — raise the spoiler level to see it._\n", out)
        self.assertIn("**Character:** Joker", out)
        self.assertIn("### Sources\nKept at every level.", out)  # a heading of level <= 4 ends the hidden body

    def test_strategy_shows_hint_and_strategy(self):
        out = redact(FULL, "strategy")
        self.assertIn("Look for the vent.", out)
        self.assertIn("nested strategy detail", out)
        self.assertNotIn("Exploit", out)
        self.assertIn("_Answer hidden", out)
        self.assertNotIn("_Strategy hidden", out)

    def test_answer_changes_nothing(self):
        self.assertEqual(redact(FULL, "answer"), FULL)

    def test_only_hint_researched(self):
        notes = "## Spot\n\n#### Hint\nVent.\n"
        self.assertEqual(redact(notes, "hint"), notes)
        self.assertEqual(redact(notes, "answer"), notes)

    def test_legacy_notes_are_unchanged(self):
        notes = "# Game\n\n## Spot\nBoss weak to fire.\n\n### Tips\nUse the vent.\n"
        for level in ("hint", "strategy", "answer"):
            self.assertEqual(redact(notes, level), notes)

    def test_two_spots_are_redacted_independently(self):
        notes = ("## One\n\n#### Hint\nh1\n\n#### Answer\na1\n\n"
                 "## Two\n\n#### Hint\nh2\n\n#### Strategy\ns2\n\n#### Answer\na2\n")
        out = redact(notes, "hint")
        self.assertIn("h1", out)
        self.assertIn("h2", out)
        self.assertNotIn("\na1", out)
        self.assertNotIn("\ns2", out)
        self.assertNotIn("\na2", out)
        self.assertIn("## Two", out)
        self.assertEqual(out.count("hidden — raise"), 3)

    def test_media_sources_come_only_from_visible_text(self):
        self.assertEqual([v.id for v in videos(redact(FULL, "hint"))], ["aaaaaaaaaaa"])
        self.assertEqual([v.id for v in videos(redact(FULL, "strategy"))], ["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertEqual(len(videos(redact(FULL, "answer"))), 3)


class Tui(unittest.IsolatedAsyncioTestCase):
    async def test_changing_the_spoiler_rerenders_notes_and_media(self):
        from textual.widgets import Select

        from game_guide.app import GameGuide
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            Path(d, "GAMES.md").write_text(GAMES)
            Path(d, "persona-5.md").write_text(FULL)
            app = GameGuide()
            async with app.run_test() as pilot:
                await pilot.pause()
                notes = app.query_one("#notes")
                self.assertNotIn("Exploit", notes._markdown)
                self.assertEqual(len(app.media), 1)
                app.query_one("#spoiler", Select).value = "answer"
                await pilot.pause()
                self.assertIn("Exploit", notes._markdown)
                self.assertEqual(len(app.media), 3)
                app.query_one("#spoiler", Select).value = "hint"
                await pilot.pause()
                self.assertNotIn("Exploit", notes._markdown)
                self.assertEqual(len(app.media), 1)


if __name__ == "__main__":
    unittest.main()
