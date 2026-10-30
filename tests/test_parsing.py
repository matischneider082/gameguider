import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from game_guide import notes

ROOT = Path(__file__).resolve().parents[1]
VTT_SCRIPT = ROOT / "skills/game-guide/scripts/vtt_to_text.py"


def load_convert():
    import importlib.util
    spec = importlib.util.spec_from_file_location("vtt_to_text", VTT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.convert


def cue(start, *lines):
    return f"00:00:{start:02d}.000 --> 00:00:{start + 2:02d}.000\n" + "\n".join(lines) + "\n\n"


class Vtt(unittest.TestCase):
    def test_float_from_and_to(self):
        vtt = "WEBVTT\n\n" + cue(10, "intro") + cue(20, "inside") + cue(50, "outside")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "a.vtt")
            path.write_text(vtt)
            out = subprocess.run([sys.executable, str(VTT_SCRIPT), str(path), "--from", "15.0", "--to", "40.5"],
                                 capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "[0:20] inside"), out.stderr)

    def test_repeat_two_cues_apart_is_kept(self):
        convert = load_convert()
        out = convert("WEBVTT\n\n" + cue(1, "dodge left") + cue(3, "then roll") + cue(5, "dodge left"), every=1)
        self.assertEqual(out, "[0:01] dodge left\n[0:03] then roll\n[0:05] dodge left")

    def test_rolling_repeat_in_next_cue_is_dropped(self):
        convert = load_convert()
        out = convert("WEBVTT\n\n" + cue(1, "dodge left") + cue(3, "dodge left", "then roll"), every=1)
        self.assertEqual(out, "[0:01] dodge left\n[0:03] then roll")


class Notes(unittest.TestCase):
    def test_youtube_start_formats(self):
        for tail, at in [("&t=95s", 95), ("?t=1m35s", 95), ("?t=1h2m3s", 3723), ("?t=95.", 95), ("?t=95,", 95), ("", 0)]:
            with self.subTest(tail=tail):
                [v] = notes.videos(f"- clip: https://youtu.be/abcdefghijk{tail}" if "&" not in tail else
                                   f"- clip: https://www.youtube.com/watch?v=abcdefghijk{tail}")
                self.assertEqual((v.id, v.at, v.label), ("abcdefghijk", at, "clip"))

    def test_youtube_in_parentheses_and_other_hosts(self):
        [a, b, c] = notes.videos("see (https://youtu.be/abcdefghijk?t=95), then\n"
                                 "https://m.youtube.com/watch?v=abcdefghijk&t=7s\n"
                                 "https://youtube.com/shorts/abcdefghijk")
        self.assertEqual((a.at, b.at, c.at), (95, 7, 0))

    def test_slugify_folds_accents_and_apostrophes(self):
        self.assertEqual([notes.slugify(n) for n in ("Pokémon Scarlet", "Marvel's Spider-Man 2", "Marvel’s Spider-Man 2",
                                                     "Hollow Knight: Silksong")],
                         ["pokemon-scarlet", "marvels-spider-man-2", "marvels-spider-man-2", "hollow-knight-silksong"])

    def test_images_with_title_and_obsidian_embed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve() / "notes"  # resolve(): notes.images joins and resolves against the real folder
        got = notes.images('![a](p/one.png "a title")\n![[two.png]]\n![[three.png|300]]\n![r](https://x.io/y.png)', base)
        self.assertEqual(got, [("a", base / "p" / "one.png"), ("two.png", base / "two.png"),
                               ("three.png", base / "three.png")])


if __name__ == "__main__":
    unittest.main()
