"""Structured answers: the Guide schema sent to both CLIs, parsing their payloads, and rendering them."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from game_guide import backends, cli
from game_guide.backends import Guide, Source, render

GAMES = """# Games

| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Elden Ring | PS5 | soulslike | Playing | Malenia | 2026-10-23 |
"""

PAYLOAD = {"kind": "guide", "game": "Elden Ring", "spot": "Malenia", "spoiler": "hint",
           "summary": "Run away from Waterfowl Dance, then dodge into it.",
           "sources": [{"title": "Malenia guide", "url": "https://youtu.be/abcdefghijk?t=252", "at_seconds": 252}]}


class Parse(unittest.TestCase):
    def test_claude_structured_result_sets_the_guide(self):
        [done] = backends.claude_events({"type": "result", "is_error": False, "result": json.dumps(PAYLOAD),
                                         "structured_output": PAYLOAD, "session_id": "s1"})
        self.assertEqual((done.kind, done.session), ("done", "s1"))
        self.assertEqual(done.guide, Guide.model_validate(PAYLOAD))

    def test_claude_invalid_payload_falls_back_to_text(self):
        [done] = backends.claude_events({"type": "result", "is_error": False, "result": "Dodge left.",
                                         "structured_output": {"kind": "nonsense"}, "session_id": "s1"})
        self.assertEqual((done.kind, done.text, done.guide), ("done", "Dodge left.", None))

    def test_codex_last_message_json_becomes_the_guide(self):
        script = ('import json\n'
                  f'print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": {json.dumps(json.dumps(PAYLOAD))}}}}}))\n'
                  'print(json.dumps({"type": "turn.completed"}))\n')
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch.object(backends, "codex_command", return_value=[sys.executable, "-c", script]):
            *_, done = backends.ask("codex", "q")
        self.assertEqual((done.kind, done.guide), ("done", Guide.model_validate(PAYLOAD)))

    def test_codex_citation_markers_are_stripped(self):
        marked = {**PAYLOAD, "summary": PAYLOAD["summary"] + " \ue200cite\ue202turn954121view0\ue201"}
        for text in (json.dumps(marked), json.dumps(marked, ensure_ascii=False)):  # escaped and raw markers
            script = ('import json\n'
                      f'print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": {json.dumps(text)}}}}}))\n'
                      'print(json.dumps({"type": "turn.completed"}))\n')
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                    mock.patch.object(backends, "codex_command", return_value=[sys.executable, "-c", script]):
                *_, done = backends.ask("codex", "q")
            self.assertEqual(done.guide.summary, PAYLOAD["summary"])  # the space before the marker goes too
            self.assertNotIn("\ue200", done.text)

    def test_a_citation_marker_spanning_two_fields_does_not_break_the_guide(self):
        marked = {**PAYLOAD, "summary": PAYLOAD["summary"] + "\ue200", "follow_up": "next?\ue201"}
        script = ('import json\n'
                  f'print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": {json.dumps(json.dumps(marked))}}}}}))\n'
                  'print(json.dumps({"type": "turn.completed"}))\n')
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch.object(backends, "codex_command", return_value=[sys.executable, "-c", script]):
            *_, done = backends.ask("codex", "q")  # used to raise ValidationError: Invalid JSON
        self.assertEqual((done.guide.summary, done.guide.follow_up), (PAYLOAD["summary"] + "\ue200", "next?\ue201"))

    def test_stripping_a_citation_keeps_words_and_line_breaks_apart(self):
        mark = "\ue200cite\ue202turn0view0\ue201"
        self.assertEqual(backends.strip_citations(f"Bring 5 {mark}gold."), "Bring 5 gold.")
        self.assertEqual(backends.strip_citations(f"**Go left. {mark}**"), "**Go left.**")
        self.assertEqual(backends.strip_citations(f"line one  {mark}\nline two"), "line one  \nline two")

    def test_codex_schema_is_strict(self):
        schema = json.loads(backends.SCHEMA)
        for obj in (schema, schema["$defs"]["Source"]):
            self.assertEqual(set(obj["required"]), set(obj["properties"]))
            self.assertIs(obj["additionalProperties"], False)


class Render(unittest.TestCase):
    def test_guide(self):
        guide = Guide.model_validate(PAYLOAD | {"prep": ["Bloodhound Step"], "steps": ["Sprint away", "Dodge in"],
                                                "watch_out": ["Her heal on hit"], "follow_up": "Want the strategy?"})
        out = render(guide)
        self.assertTrue(out.startswith("## Elden Ring — Malenia `hint`"))
        self.assertIn("**Run away from Waterfowl Dance, then dodge into it.**", out)
        self.assertIn("1. Sprint away\n2. Dodge in", out)
        self.assertIn("- Her heal on hit", out)
        self.assertIn("- [Malenia guide](https://youtu.be/abcdefghijk?t=252) ▶ 4:12", out)
        self.assertTrue(out.endswith("_Want the strategy?_"))

    def test_tracked_is_one_line(self):
        out = render(Guide(kind="tracked", game="Hollow Knight", spoiler="hint", summary="Added as Playing."))
        self.assertEqual(out, "✓ **Hollow Knight**: Added as Playing.")

    def test_question_without_spot_or_sections(self):
        out = render(Guide(kind="question", game="Elden Ring", spoiler="hint", summary="Melee or magic?",
                           sources=[Source(title="wiki", url="https://x.io")]))
        self.assertEqual(out, "## Elden Ring `hint`\n\n**Melee or magic?**\n\n**Sources**\n\n- [wiki](https://x.io)")


class Command(unittest.TestCase):
    def test_claude_sends_schema_and_sonnet_by_default(self):
        with mock.patch.dict(os.environ, {"GAME_GUIDE_MODEL": ""}):
            for session in (None, "s1"):
                cmd = backends.claude_command("q", session, Path("/n"))
                self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), Guide.model_json_schema())
                self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")

    def test_model_from_env_for_both_backends(self):
        with mock.patch.dict(os.environ, {"GAME_GUIDE_MODEL": "opus"}):
            claude = backends.claude_command("q", None, Path("/n"))
            codex = backends.codex_command("q", "t1")
        self.assertEqual(claude[claude.index("--model") + 1], "opus")
        self.assertEqual(codex[codex.index("-m") + 1], "opus")

    def test_codex_sends_schema_file_and_keeps_its_own_model(self):
        with mock.patch.dict(os.environ, {"GAME_GUIDE_MODEL": ""}):
            for session in (None, "t1"):
                cmd = backends.codex_command("q", session)
                self.assertEqual(Path(cmd[cmd.index("--output-schema") + 1]).read_text(), backends.SCHEMA)
                self.assertNotIn("-m", cmd)


class Cli(unittest.TestCase):
    def run_cli(self, *events, argv=()):
        calls = []

        def fake_ask(backend, question, session=None):
            calls.append(os.environ.get("GAME_GUIDE_MODEL"))
            yield from events

        out = io.StringIO()
        with mock.patch("game_guide.conversation.ask", fake_ask), mock.patch.dict(os.environ, {"GAME_GUIDE_MODEL": ""}), \
                redirect_stdout(out):
            cli.main(["ask", "help", *argv])
        return out.getvalue(), calls

    def test_json_prints_the_guide(self):
        out, _ = self.run_cli(backends.Event("done", "raw", guide=Guide.model_validate(PAYLOAD)), argv=["--json"])
        self.assertEqual(Guide.model_validate_json(out), Guide.model_validate(PAYLOAD))

    def test_json_without_a_guide_exits_with_the_text(self):
        with self.assertRaises(SystemExit) as exit:
            self.run_cli(backends.Event("done", "plain answer"), argv=["--json"])
        self.assertEqual(exit.exception.code, "plain answer")

    def test_prints_render_or_text_and_passes_the_model(self):
        out, calls = self.run_cli(backends.Event("done", "raw", guide=Guide.model_validate(PAYLOAD)), argv=["--model", "opus"])
        self.assertIn("▶ 4:12", out)
        self.assertEqual(calls, ["opus"])
        out, _ = self.run_cli(backends.Event("done", "plain answer"))
        self.assertEqual(out, "plain answer\n")


class Tui(unittest.IsolatedAsyncioTestCase):
    async def test_answer_tab_renders_the_guide(self):
        from textual.widgets import Input

        from game_guide.app import GameGuide

        def fake_ask(backend, question, session=None):
            yield backends.Event("done", "raw", "s1", Guide.model_validate(PAYLOAD))

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch("game_guide.conversation.ask", fake_ask):
            Path(d, "GAMES.md").write_text(GAMES)
            app = GameGuide()
            async with app.run_test() as pilot:
                app.query_one("#question", Input).value = "how do I beat her?"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                answer = app.query_one("#answer")._markdown
        self.assertIn(PAYLOAD["summary"], answer)
        self.assertIn("▶ 4:12", answer)


if __name__ == "__main__":
    unittest.main()
