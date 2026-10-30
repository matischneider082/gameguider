"""install(): the Linux application-menu entry and icon it writes, and how it finds the game-guide command."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from game_guide import linux_desktop


@unittest.skipUnless(os.name == "posix", "the .desktop entry is a Linux feature")
class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "game-guide").write_text("#!/bin/sh\n")
        (self.bin / "game-guide").chmod(0o755)
        self.icon = self.root / "icon.png"
        self.icon.write_bytes(b"\x89PNG fake")
        self.data = self.root / "share"

    def install(self):
        return linux_desktop.install(data_dir=self.data, bin_dir=self.bin, icon=self.icon)

    def test_writes_a_menu_entry_and_icon_into_the_xdg_data_dirs(self):
        entry = self.install()
        self.assertEqual(entry, self.data / "applications" / "gameguider.desktop")
        text = entry.read_text(encoding="utf-8")
        self.assertIn(f'Exec="{self.bin}/game-guide" web', text)
        self.assertIn("Icon=gameguider", text)
        self.assertIn("Terminal=false", text)
        icon = self.data / "icons" / "hicolor" / "512x512" / "apps" / "gameguider.png"
        self.assertEqual(icon.read_bytes(), b"\x89PNG fake")

    def test_reinstalling_overwrites_in_place(self):
        self.install()
        self.icon.write_bytes(b"newer")
        self.install()
        self.assertEqual([p.name for p in (self.data / "applications").iterdir()], ["gameguider.desktop"])
        self.assertEqual([p.name for p in (self.data / "icons" / "hicolor" / "512x512" / "apps").iterdir()],
                         ["gameguider.png"])

    def test_xdg_data_home_is_honoured_without_an_explicit_dir(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data)}):
            entry = linux_desktop.install(bin_dir=self.bin, icon=self.icon)
        self.assertEqual(entry, self.data / "applications" / "gameguider.desktop")

    def test_without_a_bin_dir_the_command_is_taken_from_the_path(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            entry = linux_desktop.install(data_dir=self.data, icon=self.icon)
        self.assertIn(f'Exec="{self.bin}/game-guide" web', entry.read_text(encoding="utf-8"))

    def test_a_bin_dir_without_the_command_stops_with_the_fix(self):
        with self.assertRaises(SystemExit) as stopped:
            linux_desktop.install(data_dir=self.data, bin_dir=self.root / "nowhere", icon=self.icon)
        self.assertIn("no game-guide command", str(stopped.exception))

    def test_a_path_without_the_command_stops_with_the_fix(self):
        empty = self.root / "empty"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}), self.assertRaises(SystemExit) as stopped:
            linux_desktop.install(data_dir=self.data, icon=self.icon)
        self.assertIn("--bin", str(stopped.exception))


if __name__ == "__main__":
    unittest.main()
