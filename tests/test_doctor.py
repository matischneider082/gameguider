"""The setup checks and the walkthrough that connects an AI: what's needed (a writable notes folder and one AI that's
installed and signed in), what's optional, and how each gets fixed."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from game_guide import actions, doctor
from game_guide.actions import ActionError

SIGNED_IN = '#!/bin/sh\ncase "$*" in "auth status --json") echo \'{"loggedIn": true, "email": "x@example.com"}\' ;; esac\n'
SIGNED_OUT = '#!/bin/sh\ncase "$*" in "auth status --json") echo \'{"loggedIn": false}\' ;; "login status") exit 1 ;; esac\n'


class Machine(unittest.TestCase):
    """A temp home and an empty PATH that tests fill with stub CLIs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "user"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.root / "config"),
                                           "GAME_GUIDE_DIR": str(self.home / "Notes")})
        env.start()
        self.addCleanup(env.stop)

    def install(self, *tools, script="#!/bin/sh\n"):
        for tool in tools:
            path = self.bin / tool
            path.write_text(script)
            path.chmod(0o755)

    def by_name(self):
        return {c.name: c for c in doctor.checks()}


@unittest.skipUnless(os.name == "posix", "the stub CLIs are POSIX shebang scripts on PATH")
class DoctorTests(Machine):
    def test_without_an_ai_it_is_not_ready_and_says_how_to_install_and_sign_in(self):
        checks = self.by_name()
        self.assertFalse(doctor.ready(list(checks.values())))
        for name, installer, sign_in in (("Claude (Claude Code)", "https://claude.ai/install.sh", "claude auth login"),
                                         ("ChatGPT (the Codex CLI)", "https://chatgpt.com/codex/install.sh", "codex login")):
            self.assertTrue(checks[name].required)
            self.assertEqual(checks[name].detail, "not installed")
            self.assertIn(installer, checks[name].fix)
            self.assertIn(sign_in, checks[name].fix)
        self.assertTrue(checks["Notes folder"].ok)  # not created yet, but its parent is writable
        self.assertIn("Not ready", doctor.report())

    def test_one_signed_in_ai_is_enough_and_media_tools_are_optional(self):
        self.install("claude", script=SIGNED_IN)
        checks = self.by_name()
        self.assertTrue(doctor.ready(list(checks.values())))
        self.assertEqual(checks["Claude (Claude Code)"].detail, "signed in")
        self.assertFalse(checks["ChatGPT (the Codex CLI)"].required)
        self.assertFalse(checks["yt-dlp"].required)
        self.assertIn("uv tool install yt-dlp", checks["yt-dlp"].fix)
        self.assertNotIn("x@example.com", doctor.report())  # only the yes/no is read

    def test_windows_gets_native_installer_and_package_hints(self):
        with mock.patch.object(doctor.sys, "platform", "win32"):
            self.assertIn("install.ps1", doctor._ai_install("claude"))
            self.assertIn("@openai/codex", doctor._ai_install("codex"))
            self.assertEqual(doctor._package_fix("ffmpeg"), "winget install ffmpeg")
        # MEDIA and AI are built at import from the running platform, so the mocked hints above are what
        # a Windows import would bake into its checks.

    def test_an_installed_but_signed_out_ai_is_not_ready(self):
        self.install("claude", "codex", script=SIGNED_OUT)
        checks = self.by_name()
        self.assertFalse(doctor.ready(list(checks.values())))
        self.assertEqual(checks["ChatGPT (the Codex CLI)"].detail, "installed, not signed in")
        self.assertEqual(checks["Claude (Claude Code)"].fix, "Sign in with `claude auth login`.")

    def test_a_cli_that_cannot_report_its_sign_in_counts_as_ready(self):
        self.install("claude")  # an older CLI: prints nothing for `auth status`
        self.assertIsNone(doctor.signed_in("claude"))
        self.assertTrue(doctor.ready(doctor.checks()))

    def test_an_unwritable_notes_folder_blocks(self):
        self.install("claude", script=SIGNED_IN)
        folder = self.home / "Notes"
        folder.mkdir()
        folder.chmod(0o500)
        self.addCleanup(folder.chmod, 0o700)
        checks = self.by_name()
        self.assertFalse(checks["Notes folder"].ok)
        self.assertIn("--notes-dir", checks["Notes folder"].fix)
        self.assertFalse(doctor.ready(list(checks.values())))

    def test_the_setup_actions_report_and_save_the_folder(self):
        os.environ.pop("GAME_GUIDE_DIR")
        self.install("claude", script=SIGNED_IN)
        setup = actions.setup()
        self.assertTrue(setup["ready"])
        self.assertEqual([(a["id"], a["installed"], a["signed_in"]) for a in setup["ai"]], [("claude", True, True), ("codex", False, None)])
        saved = actions.save_setup({"notes_dir": "~/Vault/Games"})
        self.assertEqual(saved["notes_dir"], str(self.home / "Vault" / "Games"))
        with self.assertRaises(ActionError) as refused:
            actions.save_setup({"notes_dir": "~"})
        self.assertEqual(refused.exception.status, 400)


@unittest.skipUnless(os.name == "posix", "the stub CLIs are POSIX shebang scripts on PATH")
class WalkthroughTests(Machine):
    def walk(self, answers, run=None):
        said = []
        result = doctor.walkthrough(ask=lambda _prompt: answers.pop(0), run=run or mock.Mock(), say=said.append)
        return result, "\n".join(said)

    def test_skipping_installs_nothing(self):
        run = mock.Mock()
        result, said = self.walk(["3"], run)
        self.assertFalse(result)
        run.assert_not_called()
        self.assertIn("game-guide setup", said)

    def test_choosing_claude_installs_it_then_signs_in(self):
        def run(command, shell=False):
            if shell:  # the official installer: here it "installs" a CLI that is signed in once `auth login` ran
                self.assertEqual(command, doctor.AI["claude"]["install"])
                self.install("claude", script=SIGNED_OUT)
            else:
                self.assertEqual(command, ["claude", "auth", "login"])
                self.install("claude", script=SIGNED_IN)
        result, said = self.walk(["1", ""], mock.Mock(side_effect=run))
        self.assertTrue(result)
        self.assertIn("Step 1 of 2", said)
        self.assertIn("Claude is connected", said)

    def test_an_installed_ai_only_needs_signing_in(self):
        self.install("codex", script=SIGNED_OUT)
        run = mock.Mock(side_effect=lambda command: self.install("codex"))  # `codex login` succeeds
        result, said = self.walk(["2"], run)
        self.assertTrue(result)
        run.assert_called_once_with(["codex", "login"])
        self.assertNotIn("Step 1", said)

    def test_declining_the_installer_stops_with_the_command_to_run(self):
        run = mock.Mock()
        result, said = self.walk(["1", "n"], run)
        self.assertFalse(result)
        run.assert_not_called()
        self.assertIn("code.claude.com", said)


class TerminalTests(unittest.TestCase):
    def test_only_the_fixed_walkthrough_commands_can_be_opened(self):
        with mock.patch.object(doctor.sys, "platform", "darwin"), mock.patch.object(doctor.subprocess, "run") as run:
            actions.setup_terminal({"backend": "claude", "step": "sign_in"})
            self.assertIn('do script "claude auth login"', run.call_args.args[0][-1])
            for body in ({"backend": "claude", "step": "rm -rf ~"}, {"backend": "bash", "step": "install"}):
                with self.assertRaises(ActionError) as refused:
                    actions.setup_terminal(body)
                self.assertEqual(refused.exception.status, 400)
        self.assertEqual(run.call_count, 1)

    def test_elsewhere_it_asks_you_to_copy_the_command(self):
        with mock.patch.object(doctor.sys, "platform", "linux"), self.assertRaises(ActionError) as refused:
            actions.setup_terminal({"backend": "codex", "step": "install"})
        self.assertIn("Copy the command", refused.exception.message)


if __name__ == "__main__":
    unittest.main()
