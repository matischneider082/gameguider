"""install.ps1: what it installs (uv, the wheel, yt-dlp, a Start Menu shortcut), stamped by the release workflow or
not, and that it parses. Its commands are only run on Windows; here they are read and, where pwsh exists, parsed."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "install.ps1"


class InstallPs1Tests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_an_unstamped_copy_falls_back_to_the_latest_release(self):
        self.assertIn("if ($Version -match '^__')", self.text)
        self.assertIn("__VERSION__", self.text)  # the release workflow stamps it
        self.assertIn("releases/latest", self.text)
        self.assertIn("GAMEGUIDER_VERSION", self.text)
        self.assertIn("GAMEGUIDER_BASE_URL", self.text)

    def test_it_installs_the_wheel_with_uv_and_then_runs_setup(self):
        self.assertIn('uv tool install --force "game-guide @ $Base/game_guide-$Version-py3-none-any.whl"', self.text)
        self.assertIn("uv tool install yt-dlp", self.text)
        self.assertIn("game-guide.exe') setup", self.text)

    def test_the_start_menu_shortcut_launches_the_web_ui(self):
        self.assertIn("WScript.Shell", self.text)
        self.assertIn("GameGuider.lnk", self.text)
        self.assertIn("Arguments = 'web'", self.text)
        self.assertIn("GetFolderPath('Programs')", self.text)
        self.assertIn("$Base/icon.ico", self.text)


@unittest.skipUnless(shutil.which("pwsh"), "pwsh is not installed")
class ParseTests(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "install.ps1"
            script.write_text(text, encoding="utf-8", newline="\n")
            return subprocess.run(
                ["pwsh", "-NoProfile", "-Command",
                 f"$errors = $null; "
                 f"[System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$errors) "
                 f"| Out-Null; if ($errors.Count) {{ $errors | ForEach-Object Message; exit 1 }}"],
                capture_output=True, text=True)

    def test_the_script_parses_without_errors(self):
        result = self.parse(SCRIPT.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "posix", "the stamping sed is the release workflow's Linux one")
    def test_a_stamped_copy_parses_without_errors(self):
        stamped = subprocess.run(["sed", "s/__VERSION__/9.9.9/", str(SCRIPT)], capture_output=True, text=True,
                                 check=True).stdout
        self.assertNotIn("__VERSION__", stamped)
        self.assertIn("$Version = '9.9.9'", stamped)
        self.assertIn("if ($Version -match '^__')", stamped)  # the fallback lookup stays, unused
        result = self.parse(stamped)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
