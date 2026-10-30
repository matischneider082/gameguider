import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from game_guide import backends, conversation, notes

ROOT = Path(__file__).resolve().parents[1]

GAMES = """# Games

| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Elden Ring | PS5 | soulslike | Playing | Malenia | 2026-10-23 |
| Hollow Knight: Silksong | Switch | metroidvania | Paused | | 2026-10-01 |
"""

NOTES = """## Malenia
- Phase 2 waterfowl dodge: https://youtu.be/abcdefghijk?t=252
- Overview https://www.youtube.com/watch?v=ABCDEFGHIJK
![arena map](images/elden-ring/arena.png)
![remote](https://example.com/x.png)
"""


class Notes(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_videos_keep_timestamp_and_label(self):
        found = notes.videos(NOTES)
        self.assertEqual([(v.id, v.at) for v in found], [("abcdefghijk", 252), ("ABCDEFGHIJK", 0)])
        self.assertEqual(found[0].label, "Phase 2 waterfowl dodge")
        self.assertEqual(found[0].url, "https://youtu.be/abcdefghijk?t=252")

    def test_labels_keep_inner_punctuation_and_accept_link_style(self):
        found = notes.videos("- Phase 2 (two Lords) centre-stand heal: https://youtu.be/Bc-UtkWMuus?t=286\n"
                             "- [1:03](https://youtu.be/Bc-UtkWMuus?t=63) Route to the arena\n"
                             "**Video** ([Bc-UtkWMuus](https://youtu.be/Bc-UtkWMuus)):")
        self.assertEqual([(v.label, v.at) for v in found],
                         [("Phase 2 (two Lords) centre-stand heal", 286), ("Route to the arena", 63), ("Video (Bc-UtkWMuus)", 0)])

    def test_only_local_images(self):
        self.assertEqual(notes.images(NOTES, self.dir),
                         [("arena map", (self.dir / "images/elden-ring/arena.png").resolve())])


class Backends(unittest.TestCase):
    def test_claude_stream(self):
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {"query": "malenia guide"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {"description": "Chapters 1-3"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Dodge left."}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Dodge left.", "session_id": "s1"},
        ]
        got = [(e.kind, e.text, e.session) for raw in events for e in backends.claude_events(raw)]
        self.assertEqual(got, [("session", "", "s1"), ("status", "searching: malenia guide", None),
                               ("status", "subagent: Chapters 1-3", None),
                               ("text", "Dodge left.", None), ("done", "Dodge left.", "s1")])

    def test_describe_subagent_tools(self):
        self.assertEqual(backends.describe("Agent", {"description": "Chapters 1-4"}), "subagent: Chapters 1-4")
        self.assertEqual(backends.describe("Task", {"prompt": "Research Act 2 chapters"}), "subagent: Research Act 2 chapters")
        self.assertEqual(backends.describe("Agent", {}), "running subagent")

    def test_claude_ignores_string_messages(self):
        self.assertEqual(backends.claude_events({"type": "system", "subtype": "notice", "message": "hello"}), [])

    def test_claude_error_result(self):
        [event] = backends.claude_events({"type": "result", "is_error": True, "result": "rate limited"})
        self.assertEqual((event.kind, event.text), ("error", "rate limited"))

    def test_claude_skill_load_failure_is_an_error(self):
        raw = {"type": "user", "message": {"role": "user", "content":
               "<local-command-stderr>Shell command permission check failed</local-command-stderr>"}}
        [event] = backends.claude_events(raw)
        self.assertEqual((event.kind, event.text), ("error", "Shell command permission check failed"))

    def test_codex_stream(self):
        events = [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "yt-dlp --skip-download x"}},
            {"type": "item.started", "item": {"type": "collab_tool_call", "prompt": "Chapters 4-6"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
            {"type": "turn.completed", "usage": {}},
        ]
        got = [(e.kind, e.text, e.session) for raw in events for e in backends.codex_events(raw)]
        self.assertEqual(got, [("session", "", "t1"), ("status", "fetching video captions", None),
                               ("status", "subagent: Chapters 4-6", None),
                               ("text", "hi", None), ("done", "", None)])

    def test_codex_retry_errors_are_status_and_an_empty_turn_is_an_error(self):
        script = ('import json\n'
                  'print(json.dumps({"type": "error", "message": "Reconnecting... 1/5"}))\n'
                  'print(json.dumps({"type": "turn.completed"}))\n')
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch.object(backends, "codex_command", return_value=[sys.executable, "-c", script]):
            got = [(e.kind, e.text) for e in backends.ask("codex", "q")]
        self.assertEqual(got, [("status", "Reconnecting... 1/5"), ("error", "codex ended the turn without an answer")])

    def test_ask_fills_done_with_last_text_and_keeps_it_over_the_exit_code(self):
        script = ('import json\n'
                  'print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}}))\n'
                  'print("not json")\n'
                  'print(json.dumps({"type": "turn.completed"}))\n'
                  'raise SystemExit(3)\n')
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch.object(backends, "codex_command", return_value=[sys.executable, "-c", script]):
            got = [(e.kind, e.text) for e in backends.ask("codex", "q")]
        self.assertEqual(got, [("text", "answer"), ("done", "answer")])  # a late exit code must not replace the answer

    def test_skill_prompt_inlines_games_table(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            Path(d, "GAMES.md").write_text(GAMES)
            prompt = backends.skill_prompt("help")
        self.assertIn("| Elden Ring | PS5 |", prompt)
        self.assertIn(f"Notes folder: {Path(d).resolve()}", prompt)
        self.assertNotIn("!`cat", prompt)
        self.assertNotIn("name: game-guide", prompt)
        self.assertTrue(prompt.endswith("User: help"))

    def test_claude_command_resumes_and_scopes_edits(self):
        cmd = backends.claude_command("q", "s1", Path("/n"))
        self.assertEqual(cmd[-2:], ["--resume", "s1"])
        self.assertEqual(cmd[cmd.index("-p") + 1], "q")  # a resumed session already has the skill loaded
        self.assertEqual(backends.claude_command("q", None, Path("/n"))[2], "/game-guide:game-guide q")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
        self.assertNotIn("Edit", allowed)
        self.assertIn("Agent", allowed)
        self.assertIn("Task", allowed)

    def test_walkthrough_prompt_and_skill_use_parallel_subagents(self):
        prompt = conversation.walkthrough_prompt("Hollow Knight")
        self.assertIn("full walkthrough for Hollow Knight, with all three spoiler levels", prompt)
        self.assertIn("subagents in parallel", prompt)
        self.assertIn("cover artwork", prompt)
        self.assertIn("walkthrough preamble", prompt)
        skill_text = backends.SKILL.read_text(encoding="utf-8")
        self.assertIn("Parallel Subagents per Chapter Batch", skill_text)

    def test_codex_resume_sends_only_the_question(self):
        cmd = backends.codex_command("q", "t1")
        self.assertEqual(cmd[-2:], ["t1", "q"])
        self.assertLess(cmd.index("-c"), cmd.index("resume"))


class Tui(unittest.IsolatedAsyncioTestCase):
    async def test_lists_games_notes_and_media(self):
        from textual.widgets import ListView, OptionList

        from game_guide.app import GameGuide
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            Path(d, "GAMES.md").write_text(GAMES)
            Path(d, "elden-ring.md").write_text(NOTES)
            app = GameGuide()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(len(app.query_one("#games", ListView).children), 2)
                self.assertEqual(app.game.name, "Elden Ring")
                sources = app.query_one("#sources", OptionList)
                self.assertEqual(sources.option_count, 3)  # two videos, one local image
                self.assertIn("4:12", str(sources.get_option_at_index(0).prompt))
                app.query_one("#games", ListView).index = 1
                await pilot.pause()
                self.assertEqual(app.game.name, "Hollow Knight: Silksong")
                self.assertEqual(sources.option_count, 0)


    async def test_ask_streams_status_then_answer_and_keeps_session(self):
        from textual.widgets import Input

        from game_guide.app import GameGuide
        calls = []

        def fake_ask(backend, question, session=None):
            calls.append((backend, question, session))
            yield backends.Event("session", session="s1")
            yield backends.Event("status", "searching: malenia")
            yield backends.Event("done", "Dodge **left**.", "s1")

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch("game_guide.conversation.ask", fake_ask):
            Path(d, "GAMES.md").write_text(GAMES)
            app = GameGuide()
            async with app.run_test() as pilot:
                for question in ("how do I beat her?", "and phase 2?"):
                    app.query_one("#question", Input).value = question
                    await pilot.press("enter")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                self.assertEqual(app.query_one("#main").active, "answer-tab")
                self.assertIn("Dodge", app.query_one("#answer")._markdown)
        self.assertEqual([c[2] for c in calls], [None, "s1"])  # second question resumes the session
        self.assertIn("Spoiler level: hint. Game selected in the TUI (ignore if the user names another game): Elden Ring,"
                      " currently at: Malenia.", calls[0][1])

    async def test_frames_key_shows_images(self):
        from PIL import Image as Pil

        from game_guide.app import GameGuide
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            Path(d, "GAMES.md").write_text(GAMES)
            Path(d, "elden-ring.md").write_text(NOTES)
            frame = Path(d, "frame.jpg")
            Pil.new("RGB", (64, 36), "red").save(frame)
            with mock.patch("game_guide.media.frames", return_value=[frame, frame]), \
                    mock.patch("game_guide.media.thumbnail", return_value=frame), \
                    mock.patch("game_guide.media.missing", return_value=[]):
                app = GameGuide()
                async with app.run_test() as pilot:
                    app.query_one("#main").active = "media-tab"
                    await pilot.pause()
                    self.assertIs(app.focused, app.query_one("#sources"))  # so p/f reach the app
                    app.query_one("#sources").highlighted = 0
                    await pilot.pause()
                    await pilot.press("f")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertEqual(len(app.query_one("#frames").children), 2)

    async def test_cached_critic_score_shows_on_the_header_and_in_the_list(self):
        from textual.widgets import ListView

        from game_guide import scores
        from game_guide.app import GameGuide
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            Path(d, "GAMES.md").write_text(GAMES)
            Path(d, scores.CACHE_NAME).write_text(json.dumps({
                "elden-ring": {"name": "Elden Ring", "score": 93.2, "count": 11, "source": "IGDB"},
            }))
            app = GameGuide()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertEqual(app.query_one("#critic").content, "Critics 93/100 · IGDB (11)")
                labels = [str(label.content) for label in app.query("#games Label")]
                self.assertIn("93/100", labels[0])
                self.assertNotIn("/100", labels[1])
                # The header row must not let the notes pane grow past the window.
                main = app.query_one("#main")
                self.assertLessEqual(main.region.right, 120)
                self.assertGreater(main.region.width, 60)
                app.query_one("#games", ListView).index = 1
                await pilot.pause()
                self.assertEqual(app.game.name, "Hollow Knight: Silksong")
                self.assertEqual(app.query_one("#critic").content, "No critic score yet. ctrl+g fetches one.")

    async def test_ctrl_g_fetches_a_critic_score_while_the_question_box_is_focused(self):
        from game_guide import scores
        from game_guide.app import GameGuide
        started, release = threading.Event(), threading.Event()

        seen = {}

        def fake(game, directory):
            seen["directory"] = Path(directory)
            started.set()
            release.wait(5)
            result = {"score": 89, "count": 72, "source": "OpenCritic"}
            Path(directory, scores.CACHE_NAME).write_text(json.dumps(
                {game.slug: {"name": game.name, **result}}))
            return result

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch("game_guide.scores.refresh_score", fake), \
                mock.patch("game_guide.doctor.ai_status", side_effect=AssertionError("scores must not ask the AI")):
            Path(d, "GAMES.md").write_text(GAMES)
            app = GameGuide()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertIs(app.focused, app.query_one("#question"))
                await pilot.press("ctrl+g")
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                await pilot.pause()
                self.assertEqual(app.query_one("#status").content, "Fetching critic score for Elden Ring…")
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.query_one("#critic").content, "Critics 89/100 · OpenCritic (72)")
                self.assertEqual(app.query_one("#status").content, "Critics 89/100 · OpenCritic (72)")
                self.assertIn("89/100", str(app.query("#games Label")[0].content))
                self.assertEqual(seen["directory"], Path(d))

    async def test_c_fetches_a_critic_score_from_the_walkthrough_tab(self):
        from game_guide import scores
        from game_guide.app import GameGuide

        def fake(game, directory):
            result = {"score": 91, "count": None, "source": "Metacritic"}
            Path(directory, scores.CACHE_NAME).write_text(json.dumps(
                {game.slug: {"name": game.name, "score": 91, "count": None, "source": "Metacritic"}}))
            return result

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch("game_guide.scores.refresh_score", fake):
            Path(d, "GAMES.md").write_text(GAMES)
            app = GameGuide()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one("#main").active = "walk-tab"
                await pilot.pause()
                await pilot.press("c")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.query_one("#critic").content, "Critics 91/100 · Metacritic")
                self.assertIn("91/100", str(app.query("#games Label")[0].content))

    async def test_a_failed_critic_fetch_keeps_the_saved_score(self):
        from game_guide import scores
        from game_guide.app import SCORE_FAILED, GameGuide
        failures = iter([
            ValueError("ERROR: [wikidata] no critic score for this game yet."),
            RuntimeError("boom"),
        ])

        def fake(game, directory):
            raise next(failures)

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch("game_guide.scores.refresh_score", fake):
            Path(d, "GAMES.md").write_text(GAMES)
            Path(d, scores.CACHE_NAME).write_text(json.dumps({
                "elden-ring": {"name": "Elden Ring", "score": 90, "count": 4, "source": "Metacritic"},
            }))
            app = GameGuide()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.query_one("#critic").content, "Critics 90/100 · Metacritic (4)")
                await pilot.press("ctrl+g")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.query_one("#status").content, "ERROR: [wikidata] no critic score for this game yet.")
                self.assertEqual(app.query_one("#critic").content, "Critics 90/100 · Metacritic (4)")
                await pilot.press("ctrl+g")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.query_one("#status").content, SCORE_FAILED)
                self.assertIn("90/100", str(app.query("#games Label")[0].content))
                self.assertIsNotNone(app.query_one("#critic"))  # the app is still up

    async def test_critic_score_without_a_game_asks_you_to_pick_one(self):
        from game_guide.app import GameGuide
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            app = GameGuide()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("ctrl+g")
                await pilot.pause()
                self.assertTrue(any(n.message == "Pick or add a game first." for n in app._notifications))
                self.assertEqual(app.query_one("#critic").content, "")


class Script(unittest.TestCase):
    def test_vtt_selftest(self):
        out = subprocess.run([sys.executable, ROOT / "skills/game-guide/scripts/vtt_to_text.py", "--selftest"],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "selftest ok", out.stderr)


if __name__ == "__main__":
    unittest.main()
