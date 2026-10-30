"""The Game View interface: only what the player may see of a game at a spoiler level, read from a notes folder."""

import tempfile
import unittest
from pathlib import Path

from game_guide.notes import Game, Video
from game_guide.view import MapImage, Progress, game_view, progress, search

NOTES = """# Hollow Knight

## Current
#### Hint
Look near the bench.
![Village map](images/hk/village-map.png)
- Bench: https://youtu.be/aaaaaaaaaaa?t=5

#### Answer
Pogo off the third sister.
![Arena map](images/hk/arena-map.png)
- Arena: https://youtu.be/bbbbbbbbbbb
"""

WALKTHROUGH = """# Hollow Knight Walkthrough

![Cover](images/hk/cover.png)

#### Answer
Preamble spoiler.

## 1. Forgotten Crossroads

#### Hint
Head down from Dirtmouth.
![Crossroads map](images/hk/crossroads-map.png)

## 2. Fungal Wastes

#### Hint
Follow the bouncy mushrooms.

#### Strategy
- Claw route: https://youtu.be/ccccccccccc?t=90
![Claw](images/hk/claw.png)

#### Answer
Defeat the Mantis Lords.
![Secret map](images/hk/secret-map.png)
"""


class Folder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.images = self.base / "images" / "hk"
        self.images.mkdir(parents=True)
        for name in ("village-map.png", "arena-map.png", "cover.png", "crossroads-map.png", "claw.png", "secret-map.png"):
            (self.images / name).write_bytes(b"png")
        (self.base / "hollow-knight.md").write_text(NOTES, encoding="utf-8")
        (self.base / "hollow-knight.walkthrough.md").write_text(WALKTHROUGH, encoding="utf-8")
        self.game = Game("Hollow Knight", status="Playing", spot="Mantis Lords")

    def image(self, name):
        return (self.images / name).resolve()


class GameViewTests(Folder):
    def test_a_spot_named_only_in_a_hidden_answer_still_marks_its_chapter_current(self):
        view = game_view(self.game, "hint", self.base)
        self.assertEqual(view.current, 1)
        self.assertEqual([c.current for c in view.chapters], [False, True])
        self.assertNotIn("Mantis Lords", view.chapters[1].body)

    def test_maps_and_media_only_hold_images_from_visible_levels(self):
        hint = game_view(self.game, "hint", self.base)
        self.assertEqual(hint.maps, (
            MapImage("Village map", self.image("village-map.png"), "Notes", None),
            MapImage("Crossroads map", self.image("crossroads-map.png"), "1. Forgotten Crossroads", 0),
        ))
        hint_images = [item[1] for item in hint.media if not isinstance(item, Video)]
        self.assertEqual(hint_images, [self.image("village-map.png"), self.image("crossroads-map.png")])
        answer = game_view(self.game, "answer", self.base)
        self.assertEqual([m.label for m in answer.maps], ["Village map", "Arena map", "Crossroads map", "Secret map"])
        self.assertEqual(answer.maps[-1].chapter_index, 1)
        self.assertIn(self.image("secret-map.png"), [item[1] for item in answer.media if not isinstance(item, Video)])
        self.assertEqual(answer.chapters[1].images, (("Claw", self.image("claw.png")), ("Secret map", self.image("secret-map.png"))))

    def test_videos_in_hidden_levels_are_left_out_of_chapters_and_media(self):
        def ids(items):
            return [v.id for v in items if isinstance(v, Video)]
        hint = game_view(self.game, "hint", self.base)
        self.assertEqual(hint.chapters[1].videos, ())
        self.assertEqual(ids(hint.media), ["aaaaaaaaaaa"])
        strategy = game_view(self.game, "strategy", self.base)
        self.assertEqual(strategy.chapters[1].videos, (Video("ccccccccccc", 90, "Claw route"),))
        self.assertEqual(ids(strategy.media), ["aaaaaaaaaaa", "ccccccccccc"])
        answer = game_view(self.game, "answer", self.base)
        self.assertEqual(ids(answer.media), ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
        first_image = next(i for i, item in enumerate(answer.media) if not isinstance(item, Video))
        self.assertTrue(all(not isinstance(item, Video) for item in answer.media[first_image:]))  # videos, then images

    def test_notes_lose_their_game_name_heading_and_the_preamble_is_redacted(self):
        view = game_view(self.game, "bogus", self.base)
        self.assertEqual(view.spoiler, "hint")
        self.assertTrue(view.notes.startswith("## Current\n"), view.notes)
        self.assertIn("![Cover](images/hk/cover.png)", view.preamble)
        self.assertNotIn("Preamble spoiler", view.preamble)
        self.assertNotIn("## 1.", view.preamble)
        self.assertIn("Preamble spoiler", game_view(self.game, "answer", self.base).preamble)

    def test_a_cut_level_leaves_the_hidden_note_for_the_renderer(self):
        self.assertIn("_Answer hidden — raise the spoiler level to see it._", game_view(self.game, "hint", self.base).notes)


class MapTests(Folder):
    def test_maps_are_labelled_map_images_in_guide_order_without_duplicates_or_covers(self):
        for name in ("battle-map.png", "minimap.png", "screenshot.png", "cover-map.jpg", "mapping-pylon.jpg"):
            (self.images / name).touch()
        (self.base / "hollow-knight.walkthrough.md").write_text("""## The Plains
![Battle of the Plains](images/hk/battle-map.png)
![Battle again](images/hk/battle-map.png)
![[images/hk/minimap.png]]
![Boss screenshot](images/hk/screenshot.png)
![World map cover](images/hk/cover-map.jpg)
![Pylon approach](images/hk/mapping-pylon.jpg)
![Missing map](images/hk/missing-map.png)
""", encoding="utf-8")
        (self.base / "hollow-knight.md").unlink()
        self.assertEqual(game_view(self.game, "answer", self.base).maps, (
            MapImage("Battle of the Plains", self.image("battle-map.png"), "The Plains", 0),
            MapImage("Minimap", self.image("minimap.png"), "The Plains", 0),
        ))

    def test_maps_never_point_outside_the_images_folder(self):
        outside = self.base / "outside-map.png"
        outside.touch()
        (self.images / "link-map.png").symlink_to(outside)
        (self.base / "hollow-knight.walkthrough.md").unlink()
        (self.base / "hollow-knight.md").write_text("""![Map](https://example.com/map.png)
![Map](images/../outside-map.png)
![Map](images/hk/link-map.png)
""", encoding="utf-8")
        self.assertEqual(game_view(self.game, "answer", self.base).maps, ())


class ProgressTests(Folder):
    def test_progress_counts_the_chapter_naming_the_spot(self):
        (self.base / "hollow-knight.walkthrough.md").write_text("## One\n## Two\n## Three\n## Four\n", encoding="utf-8")
        self.assertEqual(progress(Game("Hollow Knight", status="Playing", spot="Two"), self.base), Progress(4, 2, 50))

    def test_a_beaten_game_whose_spot_names_no_chapter_is_finished(self):
        self.assertEqual(progress(Game("Hollow Knight", status="Beaten", spot=""), self.base), Progress(2, 2, 100))

    def test_a_game_without_a_walkthrough_has_no_chapters(self):
        self.assertEqual(progress(Game("Hades", status="Playing", spot="Elysium"), self.base), Progress(0, None, 0))
        self.assertEqual(progress(Game("Hades", status="Beaten"), self.base), Progress(0, None, 100))


class SearchTests(Folder):
    def test_hidden_levels_never_match_or_leak_into_snippets(self):
        self.assertEqual(search("mantis lords", [self.game], "hint", self.base), [])
        hits = search("mantis lords", [self.game], "answer", self.base)
        self.assertEqual([(h.place, h.chapter_index) for h in hits], [("2. Fungal Wastes", 1)])
        self.assertIn("Defeat the Mantis Lords", hits[0].snippet)
        hint_hits = search("mushrooms", [self.game], "hint", self.base)
        self.assertEqual(len(hint_hits), 1)
        self.assertNotIn("Claw route", hint_hits[0].snippet)  # the Strategy level stays hidden at hint

    def test_every_word_must_match_titles_rank_first_and_notes_are_searched(self):
        (self.base / "celeste.md").write_text("# Celeste\n\nThe crossroads of the summit.\n", encoding="utf-8")
        hits = search("CROSSROADS", [Game("Celeste"), self.game], "hint", self.base)
        self.assertEqual([(h.game.name, h.place) for h in hits], [("Hollow Knight", "1. Forgotten Crossroads"), ("Celeste", "Notes")])
        self.assertIsNone(hits[1].chapter_index)
        self.assertEqual(search("crossroads summit", [Game("Celeste"), self.game], "hint", self.base)[0].game.name, "Celeste")
        self.assertEqual(search("   ", [self.game], "hint", self.base), [])

    def test_snippets_are_plain_text_around_the_match(self):
        hit = search("bench", [self.game], "hint", self.base)[0]
        self.assertEqual(hit.place, "Notes")
        self.assertNotIn("](", hit.snippet)
        self.assertNotIn("#", hit.snippet)
        self.assertIn("Look near the bench.", hit.snippet)


if __name__ == "__main__":
    unittest.main()
