"""The Conversation interface: sessions per backend, a newer question superseding an older one, reset, the
user-facing error wording, and a walkthrough's artwork hooks. ask is faked at its one patch point."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from game_guide.backends import Event, Guide, render
from game_guide.conversation import FAILED_MESSAGE, PLANTED_MESSAGE, Conversation, Done, Error, Status, Text
from game_guide.notes import Game


def patch_ask(fake):
    return mock.patch("game_guide.conversation.ask", fake)


def replies(*events):
    return lambda *_: (e for e in events)  # a generator, as ask is


class Sessions(unittest.TestCase):
    def test_a_session_is_stored_and_reused_per_backend(self):
        calls = []

        def fake(backend, question, session=None):
            calls.append((backend, session))
            yield Event("session", session=f"{backend}-{len(calls)}")
            yield Event("done", "ok")

        conversation = Conversation({"codex": "resumed"})
        with patch_ask(fake):
            *_, first = conversation.ask("claude", "q", "hint")
            list(conversation.ask("claude", "q", "hint"))
            list(conversation.ask("codex", "q", "hint"))
        self.assertEqual(calls, [("claude", None), ("claude", "claude-1"), ("codex", "resumed")])
        self.assertEqual(first.session, "claude-1")  # the done event carried none, so the stored one
        self.assertEqual(conversation.sessions, {"claude": "claude-2", "codex": "codex-3"})

    def test_reset_clears_sessions_and_stops_the_running_question(self):
        closed = []

        def fake(backend, question, session=None):
            try:
                yield Event("session", session="s1")
                yield Event("status", "searching")
                yield Event("done", "late answer", "s1")
            except GeneratorExit:
                closed.append(True)
                raise

        conversation = Conversation()
        with patch_ask(fake):
            running = conversation.ask("claude", "q", "hint")
            self.assertEqual(next(running), Status("searching"))
            conversation.reset()
            self.assertEqual(list(running), [])
        self.assertEqual(closed, [True])
        self.assertEqual(conversation.sessions, {})


class Superseding(unittest.TestCase):
    def test_a_newer_question_closes_an_idle_older_one(self):
        closed = []

        def fake(backend, question, session=None):
            name = question.split()[0]  # the question comes framed with the spoiler level
            try:
                yield Event("session", session=name)
                yield Event("text", f"{name} draft")
                yield Event("done", f"{name} answer", name)
            except GeneratorExit:
                closed.append(name)
                raise

        conversation = Conversation()
        with patch_ask(fake):
            older = conversation.ask("claude", "first", "hint")
            self.assertEqual(next(older), Text("first draft"))
            newer = list(conversation.ask("claude", "second", "hint"))
            self.assertEqual(list(older), [])
        self.assertEqual(closed, ["first"])
        self.assertEqual(newer[-1].markdown, "second answer")
        self.assertEqual(conversation.sessions, {"claude": "second"})

    def test_an_older_question_running_on_another_thread_stops_at_its_next_event(self):
        started, release = threading.Event(), threading.Event()
        closed, older_updates = [], []

        def fake(backend, question, session=None):
            if question.startswith("first"):
                try:
                    yield Event("session", session="old")
                    started.set()
                    release.wait(5)  # still inside the backend: closing it from the other thread fails
                    yield Event("done", "first answer", "old")
                except GeneratorExit:
                    closed.append(True)
                    raise
                return
            yield Event("session", session="new")
            yield Event("done", "second answer", "new")

        conversation = Conversation()
        with patch_ask(fake):
            older = threading.Thread(target=lambda: older_updates.extend(conversation.ask("claude", "first", "hint")))
            older.start()
            self.assertTrue(started.wait(5))
            *_, newer = conversation.ask("claude", "second", "hint")
            release.set()
            older.join(5)
        self.assertEqual(newer.markdown, "second answer")
        self.assertEqual(older_updates, [])
        self.assertEqual(closed, [True])
        self.assertEqual(conversation.sessions, {"claude": "new"})


class Outcomes(unittest.TestCase):
    def test_the_planted_files_refusal_gives_its_reason_and_message(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            Path(d, "CLAUDE.md").write_text("planted")
            (error,) = Conversation().ask("claude", "q", "hint")  # the real ask, which refuses before running
        self.assertEqual((error.reason, error.message), ("planted", PLANTED_MESSAGE))
        self.assertIn("CLAUDE.md", error.detail)

    def test_other_errors_give_the_generic_message_with_the_detail(self):
        with patch_ask(replies(Event("error", "rate limit exceeded"))):
            updates = list(Conversation().ask("codex", "q", "hint"))
        self.assertEqual(updates, [Error("failed", FAILED_MESSAGE, "rate limit exceeded")])

    def test_done_renders_the_guide_or_falls_back_to_the_text(self):
        guide = Guide(kind="guide", game="Hollow Knight", spoiler="hint", summary="Parry the dash.")
        with patch_ask(replies(Event("done", "raw", guide=guide))):
            (with_guide,) = Conversation().ask("claude", "q", "hint")
        with patch_ask(replies(Event("done", "plain answer"))):
            (plain,) = Conversation().ask("claude", "q", "hint")
        self.assertEqual((with_guide.markdown, with_guide.guide, with_guide.text), (render(guide), guide, "raw"))
        self.assertEqual(plain, Done("plain answer", None, None, "plain answer"))


class Walkthrough(unittest.TestCase):
    def test_the_artwork_hooks_run_even_on_error_and_never_raise(self):
        questions = []
        game = Game("Hollow Knight")
        for last in (Event("done", "built"), Event("error", "codex failed")):
            def fake(backend, question, session=None, last=last):
                questions.append(question)
                yield Event("status", "researching")
                yield last

            with patch_ask(fake), \
                    mock.patch("game_guide.conversation.fetch_game_artwork", side_effect=OSError("offline")) as fetch, \
                    mock.patch("game_guide.conversation.ensure_walkthrough_artwork", side_effect=ValueError) as ensure:
                updates = list(Conversation().walkthrough("claude", game, "answer"))
            self.assertEqual(updates[0], Status("researching"))
            self.assertIsInstance(updates[-1], Done if last.kind == "done" else Error)
            fetch.assert_called_once_with(game)
            ensure.assert_called_once_with(game)
        self.assertIn("full walkthrough for Hollow Knight, with all three spoiler levels", questions[0])
        self.assertIn("Spoiler level: answer.", questions[0])


if __name__ == "__main__":
    unittest.main()
