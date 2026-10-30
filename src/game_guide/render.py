"""The Renderer: the notes Markdown dialect turned into HTML in one place. Its interface is render() and a Policy;
behind it sit the dialect's rules: `![[path|caption]]` images, a YouTube source line becoming a video, the spoiler
placeholder line becoming a reveal, raw HTML staying escaped, and local images confined to <notes dir>/images.
The Policy is the seam between its two adapters: the web page gets /images/ URLs and data-* placeholders that its
script hydrates; the share export gets a standalone file, with images as data: URIs and videos as plain links."""

from __future__ import annotations

import base64
import hashlib
import html
import mimetypes
import re
import threading
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

from . import cache
from .notes import HIDDEN_NOTE, IMAGE_EXTS, YOUTUBE, linked_image_stamp, videos

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 40 * 1024 * 1024
WIKI_IMAGE = re.compile(r"!\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")
LEVEL_NAMES = ("Hint", "Strategy", "Answer")
PLACEHOLDER = re.compile(re.escape(HIDDEN_NOTE).replace(re.escape("{level}"), f"({'|'.join(LEVEL_NAMES)})"))


@dataclass
class Policy:
    """How rendered media leaves the renderer. The default is the web page's; a share export passes one
    Policy(share=True) to all of its render() calls, so the byte caps on inlined images span the whole file."""
    share: bool = False
    image_bytes: int = 0  # inlined so far


_HTML: OrderedDict = OrderedDict()
_HTML_LOCK = threading.Lock()
_HTML_MAX = 512
_PARSER = threading.local()


def render(text: str, directory: Path, policy: Policy | None = None) -> str:
    """`text`, written in the notes folder `directory`, as HTML under `policy` (the web page's by default)."""
    policy = policy or Policy()
    base = Path(directory).resolve()
    if policy.share:  # share tallies inlined bytes on the policy; a cache hit would skip that
        return _render(text, base, policy)
    stamp = linked_image_stamp(text, base)
    key = (hashlib.blake2s(text.encode("utf-8", "surrogatepass"), digest_size=16).digest(), str(base), stamp)
    hit = cache.recall(_HTML, key, _HTML_LOCK)
    if hit is not cache.MISS:
        return hit
    html_out = _render(text, base, policy)
    if linked_image_stamp(text, base) == stamp:
        cache.remember(_HTML, key, html_out, _HTML_MAX, _HTML_LOCK)
    return html_out


def _render(text: str, base: Path, policy: Policy) -> str:
    md = _markdown()
    tokens = md.parse(text)
    for i, token in enumerate(tokens):
        if token.type == "video":
            token.type, token.content = "html_block", _video_html(token.meta, policy)
        elif token.type == "reveal":
            token.type, token.content = "html_block", _reveal_html(token.meta["level"], policy)
        elif token.type == "heading_open" and token.tag == "h4" and not policy.share and tokens[i + 1].content in LEVEL_NAMES:
            token.attrSet("class", f"level-heading level-{tokens[i + 1].content.lower()}")
            token.tag = tokens[i + 2].tag = "div"
        for child in token.children or []:
            if child.type == "image":
                _image(child, base, policy)
            elif child.type == "link_open" and not policy.share:  # never navigate the page away from the app
                child.attrSet("target", "_blank")
                child.attrSet("rel", "noopener noreferrer")
    return md.renderer.render(tokens, md.options, {})


def _markdown() -> MarkdownIt:
    # MarkdownIt is not thread-safe, and building one is most of a short chapter's render.
    # One parser per thread is reused; each parse() gets a fresh token state.
    md = getattr(_PARSER, "md", None)
    if md is None:
        md = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable(["table", "linkify"])
        md.inline.ruler.before("image", "wiki_image", _wiki_image)
        md.block.ruler.before("list", "video", _video_line, {"alt": ["paragraph", "reference", "blockquote", "list"]})
        md.block.ruler.before("list", "reveal", _reveal_line)
        md.core.ruler.push("prose_videos", _prose_videos)
        _PARSER.md = md
    return md


def _wiki_image(state, silent: bool) -> bool:
    match = WIKI_IMAGE.match(state.src, state.pos)
    if not match:
        return False
    if not silent:
        token = state.push("image", "img", 0)
        token.attrSet("src", match.group(1))
        token.content = match.group(2) or Path(match.group(1)).name
        caption = Token("text", "", 0)
        caption.content = token.content
        token.children = [caption]
    state.pos = match.end()
    return True


def _line(state, start: int) -> str:
    return state.src[state.bMarks[start] + state.tShift[start]:state.eMarks[start]]


def _video_line(state, start: int, end: int, silent: bool) -> bool:
    """A YouTube source line (one video and a short label, not prose) becomes a video in place of the line."""
    line = _line(state, start)
    found, rest = videos(line), YOUTUBE.sub("", line).strip()
    if state.is_code_block(start) or len(found) != 1 or len(rest) >= 95 or re.search(r"[.!?].*[.!?]", rest):
        return False
    if not silent:
        token = state.push("video", "", 0)
        token.map, token.meta = [start, start + 1], {"video": found[0], "prose": False}
        state.line = start + 1
    return True


def _reveal_line(state, start: int, end: int, silent: bool) -> bool:
    match = None if state.is_code_block(start) else PLACEHOLDER.fullmatch(_line(state, start).rstrip())
    if match and not silent:
        token = state.push("reveal", "", 0)
        token.map, token.meta = [start, start + 1], {"level": match.group(1)}
        state.line = start + 1
    return bool(match)


def _prose_videos(state) -> None:
    """A video named mid-paragraph keeps its link there and also gets a video after the paragraph."""
    tokens = []
    for token in state.tokens:
        tokens.append(token)
        if token.type == "paragraph_close":  # always preceded by the paragraph's inline token
            tokens += [Token("video", "", 0, meta={"video": v, "prose": True}, block=True) for v in videos(tokens[-2].content)]
    state.tokens = tokens


def _video_html(meta: dict, policy: Policy) -> str:
    video = meta["video"]
    link = f'<a href="{video.url}">{html.escape(video.label)}</a>'
    if policy.share:
        return "" if meta["prose"] else f"<p>{link}</p>\n"
    return f'<p data-video="{video.id}" data-at="{video.at}" data-label="{html.escape(video.label)}">{link}</p>\n'


def _reveal_html(level: str, policy: Policy) -> str:
    if policy.share:
        return f"<p><em>{level} omitted from this shared guide.</em></p>\n"
    return f'<div class="redacted-box" data-reveal="{level.lower()}"><span><strong>{level}</strong> hidden — raise the spoiler level to see it.</span></div>\n'


def _image(token: Token, base: Path, policy: Policy) -> None:
    """Point an image at its file under <base>/images, or make it plain text when it has none there."""
    root = (base / "images").resolve()
    parsed = urllib.parse.urlsplit(token.attrGet("src") or "")
    path = (base / urllib.parse.unquote(parsed.path)).resolve()
    if (parsed.scheme or parsed.netloc or not root.is_relative_to(base) or not path.is_relative_to(root)
            or path.suffix.lower() not in IMAGE_EXTS or not path.is_file()):
        token.type, token.content = "text", f"[Image unavailable: {token.content}]"
        return
    if policy.share:
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES or policy.image_bytes + size > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("The images in this guide are too large to share as one file.")
        data = path.read_bytes()
        policy.image_bytes += len(data)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        token.attrSet("src", f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
        return
    src = html.escape("/images/" + urllib.parse.quote(path.relative_to(root).as_posix()))
    alt = html.escape(token.content or path.name)
    token.type, token.content = "html_inline", (
        f'<figure class="guide-figure"><img src="{src}" alt="{alt}" loading="lazy"><figcaption><span>{alt}</span>'
        '<span style="font-family:var(--font-mono);font-size:12px">Click to inspect</span></figcaption></figure>')
