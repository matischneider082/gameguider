"""The macOS launcher's in-app browser blocklist: WebKit content-blocker rules built from AD_DOMAINS."""

import json
import re
import unittest

from game_guide import desktop


class ContentRulesTests(unittest.TestCase):
    def setUp(self):
        self.rules = json.loads(desktop.content_rules())
        self.filters = [r["trigger"]["url-filter"] for r in self.rules if r["action"]["type"] == "block"]

    def blocked(self, url):
        # WebKit's url-filter is a regex subset that Python's re reads the same way for these patterns.
        return any(re.search(f, url) for f in self.filters)

    def test_ad_networks_and_their_subdomains_are_blocked(self):
        self.assertEqual(len(self.filters), len(desktop.AD_DOMAINS))
        for url in ("https://securepubads.g.doubleclick.net/tag/js/gpt.js", "https://pagead2.googlesyndication.com/x.js",
                    "http://cdn.taboola.com:443/libtrc/loader.js", "https://amazon-adsystem.com/aax2/apstag.js"):
            self.assertTrue(self.blocked(url), url)

    def test_guide_sites_and_lookalike_domains_load(self):
        for url in ("https://game8.co/games/Elden-Ring/archives/123", "https://www.ign.com/wikis/hollow-knight",
                    "https://notdoubleclick.net/", "https://example.com/?next=https://doubleclick.net/",
                    "https://www.youtube.com/watch?v=abcdefghijk"):
            self.assertFalse(self.blocked(url), url)

    def test_leftover_ad_slots_are_hidden(self):
        hiding = [r for r in self.rules if r["action"]["type"] == "css-display-none"]
        self.assertEqual(len(hiding), 1)
        self.assertIn("ins.adsbygoogle", hiding[0]["action"]["selector"])

    def test_the_rules_embed_in_the_swift_raw_string(self):
        self.assertEqual(desktop.SWIFT_LAUNCHER.count('#"__CONTENT_RULES__"#'), 1)
        self.assertNotIn('"#', desktop.content_rules())  # would end Swift's #"..."# literal early


if __name__ == "__main__":
    unittest.main()
