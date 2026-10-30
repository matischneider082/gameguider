"""Cancelling superseded work: questions, their CLI processes, media downloads; plus markup-safe labels."""

import asyncio
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from game_guide import backends, media
from game_guide.notes import Video

GAMES = """# Games

| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Persona 5 [Royal] | PS5 | jrpg | Playing | Palace 1 | 2026-10-23 |
"""


def fake_cli(backend, script):
    return mock.patch.object(backends, f"{backend}_command", return_value=[sys.executable, "-c", script])


class Ask(unittest.TestCase):
    def setUp(self):
        notes = tempfile.TemporaryDirectory()
        self.addCleanup(notes.cleanup)
        patch = mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": notes.name})
        patch.start()
        self.addCleanup(patch.stop)

    def test_failed_result_is_the_only_error(self):
        script = ('import json, sys\n'
                  'print(json.dumps({"type": "result", "is_error": True, "result": "rate limited"}))\n'
                  'sys.stderr.write("noise")\n'
                  'raise SystemExit(1)\n')
        with fake_cli("claude", script):
            got = [(e.kind, e.text) for e in backends.ask("claude", "q")]
        self.assertEqual(got, [("error", "rate limited")])

    def test_exit_code_is_reported_when_the_cli_said_nothing(self):
        with fake_cli("codex", 'import sys; sys.stderr.write("boom"); raise SystemExit(2)'):
            got = [(e.kind, e.text) for e in backends.ask("codex", "q")]
        self.assertEqual(got, [("error", "boom")])

    @unittest.skipUnless(os.name == "posix", "POSIX process probing")
    def test_closing_early_terminates_the_cli(self):
        script = ('import json, os, time\n'
                  'print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": str(os.getpid())}}),'
                  ' flush=True)\n'
                  'time.sleep(20)\n')
        with fake_cli("codex", script):
            events = backends.ask("codex", "q")
            pid = int(next(events).text)
            start = time.monotonic()
            events.close()
        self.assertLess(time.monotonic() - start, 5)
        with self.assertRaises(ProcessLookupError):  # terminated and reaped
            os.kill(pid, 0)

    @unittest.skipUnless(os.name == "posix", "POSIX process probing")
    def test_closing_early_kills_the_clis_children_too(self):
        script = ('import json, subprocess, sys\n'
                  'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])\n'
                  'print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": str(child.pid)}}),'
                  ' flush=True)\n'
                  'child.wait()\n')
        with fake_cli("codex", script):
            events = backends.ask("codex", "q")
            grandchild = int(next(events).text)
            events.close()
        for _ in range(50):  # init reaps the orphan a moment after the kill
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(grandchild, 9)
        self.fail("grandchild survived")

    def test_the_cli_gets_no_stdin(self):
        script = ('import json, sys\n'
                  'print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": repr(sys.stdin.read())}}))\n')
        with fake_cli("codex", script):
            self.assertEqual(next(backends.ask("codex", "q")).text, "''")


class Download(unittest.TestCase):
    def setUp(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.cache = Path(cache.name)
        patch = mock.patch.object(media, "CACHE", self.cache)
        patch.start()
        self.addCleanup(patch.stop)
        self.video = Video("abcdefghijk", 10, "x")
        self.calls = []

    def fake_yt_dlp(self, cmd, timeout):
        self.calls.append(cmd)
        time.sleep(0.2)  # long enough for a second caller to arrive mid-download
        final = self.cache / f"{self.video.id}.video.mp4"
        final.write_bytes(b"video")
        return f"{final}\n"

    def test_partial_files_are_not_cache_hits(self):
        for leftover in ("webm.part", "f137.mp4", "temp.mp4"):
            (self.cache / f"{self.video.id}.video.{leftover}").write_bytes(b"partial")
        with mock.patch.object(media.procs, "run", self.fake_yt_dlp):
            self.assertEqual(media.download(self.video).name, f"{self.video.id}.video.mp4")
            self.assertEqual(media.download(self.video).name, f"{self.video.id}.video.mp4")
        self.assertEqual(len(self.calls), 1)  # the finished file is the hit the second time

    def test_concurrent_downloads_of_one_video_run_once(self):
        with mock.patch.object(media.procs, "run", self.fake_yt_dlp):
            threads = [threading.Thread(target=media.download, args=(self.video,)) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(self.calls), 1)

    @unittest.skipUnless(os.name == "posix", "a /bin/sh ffmpeg stub")
    def test_ffmpeg_errors_are_captured_for_the_status_line(self):
        bin_dir = self.cache / "bin"
        bin_dir.mkdir()
        ffmpeg = bin_dir / "ffmpeg"
        ffmpeg.write_text('#!/bin/sh\necho "Invalid data found when processing input" >&2\nexit 1\n')
        ffmpeg.chmod(0o755)
        with mock.patch.object(media, "download", return_value=self.cache / "v.mp4"), \
                mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}), \
                self.assertRaises(subprocess.CalledProcessError) as caught:
            media.frames(self.video)
        self.assertEqual(media.short_error(caught.exception), "Invalid data found when processing input")

    def test_a_frame_past_the_end_of_the_video_drops_only_that_frame(self):
        def ffmpeg(cmd, timeout):
            if cmd[cmd.index("-ss") + 1] == "12":  # past the end: ffmpeg writes nothing and fails
                raise subprocess.CalledProcessError(234, cmd, "", "Nothing was written into output file")
            Path(cmd[-1]).write_bytes(b"jpg")
        with mock.patch.object(media, "download", return_value=self.cache / "v.mp4"), \
                mock.patch.object(media.procs, "run", ffmpeg):
            self.assertEqual([p.name for p in media.frames(self.video)], ["abcdefghijk-8.jpg", "abcdefghijk-10.jpg"])

    def test_yt_dlp_ignores_user_config(self):
        with mock.patch.object(media.procs, "run", self.fake_yt_dlp):
            media.download(self.video)
        self.assertIn("--ignore-config", self.calls[0])

    def test_an_interrupted_thumbnail_is_not_cached(self):
        class Dropped(io.BytesIO):
            def read(self, *args):
                raise OSError("connection reset")
        with mock.patch.object(media.urllib.request, "urlopen", return_value=Dropped()):
            self.assertRaises(OSError, media.thumbnail, self.video)
        self.assertEqual(list(self.cache.iterdir()), [])
        with mock.patch.object(media.urllib.request, "urlopen", return_value=io.BytesIO(b"jpg")):
            self.assertEqual(media.thumbnail(self.video).read_bytes(), b"jpg")
        self.assertEqual([p.name for p in self.cache.iterdir()], ["abcdefghijk.jpg"])


    def test_no_frames_written_is_an_error_not_a_crash(self):
        with mock.patch.object(media, "download", return_value=self.cache / "v.mp4"), \
                mock.patch.object(media.procs, "run", lambda cmd, timeout: ""):  # exits 0, writes nothing
            self.assertRaisesRegex(RuntimeError, "past the end", media.frames, self.video)

    def test_a_killed_ffmpeg_stops_the_frame_loop(self):
        calls = []
        def killed(cmd, timeout):
            calls.append(cmd)
            raise subprocess.CalledProcessError(-15, cmd)
        with mock.patch.object(media, "download", return_value=self.cache / "v.mp4"), \
                mock.patch.object(media.procs, "run", killed):
            self.assertRaises(subprocess.CalledProcessError, media.frames, self.video)
        self.assertEqual(len(calls), 1)  # the next ffmpeg would start untracked while the app quits


class ProcsTreeKill(unittest.TestCase):
    def test_kill_tree_reaches_a_process_with_children_of_its_own(self):
        """The tree kill really ends a process that has children, on this platform."""
        from game_guide import procs
        cmd = ["cmd", "/c", "ping -n 30 127.0.0.1 > nul"] if procs.WIN else ["sh", "-c", "sleep 30 | cat"]
        proc = procs.start(cmd)
        try:
            self.assertIsNone(proc.poll())  # running
            procs.kill_tree(proc.pid)
            self.assertIsNotNone(proc.wait(timeout=10))  # killed, on POSIX and Windows alike
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            procs.untrack(proc)


@unittest.skipUnless(os.name == "posix", "sleep and POSIX exit codes")
class Procs(unittest.TestCase):
    def test_a_process_started_by_a_cancelled_worker_dies_at_once(self):
        from game_guide import procs
        with mock.patch.object(procs, "worker_cancelled", return_value=True):  # e.g. the next ffmpeg as the app quits
            late = procs.start(["sleep", "30"])
        self.assertEqual(late.wait(timeout=5), -15)
        procs.untrack(late)
        self.assertFalse(procs.worker_cancelled())  # outside a worker (the CLI) nothing is killed

    def test_stop_all_survives_an_unreaped_zombie_and_kills_the_rest(self):
        from game_guide import procs
        zombie = procs.start(["true"])
        time.sleep(0.3)  # exited but never waited on: killpg on its group raises EPERM on macOS
        sleeper = procs.start(["sleep", "30"])
        try:
            procs.stop_all()
            self.assertEqual(sleeper.wait(timeout=5), -15)
        finally:
            for proc in (zombie, sleeper):
                proc.kill() if proc.poll() is None else None
                proc.wait()
                procs.untrack(proc)


@unittest.skipUnless(os.name == "posix", "sleep and POSIX exit codes")
class Quit(unittest.IsolatedAsyncioTestCase):
    async def test_quitting_kills_a_running_download_instead_of_waiting(self):
        from textual import work

        from game_guide import procs
        from game_guide.app import GameGuide

        class App(GameGuide):
            @work(thread=True)
            def slow(self):
                procs.run(["sleep", "30"], timeout=60)  # stands in for a long yt-dlp download

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            app = App()
            start = time.monotonic()
            async with app.run_test() as pilot:
                app.slow()
                await pilot.pause(0.5)
                self.assertEqual(len(procs.RUNNING), 1)
            self.assertLess(time.monotonic() - start, 10)  # used to block until sleep 30 finished
            self.assertEqual(procs.RUNNING, set())

    async def test_a_process_started_while_quitting_is_killed_too(self):
        from textual import work
        from textual.worker import get_current_worker

        from game_guide import procs
        from game_guide.app import GameGuide

        class App(GameGuide):
            @work(thread=True)
            def late(self):
                while not get_current_worker().is_cancelled:  # like frames() between two ffmpeg runs
                    time.sleep(0.05)
                time.sleep(0.2)  # after stop_all has taken its list
                procs.run(["sleep", "30"], timeout=60)

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            app = App()
            async with app.run_test() as pilot:
                app.late()
                await pilot.pause(0.2)
            await asyncio.sleep(1.5)  # the worker starts its process 0.2 s into quitting
            alive = [p for p in procs.RUNNING if p.poll() is None]
            procs.stop_all()  # don't leave a sleep behind if this fails
            self.assertEqual(alive, [])  # used to keep running for the whole sleep 30, holding up the exit


class Tui(unittest.IsolatedAsyncioTestCase):
    async def test_second_question_cancels_the_first(self):
        from textual.widgets import Input

        from game_guide.app import GameGuide
        started, release, ended, second_done = (threading.Event() for _ in range(4))
        closed, calls = [], []

        def fake_ask(backend, question, session=None):
            calls.append(question)
            if len(calls) == 1:
                try:
                    yield backends.Event("session", session="old")
                    started.set()
                    release.wait(5)
                    yield backends.Event("done", "first answer", "old")
                except GeneratorExit:
                    closed.append(True)
                    raise
                finally:
                    ended.set()
                return
            yield backends.Event("session", session="new")
            yield backends.Event("done", "second answer", "new")
            second_done.set()

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch("game_guide.conversation.ask", fake_ask):
            Path(d, "GAMES.md").write_text(GAMES)
            app = GameGuide()
            async with app.run_test() as pilot:
                app.query_one("#question", Input).value = "first?"
                await pilot.press("enter")
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                app.query_one("#question", Input).value = "second?"
                await pilot.press("enter")
                self.assertTrue(await asyncio.to_thread(second_done.wait, 5))
                await pilot.pause()
                release.set()  # the first run resumes only after the second has finished
                self.assertTrue(await asyncio.to_thread(ended.wait, 5))
                await app.workers.wait_for_complete()
                await pilot.pause()
                answer = app.query_one("#answer")._markdown
                sessions = app.conversation.sessions
        self.assertIn("second answer", answer)
        self.assertNotIn("first answer", answer)
        self.assertEqual(closed, [True])
        self.assertEqual(sessions, {"claude": "new"})

    async def test_new_chat_during_a_run_is_not_undone(self):
        from textual.widgets import Input

        from game_guide.app import GameGuide
        started, release = threading.Event(), threading.Event()

        def fake_ask(backend, question, session=None):
            yield backends.Event("session", session="s1")
            started.set()
            release.wait(5)
            yield backends.Event("done", "answer", "s1")

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch("game_guide.conversation.ask", fake_ask):
            Path(d, "GAMES.md").write_text(GAMES)
            app = GameGuide()
            async with app.run_test() as pilot:
                app.query_one("#question", Input).value = "q?"
                await pilot.press("enter")
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                await pilot.press("ctrl+n")
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertNotIn("answer", app.query_one("#answer")._markdown)  # ctrl+n cancels the running question
                self.assertIn("stopped", app.query_one("#answer")._markdown)  # not left on "Working…"
                self.assertEqual(app.conversation.sessions, {})

    async def test_brackets_in_names_and_statuses_are_shown_verbatim(self):
        from textual.widgets import Label, OptionList

        from game_guide.app import GameGuide
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            Path(d, "GAMES.md").write_text(GAMES)
            Path(d, "persona-5-royal.md").write_text("![](images/boss[b].png)\n")  # untitled image: labelled by file name
            app = GameGuide()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.set_status("play failed: ERROR: [youtube] abcdefghijk: Video unavailable")
                await pilot.pause()
                self.assertIn("Persona 5 [Royal]", str(app.query_one("#games Label", Label).render()))
                self.assertIn("[youtube]", str(app.query_one("#status").render()))
                app.query_one("#main").active = "media-tab"
                await pilot.pause()
                sources = app.query_one("#sources", OptionList)
                lines = "".join(s.text for s in sources.render_lines(sources.region.reset_offset))
                self.assertIn("boss[b].png", lines)


class Shutdown(unittest.IsolatedAsyncioTestCase):
    async def test_a_worker_finishing_after_the_widgets_are_gone_does_not_crash(self):
        from textual import work

        from game_guide.app import GameGuide

        class App(GameGuide):
            @work(thread=True)
            def late(self):
                self.from_worker(self.set_status, "thumbnail failed")

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            app = App()
            async with app.run_test() as pilot:
                await app.query_one("#status").remove()  # as when the app is closing
                await app.late().wait()
                await pilot.pause()  # used to fail the app with NoMatches('#status')


if __name__ == "__main__":
    unittest.main()
