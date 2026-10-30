"""The Renderer interface: the notes Markdown dialect as HTML, under the web page's policy or the share export's."""

import html
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from game_guide import render as renderer
from game_guide.notes import HIDDEN_NOTE
from game_guide.render import Policy, render


class Attrs(HTMLParser):
    """Every (tag, attribute, value) in a document, values decoded the way a browser would."""

    def __init__(self, text):
        super().__init__()
        self.found = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.found += [(tag, name, value or "") for name, value in attrs]


def placeholders(out):
    return [(i, int(at), html.unescape(label)) for i, at, label in re.findall(r'data-video="([^"]*)" data-at="(\d+)" data-label="([^"]*)"', out)]


class RenderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        (self.base / "images" / "hk").mkdir(parents=True)
        (self.base / "images" / "hk" / "map.png").write_bytes(b"PNG")
        (self.base / "images" / "hk" / "village map.png").write_bytes(b"PNG")

    def test_a_youtube_source_line_becomes_a_video_placeholder(self):
        cases = {
            "- Mantis Lords dodge: https://youtu.be/Bc-UtkWMuus?t=1h2m3s": ("Bc-UtkWMuus", 3723, "Mantis Lords dodge"),
            "- Climb route: https://www.youtube.com/watch?v=abcdefghijk&t=95s": ("abcdefghijk", 95, "Climb route"),
            "- Dodge: https://youtu.be/abcdefghijk?t=90.": ("abcdefghijk", 90, "Dodge"),
            "- Parry: https://youtube.com/shorts/abcdefghijk": ("abcdefghijk", 0, "Parry"),
            "- [1:03](https://m.youtube.com/watch?v=abcdefghijk&t=63) Boss \"fight\"": ("abcdefghijk", 63, 'Boss "fight"'),
            "https://youtu.be/abcdefghijk": ("abcdefghijk", 0, "abcdefghijk"),
        }
        for line, expected in cases.items():
            with self.subTest(line):
                out = render(f"Before.\n{line}\n- After\n", self.base)
                self.assertEqual(placeholders(out), [expected])
                self.assertIn("<p>Before.</p>", out)  # the line interrupts the paragraph and is not a bullet
                self.assertEqual(out.count("<li>"), 1)

    def test_a_video_named_mid_paragraph_keeps_its_link_and_gets_a_placeholder_after_it(self):
        out = render("See https://youtu.be/abcdefghijk?t=30 for the fight. It shows phase two clearly.\n", self.base)
        self.assertIn('<a href="https://youtu.be/abcdefghijk?t=30" target="_blank" rel="noopener noreferrer">', out)
        self.assertEqual(placeholders(out), [("abcdefghijk", 30, "See for the fight. It shows phase two clearly.")])
        self.assertLess(out.index("</p>"), out.index("data-video"))

    def test_wiki_and_markdown_images_map_to_confined_images_urls(self):
        out = render("![Village](images/hk/map.png)\n\n![[images/hk/village map.png|Wiki map]]\n\n![[images/hk/map.png]]\n", self.base)
        imgs = [(name, value) for tag, name, value in Attrs(out).found if tag == "img" and name in ("src", "alt")]
        self.assertEqual(imgs, [
            ("src", "/images/hk/map.png"), ("alt", "Village"),
            ("src", "/images/hk/village%20map.png"), ("alt", "Wiki map"),
            ("src", "/images/hk/map.png"), ("alt", "map.png"),
        ])
        self.assertEqual(out.count('<figure class="guide-figure">'), 3)

    def test_images_outside_images_remote_missing_or_not_images_are_unavailable(self):
        (self.base / "secret.png").write_bytes(b"SECRET")
        (self.base / "images" / "hk" / "notes.txt").write_text("text", encoding="utf-8")
        for text in (
            "![Secret](images/../secret.png)",
            "![[../secret.png|Secret]]",
            "![[images/hk/../../secret.png|Secret]]",
            f"![[{self.base / 'secret.png'}|Secret]]",
            "![Secret](https://example.com/images/hk/map.png)",
            "![Secret](images/hk/missing.png)",
            "![Secret](images/hk/notes.txt)",
        ):
            for policy in (None, Policy(share=True)):
                with self.subTest(text, share=bool(policy)):
                    out = render(text, self.base, policy)
                    self.assertIn("[Image unavailable: Secret]", out)
                    self.assertNotIn("<img", out)
                    self.assertNotIn("U0VDUkVU", out)

    def test_the_inline_handler_injection_payload_stays_inert(self):
        missing = render("![[x');alert(1);('|Map]]", self.base)
        self.assertIn("[Image unavailable: Map]", missing)
        (self.base / "images" / "x');alert(1);('.png").write_bytes(b"PNG")
        present = render("![[images/x');alert(1);('.png|Map');alert(2);(']]", self.base)
        for out in (missing, present):
            self.assertNotIn("onclick", out)
            self.assertNotIn("javascript:", out)
            self.assertFalse([name for _, name, _ in Attrs(out).found if name.startswith("on")])
        self.assertNotIn("'", present)  # every quote is escaped...
        self.assertIn(("img", "alt", "Map');alert(2);('"), Attrs(present).found)  # ...inside an attribute value

    def test_raw_html_in_notes_stays_escaped(self):
        out = render("<script>alert(1)</script>\n\nHi <img src=x onerror=alert(1)>\n", self.base)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out)
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)

    def test_the_spoiler_placeholder_line_becomes_a_reveal_carrying_its_level(self):
        text = "#### Strategy\n\n" + HIDDEN_NOTE.format(level="Strategy") + "\n"
        out = render(text, self.base)
        self.assertIn('<div class="redacted-box" data-reveal="strategy">', out)
        self.assertIn('<div class="level-heading level-strategy">Strategy</div>', out)
        shared = render(text, self.base, Policy(share=True))
        self.assertIn("<h4>Strategy</h4>", shared)
        self.assertIn("<p><em>Strategy omitted from this shared guide.</em></p>", shared)
        self.assertNotIn("raise the spoiler level", shared)

    def test_share_policy_inlines_images_and_links_videos_within_the_byte_caps(self):
        text = "![Map](images/hk/map.png)\n\n- Dodge: https://youtu.be/abcdefghijk?t=90\n\n[Wiki](https://example.com)\n"
        out = render(text, self.base, Policy(share=True))
        self.assertIn('<img src="data:image/png;base64,UE5H" alt="Map" />', out)
        self.assertIn('<p><a href="https://youtu.be/abcdefghijk?t=90">Dodge</a></p>', out)
        self.assertIn('<a href="https://example.com">Wiki</a>', out)
        self.assertNotIn("data-", out.replace("data:image", ""))
        with mock.patch.object(renderer, "MAX_IMAGE_BYTES", 2), self.assertRaises(ValueError):
            render(text, self.base, Policy(share=True))
        policy = Policy(share=True)
        with mock.patch.object(renderer, "MAX_TOTAL_IMAGE_BYTES", 5):
            render(text, self.base, policy)
            with self.assertRaises(ValueError):  # the total spans every render() given the same policy
                render(text, self.base, policy)


if __name__ == "__main__":
    unittest.main()
