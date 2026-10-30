"""The Library module at its interface: reading GAMES.md, finding a game, and writes that are atomic and lose nothing."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from game_guide import library
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
