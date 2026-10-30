#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
#   "beautifulsoup4>=4.12.0",
# ]
# ///
"""
Extracts clean, ad-free text walkthrough sections, puzzle tables, ASCII maps,
walkthrough images, embedded YouTube video links, and Steam Community Guides
from walkthrough URLs (including Google Grounding redirect URLs returned by WebSearch).

Features:
  - Automatically resolves search redirect URLs to their canonical source URLs
  - Parses Steam Community Guides (.subSection blocks), GameFAQs (#faqtext & ASCII maps),
    MediaWiki/Fandom wikis, and standard walkthrough websites (IGN, Polygon, Eurogamer, Game8, etc.)
  - Preserves multi-column puzzle/boss tables as Markdown tables and <pre> blocks as ASCII maps
  - Extracts walkthrough images/maps as Markdown `![alt](https://...)` while filtering out ads & tracking pixels
  - Extracts embedded YouTube walkthrough videos (`<iframe>` / `<lite-youtube>`) as clean `https://youtu.be/<id>` links
  - Supports --toc to list all section headings with hierarchy levels
  - Supports hierarchical --section "<keyword>" filtering (matching an H2 heading
    automatically includes its child H3/H4 sub-sections, such as Light/Standard/Hard difficulty tabs)

Usage:
  # List all section headings in one or more walkthrough URLs:
  uv run .agents/skills/game-level-guide/scripts/fetch_text_guide.py "<url>" --toc

  # Extract specific section(s) (and their sub-headings) matching a keyword:
  uv run .agents/skills/game-level-guide/scripts/fetch_text_guide.py "<url>" --section "<Room/Puzzle/Boss>"
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# game-guide: URLs come from untrusted search results, so bound what one fetch can cost.
MAX_REDIRECTS = 5
MAX_BYTES = 5_000_000
DEADLINE_SECONDS = 30.0

AD_IMAGE_PATTERNS = re.compile(
    r"(?:doubleclick|googlesyndication|googleadservices|amazon-adsystem|adsystem|"
    r"taboola|outbrain|moatads|criteo|scorecardresearch|pixel|tracker|beacon|"
    r"1x1|spacer\.gif|blank\.gif|avatar|gravatar|emoji|smilie|logo|icon-)",
    re.IGNORECASE,
)
YOUTUBE_EMBED_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/embed/|youtu\.be/|youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})"
)


@dataclass
class Section:
    level: int
    heading: str
    body: str


def clean_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _best_srcset_url(srcset: str) -> str:
    """Pick the highest-resolution candidate URL from a srcset attribute."""
    best_url = ""
    best_score = -1.0
    for part in srcset.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        candidate = tokens[0]
        score = 1.0
        if len(tokens) > 1:
            m = re.match(r"^(\d+(?:\.\d+)?)([wx])$", tokens[1].lower())
            if m:
                score = float(m.group(1))
        if score >= best_score:
            best_score = score
            best_url = candidate
    return best_url


def image_to_markdown(img: Tag, base_url: str = "", caption: str = "", min_dim: int = 80) -> str:
    """Convert an <img> tag into Markdown `![alt](url)`, skipping ads, small icons, and tracking pixels."""
    for attr in ("width", "height"):
        raw_dim = (img.get(attr) or "").strip().rstrip("px")
        if raw_dim.isdigit() and int(raw_dim) < min_dim:
            return ""

    raw_url = ""
    for attr in ("data-src", "data-lazy-src", "data-original", "data-image-src"):
        val = (img.get(attr) or "").strip()
        if val and not val.startswith("data:"):
            raw_url = val
            break
    if not raw_url:
        for attr in ("data-srcset", "srcset"):
            val = (img.get(attr) or "").strip()
            if val:
                picked = _best_srcset_url(val)
                if picked and not picked.startswith("data:"):
                    raw_url = picked
                    break
    if not raw_url:
        val = (img.get("src") or "").strip()
        if val and not val.startswith("data:"):
            raw_url = val

    if not raw_url:
        return ""

    resolved = urljoin(base_url, raw_url) if base_url else raw_url
    parsed = urlparse(resolved)
    if parsed.scheme not in ("http", "https"):
        return ""
    if parsed.path.lower().endswith(".svg"):
        return ""
    if AD_IMAGE_PATTERNS.search(resolved):
        return ""

    alt = clean_whitespace(
        caption
        or str(img.get("alt") or "")
        or str(img.get("title") or "")
        or "Walkthrough image"
    )
    alt = re.sub(r"[\[\]\r\n]+", " ", alt).strip() or "Walkthrough image"
    return f"![{alt}]({resolved})"


def youtube_embed_to_markdown(node: Tag) -> str:
    """Extract YouTube video links from <iframe> or <lite-youtube> walkthrough embeds."""
    vid = (node.get("videoid") or node.get("data-video-id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        vid = ""
        for attr in ("src", "data-src", "data-lazy-src"):
            val = str(node.get(attr) or "")
            m = YOUTUBE_EMBED_RE.search(val)
            if m:
                vid = m.group(1)
                break
    if not vid:
        return ""
    title = clean_whitespace(str(node.get("title") or "Walkthrough video"))
    if title.lower() in ("youtube video player", "video player", "iframe", ""):
        title = "Walkthrough video"
    return f"- {title}: https://youtu.be/{vid}"


def table_to_markdown(table: Tag, base_url: str = "") -> str:
    rows: list[list[str]] = []
    extracted_images: list[str] = []
    for img in table.find_all("img"):
        md_img = image_to_markdown(img, base_url=base_url, min_dim=200)
        if md_img and md_img not in extracted_images:
            extracted_images.append(md_img)

    for tr in table.find_all("tr"):
        cells = [
            clean_whitespace(cell.get_text(" ", strip=True)).replace("|", "\\|")
            for cell in tr.find_all(["th", "td"])
        ]
        if any(cells):
            rows.append(cells)
    if not rows:
        return "\n".join(extracted_images)
    col_count = max(len(r) for r in rows)
    # Convert 1-column layout boxes into clean bullet points instead of awkward 1-cell tables
    if col_count == 1:
        table_md = "\n".join(f"- {r[0]}" for r in rows if r[0])
    else:
        normalized = [r + [""] * (col_count - len(r)) for r in rows]
        header = "| " + " | ".join(normalized[0]) + " |"
        divider = "| " + " | ".join(["---"] * col_count) + " |"
        body = ["| " + " | ".join(r) + " |" for r in normalized[1:]]
        table_md = "\n".join([header, divider, *body])
    if extracted_images:
        return "\n\n".join(extracted_images) + "\n\n" + table_md
    return table_md


def flowchart_to_markdown(node: Tag, base_url: str = "") -> str:
    """Convert a Game8 .a-flowChart step block into numbered Markdown with its walkthrough screenshot."""
    num_el = node.select_one(".a-flowChart__number")
    title_el = node.select_one(".a-flowChart__title")
    content_el = node.select_one(".a-flowChart__content")
    num = clean_whitespace(num_el.get_text(" ", strip=True)) if num_el else ""
    title = clean_whitespace(title_el.get_text(" ", strip=True)) if title_el else ""
    header = f"{num}. **{title}**" if (num and title) else (f"**{title}**" if title else "")
    body = clean_whitespace(node_to_markdown(content_el, base_url=base_url)) if content_el else ""
    if header and body:
        return f"{header}\n{body}"
    return header or body


def node_to_markdown(node: Tag | NavigableString, base_url: str = "") -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name in ("script", "style", "noscript", "svg", "nav", "footer", "aside"):
        return ""
    classes = set(node.get("class") or [])
    if "a-flowChart" in classes:
        fc = flowchart_to_markdown(node, base_url=base_url)
        return f"\n{fc}\n" if fc else ""
    if name in ("iframe", "lite-youtube"):
        yt = youtube_embed_to_markdown(node)
        return f"\n{yt}\n" if yt else ""
    if name == "figure":
        img = node.find("img")
        if not img:
            return ""
        figcaption = node.find("figcaption")
        caption = clean_whitespace(figcaption.get_text(" ", strip=True)) if figcaption else ""
        md = image_to_markdown(img, base_url=base_url, caption=caption)
        return f"\n{md}\n" if md else ""
    if name == "img":
        md = image_to_markdown(node, base_url=base_url)
        return f"\n{md}\n" if md else ""
    if name == "table":
        return "\n\n" + table_to_markdown(node, base_url=base_url) + "\n\n"
    if name == "pre":
        raw = node.get_text()
        return f"\n\n```text\n{raw.strip()}\n```\n\n"
    if name in ("ul", "ol"):
        items = []
        for idx, li in enumerate(node.find_all("li", recursive=False), start=1):
            bullet = f"{idx}." if name == "ol" else "-"
            li_text = clean_whitespace(node_to_markdown_inline(li, base_url=base_url))
            if li_text:
                items.append(f"{bullet} {li_text}")
        return "\n" + "\n".join(items) + "\n"
    if name in ("p", "div", "section", "article", "blockquote"):
        parts = [node_to_markdown(child, base_url=base_url) for child in node.children]
        combined = "".join(parts).strip()
        if name == "blockquote" and combined:
            return "\n> " + combined.replace("\n", "\n> ") + "\n"
        return f"\n{combined}\n" if combined else ""
    if name in ("strong", "b"):
        txt = node.get_text(" ", strip=True)
        return f" **{txt}** " if txt else ""
    if name in ("code", "kbd"):
        txt = node.get_text(" ", strip=True)
        return f" `{txt}` " if txt else ""
    if name == "br":
        return "\n"

    return "".join(node_to_markdown(child, base_url=base_url) for child in node.children)


def node_to_markdown_inline(node: Tag, base_url: str = "") -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            cname = child.name.lower()
            if cname in ("strong", "b"):
                txt = child.get_text(" ", strip=True)
                if txt:
                    parts.append(f" **{txt}** ")
            elif cname in ("code", "kbd"):
                txt = child.get_text(" ", strip=True)
                if txt:
                    parts.append(f" `{txt}` ")
            elif cname == "img":
                md = image_to_markdown(child, base_url=base_url)
                if md:
                    parts.append(f" {md} ")
            else:
                parts.append(child.get_text(" ", strip=True))
    return "".join(parts)


def parse_steam_guide(soup: BeautifulSoup, base_url: str = "") -> list[Section]:
    """Extract sections from a Steam Community Guide page."""
    sections: list[Section] = []
    for box in soup.select(".subSection"):
        title_el = box.select_one(".subSectionTitle")
        desc_el = box.select_one(".subSectionDesc")
        title = clean_whitespace(title_el.get_text(" ", strip=True)) if title_el else "Overview"
        body = clean_whitespace(node_to_markdown(desc_el, base_url=base_url)) if desc_el else ""
        if body:
            sections.append(Section(level=2, heading=title, body=body))
    return sections


def _pick_main_container(soup: BeautifulSoup) -> Tag:
    """Pick the richest walkthrough content container, avoiding small login/modal <main> tags."""
    specific_selectors = (
        ".archive-style-wrapper, .p-archiveContent__main, .mw-parser-output, "
        ".faqtext, #faqtext, .article-content, .entry-content, .post-content, .guide-content"
    )
    specific_hits = soup.select(specific_selectors)
    if specific_hits:
        return max(specific_hits, key=lambda el: len(el.get_text()))

    generic_hits = soup.select("article, main")
    if generic_hits:
        best_generic = max(generic_hits, key=lambda el: len(el.get_text()))
        body = soup.body or soup
        if len(best_generic.get_text()) >= min(2500, int(len(body.get_text()) * 0.35)):
            return best_generic
    return soup.body or soup


def parse_walkthrough_sections(soup: BeautifulSoup, base_url: str = "") -> list[Section]:
    """Split a webpage into hierarchical Section(level, heading, body) objects."""
    for junk in soup.select(
        "script, style, noscript, nav, footer, header, aside, "
        ".ad, .ads, .advertisement, .ad-unit, .ad-container, .ad-slot, .ad-wrapper, "
        ".sponsored, .newsletter, .promo-box, .affiliate, .sidebar, .comments, .related, "
        ".toc, #toc, .navbox, .cookie-banner, [aria-label='advertisement']"
    ):
        junk.decompose()

    if soup.select(".subSection"):
        steam_sections = parse_steam_guide(soup, base_url=base_url)
        if steam_sections:
            return steam_sections

    main_container = _pick_main_container(soup)

    # select_one only searches descendants, so check the container itself first.
    if main_container.css.match("#faqtext, .faqtext"):
        faq_pre = main_container
    else:
        faq_pre = main_container.select_one("#faqtext, .faqtext")
    if faq_pre and not faq_pre.find(["h1", "h2", "h3"]):
        raw_text = faq_pre.get_text()
        chunks = re.split(r"\n(?=[A-Z0-9][A-Z0-9 \-\.:]{4,60}\n[-=]{4,})", raw_text)
        if len(chunks) > 1:
            return [
                Section(level=2, heading=chunk.splitlines()[0].strip(), body=chunk.strip())
                for chunk in chunks
                if chunk.strip()
            ]
        return [Section(level=2, heading="Walkthrough", body=raw_text.strip())]

    sections: list[Section] = []
    current_level = 1
    current_heading = "Overview"
    current_parts: list[str] = []
    seen_media: set[str] = set()

    heading_levels = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
    block_tags = ("p", "ul", "ol", "table", "pre", "blockquote", "figure", "img", "iframe", "lite-youtube")
    skip_parents = ["table", "ul", "ol", "pre", "blockquote", "p", "figure"]

    for elem in main_container.descendants:
        if not isinstance(elem, Tag):
            continue
        if elem.name in heading_levels:
            content = clean_whitespace("".join(current_parts))
            if content:
                sections.append(Section(level=current_level, heading=current_heading, body=content))
            current_level = heading_levels[elem.name]
            current_heading = clean_whitespace(elem.get_text(" ", strip=True))
            current_parts = []
            seen_media = set()
            continue

        is_flowchart = elem.name == "div" and "a-flowChart" in (elem.get("class") or [])
        if elem.name in block_tags or is_flowchart:
            if elem.find_parent(skip_parents):
                continue
            if not is_flowchart and elem.find_parent(class_="a-flowChart"):
                continue
            rendered = clean_whitespace(node_to_markdown(elem, base_url=base_url))
            if not rendered:
                continue
            if elem.name in ("figure", "img", "iframe", "lite-youtube"):
                if rendered in seen_media:
                    continue
                seen_media.add(rendered)
            current_parts.append(rendered + "\n\n")

    final_content = clean_whitespace("".join(current_parts))
    if final_content:
        sections.append(Section(level=current_level, heading=current_heading, body=final_content))

    return sections


def contains_term(text: str, term: str) -> bool:
    """Case-insensitive substring match where a number never matches inside a longer number."""
    return re.search(rf"(?<!\d){re.escape(term)}(?!\d)", text, re.IGNORECASE) is not None


def best_heading_matches(headings: list[str], query: str) -> list[int]:
    """Indices of headings containing the whole query, else those matching the most keywords."""
    phrase_hits = [i for i, h in enumerate(headings) if contains_term(h, query.strip())]
    if phrase_hits:
        return phrase_hits
    scores = [sum(contains_term(h, k) for k in query.split()) for h in headings]
    best = max(scores, default=0)
    return [i for i, score in enumerate(scores) if best and score == best]


def filter_sections_hierarchically(sections: list[Section], query: str) -> list[Section]:
    """
    Match sections by keyword in heading or body.
    When a heading at level L matches, automatically include all subsequent child sections
    (level > L) until the next heading of level <= L.
    """
    keywords = [k.lower() for k in query.split() if k.strip()]
    included_indices: set[int] = set()

    for i in best_heading_matches([sec.heading for sec in sections], query):
        included_indices.add(i)
        # Include child sub-headings (e.g., H3 Light/Standard/Hard under H2 Solution)
        for j in range(i + 1, len(sections)):
            if sections[j].level > sections[i].level:
                included_indices.add(j)
            else:
                break

    # Also include sections where the body matches keywords
    for i, sec in enumerate(sections):
        b_lower = sec.body.lower()
        if all(k in b_lower for k in keywords) or (not included_indices and any(k in b_lower for k in keywords)):
            included_indices.add(i)

    return [sections[i] for i in sorted(included_indices)]


def check_public_url(url: str) -> list[str]:
    """Refuse non-http(s) URLs and hosts that resolve to loopback, private or otherwise non-public addresses.

    Returns the checked addresses, so the request connects to them rather than resolving the host again (DNS rebinding).
    """
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise ValueError(f"Refusing to fetch non-http(s) URL: {url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addrs = []
    for *_, sockaddr in socket.getaddrinfo(parsed.host, port, proto=socket.IPPROTO_TCP):
        addr = ipaddress.ip_address(sockaddr[0].split("%")[0])
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if not addr.is_global or addr.is_multicast:
            raise ValueError(f"Refusing to fetch {url}: {parsed.host} resolves to non-public address {addr}")
        addrs.append(str(addr))
    return list(dict.fromkeys(addrs))


def uses_proxy(target: httpx.URL) -> bool:
    """Whether httpx sends this URL through a proxy from the environment (it reads the same variables)."""
    proxies = urllib.request.getproxies_environment()
    return bool(proxies.get(target.scheme) or proxies.get("all")) and not urllib.request.proxy_bypass_environment(target.host)


def connect(client: httpx.Client, target: httpx.URL, addrs: list[str | None], deadline: float) -> httpx.Response:
    """Open a streamed GET to target, trying each checked address in turn, within the overall deadline."""
    for i, addr in enumerate(addrs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Fetch took longer than {DEADLINE_SECONDS:g}s")
        request_url, headers, extensions = target, {}, {}
        if addr:
            # Connect to the address just checked; Host and TLS SNI/certificate still use the real hostname.
            request_url = target.copy_with(host=addr)
            headers = {"Host": target.netloc.decode("ascii")}
            extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
        request = client.build_request("GET", request_url, headers=headers, extensions=extensions,
                                       timeout=min(20.0, remaining))
        try:
            return client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout):  # ConnectTimeout isn't a ConnectError
            if i == len(addrs) - 1:
                raise
    raise AssertionError("unreachable")


def fetch_html(url: str) -> tuple[str, str]:
    """GET url, following redirects by hand so every hop is checked, with a size cap and overall deadline."""
    deadline = time.monotonic() + DEADLINE_SECONDS
    with httpx.Client(headers=HEADERS) as client:
        for _ in range(MAX_REDIRECTS + 1):
            target, approved = httpx.URL(url), check_public_url(url)
            # A proxy resolves the name itself, and its CONNECT tunnel would send a pinned IP as the TLS server name.
            resp = connect(client, target, [None] if not approved or uses_proxy(target) else approved, deadline)
            try:
                if resp.is_redirect:
                    url = str(target.join(resp.headers["location"]))
                    continue
                if resp.status_code >= 400:  # not raise_for_status: it would name the pinned IP, not the page
                    raise ValueError(f"{resp.status_code} {resp.reason_phrase} for {url}")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise ValueError(f"Response larger than {MAX_BYTES} bytes")
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"Fetch took longer than {DEADLINE_SECONDS:g}s")
                return url, body.decode(resp.encoding or "utf-8", errors="replace")
            finally:
                resp.close()
    raise ValueError(f"More than {MAX_REDIRECTS} redirects")


def extract_hero_artwork(soup: BeautifulSoup, base_url: str = "") -> str:
    """Extract the page's primary og:image or twitter:image URL as a Markdown image link if valid."""
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").strip().lower()
        if prop in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"):
            content = (meta.get("content") or "").strip()
            if content:
                resolved = urljoin(base_url, content) if base_url else content
                if resolved.startswith("https://") and not AD_IMAGE_PATTERNS.search(resolved):
                    return resolved
    return ""


def fetch_and_extract_url(
    url: str,
    section_query: str | None = None,
    show_toc: bool = False,
    max_chars: int = 14000,
) -> str:
    resolved_url, html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    page_title = clean_whitespace(soup.title.get_text()) if soup.title else resolved_url
    hero_artwork = extract_hero_artwork(soup, base_url=resolved_url)
    sections = parse_walkthrough_sections(soup, base_url=resolved_url)

    if not sections:
        if not hero_artwork and not show_toc:
            return f"No structured walkthrough text found at {resolved_url}"
        heading = f"## Table of Contents: {page_title}" if show_toc else f"# {page_title}"
        label = "URL" if show_toc else "Source"
        out_lines = [heading, f"- **{label}**: {resolved_url}"]
        if hero_artwork:
            out_lines.append(f"- **Cover / Hero Artwork**: ![Cover Artwork]({hero_artwork})")
        out_lines.extend(["", f"No structured walkthrough text found at {resolved_url}"])
        return "\n".join(out_lines)

    if show_toc:
        toc_lines = [f"## Table of Contents: {page_title}", f"- **URL**: {resolved_url}"]
        if hero_artwork:
            toc_lines.append(f"- **Cover / Hero Artwork**: ![Cover Artwork]({hero_artwork})")
        toc_lines.append("")
        for idx, sec in enumerate(sections, start=1):
            indent = "  " * max(0, sec.level - 1)
            toc_lines.append(f"{indent}{idx}. **[H{sec.level}] {sec.heading}** ({len(sec.body)} chars)")
        return "\n".join(toc_lines)

    if section_query:
        selected = filter_sections_hierarchically(sections, section_query)
        if not selected:
            toc_preview = ", ".join(f'"{s.heading}"' for s in sections[:25])
            return (
                f"No sections matched '{section_query}' in {page_title}.\n"
                f"Available sections: {toc_preview}"
            )
    else:
        selected = sections

    out_lines = [f"# {page_title}", f"- **Source**: {resolved_url}"]
    if hero_artwork:
        out_lines.append(f"- **Cover / Hero Artwork**: ![Cover Artwork]({hero_artwork})")
    out_lines.append("")
    total_len = 0
    for sec in selected:
        hashes = "#" * min(4, max(2, sec.level + 1))
        block = f"{hashes} {sec.heading}\n\n{sec.body}\n"
        if total_len + len(block) > max_chars:
            if total_len == 0:
                out_lines.append(block[:max_chars])
            out_lines.append(
                f"\n... [Output truncated at {max_chars} chars; use --section '<heading>' "
                f"or --toc to inspect specific sections]"
            )
            break
        out_lines.append(block)
        total_len += len(block)

    return "\n".join(out_lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract clean text walkthroughs, tables, ASCII maps, images, and Steam guides."
    )
    parser.add_argument(
        "urls",
        nargs="+",
        help="One or more Walkthrough, Wiki, or Steam Guide URLs (supports search redirect URLs).",
    )
    parser.add_argument(
        "--section",
        "-s",
        help="Filter walkthrough to sections (and child sub-sections) matching this keyword.",
    )
    parser.add_argument(
        "--toc",
        action="store_true",
        help="List all section headings in the walkthrough without printing full bodies.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=14000,
        help="Maximum characters to output per URL (default: 14000).",
    )
    args = parser.parse_args()

    outputs: list[str] = []
    for url in args.urls:
        try:
            outputs.append(
                fetch_and_extract_url(
                    url,
                    section_query=args.section,
                    show_toc=args.toc,
                    max_chars=args.max_chars,
                )
            )
        except Exception as exc:
            outputs.append(f"Error fetching {url}: {exc}")

    print("\n\n---\n\n".join(outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
