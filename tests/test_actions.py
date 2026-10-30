"""The web actions at their interface: called directly against a temp notes folder, with no socket and no env var."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from game_guide import actions, library, media
from game_guide.actions import ActionError

GAMES = """# Games

| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Hollow Knight | Switch | metroidvania | Playing | Mantis Lords | 2026-10-28 |
| Celeste | PC | platformer | Backlog | | 2026-10-20 |
| Hades | PC | roguelike | Playing | | 2026-10-21 |
| Tunic | PC | adventure | Backlog | | 2026-10-22 |
"""


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "GAMES.md").write_text(GAMES, encoding="utf-8")
        (self.dir / "hollow-knight.md").write_text("# Hollow Knight\n\n## Mantis Lords\n\n#### Hint\nParry.\n", encoding="utf-8")
        cache = mock.patch.object(media, "CACHE", self.dir / "cache")
        cache.start()
        self.addCleanup(cache.stop)

    def slugs(self):
        return [g.slug for g in library.read_games(self.dir)]

    def test_spot_notes_and_delete_find_the_game_by_slug_or_name(self):
        payload = actions.set_spot({"game": "Hollow Knight", "spot": "City of Tears", "status": "Paused",
                                    "note": "Bench by the lift."}, self.dir)
        self.assertEqual((payload["selected"]["slug"], payload["selected"]["spot"]), ("hollow-knight", "City of Tears"))
        self.assertEqual(payload["selected"]["status"], "Paused")
        self.assertIn("## City of Tears", payload["raw_notes"])
        self.assertIn("Bench by the lift.", payload["raw_notes"])

        payload = actions.edit_notes({"game": "hollow-knight", "entry": "Got the lantern.", "heading": "Crystal Peak"},
                                     self.dir)
        self.assertIn("## Crystal Peak\n\n#### Hint\nGot the lantern.", payload["raw_notes"])
        payload = actions.edit_notes({"game": "HOLLOW KNIGHT", "notes": "## Fresh\n\nStart over."}, self.dir)
        self.assertEqual(payload["raw_notes"], "## Fresh\n\nStart over.\n")
        self.assertEqual(self.slugs(), ["hollow-knight", "celeste", "hades", "tunic"])  # no duplicate rows

        payload = actions.delete({"game": "Celeste"}, self.dir)
        actions.delete({"game": "hollow-knight"}, self.dir)
        self.assertEqual(self.slugs(), ["hades", "tunic"])
        self.assertTrue((self.dir / "hollow-knight.md").is_file())
        self.assertEqual(payload["selected"]["slug"], "hollow-knight")  # first remaining game at the time

    def test_refused_input_raises_action_error_with_status_and_message_and_changes_nothing(self):
        before = (self.dir / "GAMES.md").read_text(encoding="utf-8")
        cases = [
            (actions.set_spot, {"spot": "Somewhere"}, 400, "Game is required."),
            (actions.edit_notes, {"game": "  ", "entry": "x"}, 400, "Game is required."),
            (actions.delete, {}, 400, "Game is required."),
            (actions.edit_notes, {"game": "hollow-knight", "entry": "  "}, 400, "Note content is required."),
            (actions.save, {"name": " "}, 400, "Game name is required."),
            (actions.reorder, {"ordered_slugs": ["celeste"], "status": "Finished"}, 400, "Unknown status: Finished"),
            (actions.reorder, {"ordered_slugs": "celeste"}, 400, "Provide a non-empty ordered_slugs list."),
            (actions.reorder, {"ordered_slugs": [1, 2]}, 400, "Provide a non-empty ordered_slugs list."),
            (actions.reorder, {"ordered_slugs": []}, 400, "Provide a non-empty ordered_slugs list."),
            (actions.add_backlog, {"games": 5}, 400, "Provide backlog games as text or a list of titles."),
            (actions.add_backlog, {"lines": ["Tunic", 3]}, 400, "Provide backlog games as text or a list of titles."),
            (actions.add_backlog, {"games": "\n# only a comment\n"}, 400, "Add at least one game title."),
            (actions.refresh_artwork, {"game": "Okami"}, 404, "Game not found."),
            (actions.refresh_score, {"game": ""}, 404, "Game not found."),
            (actions.share, {"game": "hollow-knight", "spoiler": "everything"}, 400, "Invalid spoiler level."),
            (actions.share, {"game": "Okami"}, 404, "Game not found."),
        ]
        for action, body, status, message in cases:
            with self.subTest(action=action.__name__, body=body), self.assertRaises(ActionError) as caught:
                action(body, self.dir)
            self.assertEqual((caught.exception.status, caught.exception.message), (status, message))
        self.assertEqual((self.dir / "GAMES.md").read_text(encoding="utf-8"), before)

    def test_save_returns_the_state_for_the_saved_game(self):
        payload = actions.save({"name": "Outer Wilds", "platform": "PC", "status": "Playing", "spoiler": "strategy"},
                               self.dir)
        self.assertEqual(payload["selected"]["slug"], "outer-wilds")
        self.assertEqual(payload["selected"]["platform"], "PC")
        self.assertEqual(payload["spoiler"], "strategy")
        self.assertIn("outer-wilds", [g["slug"] for g in payload["games"]])
        self.assertTrue((self.dir / "outer-wilds.md").is_file())

        renamed = actions.save({"name": "Outer Wilds: Echoes", "slug": "outer-wilds", "spoiler": "everything"}, self.dir)
        self.assertEqual(renamed["selected"]["slug"], "outer-wilds-echoes")
        self.assertEqual(renamed["spoiler"], "hint")  # an unknown level falls back instead of revealing more
        self.assertEqual(self.slugs(), ["hollow-knight", "celeste", "hades", "tunic", "outer-wilds-echoes"])

    def test_reorder_within_a_status_leaves_the_other_games_in_place(self):
        payload = actions.reorder({"ordered_slugs": ["tunic", "celeste"], "status_filter": "backlog", "game": "Tunic"},
                                  self.dir)
        self.assertEqual(self.slugs(), ["hollow-knight", "tunic", "hades", "celeste"])
        self.assertEqual(payload["selected"]["slug"], "tunic")
        self.assertEqual([g["slug"] for g in payload["games"]], self.slugs())

    def test_state_selects_by_name_and_falls_back_to_the_first_game(self):
        self.assertEqual(actions.state({"game": "Hades"}, self.dir)["selected"]["slug"], "hades")
        self.assertEqual(actions.state({"game": "Okami"}, self.dir)["selected"]["slug"], "hollow-knight")
        empty = actions.state({}, self.dir / "empty")
        self.assertEqual((empty["selected"], empty["games"], empty["spoiler"]), (None, [], "hint"))

    def test_questions_are_validated_and_resolved_before_the_conversation_starts(self):
        conversation = mock.Mock()
        with self.assertRaises(ActionError) as caught:
            actions.ask(conversation, {"backend": "gpt", "question": "Where?"}, self.dir)
        self.assertEqual((caught.exception.status, caught.exception.message), (400, "Unknown backend: gpt"))
        with self.assertRaises(ActionError) as caught:
            actions.ask(conversation, {"game": "hades", "question": "  "}, self.dir)
        self.assertEqual((caught.exception.status, caught.exception.message), (400, "Question is required."))
        with self.assertRaises(ActionError) as caught:
            actions.walkthrough(conversation, {}, self.dir / "empty")
        self.assertEqual((caught.exception.status, caught.exception.message), (400, "Pick or add a game first."))
        conversation.ask.assert_not_called()
        conversation.walkthrough.assert_not_called()

        backend, stream = actions.ask(conversation, {"game": "Hollow Knight", "question": " Mantis Lords? ",
                                                     "spoiler": "everything"}, self.dir)
        self.assertEqual(backend, "claude")
        self.assertIs(stream, conversation.ask.return_value)
        args = conversation.ask.call_args.args
        self.assertEqual((args[0], args[1], args[2], args[3].slug), ("claude", "Mantis Lords?", "hint", "hollow-knight"))

        actions.walkthrough(conversation, {"backend": "codex", "game": "Okami", "spoiler": "answer"}, self.dir)
        backend, game, spoiler = conversation.walkthrough.call_args.args
        self.assertEqual((backend, game.name, spoiler), ("codex", "Okami", "answer"))

    def test_media_actions_offer_youtube_when_their_tools_are_missing(self):
        with mock.patch.object(media, "missing", return_value=["mpv"]), mock.patch.object(media, "play") as play:
            with self.assertRaises(ActionError) as caught:
                actions.play({"id": "Bc-UtkWMuus", "at": 252})
            play.assert_not_called()
        self.assertEqual((caught.exception.status, caught.exception.message), (
            400, "External playback is unavailable on this installation. You can watch the video on YouTube instead."))
        with mock.patch.object(media, "missing", return_value=["ffmpeg"]), self.assertRaises(ActionError) as caught:
            actions.frames("Bc-UtkWMuus", {"at": "252"})
        self.assertEqual(caught.exception.message,
                         "Still frames are unavailable on this installation. You can watch the video on YouTube instead.")
        for call in (lambda: actions.play({"id": "not-an-id"}), lambda: actions.frames("short", {})):
            with self.assertRaises(ActionError) as caught:
                call()
            self.assertEqual((caught.exception.status, caught.exception.message), (400, "Invalid YouTube video ID"))

        with mock.patch.object(media, "missing", return_value=[]), mock.patch.object(media, "play") as play:
            self.assertEqual(actions.play({"id": "Bc-UtkWMuus", "at": "12.7", "label": "Dodge"}), {"ok": True})
        video = play.call_args.args[0]
        self.assertEqual((video.id, video.at, video.label), ("Bc-UtkWMuus", 12, "Dodge"))

    def test_search_finds_guides_across_the_library_by_slug(self):
        result = actions.search({"q": "mantis", "spoiler": "hint"}, self.dir)
        self.assertEqual([(r["slug"], r["place"], r["chapter_index"]) for r in result["results"]], [("hollow-knight", "Notes", None)])
        self.assertEqual(actions.search({"q": "m"}, self.dir)["results"], [])  # one letter matches everything

    def test_an_infinite_timestamp_is_rejected_or_ignored_not_a_crash(self):
        with self.assertRaises(ActionError) as caught:
            actions.frames("Bc-UtkWMuus", {"at": "inf"})
        self.assertEqual((caught.exception.status, caught.exception.message), (400, "Invalid timestamp"))
        with mock.patch.object(media, "missing", return_value=[]), mock.patch.object(media, "play") as play:
            actions.play({"id": "Bc-UtkWMuus", "at": "inf"})
        self.assertEqual(play.call_args.args[0].at, 0)


if __name__ == "__main__":
    unittest.main()
