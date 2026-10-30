"""Progress overview computed from a notes folder: empty libraries, missing files, and walkthrough chapter progress."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from game_guide.notes import STATUSES
from game_guide.stats import overview, relative_ago

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
EMPTY = {status: 0 for status in STATUSES}


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write_games(self, *rows: str) -> None:
        table = ["| Game | Platform | Genre | Status | Spot | Updated |", "|---|---|---|---|---|---|", *rows]
        (self.dir / "GAMES.md").write_text("\n".join(table) + "\n", encoding="utf-8")

    def age(self, name: str, seconds: float) -> None:
        stamp = NOW.timestamp() - seconds
        os.utime(self.dir / name, (stamp, stamp))

    def test_relative_ages(self):
        cases = {
            -5: "just now",
            0: "just now",
            59: "just now",
            60: "1 minute ago",
            3600: "1 hour ago",
            86400: "1 day ago",
            2 * 86400: "2 days ago",
            14 * 86400: "2 weeks ago",
            400 * 86400: "1 year ago",
        }
        for seconds, label in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(relative_ago(seconds), label)

    def test_empty_library_and_a_missing_folder(self):
        self.assertEqual(overview(self.dir, now=NOW), {"counts": EMPTY, "backlog": [], "recent": [], "progress": []})
        (self.dir / "GAMES.md").write_text("# Games\n\nNothing tracked yet.\n", encoding="utf-8")
        (self.dir / "stray.walkthrough.md").write_text("## The Slums\n", encoding="utf-8")
        self.assertEqual(overview(self.dir, now=NOW)["counts"], EMPTY)
        self.assertEqual(overview(self.dir, now=NOW)["progress"], [])  # a walkthrough with no GAMES.md row is not queued
        self.assertEqual(overview(self.dir / "missing", now=NOW)["backlog"], [])

    def test_missing_files_and_games_without_walkthroughs(self):
        self.write_games(
            "| Celeste | PC | platformer | Playing | Prologue | 2026-10-01 |",
            "| Hades | PC | roguelike | Beaten | | 2026-10-02 |",
        )
        (self.dir / "celeste.md").write_text("# Celeste\n", encoding="utf-8")
        self.age("celeste.md", 3 * 3600 + 1800)
        (self.dir / "hades.walkthrough.md").mkdir()  # present, but not a readable file
        data = overview(self.dir, now=NOW)
        self.assertEqual(data["counts"]["Playing"], 1)
        self.assertEqual(data["counts"]["Beaten"], 1)
        self.assertEqual(data["progress"], [])
        self.assertEqual([(row["slug"], row["ago"]) for row in data["recent"]], [("celeste", "3 hours ago")])

        (self.dir / "celeste.walkthrough.md").write_bytes(b"\xff\xfe not text")
        self.assertEqual(overview(self.dir, now=NOW)["progress"], [])

    def test_backlog_order_recent_mtime_and_chapter_progress(self):
        self.write_games(
            "| Hollow Knight | Switch | metroidvania | playing | City of Tears | 2026-10-01 |",
            "| Celeste | PC | platformer | Backlog | | 2026-10-02 |",
            "| Hades | PC | roguelike | Playing | | 2026-10-03 |",
            "| Tunic | PC | adventure | backlog | Manual | 2026-10-04 |",
            "| Stray | PS5 | adventure | Wishlist | | 2026-10-05 |",
        )
        (self.dir / "hollow-knight.md").write_text("# Hollow Knight\n", encoding="utf-8")
        (self.dir / "hollow-knight.walkthrough.md").write_text(
            "## Forgotten Crossroads\n## City of Tears\n## Kingdom's Edge\n## The Abyss\n",
            encoding="utf-8",
        )
        (self.dir / "hades.walkthrough.md").write_text("# Hades\n\nNo chapters yet.\n", encoding="utf-8")
        (self.dir / "tunic.md").write_text("# Tunic\n", encoding="utf-8")
        self.age("hollow-knight.md", 2 * 86400 + 12 * 3600)
        self.age("hollow-knight.walkthrough.md", 90 * 60)  # newer than the notes, so this age wins
        self.age("hades.walkthrough.md", 5 * 86400 + 12 * 3600)
        self.age("tunic.md", 2 * 86400 + 12 * 3600)

        data = overview(self.dir, now=NOW)
        self.assertEqual(data["counts"], {**EMPTY, "Playing": 2, "Backlog": 2, "Wishlist": 1})
        self.assertEqual([(row["slug"], row["position"], row["spot"]) for row in data["backlog"]], [
            ("celeste", 1, ""),
            ("tunic", 2, "Manual"),
        ])
        self.assertEqual(data["progress"], [{
            "name": "Hollow Knight",
            "slug": "hollow-knight",
            "status": "Playing",
            "spot": "City of Tears",
            "chapters": 4,
            "current": 2,
            "percent": 50,
            "chapter": "City of Tears",
        }])
        self.assertEqual([(row["slug"], row["ago"]) for row in data["recent"]], [
            ("hollow-knight", "1 hour ago"),
            ("tunic", "2 days ago"),
            ("hades", "5 days ago"),
        ])

    def test_a_beaten_game_is_finished_without_a_spot_match_and_an_unmatched_spot_stays_open(self):
        self.write_games(
            "| Hades | PC | roguelike | Beaten | | |",
            "| Tunic | PC | adventure | Playing | The Heir | |",
        )
        (self.dir / "hades.walkthrough.md").write_text("## Tartarus\n## The surface\n", encoding="utf-8")
        (self.dir / "tunic.walkthrough.md").write_text("## East Forest\n## The Quarry\n", encoding="utf-8")
        rows = {row["slug"]: row for row in overview(self.dir, now=NOW)["progress"]}
        self.assertEqual((rows["hades"]["chapters"], rows["hades"]["current"], rows["hades"]["percent"]), (2, 2, 100))
        self.assertIsNone(rows["hades"]["chapter"])  # the spot names no chapter, so the last heading stays off the panel
        self.assertEqual((rows["tunic"]["chapters"], rows["tunic"]["current"], rows["tunic"]["percent"], rows["tunic"]["chapter"]), (2, None, 0, None))

    def test_a_later_read_sees_edits_to_the_notes_folder(self):
        self.write_games("| Celeste | PC | platformer | Backlog | | |")
        self.assertEqual([row["slug"] for row in overview(self.dir, now=NOW)["backlog"]], ["celeste"])
        self.write_games(
            "| Celeste | PC | platformer | Backlog | | |",
            "| Tunic | PC | adventure | Backlog | | |",
        )
        self.assertEqual([row["slug"] for row in overview(self.dir, now=NOW)["backlog"]], ["celeste", "tunic"])
