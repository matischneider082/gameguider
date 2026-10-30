"""Tests for the local Web UI server, ad-free video/image endpoints, streaming ask/walkthrough, and CLI/TUI integration."""

from __future__ import annotations

import http.client
import io
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from game_guide import actions, backends, conversation, desktop, library, media, scores, updates, web

GAMES = """# Games

| Game | Platform | Genre | Status | Spot | Updated |
|---|---|---|---|---|---|
| Hollow Knight | Switch | metroidvania | Playing | Mantis Lords | 2026-10-28 |
"""

NOTES = """# Hollow Knight

## Current
At Mantis Village.

#### Hint
Look for the lever near the bench.
![Village Map](images/hollow-knight/village-map.png)

#### Strategy
Watch the double-dash attack.
- Mantis Lords dodge: https://youtu.be/Bc-UtkWMuus?t=252

#### Answer
Drop into the arena and pogo off the third sister.
![Arena Layout](images/hollow-knight/arena.png)
"""

WALKTHROUGH = """# Hollow Knight Walkthrough

## 1. Forgotten Crossroads

#### Hint
Head down from Dirtmouth.

#### Strategy
Buy the map and compass from Cornifer.

#### Answer
Defeat False Knight by hitting the maggot.

## 2. Fungal Wastes

#### Hint
Follow the bouncy mushrooms downward.

#### Strategy
Use the Mantis Claw to climb the village walls.
![Mantis Claw Wall](images/hollow-knight/mantis-claw.png)
- Climb route: https://youtu.be/abcdefghijk?t=90

#### Answer
Defeat the Mantis Lords at the bottom of Mantis Village.
"""


class WebServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.games_dir = Path(self.tmp.name) / "games"
        self.games_dir.mkdir(parents=True)
        self.cache_dir = Path(self.tmp.name) / "cache"
        self.cache_dir.mkdir(parents=True)

        env_patch = mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": str(self.games_dir)})
        env_patch.start()
        self.addCleanup(env_patch.stop)

        cache_patch = mock.patch.object(media, "CACHE", self.cache_dir)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

        self.fetch_artwork_mock = mock.Mock(return_value=None)  # the cover endpoints' and the walkthrough's fetch
        self.ensure_artwork_mock = mock.Mock(return_value=None)
        for module, name, fake in ((actions, "fetch_game_artwork", self.fetch_artwork_mock),
                                   (conversation, "fetch_game_artwork", self.fetch_artwork_mock),
                                   (conversation, "ensure_walkthrough_artwork", self.ensure_artwork_mock)):
            art_patch = mock.patch.object(module, name, fake)
            art_patch.start()
            self.addCleanup(art_patch.stop)

        (self.games_dir / "GAMES.md").write_text(GAMES, encoding="utf-8")
        (self.games_dir / "hollow-knight.md").write_text(NOTES, encoding="utf-8")
        (self.games_dir / "hollow-knight.walkthrough.md").write_text(WALKTHROUGH, encoding="utf-8")

        img_dir = self.games_dir / "images" / "hollow-knight"
        img_dir.mkdir(parents=True)
        (img_dir / "village-map.png").write_bytes(b"\x89PNG-village-map")
        (img_dir / "mantis-claw.png").write_bytes(b"\x89PNG-mantis-claw")
        (img_dir / "arena.png").write_bytes(b"\x89PNG-arena")

        self.server = web.start_server("127.0.0.1", 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host, self.port = self.server.server_address[:2]

    def request(
        self,
        method: str,
        path: str,
        body: dict | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            req_headers = {"Connection": "close", **(headers or {})}
            payload = None
            if isinstance(body, dict):
                payload = json.dumps(body).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
                req_headers.setdefault("Content-Length", str(len(payload)))
            elif isinstance(body, bytes):
                payload = body
                req_headers.setdefault("Content-Length", str(len(payload)))
            conn.request(method, path, body=payload, headers=req_headers)
            resp = conn.getresponse()
            data = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, data
        finally:
            conn.close()

    def get_json(self, path: str) -> tuple[int, dict]:
        status, _, data = self.request("GET", path)
        return status, json.loads(data.decode("utf-8"))

    def post_json(self, path: str, body: dict) -> tuple[int, dict]:
        status, _, data = self.request("POST", path, body=body)
        return status, json.loads(data.decode("utf-8"))

    def post_ndjson(self, path: str, body: dict) -> tuple[int, list[dict]]:
        status, _, data = self.request("POST", path, body=body)
        lines = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
        return status, lines


class PageScriptTests(unittest.TestCase):
    def test_inline_event_handlers_never_interpolate_template_values(self):
        # The browser decodes entities before running an attribute handler, so no HTML escaping makes ${...} safe there.
        handlers = re.findall(r"""\bon[a-z]+\s*=\s*(?:"[^"]*\$\{[^"]*"|'[^']*\$\{[^']*')""", web.HTML_PAGE)
        self.assertEqual(handlers, [])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_page_script_parses(self):
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", web.HTML_PAGE, re.S)
        self.assertTrue(scripts)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.cjs"
            path.write_text("\n".join(scripts), encoding="utf-8")
            result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class StateAndStaticTests(WebServerTestCase):
    def test_share_download_is_portable_respects_spoilers_and_omits_notes_by_default(self):
        status, headers, data = self.request("GET", "/api/share?game=hollow-knight&spoiler=hint")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["content-disposition"])
        html = data.decode("utf-8")
        self.assertIn("Head down from Dirtmouth", html)
        self.assertNotIn("Buy the map and compass", html)
        self.assertNotIn("At Mantis Village", html)

    def test_share_download_can_include_notes_and_only_embeds_visible_safe_images(self):
        # A private path and a hidden image must not become embedded data.
        (self.games_dir / "secret.png").write_bytes(b"PRIVATE_SECRET")
        note = NOTES + "\n![private](secret.png)\n"
        (self.games_dir / "hollow-knight.md").write_text(note, encoding="utf-8")
        status, _, data = self.request("GET", "/api/share?game=hollow-knight&spoiler=hint&notes=1")
        self.assertEqual(status, 200)
        html = data.decode("utf-8")
        self.assertIn("At Mantis Village", html)
        self.assertIn("iVBORy12aWxsYWdlLW1hcA==", html)  # village map
        self.assertNotIn("iVBORy1hcmVuYQ==", html)  # answer-level arena
        self.assertNotIn("UFJJVkFURV9TRUNSRVQ=", html)

    def test_share_refuses_missing_games_and_unknown_spoiler_level(self):
        self.assertEqual(self.request("GET", "/api/share?game=missing&spoiler=hint")[0], 404)
        self.assertEqual(self.request("GET", "/api/share?game=hollow-knight&spoiler=invalid")[0], 400)

    def test_share_preserves_obsidian_images_and_clickable_video_links_without_executing_html(self):
        text = "# Hollow Knight\n\n## Area\n\n#### Hint\n![[images/hollow-knight/village-map.png|Map]]\n\n- Video: https://youtu.be/abcdefghijk?t=90\n\n<script>alert('bad')</script>\n"
        (self.games_dir / "hollow-knight.walkthrough.md").write_text(text, encoding="utf-8")
        status, _, data = self.request("GET", "/api/share?game=hollow-knight&spoiler=hint")
        self.assertEqual(status, 200)
        html = data.decode("utf-8")
        self.assertIn("iVBORy12aWxsYWdlLW1hcA==", html)
        self.assertIn('href="https://youtu.be/abcdefghijk?t=90"', html)
        self.assertNotIn("<script>", html)

    def test_share_never_embeds_image_symlinks_outside_images(self):
        (self.games_dir / "secret.png").write_bytes(b"PRIVATE_SECRET")
        (self.games_dir / "images" / "hollow-knight" / "leak.png").symlink_to(self.games_dir / "secret.png")
        (self.games_dir / "hollow-knight.walkthrough.md").write_text("# Guide\n\n![Leak](images/hollow-knight/leak.png)\n", encoding="utf-8")
        status, _, data = self.request("GET", "/api/share?game=hollow-knight&spoiler=hint")
        self.assertEqual(status, 200)
        self.assertNotIn(b"UFJJVkFURV9TRUNSRVQ=", data)

    def test_update_check_is_available_from_the_ui(self):
        status, _, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Check for updates", html)
        with mock.patch.object(updates, "check_for_updates", return_value={"status": "current", "message": "Up to date"}):
            status, data = self.post_json("/api/updates/check", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "current")

    def test_root_serves_web_ui_html_with_ad_free_player_and_walkthrough(self):
        status, headers, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        html = data.decode("utf-8")
        self.assertIn("GameGuider", html)
        self.assertNotIn("ad-free", html.lower())
        self.assertIn("Build walkthrough", html)
        self.assertIn("playInlineVideo", html)
        self.assertIn("renderChapterMediaStrip", html)
        self.assertIn("function hydrate(root)", html)
        self.assertIn("openGameModal", html)
        self.assertIn("openBacklogModal", html)
        self.assertIn("moveGame", html)
        self.assertIn("game-card-art", html)
        self.assertIn("setGameSpot", html)
        self.assertIn("window.hydrate = hydrate", html)

    def test_score_refresh_updates_state_only_for_existing_game(self):
        with mock.patch.object(scores, "refresh_score") as refresh:
            status, result = self.post_json("/api/scores/refresh", {"game": "missing"})
            self.assertEqual(status, 404)
            refresh.assert_not_called()
            status, result = self.post_json("/api/scores/refresh", {"game": "hollow-knight"})
            self.assertEqual(status, 200)
            self.assertEqual(result["selected"]["slug"], "hollow-knight")
            refresh.assert_called_once()

    def test_cover_search_is_explicit_and_reports_unavailable_art(self):
        status, result = self.post_json("/api/games/artwork", {"game": "missing"})
        self.assertEqual(status, 404)
        self.fetch_artwork_mock.assert_not_called()
        status, result = self.post_json("/api/games/artwork", {"game": "hollow-knight"})
        self.assertEqual(status, 404)
        self.assertIn("No cover found", result["error"])
        self.fetch_artwork_mock.assert_called_once()

    def test_cached_score_is_visible_in_shelf_and_game_state(self):
        (self.games_dir / ".game-guider-scores.json").write_text(json.dumps({"hollow-knight": {"name": "Hollow Knight", "score": 90.5, "count": 12}}), encoding="utf-8")
        status, result = self.get_json("/api/state?game=hollow-knight")
        self.assertEqual(status, 200)
        expected = {"score": 90.5, "count": 12, "source": "IGDB"}
        self.assertEqual(result["games"][0]["score"], expected)
        self.assertEqual(result["selected"]["score"], expected)

    def test_score_refresh_reports_configuration_error(self):
        with mock.patch.object(scores, "refresh_score", side_effect=ValueError("Configure IGDB")):
            status, result = self.post_json("/api/scores/refresh", {"game": "hollow-knight"})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "Configure IGDB")

    def test_api_state_redacts_by_spoiler_computes_current_chapter_and_discovers_orphan_walkthroughs(self):
        # Add an orphaned walkthrough file not listed in GAMES.md
        (self.games_dir / "celeste.walkthrough.md").write_text(
            "# Celeste Walkthrough\n\n## 1. Forsaken City\n\n#### Hint\nDash upward.\n",
            encoding="utf-8",
        )

        # At 'hint': Mantis Lords is only mentioned in Fungal Wastes's Answer section,
        # so current_chapter must be computed before redaction (chapter index 1).
        status, state_hint = self.get_json("/api/state?game=hollow-knight&spoiler=hint")
        self.assertEqual(status, 200)
        slugs = [g["slug"] for g in state_hint["games"]]
        self.assertIn("hollow-knight", slugs)
        self.assertIn("celeste", slugs)
        hk_item = next(g for g in state_hint["games"] if g["slug"] == "hollow-knight")
        self.assertTrue(hk_item["tracked"])
        self.assertEqual(hk_item["chapter_count"], 2)
        self.assertEqual(hk_item["current_chapter_num"], 2)
        self.assertEqual(hk_item["progress_pct"], 100)

        self.assertEqual(state_hint["current_chapter"], 1)
        self.assertIn("Look for the lever", state_hint["notes"])
        self.assertNotIn("double-dash", state_hint["notes"])
        self.assertNotIn("pogo off the third sister", state_hint["notes"])

        ch2_hint = state_hint["walkthrough"][1]
        self.assertTrue(ch2_hint["current"])
        self.assertIn("Follow the bouncy mushrooms", ch2_hint["body"])
        self.assertNotIn("Use the Mantis Claw", ch2_hint["body"])
        self.assertEqual(ch2_hint["videos"], [])
        self.assertEqual(ch2_hint["images"], [])
        # Only the Hint-level image in notes is visible at 'hint'
        self.assertEqual([m["src"] for m in state_hint["media"]], ["/images/hollow-knight/village-map.png"])

        # Raise spoiler to 'strategy': Strategy videos and images appear, Answer remains hidden
        status, state_strat = self.get_json("/api/state?game=hollow-knight&spoiler=strategy")
        self.assertEqual(status, 200)
        self.assertIn("double-dash", state_strat["notes"])
        self.assertNotIn("pogo off the third sister", state_strat["notes"])
        ch2_strat = state_strat["walkthrough"][1]
        self.assertIn("Use the Mantis Claw", ch2_strat["body"])
        self.assertNotIn("Defeat the Mantis Lords", ch2_strat["body"])
        self.assertEqual([v["id"] for v in ch2_strat["videos"]], ["abcdefghijk"])
        self.assertEqual(ch2_strat["videos"][0]["thumb"], "/api/thumbnail/abcdefghijk?at=90")
        self.assertEqual([img["src"] for img in ch2_strat["images"]], ["/images/hollow-knight/mantis-claw.png"])
        self.assertIn('data-video="abcdefghijk" data-at="90" data-label="Climb route"', ch2_strat["html"])
        self.assertIn('data-reveal="answer"', ch2_strat["html"])
        self.assertIn('<img src="/images/hollow-knight/village-map.png"', state_strat["notes_html"])
        media_ids = [m.get("id") or m.get("src") for m in state_strat["media"]]
        self.assertIn("Bc-UtkWMuus", media_ids)
        self.assertIn("abcdefghijk", media_ids)
        self.assertIn("/images/hollow-knight/mantis-claw.png", media_ids)
        self.assertNotIn("/images/hollow-knight/arena.png", media_ids)

        # Raise spoiler to 'answer': Everything is visible
        status, state_ans = self.get_json("/api/state?game=hollow-knight&spoiler=answer")
        self.assertEqual(status, 200)
        self.assertIn("pogo off the third sister", state_ans["notes"])
        self.assertIn("Defeat the Mantis Lords", state_ans["walkthrough"][1]["body"])
        ans_media = [m.get("id") or m.get("src") for m in state_ans["media"]]
        self.assertIn("/images/hollow-knight/arena.png", ans_media)

    def test_maps_respect_spoiler_level_and_link_to_their_chapter(self):
        image_dir = self.games_dir / "images" / "hollow-knight"
        (image_dir / "crossroads-map.png").write_bytes(b"map")
        (image_dir / "secret-map.png").write_bytes(b"map")
        walk = (self.games_dir / "hollow-knight.walkthrough.md")
        walk.write_text(WALKTHROUGH.replace(
            "Head down from Dirtmouth.",
            "Head down from Dirtmouth.\n![Crossroads map](images/hollow-knight/crossroads-map.png)",
        ).replace(
            "Defeat the Mantis Lords at the bottom of Mantis Village.",
            "Defeat the Mantis Lords.\n![Secret map](images/hollow-knight/secret-map.png)",
        ), encoding="utf-8")
        _, hint = self.get_json("/api/state?game=hollow-knight&spoiler=hint")
        self.assertEqual([m["label"] for m in hint["maps"]], ["Village Map", "Crossroads map"])
        self.assertIsNone(hint["maps"][0]["chapter_index"])
        self.assertEqual(hint["maps"][1]["chapter_index"], 0)
        self.assertEqual(hint["maps"][1]["chapter"], hint["walkthrough"][0]["title"])
        _, answer = self.get_json("/api/state?game=hollow-knight&spoiler=answer")
        secret = next(m for m in answer["maps"] if m["label"] == "Secret map")
        self.assertEqual(secret["chapter_index"], 1)
        self.assertNotIn("Mantis Claw Wall", [m["label"] for m in answer["maps"]])

    def test_state_exposes_cover_artwork_for_library_and_selected_game(self):
        cover = self.games_dir / "images" / "hollow-knight" / "cover.png"
        cover.write_bytes(b"\x89PNG-cover")
        (self.games_dir / "celeste.walkthrough.md").write_text(
            "# Celeste Walkthrough\n\n## 1. Forsaken City\n\n#### Hint\nDash upward.\n",
            encoding="utf-8",
        )

        status, payload = self.get_json("/api/state?game=hollow-knight&spoiler=hint")
        self.assertEqual(status, 200)
        hollow = next(g for g in payload["games"] if g["slug"] == "hollow-knight")
        celeste = next(g for g in payload["games"] if g["slug"] == "celeste")
        self.assertEqual(hollow["artwork"], "/images/hollow-knight/cover.png")
        self.assertEqual(payload["selected"]["artwork"], "/images/hollow-knight/cover.png")
        self.assertIsNone(celeste["artwork"])

        status, headers, data = self.request("GET", hollow["artwork"])
        self.assertEqual((status, data), (200, b"\x89PNG-cover"))
        self.assertEqual(headers["content-type"], "image/png")

    def test_images_endpoint_serves_local_images_and_refuses_path_traversal(self):
        status, headers, data = self.request("GET", "/images/hollow-knight/village-map.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(data, b"\x89PNG-village-map")

        # Secret file outside images/
        (self.games_dir / "secret.txt").write_text("TOPSECRET", encoding="utf-8")

        for traversal in (
            "/images/../../etc/passwd",
            "/images/%2e%2e/%2e%2e/etc/passwd",
            "/images/../secret.txt",
            "/images/%2e%2e/secret.txt",
        ):
            status, _, body = self.request("GET", traversal)
            self.assertEqual(status, 403, traversal)
            self.assertNotIn(b"TOPSECRET", body)


class GameSavingAndTrackingTests(WebServerTestCase):
    def test_save_update_spot_notes_and_delete_game_via_api(self):
        # 1. Save a brand new game via POST /api/games/save
        status, payload = self.post_json(
            "/api/games/save",
            {
                "name": "Metroid Prime 4",
                "platform": "Switch 2",
                "genre": "Adventure",
                "status": "Playing",
                "spot": "Phenandra Drifts",
                "notes": "**Build:** Power Beam + Scan Visor\n\n## Phenandra Drifts\n\n#### Hint\nScan the ice pillars.",
                "spoiler": "hint",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["selected"]["slug"], "metroid-prime-4")
        self.assertEqual(payload["selected"]["platform"], "Switch 2")
        self.assertEqual(payload["selected"]["spot"], "Phenandra Drifts")
        self.assertIn("Scan the ice pillars.", payload["notes"])
        self.assertTrue((self.games_dir / "metroid-prime-4.md").is_file())

        # Verify GAMES.md contains both Hollow Knight and Metroid Prime 4
        tracked = library.read_games(self.games_dir)
        self.assertEqual([g.slug for g in tracked], ["hollow-knight", "metroid-prime-4"])

        # 2. Update current spot & status on Hollow Knight via POST /api/games/spot
        status, payload2 = self.post_json(
            "/api/games/spot",
            {
                "game": "hollow-knight",
                "spot": "1. Forgotten Crossroads",
                "status": "Paused",
                "note": "Saved at Dirtmouth bench.",
                "spoiler": "hint",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload2["selected"]["spot"], "1. Forgotten Crossroads")
        self.assertEqual(payload2["selected"]["status"], "Paused")
        self.assertEqual(payload2["current_chapter"], 0)
        self.assertIn("Saved at Dirtmouth bench.", payload2["raw_notes"])

        # 3. Append a progress note via POST /api/games/notes
        status, payload3 = self.post_json(
            "/api/games/notes",
            {
                "game": "metroid-prime-4",
                "spot": "Magmoor Caverns",
                "entry": "- Unlocked Varia Suit",
                "spoiler": "hint",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload3["selected"]["spot"], "Magmoor Caverns")
        self.assertIn("Unlocked Varia Suit", payload3["notes"])

        # 4. Full raw markdown edit via POST /api/games/notes
        status, payload4 = self.post_json(
            "/api/games/notes",
            {
                "game": "metroid-prime-4",
                "notes": "## Custom Section\n\n#### Hint\nFull markdown saved.",
                "spoiler": "hint",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("Full markdown saved.", payload4["notes"])

        # 5. Untrack game via POST /api/games/delete (keeps .md file safe on disk)
        status, payload5 = self.post_json(
            "/api/games/delete",
            {"game": "metroid-prime-4", "spoiler": "hint"},
        )
        self.assertEqual(status, 200)
        self.assertEqual([g.slug for g in library.read_games(self.games_dir)], ["hollow-knight"])
        self.assertTrue((self.games_dir / "metroid-prime-4.md").is_file())


    def test_bulk_backlog_and_queue_reorder_apis(self):
        status, payload = self.post_json(
            "/api/backlog",
            {
                "games": "- Celeste\n2. Hades II | PC | Roguelike\n# ignore this comment",
                "platform": "Switch 2",
                "genre": "Platformer",
                "artwork": True,
                "spoiler": "hint",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual([g["slug"] for g in payload["added"]], ["celeste", "hades-ii"])
        self.assertEqual(payload["selected"]["slug"], "hades-ii")
        self.assertEqual(self.fetch_artwork_mock.call_count, 2)
        tracked = library.read_games(self.games_dir)
        self.assertEqual([g.slug for g in tracked], ["hollow-knight", "celeste", "hades-ii"])
        self.assertEqual((tracked[1].platform, tracked[1].genre), ("Switch 2", "Platformer"))
        self.assertEqual((tracked[2].platform, tracked[2].genre), ("PC", "Roguelike"))
        self.assertTrue(all(g.status == "Backlog" for g in tracked[1:]))

        status, reordered = self.post_json(
            "/api/games/reorder",
            {
                "ordered_slugs": ["hades-ii", "celeste"],
                "status_filter": "Backlog",
                "game": "hades-ii",
                "spoiler": "hint",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [g.slug for g in library.read_games(self.games_dir)],
            ["hollow-knight", "hades-ii", "celeste"],
        )
        self.assertEqual(reordered["selected"]["slug"], "hades-ii")

        status, error = self.post_json("/api/backlog", {"games": "\n# no titles\n"})
        self.assertEqual(status, 400)
        self.assertIn("at least one", error["error"])


class CrossSiteTests(WebServerTestCase):
    """Other websites the user has open can reach this port; only the app's own page may drive it."""

    def test_a_post_from_another_site_is_refused_before_it_runs(self):
        with mock.patch("game_guide.conversation.ask") as ask:
            status, _, data = self.request("POST", "/api/ask", body={"question": "hi"}, headers={"Origin": "https://evil.example"})
        self.assertEqual((status, json.loads(data)["error"]), (403, "Cross-site request refused"))
        ask.assert_not_called()

    def test_a_form_or_text_plain_post_is_refused(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", None):
            headers = {"Content-Type": content_type} if content_type else {}
            status, _, _ = self.request("POST", "/api/games/save", body=b'{"name": "Pwned"}', headers=headers)
            self.assertEqual(status, 403, content_type)
        self.assertNotIn("Pwned", (self.games_dir / "GAMES.md").read_text(encoding="utf-8"))

    def test_a_dns_rebinding_page_cannot_read_the_library(self):
        status, _, data = self.request("GET", "/api/state", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual((status, json.loads(data)["error"]), (403, "Unknown host"))

    def test_the_apps_own_page_is_allowed(self):
        origin = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.request("GET", "/api/state", headers=origin)[0], 200)
        status, _, _ = self.request("POST", "/api/games/save", body={"name": "Celeste"}, headers=origin)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": f"localhost:{self.port}"})[0], 200)


class MediaEndpointsTests(WebServerTestCase):
    def test_a_json_body_that_is_not_an_object_is_a_400(self):
        for raw in (b"[1]", b'"text"', b"null"):
            status, _, data = self.request("POST", "/api/games/save", body=raw,
                                           headers={"Content-Type": "application/json"})
            self.assertEqual((status, json.loads(data)), (400, {"error": "Invalid JSON body"}))

    def test_missing_media_tools_offer_a_youtube_fallback(self):
        with mock.patch.object(media, "missing", return_value=["yt-dlp"]):
            responses = (
                self.get_json("/api/frames/Bc-UtkWMuus?at=252"),
                self.get_json("/api/video/Bc-UtkWMuus"),
                self.post_json("/api/play", {"id": "Bc-UtkWMuus", "at": 252}),
            )
        for status, payload in responses:
            self.assertEqual(status, 400)
            self.assertIn("YouTube", payload["error"])
            self.assertNotIn("brew install", payload["error"])
            self.assertNotIn("mise use", payload["error"])

    def test_thumbnail_frames_and_frame_endpoints_support_hyphenated_video_ids_and_reject_invalid_ids(self):
        vid = "Bc-UtkWMuus"
        thumb_file = self.cache_dir / f"{vid}.jpg"
        thumb_file.write_bytes(b"JPG-THUMB")

        status, headers, data = self.request("GET", f"/api/thumbnail/{vid}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/jpeg")
        self.assertEqual(data, b"JPG-THUMB")

        # Timestamp-specific thumbnail (?at=252) serves <id>-thumb-252.jpg via timestamp_thumbnail
        ts_thumb_file = self.cache_dir / f"{vid}-thumb-252.jpg"
        ts_thumb_file.write_bytes(b"JPG-THUMB-252")
        status, headers, ts_data = self.request("GET", f"/api/thumbnail/{vid}?at=252")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/jpeg")
        self.assertEqual(ts_data, b"JPG-THUMB-252")

        # Pre-populate extracted frames for timestamp 252 (-2, 0, +2 -> 250, 252, 254)
        for t in (250, 252, 254):
            (self.cache_dir / f"{vid}-{t}.jpg").write_bytes(f"FRAME-{t}".encode("utf-8"))

        with mock.patch.object(media, "missing", return_value=[]):
            status, payload = self.get_json(f"/api/frames/{vid}?at=252")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["frames"],
            [
                {"time": 250, "url": f"/api/frame/{vid}/250"},
                {"time": 252, "url": f"/api/frame/{vid}/252"},
                {"time": 254, "url": f"/api/frame/{vid}/254"},
            ],
        )

        status, headers, frame_bytes = self.request("GET", f"/api/frame/{vid}/250")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/jpeg")
        self.assertEqual(frame_bytes, b"FRAME-250")

        # Reject invalid video IDs with 400
        for bad in ("/api/thumbnail/not-valid!", "/api/frames/short?at=10", "/api/frame/bad_id/250", "/api/frame/Bc-UtkWMuus/notnum"):
            status, _ = self.get_json(bad)
            self.assertEqual(status, 400, bad)

    def test_video_endpoint_streams_full_200_and_206_byte_ranges_for_ad_free_seeking(self):
        vid = "Bc-UtkWMuus"
        video_file = self.cache_dir / f"{vid}.video.mp4"
        video_bytes = b"0123456789ABCDEF"
        video_file.write_bytes(video_bytes)

        with mock.patch.object(media, "download", return_value=video_file) as dl_mock:
            # Full 200 OK request
            status, headers, data = self.request("GET", f"/api/video/{vid}")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("accept-ranges"), "bytes")
            self.assertEqual(headers.get("content-length"), str(len(video_bytes)))
            self.assertEqual(data, video_bytes)
            dl_mock.assert_called_once()

            # Range: bytes=2-5 -> 206 Partial Content
            status, headers, part = self.request(
                "GET",
                f"/api/video/{vid}",
                headers={"Range": "bytes=2-5"},
            )
            self.assertEqual(status, 206)
            self.assertEqual(headers.get("accept-ranges"), "bytes")
            self.assertEqual(headers.get("content-range"), f"bytes 2-5/{len(video_bytes)}")
            self.assertEqual(headers.get("content-length"), "4")
            self.assertEqual(part, b"2345")

        # Reject invalid video ID with 400
        status, _ = self.get_json("/api/video/invalid!")
        self.assertEqual(status, 400)


class StreamingAskAndSessionsTests(WebServerTestCase):
    def test_ask_and_walkthrough_stream_ndjson_preserve_sessions_and_new_chat_clears_them(self):
        seen_calls: list[tuple[str, str, str | None]] = []

        def fake_ask(backend, prompt, session=None):
            seen_calls.append((backend, prompt, session))
            next_session = "sess-2" if session == "sess-1" else "sess-1"
            yield backends.Event("session", session=next_session)
            yield backends.Event("status", text="running fetch_text_guide.py")
            yield backends.Event("done", text="Parry the dash.", session=next_session)

        with mock.patch.object(conversation, "ask", side_effect=fake_ask):
            status, events1 = self.post_ndjson(
                "/api/ask",
                {"backend": "claude", "spoiler": "strategy", "game": "hollow-knight", "question": "How to beat Mantis Lords?"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(events1[0], {"kind": "status", "text": "claude: running fetch_text_guide.py"})
            self.assertEqual(events1[1]["kind"], "done")
            self.assertEqual(events1[1]["rendered"], "Parry the dash.")
            self.assertEqual(events1[1]["html"], "<p>Parry the dash.</p>\n")
            self.assertEqual(events1[1]["session"], "sess-1")
            self.assertIsNone(seen_calls[0][2])

            # Second turn on /api/walkthrough reuses sess-1
            status, events2 = self.post_ndjson(
                "/api/walkthrough",
                {"backend": "claude", "spoiler": "answer", "game": "hollow-knight"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(seen_calls[1][2], "sess-1")
            self.assertIn("full walkthrough for Hollow Knight", seen_calls[1][1])
            self.assertIn("cover artwork", seen_calls[1][1])
            self.assertEqual(events2[-1]["session"], "sess-2")
            self.fetch_artwork_mock.assert_called_once()
            self.assertEqual(self.fetch_artwork_mock.call_args.args[0].slug, "hollow-knight")
            self.ensure_artwork_mock.assert_called_once()

            # POST /api/new-chat clears sessions
            status, _, raw = self.request("POST", "/api/new-chat", body={})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8")), {"ok": True})
            self.assertEqual(self.server.conversation.sessions, {})

        # Error event streaming
        def failing_ask(backend, prompt, session=None):
            yield backends.Event("error", text="rate limit exceeded")

        with mock.patch.object(conversation, "ask", side_effect=failing_ask):
            status, err_events = self.post_ndjson(
                "/api/ask",
                {"backend": "claude", "spoiler": "hint", "game": "hollow-knight", "question": "Where is Cornifer?"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(err_events[0]["kind"], "error")
            self.assertIn("rate limit exceeded", err_events[0]["rendered"])
            self.assertEqual((err_events[0]["reason"], err_events[0]["message"]), ("failed", conversation.FAILED_MESSAGE))

    def test_second_question_cancels_superseded_in_flight_question(self):
        started = threading.Event()
        release = threading.Event()
        closed = []
        calls = []

        def fake_ask(backend, prompt, session=None):
            calls.append(prompt)
            if len(calls) == 1:
                try:
                    yield backends.Event("session", session="old-sess")
                    yield backends.Event("status", text="searching")
                    started.set()
                    release.wait(5)
                    yield backends.Event("done", text="first answer", session="old-sess")
                except GeneratorExit:
                    closed.append(True)
                    raise
                return
            yield backends.Event("session", session="new-sess")
            yield backends.Event("done", text="second answer", session="new-sess")

        first_result: list[list[dict]] = []

        def run_first():
            _, evs = self.post_ndjson(
                "/api/ask",
                {"backend": "claude", "spoiler": "hint", "game": "hollow-knight", "question": "first?"},
            )
            first_result.append(evs)

        with mock.patch.object(conversation, "ask", side_effect=fake_ask):
            t1 = threading.Thread(target=run_first)
            t1.start()
            self.assertTrue(started.wait(5))

            status, second_events = self.post_ndjson(
                "/api/ask",
                {"backend": "claude", "spoiler": "hint", "game": "hollow-knight", "question": "second?"},
            )
            release.set()
            t1.join(timeout=5)

        self.assertEqual(status, 200)
        self.assertEqual(second_events[-1]["rendered"], "second answer")
        self.assertEqual(closed, [True])
        self.assertEqual(self.server.conversation.sessions.get("claude"), "new-sess")
        self.assertTrue(all(ev.get("rendered") != "first answer" for ev in first_result[0]))


class CliAndTuiWebIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_cli_web_subcommand_and_flag_invoke_web_run(self):
        from game_guide import cli

        with mock.patch("game_guide.web.run") as run_mock:
            cli.main(["web", "--no-open", "--host", "127.0.0.1", "--port", "8765"])
            run_mock.assert_called_once_with(host="127.0.0.1", port=8765, open_browser=False)

        with mock.patch("game_guide.web.run") as run_mock:
            cli.main(["--web"])
            run_mock.assert_called_once_with(host="127.0.0.1", port=0, open_browser=True)

    def test_cli_track_spot_status_and_remove_subcommands(self):
        from game_guide import cli

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["track", "Hades II", "--platform", "PC", "--genre", "roguelike", "--status", "Playing", "--spot", "Erebus", "--note", "Beat Hecate"])
                cli.main(["spot", "hades-ii", "Oceanus"])
                cli.main(["status", "hades-ii", "Beaten"])
            games = library.read_games(Path(d))
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0].name, "Hades II")
            self.assertEqual(games[0].spot, "Oceanus")
            self.assertEqual(games[0].status, "Beaten")
            self.assertIn("Beat Hecate", (Path(d) / "hades-ii.md").read_text(encoding="utf-8"))

            with redirect_stdout(buf):
                cli.main(["remove", "hades-ii"])
            self.assertEqual(library.read_games(Path(d)), [])

    def test_cli_backlog_accepts_multiline_lists_and_fetches_artwork_opt_in(self):
        from game_guide import cli

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}), \
                mock.patch("game_guide.cli.fetch_game_artwork") as art_mock:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["backlog", "Celeste\nHades II", "--platform", "PC", "--artwork"])
                cli.main(["track", "Hollow Knight", "--artwork"])
                cli.main(["backlog"])
            games = library.read_games(Path(d))
            self.assertEqual([g.name for g in games], ["Celeste", "Hades II", "Hollow Knight"])
            self.assertEqual([g.status for g in games], ["Backlog", "Backlog", "Playing"])
            self.assertEqual(art_mock.call_count, 3)
            output = buf.getvalue()
            self.assertIn("1. Celeste (PC)", output)
            self.assertIn("2. Hades II (PC)", output)
            self.assertNotIn("3. Hollow Knight", output)

    def test_cli_package_app_and_install_app_subcommands(self):
        from game_guide import cli

        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d) / "out"
            dest_dir = Path(d) / "Applications"
            fake_app = out_dir / "Game Guide.app"
            installed_app = dest_dir / "Game Guide.app"

            buf = io.StringIO()
            with mock.patch("game_guide.desktop.package_app", return_value=fake_app) as pkg_mock, redirect_stdout(buf):
                cli.main(["package-app", "--out-dir", str(out_dir)])
            pkg_mock.assert_called_once_with(out_dir=out_dir)
            self.assertEqual(buf.getvalue().strip(), str(fake_app))

            buf = io.StringIO()
            with mock.patch("game_guide.desktop.install_app", return_value=installed_app) as inst_mock, redirect_stdout(buf):
                cli.main(["install-app", "--no-dock", "--dest-dir", str(dest_dir)])
            inst_mock.assert_called_once_with(dest_dir=dest_dir, add_to_dock=False, app=None)
            self.assertEqual(buf.getvalue().strip(), f"Installed {installed_app}")

    def test_add_app_to_dock_updates_plist_idempotently_without_touching_real_dock(self):
        with tempfile.TemporaryDirectory() as d:
            app_bundle = Path(d) / "Game Guide.app"
            app_bundle.mkdir()
            dock_plist = Path(d) / "com.apple.dock.plist"
            dock_plist.write_bytes(plistlib.dumps({"persistent-apps": []}, fmt=plistlib.FMT_BINARY))

            with mock.patch("game_guide.desktop.subprocess.run") as sub_mock:
                added_first = desktop.add_app_to_dock(app_bundle, dock_plist=dock_plist, restart_dock=False)
                added_second = desktop.add_app_to_dock(app_bundle, dock_plist=dock_plist, restart_dock=False)
                sub_mock.assert_not_called()

            self.assertTrue(added_first)
            self.assertFalse(added_second)
            saved = plistlib.loads(dock_plist.read_bytes())
            self.assertEqual(len(saved["persistent-apps"]), 1)
            tile = saved["persistent-apps"][0]["tile-data"]
            self.assertEqual(tile["file-label"], "Game Guide")
            self.assertIn("Game%20Guide.app", tile["file-data"]["_CFURLString"])

    async def test_pressing_o_and_s_in_tui_opens_web_and_updates_spot(self):
        from game_guide.app import GameGuide

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GAME_GUIDE_DIR": d}):
            Path(d, "GAMES.md").write_text(GAMES, encoding="utf-8")
            Path(d, "hollow-knight.walkthrough.md").write_text(WALKTHROUGH, encoding="utf-8")
            app = GameGuide()
            with mock.patch("game_guide.app.webbrowser.open") as open_mock:
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    app.query_one("#main").active = "walk-tab"
                    await pilot.pause()
                    app.query_one("#chapters").highlighted = 0
                    await pilot.press("s")
                    await pilot.pause()
                    self.assertEqual(library.read_games(Path(d))[0].spot, "1. Forgotten Crossroads")
                    await pilot.press("o")
                    await pilot.pause()
                    self.assertIsNotNone(app.web_server)
                    expected_url = f"{app.web_server.url}/?game=hollow-knight"
                    open_mock.assert_called_once_with(expected_url)
                self.assertIsNone(app.web_server)


if __name__ == "__main__":
    unittest.main()
