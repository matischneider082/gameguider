"""fetch_text_guide.py and fetch_yt_walkthrough.py, run in their own PEP 723 environments.

Their dependencies (httpx, bs4, yt-dlp, youtube-transcript-api) are not in the project venv,
so module-level checks run as snippets under `uv run --with-requirements <script>` and CLI
checks under `uv run --no-config <script>`. Only local servers are contacted.
"""

import json
import os
import subprocess
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from game_guide import backends

SCRIPTS = backends.SKILL.parent / "scripts"
TEXT = SCRIPTS / "fetch_text_guide.py"
YT = SCRIPTS / "fetch_yt_walkthrough.py"
ENV = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV" and not k.startswith("GIT_")}
PRELUDE = """\
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("m", sys.argv[1])
m = sys.modules["m"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
"""


def in_script_env(script, code, *args):
    """Run code with the script loaded as `m`; return the JSON it prints last."""
    done = subprocess.run(
        ["uv", "run", "--no-config", "--no-project", "--quiet", "--with-requirements", script,
         "python", "-c", PRELUDE + textwrap.dedent(code), script, *args],
        capture_output=True, text=True, env=ENV,
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout.strip().splitlines()[-1])


def cli(script, *args):
    return subprocess.run(["uv", "run", "--no-config", "--quiet", script, *args],
                          capture_output=True, text=True, env=ENV)


class Site:
    """Local server: path -> (status, headers, body); records every path requested."""

    def __init__(self, case, routes):
        self.requests, self.routes = [], routes
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.requests.append(self.path)
                status, headers, body = routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)) if isinstance(body, bytes) else "100000")
                self.end_headers()
                if isinstance(body, bytes):
                    self.wfile.write(body)
                else:  # a slow body: an int number of 100-byte chunks, 0.1 s apart
                    try:
                        for _ in range(body):
                            self.wfile.write(b"x" * 100)
                            self.wfile.flush()
                            time.sleep(0.1)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # the client gave up, which is what the test wants

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        case.addCleanup(self.server.server_close)
        case.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"


HTML = {"Content-Type": "text/html; charset=utf-8"}
# Lets the first hop reach the local server so later hops still meet the real check.
ALLOW_PATHS = """
orig = m.check_public_url
allowed = set(sys.argv[2].split(","))
m.check_public_url = lambda url: None if url.rsplit("/", 1)[-1] in allowed else orig(url)
"""


class TextGuideSsrf(unittest.TestCase):
    def setUp(self):
        self.site = Site(self, {
            "/secret": (200, HTML, b"<html><body><p>TOPSECRET</p></body></html>"),
            "/hop": (302, {"Location": "/secret"}, b""),
            "/page": (200, HTML, b"<html><body><h2>Boss</h2><p>Dodge left.</p></body></html>"),
            "/to-page": (302, {"Location": "/page"}, b""),
            "/loop": (302, {"Location": "/loop"}, b""),
        })

    def test_cli_refuses_loopback_hosts_and_never_prints_the_body(self):
        port = self.site.server.server_port
        for url in (f"http://127.0.0.1:{port}/secret", f"http://localhost:{port}/secret"):
            done = cli(TEXT, url)
            self.assertIn("Refusing to fetch", done.stdout, url)
            self.assertNotIn("TOPSECRET", done.stdout + done.stderr, url)
        self.assertEqual(self.site.requests, [])

    def test_refuses_a_redirect_to_loopback(self):
        out = in_script_env(TEXT, ALLOW_PATHS + """
try:
    m.fetch_and_extract_url(sys.argv[3] + "/hop")
    print(json.dumps("fetched"))
except ValueError as exc:
    print(json.dumps(str(exc)))
""", "hop", self.site.url)
        self.assertIn("Refusing to fetch", out)
        self.assertEqual(self.site.requests, ["/hop"])

    def test_refuses_non_http_schemes(self):
        out = in_script_env(TEXT, """
errors = []
for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/"):
    try:
        m.check_public_url(url)
    except ValueError as exc:
        errors.append(str(exc))
print(json.dumps(errors))
""")
        self.assertEqual(len(out), 3, out)
        self.assertTrue(all("non-http(s)" in e for e in out), out)

    def test_connects_to_the_checked_address_not_a_fresh_lookup(self):
        # rebind.invalid never resolves, so this fetch only works if the request uses the address the check approved;
        # resolving the name again is what lets a DNS-rebinding host swap in a private address after the check.
        port = self.site.url.rsplit(":", 1)[1]
        out = in_script_env(TEXT, """
m.check_public_url = lambda url: ["127.0.0.1"]
resolved, html = m.fetch_html(f"http://rebind.invalid:{sys.argv[2]}/to-page")
print(json.dumps([resolved, "Dodge left." in html]))
""", port)
        self.assertEqual(out, [f"http://rebind.invalid:{port}/page", True])

    def test_falls_back_after_a_connect_timeout_too(self):
        port = self.site.url.rsplit(":", 1)[1]
        out = in_script_env(TEXT, """
real_send = m.httpx.Client.send
def send(self, request, **kwargs):
    if request.url.host == "192.0.2.1":
        raise m.httpx.ConnectTimeout("timed out", request=request)  # a silently dropped first address
    return real_send(self, request, **kwargs)
m.httpx.Client.send = send
m.check_public_url = lambda url: ["192.0.2.1", "127.0.0.1"]
resolved, html = m.fetch_html(f"http://slowfirst.invalid:{sys.argv[2]}/page")
print(json.dumps("Dodge left." in html))
""", port)
        self.assertTrue(out)

    def test_falls_back_to_the_next_checked_address(self):
        port = self.site.url.rsplit(":", 1)[1]
        out = in_script_env(TEXT, """
m.check_public_url = lambda url: ["::1", "127.0.0.1"]  # the server listens on IPv4 only, so ::1 is refused
resolved, html = m.fetch_html(f"http://twohomes.invalid:{sys.argv[2]}/page")
print(json.dumps("Dodge left." in html))
""", port)
        self.assertTrue(out)

    def test_a_proxied_request_is_not_pinned(self):
        # Through a CONNECT tunnel httpcore ignores sni_hostname, so a pinned https fetch sent the IP as the server name.
        self.site.routes["http://proxied.invalid/page"] = self.site.routes["/page"]
        out = in_script_env(TEXT, """
import os
os.environ["HTTP_PROXY"] = sys.argv[2]
m.check_public_url = lambda url: ["192.0.2.1"]
resolved, html = m.fetch_html("http://proxied.invalid/page")
print(json.dumps(["Dodge left." in html, m.uses_proxy(m.httpx.URL("https://example.com/")),
                  m.uses_proxy(m.httpx.URL("ftp://example.com/"))]))
""", self.site.url)
        self.assertEqual(out, [True, False, False])
        self.assertIn("http://proxied.invalid/page", self.site.requests)

    def test_the_deadline_covers_connecting_and_every_hop(self):
        out = in_script_env(TEXT, """
m.check_public_url = lambda url: None
m.DEADLINE_SECONDS = 0
try:
    m.fetch_html(sys.argv[2] + "/loop")
    print(json.dumps("fetched"))
except TimeoutError as exc:
    print(json.dumps(str(exc)))
""", self.site.url)
        self.assertIn("longer than", out)
        self.assertEqual(self.site.requests, [])

    def test_follows_redirects_by_hand_up_to_the_cap(self):
        out = in_script_env(TEXT, """
m.check_public_url = lambda url: None
resolved, html = m.fetch_html(sys.argv[2] + "/to-page")
try:
    m.fetch_html(sys.argv[2] + "/loop")
    loop = "fetched"
except ValueError as exc:
    loop = str(exc)
print(json.dumps([resolved, "Dodge left." in html, loop]))
""", self.site.url)
        self.assertEqual(out[:2], [f"{self.site.url}/page", True])
        self.assertIn("redirects", out[2])
        self.assertEqual(self.site.requests.count("/loop"), 6)


class TextGuideLimits(unittest.TestCase):
    def setUp(self):
        self.site = Site(self, {
            "/big": (200, HTML, b"<html><body><p>" + b"x" * 5000 + b"</p></body></html>"),
            "/slow": (200, HTML, 30),
            "/pre": (200, HTML, b"<html><body><h2>Map</h2><pre>" + b"#" * 100_000 + b"</pre></body></html>"),
        })

    def test_caps_body_size_and_total_time(self):
        out = in_script_env(TEXT, """
m.check_public_url = lambda url: None
m.MAX_BYTES, m.DEADLINE_SECONDS = 1000, 0.5
errors = []
for path in ("/big", "/slow"):
    try:
        m.fetch_html(sys.argv[2] + path)
        errors.append("fetched")
    except (ValueError, TimeoutError) as exc:
        errors.append(str(exc))
print(json.dumps(errors))
""", self.site.url)
        self.assertIn("larger than 1000 bytes", out[0])
        self.assertIn("longer than 0.5s", out[1])

    def test_first_section_is_truncated_to_max_chars(self):
        out = in_script_env(TEXT, """
m.check_public_url = lambda url: None
print(json.dumps(m.fetch_and_extract_url(sys.argv[2] + "/pre", max_chars=100)))
""", self.site.url)
        self.assertLess(len(out), 400, out[:500])
        self.assertIn("Output truncated at 100 chars", out)


class TextGuideParsing(unittest.TestCase):
    def test_section_prefers_the_full_phrase_then_most_keywords(self):
        out = in_script_env(TEXT, """
S = m.Section
secs = [S(2, "Chapter 1", "walk"), S(2, "Chapter 2", "run"), S(2, "Chapter 12", "jump"),
        S(2, "Chapter 3: The Boss", "fight"), S(3, "Hard mode", "dodge")]
pick = lambda q: [s.heading for s in m.filter_sections_hierarchically(secs, q)]
print(json.dumps([pick("Chapter 2"), pick("boss chapter 3"), pick("chapter")]))
""")
        self.assertEqual(out[0], ["Chapter 2"])
        self.assertEqual(out[1], ["Chapter 3: The Boss", "Hard mode"])
        self.assertEqual(len(out[2]), 5)

    def test_a_bare_faqtext_container_is_parsed(self):
        out = in_script_env(TEXT, """
from bs4 import BeautifulSoup
html = '<html><body><div id="faqtext">INTRODUCTION\\n=====\\nHello.\\nWALKTHROUGH\\n=====\\nGo north.</div></body></html>'
print(json.dumps([s.heading for s in m.parse_walkthrough_sections(BeautifulSoup(html, "html.parser"))]))
""")
        self.assertEqual(out, ["INTRODUCTION", "WALKTHROUGH"])

    def test_extracts_walkthrough_images_from_img_figure_datasrc_srcset_and_relative_urls(self):
        out = in_script_env(TEXT, """
from bs4 import BeautifulSoup
html = '''
<html><body><article>
  <h2>Greenpath Map</h2>
  <p>Follow the mossy corridor.</p>
  <figure>
    <img data-src="/assets/maps/greenpath-map.png" alt="ignored when figcaption exists">
    <figcaption>Cornifer location in Greenpath</figcaption>
  </figure>
  <img srcset="https://guides.example.com/img/room-320.jpg 320w, https://guides.example.com/img/room-1280.jpg 1280w" alt="Boss arena layout">
  <img src="diagrams/bench.webp" title="Hidden bench">
</article></body></html>
'''
secs = m.parse_walkthrough_sections(BeautifulSoup(html, "html.parser"), base_url="https://guides.example.com/hollow-knight/walkthrough")
print(json.dumps([(s.heading, s.body) for s in secs]))
""")
        self.assertEqual(len(out), 1)
        heading, body = out[0]
        self.assertEqual(heading, "Greenpath Map")
        self.assertIn("![Cornifer location in Greenpath](https://guides.example.com/assets/maps/greenpath-map.png)", body)
        self.assertIn("![Boss arena layout](https://guides.example.com/img/room-1280.jpg)", body)
        self.assertIn("![Hidden bench](https://guides.example.com/hollow-knight/diagrams/bench.webp)", body)

    def test_strips_ad_blocks_tracking_pixels_and_ad_image_urls(self):
        out = in_script_env(TEXT, """
from bs4 import BeautifulSoup
html = '''
<html><body><article>
  <h2>Soul Sanctum</h2>
  <div class="ad"><img src="https://guides.example.com/ad-banner.jpg" alt="Buy now"></div>
  <div class="advertisement"><p>Sponsored promo text</p></div>
  <section class="sponsored"><img src="https://guides.example.com/sponsor.png" alt="Sponsor"></section>
  <img src="https://guides.example.com/pixel.jpg" width="1" height="1" alt="tiny">
  <img src="https://stats.doubleclick.net/pagead/ad.jpg" alt="doubleclick">
  <img src="https://guides.example.com/user-avatar.png" alt="avatar">
  <img src="https://guides.example.com/ui/icon.svg" alt="svg icon">
  <img src="https://guides.example.com/maps/soul-master-route.jpg" width="800" height="450" alt="Soul Master route">
</article></body></html>
'''
secs = m.parse_walkthrough_sections(BeautifulSoup(html, "html.parser"), base_url="https://guides.example.com/")
print(json.dumps(secs[0].body))
""")
        self.assertIn("![Soul Master route](https://guides.example.com/maps/soul-master-route.jpg)", out)
        self.assertNotIn("ad-banner", out)
        self.assertNotIn("Sponsored promo text", out)
        self.assertNotIn("sponsor.png", out)
        self.assertNotIn("pixel.jpg", out)
        self.assertNotIn("doubleclick", out)
        self.assertNotIn("avatar", out)
        self.assertNotIn("icon.svg", out)

    def test_extracts_embedded_youtube_videos_and_ignores_ad_iframes(self):
        out = in_script_env(TEXT, """
from bs4 import BeautifulSoup
html = '''
<html><body><article>
  <h2>Mantis Lords Fight</h2>
  <p>Watch the dash timing:</p>
  <iframe src="https://www.youtube.com/embed/abcdefghijk?rel=0" title="YouTube video player"></iframe>
  <lite-youtube videoid="Bc-UtkWMuus" title="Mantis Lords Radiant Guide"></lite-youtube>
  <iframe src="https://ads.example.com/banner.html" title="Ad frame"></iframe>
</article></body></html>
'''
secs = m.parse_walkthrough_sections(BeautifulSoup(html, "html.parser"), base_url="https://guides.example.com/")
print(json.dumps(secs[0].body))
""")
        self.assertIn("- Walkthrough video: https://youtu.be/abcdefghijk", out)
        self.assertIn("- Mantis Lords Radiant Guide: https://youtu.be/Bc-UtkWMuus", out)
        self.assertNotIn("ads.example.com", out)

    def test_hero_artwork_is_emitted_in_full_toc_and_metadata_only_output(self):
        out = in_script_env(TEXT, """
html = '''
<html><head>
  <title>Celeste Guide</title>
  <meta property="og:image" content="/images/celeste-cover.png">
</head><body><article><h2>Chapter 1</h2><p>Dash to the summit.</p></article></body></html>
'''
m.fetch_html = lambda url: (url, html)
url = "https://guides.example.com/celeste/"
full = m.fetch_and_extract_url(url)
toc = m.fetch_and_extract_url(url, show_toc=True)
m.fetch_html = lambda url: (url, '''<html><head><title>Hub</title>
<meta name="twitter:image" content="https://cdn.example.com/hub-cover.webp">
</head><body></body></html>''')
metadata_only = m.fetch_and_extract_url(url, show_toc=True)
print(json.dumps([full, toc, metadata_only]))
""")
        expected = "- **Cover / Hero Artwork**: ![Cover Artwork](https://guides.example.com/images/celeste-cover.png)"
        self.assertIn(expected, out[0])
        self.assertIn(expected, out[1])
        self.assertIn("Chapter 1", out[1])
        self.assertIn("https://cdn.example.com/hub-cover.webp", out[2])
        self.assertIn("No structured walkthrough text found", out[2])

    def test_game8_archive_wrapper_flowcharts_and_table_battle_maps_are_extracted(self):
        out = in_script_env(TEXT, """
from bs4 import BeautifulSoup
html = '''
<html><body>
  <main><h2>Log In</h2><p>Tiny modal</p></main>
  <div class="archive-style-wrapper">
    <h2>Chapter 5 Walkthrough</h2>
    <table>
      <tr><th colspan="2">Battle Overview</th></tr>
      <tr><td colspan="2"><img data-src="https://img.game8.co/battle-map.png/show" width="728" height="410" alt="Chapter 5 Battle Map"></td></tr>
      <tr><th>Victory Condition</th><td>Defeat the Boss</td></tr>
      <tr><th>Units</th><td><img data-src="https://img.game8.co/cai-icon.png/show" width="60" height="60" alt="Cai Icon"> Cai</td></tr>
    </table>
    <h3>Thundercloud Knights Qualifier</h3>
    <div class="a-flowChart">
      <div class="a-flowChart__number">1</div>
      <div class="a-flowChart__title">Advance North</div>
      <div class="a-flowChart__content">
        <p class="a-paragraph">Advance north and bait the archers from the rooftops.</p>
        <p class="a-paragraph"><img data-src="https://img.game8.co/step1.png/show" width="728" height="410" alt="Baiting archers"></p>
      </div>
    </div>
  </div>
</body></html>
'''
secs = m.parse_walkthrough_sections(BeautifulSoup(html, "html.parser"), base_url="https://game8.co/games/Fire-Emblem")
print(json.dumps([(s.heading, s.body) for s in secs]))
""")
        headings = [h for h, _ in out]
        self.assertIn("Chapter 5 Walkthrough", headings)
        self.assertIn("Thundercloud Knights Qualifier", headings)
        self.assertNotIn("Log In", headings)
        ch5_body = dict(out)["Chapter 5 Walkthrough"]
        qual_body = dict(out)["Thundercloud Knights Qualifier"]
        self.assertIn("![Chapter 5 Battle Map](https://img.game8.co/battle-map.png/show)", ch5_body)
        self.assertNotIn("cai-icon.png", ch5_body)
        self.assertIn("1. **Advance North**", qual_body)
        self.assertIn("Advance north and bait the archers from the rooftops.", qual_body)
        self.assertIn("![Baiting archers](https://img.game8.co/step1.png/show)", qual_body)


class YouTubeWalkthrough(unittest.TestCase):
    def test_non_youtube_urls_are_never_fetched(self):
        site = Site(self, {"/watch?v=abcdefghijk": (302, {"Location": "https://www.youtube.com/watch?v=abcdefghijk"}, b"")})
        done = cli(YT, f"{site.url}/watch?v=abcdefghijk")
        self.assertIn("Not a YouTube video", done.stderr)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(site.requests, [])

    def test_known_redirectors_are_read_one_hop_without_following(self):
        site = Site(self, {"/r": (302, {"Location": "http://127.0.0.1:9/watch?v=abcdefghijk"}, b"")})
        out = in_script_env(YT, """
m.REDIRECTOR_HOSTS = {"127.0.0.1"}
print(json.dumps(m.resolve_if_redirect(sys.argv[2] + "/r")))
""", site.url)
        self.assertEqual(out, "http://127.0.0.1:9/watch?v=abcdefghijk")
        self.assertEqual(site.requests, ["/r"])

    def test_section_prefers_the_full_phrase(self):
        out = in_script_env(YT, """
chapters = {"duration": 900, "chapters": [
    {"title": "World 1-1", "start_time": 0, "end_time": 300},
    {"title": "World 1-2", "start_time": 300, "end_time": 600}]}
desc = {"duration": 900, "description": "0:00 World 1-1\\n5:00 World 1-2 castle\\n10:00 Credits"}
print(json.dumps([m.resolve_section_window(chapters, "World 1-2")[2],
                  m.resolve_section_window(desc, "World 1-2")[2],
                  m.resolve_section_window(desc, "castle world")[2],
                  m.resolve_section_window(desc, "nothing here")]))
""")
        self.assertEqual(out, ["World 1-2", "World 1-2 castle", "World 1-2 castle", None])

    def test_timestamp_lines_accept_minutes_past_59(self):
        out = in_script_env(YT, """
print(json.dumps(m.extract_timestamp_lines("75:30 Final Boss\\n1:02:03 Credits\\n04:05 Intro")))
""")
        self.assertEqual(out, [[4530.0, "Final Boss"], [3723.0, "Credits"], [245.0, "Intro"]])

    def test_parses_decimal_seconds(self):
        out = in_script_env(YT, """
print(json.dumps([m.parse_timestamp_str(s) for s in ("4:30.5", "90", "1:02:03")]))
""")
        self.assertEqual(out, [270.5, 90.0, 3723.0])

    def test_bad_time_range_or_grep_exits_with_one_line(self):
        for args in (["--time-range", "abc", "5:00"], ["--time-range", "4:30", "1:2:3:4"], ["--grep", "("]):
            done = cli(YT, "notavideo", *args)
            self.assertEqual(done.returncode, 2, (args, done.stderr))
            self.assertNotIn("Traceback", done.stderr, args)
            self.assertEqual(len(done.stderr.strip().splitlines()), 1, (args, done.stderr))

    def test_short_section_keywords_fall_back_to_the_phrase(self):
        out = in_script_env(YT, """
seen = []
m.fetch_transcript_segments = lambda vid, **kw: seen.append(kw["grep_pattern"]) or ["[00:01] reached W1 boss"]
md = m.render_video_markdown({"id": "abcdefghijk", "title": "t"}, section_query="W1")
print(json.dumps([seen, md.splitlines()[-1]]))
""")
        self.assertEqual(out, [["W1"], "[00:01] reached W1 boss"])

    def test_transcript_requests_have_a_timeout(self):
        out = in_script_env(YT, """
import requests
sent, clients = [], []
requests.Session.request = lambda self, *a, **kw: sent.append(kw.get("timeout"))
m._TimeoutSession().get("http://example.invalid/")
class FakeApi:
    def __init__(self, **kw):
        clients.append(type(kw.get("http_client")).__name__)
    def list(self, vid):
        raise RuntimeError("offline")
m.YouTubeTranscriptApi = FakeApi
m.fetch_transcript_segments("abcdefghijk")
print(json.dumps([sent, clients]))
""")
        self.assertEqual(out, [[20.0], ["_TimeoutSession"]])


if __name__ == "__main__":
    unittest.main()
