"""The Library module at its interface: reading GAMES.md, finding a game, and writes that are atomic and lose nothing."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from game_guide import library, notes
from game_guide.notes import Game

GAMES = """# Games

| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Elden Ring | PS5 | soulslike | Playing | Malenia | 2026-10-23 |
| Hollow Knight: Silksong | Switch | metroidvania | Paused | | 2026-10-01 |
"""


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


class ReadAndFind(LibraryTests):
    def setUp(self):
        super().setUp()
        (self.dir / "GAMES.md").write_text(GAMES, encoding="utf-8")

    def test_reads_games_table(self):
        games = library.read_games(self.dir)
        self.assertEqual([g.name for g in games], ["Elden Ring", "Hollow Knight: Silksong"])
        self.assertEqual((games[0].genre, games[0].spot), ("soulslike", "Malenia"))
        self.assertEqual(games[1].slug, "hollow-knight-silksong")

    def test_missing_index_means_no_games(self):
        self.assertEqual(library.read_games(self.dir / "nope"), [])

    def test_find_game_by_slug_or_name_ignoring_case(self):
        self.assertEqual(library.find_game("hollow-knight-silksong", self.dir).name, "Hollow Knight: Silksong")
        self.assertEqual(library.find_game("  elden RING ", self.dir).spot, "Malenia")

    def test_find_game_finds_an_untracked_walkthrough_by_its_title_heading(self):
        (self.dir / "celeste.walkthrough.md").write_text("# Celeste: Farewell — Walkthrough\n\n## Chapter 1\n", encoding="utf-8")
        game = library.find_game("celeste: farewell", self.dir)
        self.assertEqual((game.name, game.status), ("Celeste: Farewell", "Walkthrough"))

    def test_find_game_returns_none_for_an_unknown_key(self):
        self.assertIsNone(library.find_game("Hades II", self.dir))


class PartialFiles(LibraryTests):
    def test_a_truncated_index_still_lists_the_intact_rows(self):
        # The last row is a cut-off UTF-8 sequence from an editor saving in place.
        (self.dir / "GAMES.md").write_bytes(
            b"# Games\n\n"
            b"| Game | Platform | Genre | Status | Spot | Updated |\n"
            b"|---|---|---|---|---|---|\n"
            b"| Celeste | PC | platform | Playing | Summit | 2026-10-01 |\n"
            b"| Caf\xc3"
        )
        games = library.read_games(self.dir)
        self.assertEqual(games[0].name, "Celeste")
        self.assertEqual(games[0].spot, "Summit")

    def test_notes_that_are_partial_or_unreadable_do_not_crash_a_selected_game(self):
        game = library.save_game("Celeste", directory=self.dir)
        path = notes.notes_path(game, self.dir)
        path.write_bytes(b"## Summit\nCaf\xc3")
        text = notes.read_notes(game, self.dir)
        self.assertIn("Summit", text)
        if os.name != "posix":
            return  # chmod 0 does not make a file unreadable on Windows
        path.write_text("secret", encoding="utf-8")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        self.assertEqual(notes.read_notes(game, self.dir), "")
        self.assertEqual(notes.read_walkthrough(game, self.dir), "")

    def test_a_note_can_be_appended_when_the_file_was_saved_mid_character(self):
        game = library.save_game("Celeste", directory=self.dir)
        notes.notes_path(game, self.dir).write_bytes(b"## Summit\nCaf\xc3")
        library.append_note(game, "Reached the top.", heading="Summit", directory=self.dir)
        text = notes.notes_path(game, self.dir).read_text(encoding="utf-8", errors="replace")
        self.assertIn("Reached the top.", text)


class SlugCollisions(LibraryTests):
    def test_titles_with_no_ascii_letters_do_not_overwrite_each_other(self):
        for name in ("ゼルダ", "ポケモン", "???", "!!!"):
            library.save_game(name, directory=self.dir)
        games = library.read_games(self.dir)
        self.assertEqual([g.name for g in games], ["ゼルダ", "ポケモン", "???", "!!!"])
        self.assertEqual(len({g.slug for g in games}), 4)
        # Accent folding stays the slug the model is told to write.
        self.assertEqual(notes.slugify("Pokémon Scarlet"), "pokemon-scarlet")
        self.assertEqual(len({p.name for p in self.dir.glob("*.md")} - {"GAMES.md"}), 4)


class FoldedFilenames(LibraryTests):
    def test_a_walkthrough_whose_filename_folds_onto_a_game_is_not_a_second_game(self):
        library.save_game("Pokemon", directory=self.dir)
        (self.dir / "Pokémon.walkthrough.md").write_text(
            "# Pokémon — Walkthrough\n\n## Pallet\nHi\n", encoding="utf-8")
        games = library.discover_games(self.dir)
        self.assertEqual([(g.name, g.slug) for g in games], [("Pokemon", "pokemon")])
        self.assertIn("Pallet", notes.read_walkthrough(games[0], self.dir))

    def test_notes_saved_under_a_folded_filename_still_open(self):
        game = library.save_game("Pokemon", directory=self.dir)
        path = notes.notes_path(game, self.dir)
        path.write_text("from the accented file", encoding="utf-8")
        path.rename(self.dir / "Pokémon.md")
        self.assertIn("accented", notes.read_notes(game, self.dir))


class Writes(LibraryTests):
    def test_concurrent_saves_lose_no_update(self):
        names = [f"Game {i}" for i in range(16)]
        for round_ in range(4):
            with self.subTest(round=round_):
                folder = self.dir / str(round_)
                barrier = threading.Barrier(len(names))
                errors = []

                def save(name):
                    barrier.wait()
                    try:
                        library.save_game(name, directory=folder)
                    except Exception as exc:  # surfaced below; a thread's exception would otherwise vanish
                        errors.append(exc)

                threads = [threading.Thread(target=save, args=(name,)) for name in names]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(errors, [])
                self.assertEqual(sorted(g.name for g in library.read_games(folder)), sorted(names))

    def test_renaming_a_tracked_game_onto_another_games_name_is_rejected(self):
        library.save_game("Hades", platform="Switch", directory=self.dir)
        library.save_game("Celeste", platform="PC", directory=self.dir)

        with self.assertRaises(ValueError):
            library.save_game("Celeste", platform="PC", spot="Chapter 3", lookup_slug="hades", directory=self.dir)

        games = {g.slug: g for g in library.read_games(self.dir)}
        # The rename must not spill onto Celeste's row: both games keep their own metadata.
        self.assertEqual(set(games), {"hades", "celeste"})
        self.assertEqual((games["hades"].platform, games["hades"].spot), ("Switch", ""))
        self.assertEqual((games["celeste"].platform, games["celeste"].spot), ("PC", ""))

    def test_rename_rejection_does_not_depend_on_row_order(self):
        library.save_game("Celeste", directory=self.dir)
        library.save_game("Hades", directory=self.dir)

        with self.assertRaises(ValueError):
            library.save_game("Celeste", spot="Chapter 3", lookup_slug="hades", directory=self.dir)

        # Hades keeps its own row: no second celeste slug sharing one notes file.
        self.assertEqual([g.slug for g in library.read_games(self.dir)], ["celeste", "hades"])

    def test_renaming_a_game_to_a_free_name_still_works(self):
        library.save_game("Hades", status="Paused", directory=self.dir)

        library.save_game("Hades II", status="Paused", lookup_slug="hades", directory=self.dir)

        games = library.read_games(self.dir)
        self.assertEqual([g.slug for g in games], ["hades-ii"])
        self.assertEqual(games[0].status, "Paused")  # renamed in place: the row's metadata carried over
        self.assertTrue((self.dir / "hades-ii.md").is_file())
        self.assertFalse((self.dir / "hades.md").exists())

    def test_failed_write_keeps_the_old_file_and_leaves_no_temp_file(self):
        library.save_game("Celeste", directory=self.dir)
        before = (self.dir / "GAMES.md").read_text(encoding="utf-8")
        files = sorted(self.dir.iterdir())
        with mock.patch("game_guide.library.os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            library.write_games([Game("Hades II")], self.dir)
        self.assertEqual((self.dir / "GAMES.md").read_text(encoding="utf-8"), before)
        self.assertEqual(sorted(self.dir.iterdir()), files)

    @unittest.skipUnless(os.name == "posix", "file modes and umask are POSIX concepts")
    def test_writes_keep_the_file_mode_and_new_files_follow_the_umask(self):
        umask = os.umask(0)
        os.umask(umask)
        library.save_game("Celeste", directory=self.dir)
        games = self.dir / "GAMES.md"
        self.assertEqual(games.stat().st_mode & 0o777, 0o666 & ~umask)  # not mkstemp's 0600
        games.chmod(0o640)
        library.save_game("Hades II", directory=self.dir)
        self.assertEqual(games.stat().st_mode & 0o777, 0o640)


class Backlog(LibraryTests):
    def test_batch_add_flattens_cli_arguments_and_parses_list_metadata(self):
        added = library.batch_add_games(
            [
                "- Hades II\n2. Hollow Knight: Silksong | Switch 2 | Metroidvania",
                "# A pasted comment\nCeleste",
            ],
            default_platform="PC",
            default_genre="Roguelike",
            directory=self.dir,
        )

        self.assertEqual([g.name for g in added], ["Hades II", "Hollow Knight: Silksong", "Celeste"])
        self.assertTrue(all(g.status == "Backlog" for g in added))
        self.assertEqual((added[0].platform, added[0].genre), ("PC", "Roguelike"))
        self.assertEqual((added[1].platform, added[1].genre), ("Switch 2", "Metroidvania"))
        self.assertIn("## Backlog Queue", (self.dir / "hades-ii.md").read_text(encoding="utf-8"))

    def test_batch_add_preserves_existing_metadata_and_reorder_keeps_other_statuses_in_place(self):
        library.save_game("Playing A", platform="PC", status="Playing", directory=self.dir)
        library.save_game("Backlog B", platform="Switch", status="Backlog", directory=self.dir)
        library.save_game("Playing C", status="Playing", directory=self.dir)
        library.save_game("Backlog D", status="Backlog", directory=self.dir)

        [updated] = library.batch_add_games(["Backlog B"], directory=self.dir)
        self.assertEqual(updated.platform, "Switch")

        reordered = library.reorder_games(
            ["backlog-d", "backlog-d", "unknown", "backlog-b"],
            status_filter="Backlog",
            directory=self.dir,
        )
        self.assertEqual([g.name for g in reordered], ["Playing A", "Backlog D", "Playing C", "Backlog B"])
        self.assertEqual([g.name for g in library.read_games(self.dir)], [g.name for g in reordered])


if __name__ == "__main__":
    unittest.main()
