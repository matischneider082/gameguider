import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

from game_guide import artwork
from game_guide.notes import Game


class FakeImageResponse:
    def __init__(self, data: bytes, content_type: str, final_url: str = "https://images.example.com/cover"):
        self.data = data
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.final_url

    def read(self, _limit=-1):
        return self.data


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()
        self.game = Game("Celeste")
        self.image_dir = self.dir / "images" / "celeste"
        self.image_dir.mkdir(parents=True)

    def test_download_uses_actual_image_type_and_replaces_stale_cover_extension(self):
        old_cover = self.image_dir / "cover.jpg"
        old_cover.write_bytes(b"not really a jpeg")
        response = FakeImageResponse(b"\x89PNG\r\nreal png", "image/png")
        destination = self.image_dir / "cover.jpg"

        with mock.patch("urllib.request.urlopen", return_value=response):
            saved = artwork._download_https_image("https://images.example.com/cover", destination, self.dir / "images")

        self.assertEqual(saved, self.image_dir / "cover.png")
        self.assertEqual(saved.read_bytes(), b"\x89PNG\r\nreal png")
        self.assertFalse(old_cover.exists())

    def test_exact_cover_avoids_network_and_embeds_into_walkthrough_once(self):
        cover = self.image_dir / "cover.webp"
        cover.write_bytes(b"webp")
        walkthrough = self.dir / "celeste.walkthrough.md"
        walkthrough.write_text("# Celeste Walkthrough\n\nSources: example.\n\n## Chapter 1\n\n#### Hint\nDash.\n", encoding="utf-8")

        with mock.patch("urllib.request.urlopen") as urlopen:
            first = artwork.fetch_game_artwork(self.game, self.dir)
            second = artwork.fetch_game_artwork(self.game, self.dir)

        self.assertEqual((first, second), (cover, cover))
        urlopen.assert_not_called()
        text = walkthrough.read_text(encoding="utf-8")
        self.assertEqual(text.count("![Celeste Cover Artwork](images/celeste/cover.webp)"), 1)
        self.assertLess(text.index("Cover Artwork"), text.index("## Chapter 1"))

    def test_existing_non_cover_art_is_fallback_and_is_embedded_after_download_failures(self):
        art = self.image_dir / "chapter-1-map.png"
        art.write_bytes(b"png")
        walkthrough = self.dir / "celeste.walkthrough.md"
        walkthrough.write_text("# Celeste Walkthrough\n\n## Chapter 1\n\n#### Hint\nDash.\n", encoding="utf-8")

        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            saved = artwork.fetch_game_artwork(self.game, self.dir)

        self.assertEqual(saved, art)
        self.assertIn("![Celeste Cover Artwork](images/celeste/chapter-1-map.png)", walkthrough.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
