import email.message
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from game_guide import backends

SCRIPTS = backends.SKILL.parent / "scripts"

FAKE_YT_DLP = f"""#!{sys.executable}
import sys
from pathlib import Path
Path(__file__).with_name("called").write_text(" ".join(sys.argv[1:]))
if "--print" in sys.argv:
    print('[{{"start_time": 0.0, "end_time": 95.5, "title": "Intro"}}, {{"start_time": 95.5, "end_time": 300.0, "title": "Phase 2"}}]')
"""


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def temp_dir(case):
    d = tempfile.TemporaryDirectory()
    case.addCleanup(d.cleanup)
    return Path(d.name)


def run_script(name, *args, env=None):
    return subprocess.run([sys.executable, SCRIPTS / name, *args], capture_output=True, text=True, env=env)


class ClaudeAllowlist(unittest.TestCase):
    def test_no_broad_rules(self):
        cmd = backends.claude_command("q", None, Path("/n"))
        rules = cmd[cmd.index("--allowedTools") + 1].split(",")
        for broad in ("Read", "Bash(python3:*)", "Bash(yt-dlp:*)", "Bash(curl:*)", "Bash(mkdir:*)"):
            self.assertNotIn(broad, rules)
        bash = [r for r in rules if r.startswith("Bash(")]
        self.assertTrue(bash)
        for rule in bash:
            self.assertTrue(rule.startswith((f"Bash(python3 {SCRIPTS}/", f"Bash(uv run --no-config {SCRIPTS}/")), rule)
        self.assertIn("Read(//n/**)", rules)
        self.assertTrue(all(r.startswith("Read(//") for r in rules if r.startswith("Read")))


class CodexSandbox(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_shell_reads_only_notes_and_skill_and_user_tools_are_off(self):
        (self.home / "config.toml").write_text('[mcp_servers.supabase]\nurl = "https://x"\n'
                                               '[mcp_servers.open-design]\ncommand = "od"\n')
        cmd = backends.codex_command("q", None)
        profile = next(a for a in cmd if a.startswith("permissions.game-guide.filesystem="))
        self.assertIn('":minimal"="read"', profile)
        self.assertIn('":workspace_roots"="write"', profile)
        self.assertIn(f'"{backends.SKILL.parent}"="read"', profile)
        self.assertIn('default_permissions="game-guide"', cmd)
        for feature in ("apps", "plugins", "hooks", "browser_use", "computer_use"):
            self.assertIn(["--disable", feature], [cmd[i:i + 2] for i in range(len(cmd))])
        self.assertIn("mcp_servers.supabase.enabled=false", cmd)
        self.assertIn("mcp_servers.open-design.enabled=false", cmd)
        self.assertNotIn("network_access=true", " ".join(cmd))
        self.assertIn("no network access", cmd[-1])
        resumed = backends.codex_command("q", "t1")
        self.assertIn("mcp_servers.supabase.enabled=false", resumed)
        self.assertLess(resumed.index('default_permissions="game-guide"'), resumed.index("resume"))

    def test_no_project_root_climb_no_memories_and_planted_paths_read_only(self):
        cmd = backends.codex_command("q", None)
        for flag in ("project_root_markers=[]", "memories.use_memories=false", "memories.generate_memories=false"):
            self.assertIn(flag, cmd)
        self.assertIn(["--disable", "memories"], [cmd[i:i + 2] for i in range(len(cmd))])
        profile = next(a for a in cmd if a.startswith("permissions.game-guide.filesystem="))
        for name in ("AGENTS.md", ".agents", ".codex", "CLAUDE.md"):
            self.assertIn(f'"{backends.games_dir().resolve() / name}"="read"', profile)

    def test_fails_closed_when_servers_cannot_be_switched_off(self):
        for config in ("not toml [", '[mcp_servers."a.b"]\ncommand = "x"\n'):
            (self.home / "config.toml").write_text(config)
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                    mock.patch.object(backends.subprocess, "Popen") as popen:
                got = [(e.kind, e.text[:30]) for e in backends.ask("codex", "q")]
            self.assertEqual(got[0][0], "error", config)
            popen.assert_not_called()


class Planted(unittest.TestCase):
    def test_either_backend_refuses_a_notes_folder_that_would_load_instructions_or_tools(self):
        for name, make in ((".agents", "mkdir"), (".codex", "mkdir"), ("AGENTS.md", "file"), (".claude", "mkdir"), (".mcp.json", "file")):
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                    mock.patch.object(backends.procs, "start") as start:
                (Path(d) / name).mkdir() if make == "mkdir" else (Path(d) / name).write_text("x")
                for backend in ("claude", "codex"):
                    got = list(backends.ask(backend, "q"))
                    self.assertEqual([e.kind for e in got], ["error"], (name, backend))
                    self.assertIn(name, got[0].text)
                start.assert_not_called()

    def test_claude_may_not_write_them(self):
        notes = temp_dir(self).resolve()
        cmd = backends.claude_command("q", None, notes)
        denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
        self.assertIn(f"Edit(/{notes}/.claude/**)", denied)
        self.assertIn(f"Edit(/{notes}/.codex/**)", denied)
        self.assertIn(f"Edit(/{notes}/.agents/**)", denied)
        self.assertIn(f"Edit(/{notes}/AGENTS.md)", denied)
        self.assertIn(f"Edit(/{notes}/.mcp.json)", denied)


class Captions(unittest.TestCase):
    def setUp(self):
        self.bin = temp_dir(self)
        fake = self.bin / "yt-dlp"
        fake.write_text(FAKE_YT_DLP)
        fake.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}

    def test_rejects_non_youtube_urls_and_options_without_running_yt_dlp(self):
        for args in (["chapters", "https://evil.example/x"], ["--exec", "x"], ["text", "--exec", "x"],
                     ["chapters", "--", "--exec"], ["text", "https://youtu.be/abc;rm -rf"],
                     ["chapters", "https://evil.example/watch?v=abcdefghijk"]):
            done = run_script("captions.py", *args, env=self.env)
            self.assertNotEqual(done.returncode, 0, args)
            self.assertFalse((self.bin / "called").exists(), args)

    def test_chapters_pass_only_the_video_id(self):
        done = run_script("captions.py", "chapters", "https://www.youtube.com/watch?v=abcdefghijk&t=3s", env=self.env)
        self.assertEqual(done.stdout, "0-95 Intro\n95-300 Phase 2\n")
        called = (self.bin / "called").read_text()
        self.assertTrue(called.startswith("--ignore-config "), called)
        self.assertTrue(called.endswith("-- https://www.youtube.com/watch?v=abcdefghijk"), called)


def load_save_image():
    spec = importlib.util.spec_from_file_location("save_image", SCRIPTS / "save_image.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SaveImage(unittest.TestCase):
    def setUp(self):
        self.notes = temp_dir(self)
        site = temp_dir(self)
        (site / "map.png").write_bytes(b"\x89PNG fake")
        (site / "page.html").write_text("<html></html>")
        self.server = HTTPServer(("127.0.0.1", 0), partial(Quiet, directory=site))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.save = load_save_image().save

    def test_saves_an_image_inside_images(self):
        target = self.save(f"{self.url}/map.png", "images/elden-ring/map.png", self.notes)
        self.assertEqual(target, (self.notes / "images/elden-ring/map.png").resolve())
        self.assertEqual(target.read_bytes(), b"\x89PNG fake")

    def test_refuses_dests_outside_images(self):
        for dest in ("../../x.png", "images/../x.png", "x.png", "images", "/tmp/x.png", str(self.notes / "x.png")):
            with self.assertRaises(SystemExit, msg=dest):
                self.save(f"{self.url}/map.png", dest, self.notes)
        self.assertEqual(list(self.notes.iterdir()), [])

    def test_refuses_non_images(self):
        with self.assertRaises(SystemExit):
            self.save(f"{self.url}/page.html", "images/g/page.png", self.notes)
        self.assertFalse((self.notes / "images").exists())

    def test_refuses_a_redirect_away_from_https(self):
        class Redirected(io.BytesIO):
            headers = email.message.Message()
            def geturl(self):
                return "http://cdn.example/map.png"
        Redirected.headers["Content-Type"] = "image/png"
        with mock.patch("urllib.request.urlopen", return_value=Redirected(b"png")), self.assertRaises(SystemExit):
            self.save("https://example.com/map.png", "images/g/map.png", self.notes)
        self.assertFalse((self.notes / "images").exists())

    def test_cli_downloads_only_https(self):
        env = {**os.environ, "GAME_GUIDE_DIR": str(self.notes)}
        for url in (f"{self.url}/map.png", "file:///etc/passwd"):
            done = run_script("save_image.py", url, "images/g/x.png", env=env)
            self.assertNotEqual(done.returncode, 0, url)
        self.assertEqual(list(self.notes.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
