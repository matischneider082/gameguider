"""Caches on the request path must follow the files. An editor can save notes out from under a running server."""

import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from game_guide import artwork, library
from game_guide.actions import search, state_payload
from game_guide.notes import Game, chapters, read_notes
from game_guide.render import Policy, render
from game_guide.view import game_view, progress

GAMES = """# Games

| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Hollow Knight | Switch | metroidvania | Playing | Forgotten Crossroads | 2026-10-30 |
"""
NOTES = "# Hollow Knight\n\n## Spot\n\n#### Hint\nVent.\n"
WALK = """# Hollow Knight — Walkthrough

## Forgotten Crossroads

#### Hint
Left.

![Map](images/hollow-knight/map.png)

## Greenpath

#### Answer
Dash.
"""


class CacheInvalidationTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "images" / "hollow-knight").mkdir(parents=True)
        (self.dir / "GAMES.md").write_text(GAMES, encoding="utf-8")
        (self.dir / "hollow-knight.md").write_text(NOTES, encoding="utf-8")
        (self.dir / "hollow-knight.walkthrough.md").write_text(WALK, encoding="utf-8")
        self.game = Game("Hollow Knight", status="Playing", spot="Forgotten Crossroads")

    def test_a_same_size_edit_replaces_cached_notes_and_the_shelf(self):
        self.assertEqual(read_notes(self.game, self.dir), NOTES)
        self.assertEqual(library.read_games(self.dir)[0].status, "Playing")
        (self.dir / "hollow-knight.md").write_text(NOTES.replace("Vent.", "Leap."), encoding="utf-8")
        (self.dir / "GAMES.md").write_text(GAMES.replace("| Playing |", "| Dropped |"), encoding="utf-8")
        self.assertIn("Leap.", read_notes(self.game, self.dir))
        self.assertNotIn("Vent.", read_notes(self.game, self.dir))
        games = library.read_games(self.dir)
        self.assertEqual(games[0].status, "Dropped")
        games[0].status = "Hacked"
        games.append(Game("Extra"))
        self.assertEqual([g.status for g in library.read_games(self.dir)], ["Dropped"])

    def test_walkthrough_and_spot_edits_move_the_cached_view(self):
        self.assertEqual(game_view(self.game, "hint", self.dir).current, 0)
        self.assertEqual(len(game_view(self.game, "answer", self.dir).chapters), 2)
        self.assertNotIn("Dash", game_view(self.game, "hint", self.dir).chapters[1].body)
        self.assertIn("Dash", game_view(self.game, "answer", self.dir).chapters[1].body)
        later = Game("Hollow Knight", status="Playing", spot="Greenpath")
        self.assertEqual(game_view(later, "hint", self.dir).current, 1)
        path = self.dir / "hollow-knight.walkthrough.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n## Crystal Peak\n\n#### Hint\nUp.\n", encoding="utf-8")
        titles = [c.title for c in game_view(self.game, "hint", self.dir).chapters]
        self.assertEqual(titles, ["Forgotten Crossroads", "Greenpath", "Crystal Peak"])
        self.assertEqual(progress(self.game, self.dir).chapters, 3)
        found = search({"q": "vent", "spoiler": "hint"}, self.dir)
        self.assertTrue(any(hit["slug"] == "hollow-knight" for hit in found["results"]))
        (self.dir / "hollow-knight.md").write_text(NOTES.replace("Vent.", "Leap."), encoding="utf-8")
        self.assertFalse(any("Vent" in hit["snippet"] for hit in search({"q": "vent", "spoiler": "hint"}, self.dir)["results"]))
        self.assertTrue(search({"q": "leap", "spoiler": "hint"}, self.dir)["results"])

    def test_rendering_follows_an_image_appearing_and_disappearing(self):
        text = "![Map](images/hollow-knight/map.png)\n"
        wiki = "![[images/hollow-knight/map.png|Map]]\n"
        self.assertIn("[Image unavailable: Map]", render(text, self.dir))
        self.assertIn("[Image unavailable: Map]", render(wiki, self.dir))
        image = self.dir / "images" / "hollow-knight" / "map.png"
        image.write_bytes(b"PNG")
        self.assertIn("/images/hollow-knight/map.png", render(text, self.dir))
        self.assertIn("/images/hollow-knight/map.png", render(wiki, self.dir))
        page = state_payload("Hollow Knight", "hint", self.dir)
        self.assertTrue(any(m["src"].endswith("map.png") for m in page["maps"]))
        self.assertIn("/images/hollow-knight/map.png", page["walkthrough"][0]["html"])
        shared = render(text, self.dir, Policy(share=True))
        image.write_bytes(b"PNG!")
        self.assertNotEqual(shared, render(text, self.dir, Policy(share=True)))
        image.unlink()
        self.assertIn("[Image unavailable: Map]", render(text, self.dir))
        self.assertEqual(state_payload("Hollow Knight", "hint", self.dir)["maps"], [])

    def test_cover_art_lookup_follows_the_images_directory(self):
        self.assertIsNone(artwork.game_artwork_path(self.game, self.dir))
        cover = self.dir / "images" / "hollow-knight" / "cover.png"
        cover.write_bytes(b"PNG")
        found = artwork.game_artwork_path(self.game, self.dir)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "cover.png")
        found.unlink()
        self.assertIsNone(artwork.game_artwork_path(self.game, self.dir))

    def test_deleting_the_notes_file_drops_the_cached_text(self):
        self.assertIn("Vent.", read_notes(self.game, self.dir))
        (self.dir / "hollow-knight.md").unlink()
        self.assertEqual(read_notes(self.game, self.dir), "")
        self.assertNotIn("Vent.", game_view(self.game, "hint", self.dir).notes)

    def test_chapter_parse_cache_does_not_share_its_list(self):
        found = chapters("## A\nbody\n")
        found.append(("B", "## B\n"))
        self.assertEqual([title for title, _ in chapters("## A\nbody\n")], ["A"])

    def test_parallel_renders_of_one_note_agree(self):
        (self.dir / "images" / "hollow-knight" / "map.png").write_bytes(b"PNG")
        text = "Hello.\n\n![Map](images/hollow-knight/map.png)\n"
        barrier = threading.Barrier(8)
        out = []

        def work():
            barrier.wait()
            out.append(render(text, self.dir))

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(out), 8)
        self.assertEqual(len(set(out)), 1)
        self.assertIn("/images/hollow-knight/map.png", out[0])

    def test_a_truncated_save_stays_readable_and_does_not_outlive_the_finished_file(self):
        notes = self.dir / "hollow-knight.md"
        notes.write_bytes(b"# Hollow Knight\n\n## Spot\nCaf\xc3")
        self.assertIn("Spot", read_notes(self.game, self.dir))
        games = self.dir / "GAMES.md"
        games.write_bytes(games.read_bytes() + b"| Caf\xc3")
        self.assertEqual(library.read_games(self.dir)[0].name, "Hollow Knight")
        notes.write_text(NOTES.replace("Vent.", "Leap."), encoding="utf-8")
        games.write_text(GAMES.replace("| Playing |", "| Dropped |"), encoding="utf-8")
        fresh = read_notes(self.game, self.dir)
        self.assertIn("Leap.", fresh)
        self.assertNotIn("Caf", fresh)
        self.assertEqual(library.read_games(self.dir)[0].status, "Dropped")

    @unittest.skipUnless(os.name == "posix", "chmod 0 does not make a file unreadable on Windows")
    def test_an_unreadable_file_is_empty_and_not_cached_under_its_stamp(self):
        path = self.dir / "hollow-knight.md"
        self.assertIn("Vent.", read_notes(self.game, self.dir))
        path.chmod(0)
        try:
            self.assertEqual(read_notes(self.game, self.dir), "")
            path.chmod(0o644)
            self.assertIn("Vent.", read_notes(self.game, self.dir))
        finally:
            path.chmod(0o644)

    def test_text_read_across_a_save_is_not_remembered_as_the_new_file(self):
        from game_guide import cache

        path = self.dir / "hollow-knight.md"
        real = Path.read_text

        def racing(self, *args, **kwargs):
            if self == path:
                self.write_text(NOTES.replace("Vent.", "Leap."), encoding="utf-8")
                return NOTES
            return real(self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", racing):
            text, stamp = cache.load(path)
        self.assertIsNone(stamp)
        self.assertIn("Vent.", text)
        finished = cache.read_text(path)
        self.assertIn("Leap.", finished)
        self.assertNotIn("Vent.", finished)

    def test_a_walkthrough_finished_after_a_partial_read_stays_redacted_at_hint(self):
        path = self.dir / "hollow-knight.walkthrough.md"
        path.write_bytes(b"## Forgotten Crossroads\n\n#### Answer\nDash.\n\xc3")
        self.assertIn("Dash", game_view(self.game, "answer", self.dir).chapters[0].body)
        self.assertNotIn("Dash", game_view(self.game, "hint", self.dir).chapters[0].body)
        path.write_text(WALK, encoding="utf-8")
        hint = game_view(self.game, "hint", self.dir)
        self.assertEqual([chapter.title for chapter in hint.chapters], ["Forgotten Crossroads", "Greenpath"])
        self.assertIn("Left.", hint.chapters[0].body)
        self.assertNotIn("Dash", hint.chapters[1].body)
        self.assertIn("Dash", game_view(self.game, "answer", self.dir).chapters[1].body)
