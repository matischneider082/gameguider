"""Where the notes live: $GAME_GUIDE_DIR, the saved setting, or the default, the same for the app, the CLI and the skill."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from game_guide import backends, settings
from game_guide.notes import games_dir

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "game-guide" / "scripts"


class SettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "user"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"), "HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GAME_GUIDE_DIR", None)  # restored when the patch stops

    def test_the_env_wins_then_the_saved_folder_then_the_default(self):
        self.assertEqual(games_dir(), self.home / "Documents" / "GameGuider")
        saved = settings.set_notes_dir(str(self.home / "Vault" / "Games"))
        self.assertEqual(games_dir(), saved)
        with mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": str(self.root / "env")}):
            self.assertEqual(games_dir(), self.root / "env")
        self.assertEqual(games_dir(self.root / "given"), self.root / "given")

    def test_saving_creates_the_folder_and_keeps_other_settings(self):
        settings.config_path().parent.mkdir(parents=True)
        settings.config_path().write_text(json.dumps({"theme": "dark"}))
        folder = settings.set_notes_dir("~/Games")
        self.assertTrue(folder.is_dir())
        self.assertEqual(json.loads(settings.config_path().read_text()), {"theme": "dark", "notes_dir": str(self.home / "Games")})

    def test_home_its_parents_and_relative_paths_are_refused(self):
        for path in ("~", str(self.root), "/", "Games", ""):
            with self.subTest(path=path), self.assertRaises(ValueError):
                settings.set_notes_dir(path)
        self.assertFalse(settings.config_path().exists())

    def test_a_broken_config_falls_back_to_the_default(self):
        settings.config_path().parent.mkdir(parents=True)
        for text in ("not json", "[1]", '{"notes_dir": 5}'):
            settings.config_path().write_text(text)
            self.assertEqual(games_dir(), self.home / "Documents" / "GameGuider")

    def test_the_skill_scripts_resolve_the_same_folder(self):
        folder = settings.set_notes_dir(str(self.home / "Vault"))
        out = subprocess.run([sys.executable, SCRIPTS / "games_table.py"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.splitlines()[0], f"Notes folder: {folder}")

    def test_setup_command_saves_and_shows_the_folder(self):
        from game_guide import cli
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.main(["setup", "--notes-dir", "~/Vault"])
        self.assertEqual(out.getvalue().splitlines()[0], f"Notes folder: {self.home / 'Vault'}")
        with self.assertRaises(SystemExit) as refused:
            cli.main(["setup", "--notes-dir", "~"])
        self.assertIn("home folder", str(refused.exception.code))
        self.assertEqual(games_dir(), self.home / "Vault")

    def test_the_cli_is_told_which_folder_to_use(self):
        folder = settings.set_notes_dir(str(self.home / "Vault"))
        script = ('import json, os\n'
                  'print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", '
                  '"text": os.environ["GAME_GUIDE_DIR"] + " " + os.getcwd()}}))\n')
        with mock.patch.object(backends, "codex_command", return_value=[sys.executable, "-c", script]):
            self.assertEqual(next(backends.ask("codex", "q")).text, f"{folder} {folder}")


if __name__ == "__main__":
    unittest.main()
