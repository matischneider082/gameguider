"""The full walkthrough: chapter parsing, the Walkthrough tab and `game-guide walkthrough`."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from game_guide.notes import chapters, current_chapter

GAMES = """| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Hollow Knight | Switch | metroidvania | Playing | Mantis Lords | 2026-10-23 |
"""
WALK = """# Hollow Knight walkthrough

Source: Game8, patch 1.5.

## Forgotten Crossroads

#### Hint
Head down from Dirtmouth.

#### Strategy
Buy the map from Cornifer.

## Fungal Wastes

#### Hint
Find the Mantis Village.

#### Strategy
Learn the Mantis Lords' dash pattern.
- dash dodge: https://youtu.be/aaaaaaaaaaa?t=65

#### Answer
Pogo off the third lord.
"""


class Chapters(unittest.TestCase):
    def test_splits_on_level_two_headings_and_finds_the_spot(self):
        found = chapters(WALK)
        self.assertEqual([t for t, _ in found], ["Forgotten Crossroads", "Fungal Wastes"])
        self.assertTrue(found[1][1].startswith("## Fungal Wastes\n"))
        self.assertIn("Pogo off", found[1][1])
        self.assertNotIn("Source: Game8", found[0][1])  # the preamble belongs to no chapter
        self.assertEqual(current_chapter(found, "mantis lords"), 1)
        self.assertIsNone(current_chapter(found, "?"))
        self.assertIsNone(current_chapter(found, "Radiance"))


    def test_code_fences_and_heading_variants(self):
        text = "## Act 1 ##\n```\n## not a chapter\n```\n   ##\tAct 2: Greenpath\r\nbody\r\n"
        self.assertEqual([t for t, _ in chapters(text)], ["Act 1", "Act 2: Greenpath"])

    def test_a_title_beats_an_earlier_chapter_that_mentions_the_spot(self):
        found = chapters("## Act 1\n#### Answer\nThe path to Greenpath opens.\n## Act 2: Greenpath\n")
        self.assertEqual(current_chapter(found, "Greenpath"), 1)


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": self.dir.name})
        self.env.start()
        Path(self.dir.name, "GAMES.md").write_text(GAMES)
        Path(self.dir.name, "hollow-knight.walkthrough.md").write_text(WALK)

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()


class Cli(Folder):
    def run_cli(self, *argv):
        from game_guide.cli import main
        out = io.StringIO()
        with redirect_stdout(out):
            main(["walkthrough", *argv])
        return out.getvalue()

    def test_lists_chapters_and_redacts_one(self):
        self.assertEqual(self.run_cli("hollow knight"), "  1. Forgotten Crossroads\n  2. Fungal Wastes\n")
        out = self.run_cli("Hollow Knight", "2")
        self.assertIn("Find the Mantis Village.", out)
        self.assertNotIn("dash pattern", out)
        self.assertIn("Pogo off", self.run_cli("Hollow Knight", "2", "--spoiler", "answer"))

    def test_reads_a_walkthrough_whose_game_left_games_md(self):
        Path(self.dir.name, "celeste.walkthrough.md").write_text("## Forsaken City\n")
        self.assertEqual(self.run_cli("Celeste"), "  1. Forsaken City\n")

    def test_missing_walkthrough_says_how_to_build_one(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cli("Hades")
        self.assertIn('full walkthrough for Hades', str(e.exception.code))
        with self.assertRaises(SystemExit):
            self.run_cli("Hollow Knight", "9")


class Tab(Folder, unittest.IsolatedAsyncioTestCase):
    async def test_opens_at_the_current_chapter_redacted_and_feeds_media(self):
        from game_guide.app import GameGuide
        app = GameGuide()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view = app.query_one("#chapters")
            self.assertEqual(view.highlighted, 1)  # the chapter naming the spot
            self.assertTrue(str(view.get_option_at_index(1).prompt).startswith("●"))
            shown = app.query_one("#chapter")._markdown
            self.assertIn("Mantis Village", shown)
            self.assertNotIn("dash pattern", shown)
            self.assertEqual([m.id for m in app.media], [])  # strategy-level video is hidden at hint
            app.query_one("#spoiler").value = "strategy"
            await pilot.pause()
            self.assertIn("dash pattern", app.query_one("#chapter")._markdown)
            self.assertEqual([m.id for m in app.media], ["aaaaaaaaaaa"])
            app.query_one("#main").active = "walk-tab"
            await pilot.pause()
            view.highlighted = 0
            await pilot.pause()
            self.assertIn("Dirtmouth", app.query_one("#chapter")._markdown)
            await pilot.press("enter")
            self.assertEqual(app.query_one("#question").value, "Help with Forgotten Crossroads: ")

    async def test_a_late_highlight_from_a_longer_list_is_ignored(self):
        from textual.widgets import OptionList
        from game_guide.app import GameGuide
        app = GameGuide()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view = app.query_one("#chapters", OptionList)
            stale = OptionList.OptionHighlighted(view, view.get_option_at_index(1), 1)
            app.chapters = []  # the list was rebuilt for a game with no walkthrough before the event arrived
            app.on_option_list_option_highlighted(stale)  # used to raise IndexError and kill the app

    async def test_notes_without_a_final_newline_keep_their_video_timestamp(self):
        Path(self.dir.name, "hollow-knight.md").write_text("- dodge: https://youtu.be/bbbbbbbbbbb?t=95")
        from game_guide.app import GameGuide
        app = GameGuide()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual([(m.id, m.at, m.label) for m in app.media], [("bbbbbbbbbbb", 95, "dodge")])

    async def test_w_asks_for_the_full_walkthrough(self):
        from game_guide.app import GameGuide
        from game_guide.backends import Event
        questions = []

        def fake_ask(backend, question, session=None):
            questions.append(question)
            yield Event("done", "built")

        app = GameGuide()
        with mock.patch("game_guide.conversation.ask", fake_ask), \
                mock.patch("game_guide.conversation.fetch_game_artwork"), \
                mock.patch("game_guide.conversation.ensure_walkthrough_artwork"):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.query_one("#main").active = "walk-tab"
                await pilot.pause()
                await pilot.press("w")
                await app.workers.wait_for_complete()
        self.assertIn("full walkthrough for Hollow Knight, with all three spoiler levels", questions[0])


if __name__ == "__main__":
    unittest.main()
