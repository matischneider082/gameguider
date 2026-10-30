"""install.sh with stub curl/uv/uname: which release it fetches, stamped by the release workflow or not, and what it runs."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$LOG"
case "$(basename "$0") $*" in
  "uname -s") echo "$FAKE_OS" ;;
  *api.github.com*) echo '  "tag_name": "v1.2.3",' ;;
  "uv tool dir --bin") echo "$STUBS" ;;
esac
"""


@unittest.skipUnless(os.name == "posix" and shutil.which("sh") and shutil.which("sed"), "needs a POSIX shell")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stubs = self.root / "bin"
        self.stubs.mkdir()
        for name in ("curl", "uv", "uname", "game-guide", "yt-dlp", "ditto", "mktemp"):
            (self.stubs / name).write_text(STUB)
            (self.stubs / name).chmod(0o755)
        self.log = self.root / "log"

    def run_script(self, text, os_name="Linux", **env):
        script = self.root / "install.sh"
        script.write_text(text)
        result = subprocess.run(["sh", str(script)], capture_output=True, text=True, env={
            "PATH": f"{self.stubs}:/usr/bin:/bin", "HOME": str(self.root), "LOG": str(self.log), "FAKE_OS": os_name,
            "STUBS": str(self.stubs), **env})
        return result, self.log.read_text().splitlines() if self.log.exists() else []

    def stamped(self, version):
        # What the release workflow's sed does: the first __VERSION__ on each line.
        return subprocess.run(["sed", f"s/__VERSION__/{version}/", str(SCRIPT)], capture_output=True, text=True, check=True).stdout

    def test_a_released_installer_installs_its_own_version_without_asking_github(self):
        result, calls = self.run_script(self.stamped("0.2.0"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("api.github.com" in c for c in calls))
        self.assertIn("uv tool install --force game-guide @ https://github.com/matischneider082/gameguider/releases/download/"
                      "v0.2.0/game_guide-0.2.0-py3-none-any.whl", calls)
        self.assertIn("game-guide setup", calls)
        desktop = [c for c in calls if c.startswith("game-guide install-desktop")]
        self.assertEqual(len(desktop), 1)
        self.assertIn(f"--bin {self.stubs}", desktop[0])
        self.assertFalse(any("install-app" in c for c in calls))  # Linux: no app

    def test_a_copy_from_the_repository_installs_the_latest_release(self):
        result, calls = self.run_script(SCRIPT.read_text())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any("api.github.com/repos/matischneider082/gameguider/releases/latest" in c for c in calls))
        self.assertTrue(any(c.endswith("game_guide-1.2.3-py3-none-any.whl") for c in calls))

    def test_on_macos_it_installs_the_prebuilt_app_and_can_leave_the_dock_alone(self):
        result, calls = self.run_script(self.stamped("0.2.0"), "Darwin", GAMEGUIDER_NO_DOCK="1", GAMEGUIDER_BASE_URL="http://mirror.test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c.startswith("curl -fsSL http://mirror.test/GameGuider-macOS.zip") for c in calls))
        installs = [c for c in calls if c.startswith("game-guide install-app")]
        self.assertEqual(len(installs), 1)
        self.assertIn(f"--dest-dir {self.root}/Applications --no-dock", installs[0])

    def test_other_systems_are_turned_away(self):
        result, calls = self.run_script(self.stamped("0.2.0"), "MINGW64_NT")
        self.assertEqual(result.returncode, 1)
        self.assertIn("README", result.stderr)
        self.assertFalse(any(c.startswith("uv ") for c in calls))


class RepositoryTests(unittest.TestCase):
    def test_every_link_points_at_the_one_public_repository(self):
        from game_guide import REPO
        root = SCRIPT.parent
        files = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True).stdout.split()
        found = set()
        for name in files or ["README.md", "install.sh"]:
            path = root / name
            if path.suffix in (".py", ".md", ".sh", ".ps1", ".json", ".toml", ".yml") and path.is_file():
                found |= set(re.findall(r"github\.com/([\w.-]+/(?:game-?guider?|game-guide))\b", path.read_text(encoding="utf-8")))
        self.assertEqual(found, {REPO})
        self.assertIn(f'REPO="{REPO}"', SCRIPT.read_text())


if __name__ == "__main__":
    unittest.main()
