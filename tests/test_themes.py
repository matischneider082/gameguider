"""Themes: complete palettes that stay readable (WCAG AA), a closed list of names, and the page served in the saved one."""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from game_guide import settings, themes, web

ROOT = re.search(r":root \{(.*?)\n  \}", web.HTML_PAGE, re.S).group(1)
DARK = dict(re.findall(r"--([\w-]+):\s*([^;]+);", ROOT))
COLORS = {k for k in DARK if not k.startswith(("radius", "font"))}


def luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class PaletteTests(unittest.TestCase):
    def palettes(self):
        return {name: {**DARK, **colors} for name, (_label, _scheme, colors) in themes.THEMES.items()}

    def test_every_theme_but_the_default_recolors_every_color(self):
        for name, (_label, _scheme, colors) in themes.THEMES.items():
            if name != themes.DEFAULT:
                self.assertEqual(set(colors), COLORS, name)  # a missing one would show the dark palette's color

    def test_text_stays_readable_in_every_theme(self):
        pairs = [(fg, bg) for fg in ("text", "text-secondary", "muted", "accent", "green", "blue", "plum", "danger")
                 for bg in ("bg", "surface-1", "surface-2")] + [("text", "surface-3"), ("on-accent", "accent")]
        for name, palette in self.palettes().items():
            for fg, bg in pairs:
                with self.subTest(theme=name, pair=f"{fg} on {bg}"):
                    self.assertGreaterEqual(contrast(palette[fg].strip(), palette[bg].strip()), 4.5)

    def test_the_css_applies_each_theme_and_auto_follows_the_system(self):
        css = themes.css()
        self.assertNotIn('data-theme="dark"', css)  # the default is the page's own :root
        self.assertIn(':root[data-theme="light"] { color-scheme: light;', css)
        self.assertIn('@media (prefers-color-scheme: light) { :root[data-theme="auto"]', css)
        self.assertIn(css, web.HTML_PAGE)


class IsolatedConfig(unittest.TestCase):
    """Settings in a temp folder, never the real config."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)


class SavedThemeTests(IsolatedConfig):
    def test_saving_keeps_other_settings_and_refuses_unknown_names(self):
        self.assertEqual(themes.current(), "dark")
        settings.save("notes_dir", "/tmp/notes")
        self.assertEqual(themes.save("light"), "light")
        self.assertEqual(settings.load(), {"notes_dir": "/tmp/notes", "theme": "light"})
        for bad in ("neon", "", '"><script>alert(1)</script>'):
            with self.assertRaises(ValueError):
                themes.save(bad)
        self.assertEqual(themes.current(), "light")

    def test_an_edited_config_falls_back_to_the_default(self):
        settings.config_path().parent.mkdir(parents=True, exist_ok=True)
        settings.config_path().write_text(json.dumps({"theme": '"><script>alert(1)</script>'}))
        self.assertEqual(themes.current(), "dark")


class ServedThemeTests(IsolatedConfig):
    def setUp(self):
        super().setUp()
        self.server = web.start_server()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None):
        import http.client
        conn = http.client.HTTPConnection(*self.server.server_address[:2], timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            resp = conn.getresponse()
            return resp.status, resp.read().decode()
        finally:
            conn.close()

    def test_the_page_is_served_in_the_saved_theme(self):
        self.assertIn('<html lang="en" data-theme="dark">', self.request("GET", "/")[1])
        self.assertEqual(self.request("POST", "/api/theme", {"theme": "contrast"}), (200, '{"theme": "contrast"}'))
        self.assertIn('<html lang="en" data-theme="contrast">', self.request("GET", "/")[1])
        status, body = self.request("POST", "/api/theme", {"theme": "neon"})
        self.assertEqual((status, json.loads(body)["error"]), (400, "Unknown theme."))
        self.assertIn('data-theme="contrast"', self.request("GET", "/")[1])

    def test_a_tampered_theme_never_reaches_the_page(self):
        settings.save("theme", '"><script>alert(1)</script>')
        page = self.request("GET", "/")[1]
        self.assertIn('<html lang="en" data-theme="dark">', page)
        self.assertNotIn("alert(1)", page)


if __name__ == "__main__":
    unittest.main()
