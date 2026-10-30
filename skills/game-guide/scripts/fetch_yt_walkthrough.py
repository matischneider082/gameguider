#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "yt-dlp",
#   "youtube-transcript-api>=1.0.0",
# ]
# ///
"""
Extracts text walkthrough data (and optional visual storyboard grids) from YouTube videos
so the user never has to watch a video:
- Automatically resolves search redirect URLs to canonical YouTube URLs
- Video title, URL, channel, and duration
- Official YouTube Chapters + parsed timestamp lines from description/comments
- Video description (often contains puzzle codes, timestamps, or written steps)
- Pinned & top-voted YouTube comments (where community members post exact codes/steps)
- Spoken transcript grouped into timestamped segments, with optional:
    * --section "<keyword>" to slice transcript to a matching chapter/timestamp window
    * --time-range START END (e.g., 04:30 08:15) to slice transcript by time
    * --grep "<pattern>" to search a long transcript for keywords with surrounding context
- Optional --save-storyboards <dir>: downloads YouTube's pre-rendered 3x3 storyboard
  frame grids (.jpg) for the target time window in milliseconds (no video download needed)
  so the agent can inspect silent/visual-only walkthrough moments with Read and
  describe them in text for the user.

Usage:
  uv run .agents/skills/game-level-guide/scripts/fetch_yt_walkthrough.py <url_or_video_id> [options]
  uv run .agents/skills/game-level-guide/scripts/fetch_yt_walkthrough.py --search "<query>" [--section "<level>"]
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests  # installed by youtube-transcript-api
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

# Minutes may exceed 59 when there is no hours part (e.g. "75:30 Final Boss").
TIMESTAMP_RE = re.compile(r"\b(?:(\d{1,2}):(?=[0-5]\d:))?(\d{1,3}):([0-5]\d)\b")
# game-guide: only these redirectors are fetched (one hop), so an untrusted URL can't make us GET arbitrary hosts.
REDIRECTOR_HOSTS = {"vertexaisearch.cloud.google.com"}
TRANSCRIPT_TIMEOUT = 20.0
STORYBOARD_ROOT = Path("/tmp/game-guide").resolve()
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def resolve_if_redirect(url_or_id: str) -> str:
    """Resolve known redirector URLs (vertexaisearch grounding links) to the URL they point at."""
    candidate = url_or_id.strip()
    if not candidate.startswith(("http://", "https://")):
        return candidate
    if (urlparse(candidate).hostname or "").lower() not in REDIRECTOR_HOSTS:
        return candidate
    try:
        req = urllib.request.Request(candidate, headers={"User-Agent": USER_AGENT})
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=10):
            return candidate
    except urllib.error.HTTPError as exc:
        return exc.headers.get("Location") or candidate
    except Exception:
        return candidate


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface the redirect as an HTTPError so only its Location is read, never fetched."""

    def redirect_request(self, *args, **kwargs):
        return None


class _TimeoutSession(requests.Session):
    """youtube-transcript-api has no timeout option, so default one on every request."""

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", TRANSCRIPT_TIMEOUT)
        return super().request(*args, **kwargs)


def extract_video_id(url_or_id: str) -> str | None:
    """Extract an 11-character YouTube video ID from a URL (including redirects) or raw ID."""
    candidate = resolve_if_redirect(url_or_id)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        return candidate

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        vid = parsed.path.lstrip("/").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            return vid
    if "youtube.com" in host:
        if parsed.path == "/watch":
            qs = parse_qs(parsed.query)
            vid = (qs.get("v") or [None])[0]
            if vid and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                return vid
        for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
            if parsed.path.startswith(prefix):
                vid = parsed.path[len(prefix) :].split("/")[0]
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                    return vid
    return None


def parse_timestamp_str(ts_str: str) -> float:
    """Parse MM:SS or HH:MM:SS or raw seconds into float seconds."""
    s = ts_str.strip()
    if not re.fullmatch(r"\d+(?::\d+){0,2}(?:\.\d+)?", s):
        raise ValueError(f"Invalid timestamp format: {ts_str}")
    total = 0.0
    for part in s.split(":"):
        total = total * 60 + float(part)
    return total


def format_timestamp(seconds: float | int | None) -> str:
    if seconds is None:
        return "00:00"
    total = max(0, int(seconds))
    hrs, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def extract_timestamp_lines(text: str) -> list[tuple[float, str]]:
    """Parse lines containing timestamps (e.g. '02:15 - Clock Puzzle') from descriptions or comments."""
    found: list[tuple[float, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = TIMESTAMP_RE.search(line)
        if not m:
            continue
        hrs = int(m.group(1)) if m.group(1) else 0
        mins = int(m.group(2))
        secs = int(m.group(3))
        total_sec = float(hrs * 3600 + mins * 60 + secs)
        label = line.replace(m.group(0), "").strip(" -–—:|[]()")
        if label:
            found.append((total_sec, label))
    return found


def resolve_section_window(
    info: dict,
    section_query: str,
) -> tuple[float, float, str] | None:
    """Find a (start_sec, end_sec, chapter_title) window matching section_query in chapters or description."""
    duration = float(info.get("duration") or 36000)

    chapters = info.get("chapters") or []
    idx = best_title_match([(ch.get("title") or "").strip() for ch in chapters], section_query)
    if idx is not None:
        ch = chapters[idx]
        start = float(ch.get("start_time") or 0)
        end = float(ch.get("end_time") or duration)
        return max(0.0, start - 5.0), min(duration, end + 10.0), (ch.get("title") or "").strip()

    desc_ts = extract_timestamp_lines(info.get("description") or "")
    idx = best_title_match([label for _, label in desc_ts], section_query)
    if idx is not None:
        start, label = desc_ts[idx]
        end = desc_ts[idx + 1][0] if idx + 1 < len(desc_ts) else min(duration, start + 600.0)
        return max(0.0, start - 5.0), min(duration, end + 10.0), label

    return None


def contains_term(text: str, term: str) -> bool:
    """Case-insensitive substring match where a number never matches inside a longer number."""
    return re.search(rf"(?<!\d){re.escape(term)}(?!\d)", text, re.IGNORECASE) is not None


def best_title_match(titles: list[str], query: str) -> int | None:
    """First title containing the whole query, else the first matching the most keywords."""
    for i, title in enumerate(titles):
        if contains_term(title, query.strip()):
            return i
    scores = [sum(contains_term(t, k) for k in query.split()) for t in titles]
    best = max(scores, default=0)
    return scores.index(best) if best else None


def save_storyboard_sheets(
    info: dict,
    out_dir: Path,
    start_sec: float | None = None,
    end_sec: float | None = None,
    max_sheets: int = 4,
) -> list[str]:
    """
    Download YouTube storyboard grid JPEGs (sb0/sb1) covering [start_sec, end_sec]
    so the agent can inspect visual-only steps with Read without downloading video.
    """
    formats = info.get("formats") or []
    storyboards = [f for f in formats if f.get("format_note") == "storyboard" and f.get("fragments")]
    if not storyboards:
        return []

    # Pick highest resolution storyboard (last in list, usually sb0)
    sb = storyboards[-1]
    rows = int(sb.get("rows") or 3)
    cols = int(sb.get("columns") or 3)
    fragments = sb.get("fragments") or []

    out_dir.mkdir(parents=True, exist_ok=True)
    video_id = info.get("id") or "video"
    notes: list[str] = []

    cur_time = 0.0
    saved_count = 0

    for idx, frag in enumerate(fragments):
        frag_dur = float(frag.get("duration") or 18.0)
        frag_start = cur_time
        frag_end = cur_time + frag_dur
        cur_time = frag_end

        if start_sec is not None and frag_end < start_sec:
            continue
        if end_sec is not None and frag_start > end_sec:
            break

        frag_url = frag.get("url")
        if not frag_url:
            continue

        file_path = out_dir / f"{video_id}_storyboard_{idx:02d}_{format_timestamp(frag_start).replace(':', 'm')}s.jpg"
        try:
            req = urllib.request.Request(frag_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                file_path.write_bytes(resp.read())
        except Exception as exc:
            notes.append(f"- Failed to download storyboard sheet {idx}: {exc}")
            continue

        saved_count += 1
        cell_dur = frag_dur / max(1, rows * cols)
        grid_timestamps = []
        for r in range(rows):
            row_ts = [
                f"`{format_timestamp(frag_start + (r * cols + c) * cell_dur)}`"
                for c in range(cols)
            ]
            grid_timestamps.append(f"Row {r + 1}: " + " | ".join(row_ts))

        notes.append(
            f"- **Saved `{file_path.resolve()}`** ({rows}x{cols} grid, "
            f"`{format_timestamp(frag_start)}` – `{format_timestamp(frag_end)}`):\n  "
            + " ; ".join(grid_timestamps)
        )
        if saved_count >= max_sheets:
            break

    return notes


def fetch_transcript_segments(
    video_id: str,
    window_seconds: int = 20,
    start_sec: float | None = None,
    end_sec: float | None = None,
    grep_pattern: str | None = None,
) -> list[str]:
    """Fetch English (or auto-translated) transcript and group into readable timestamped blocks."""
    try:
        ytt = YouTubeTranscriptApi(http_client=_TimeoutSession())
        transcript_list = ytt.list(video_id)
        try:
            transcript = transcript_list.find_transcript(["en", "en-US", "en-GB"])
        except Exception:
            available = list(transcript_list)
            if not available:
                return []
            first = available[0]
            transcript = first.translate("en") if first.is_translatable else first

        fetched = transcript.fetch()
        raw_snippets = [
            {"start": float(item.start), "text": item.text}
            for item in fetched
        ]
    except Exception:
        return []

    if not raw_snippets:
        return []

    if start_sec is not None:
        raw_snippets = [s for s in raw_snippets if s["start"] >= start_sec]
    if end_sec is not None:
        raw_snippets = [s for s in raw_snippets if s["start"] <= end_sec]

    grouped: list[tuple[float, str]] = []
    bucket_start: float | None = None
    bucket_texts: list[str] = []

    for item in raw_snippets:
        text = re.sub(r"\s+", " ", item["text"]).strip()
        if not text or text.lower() in ("[music]", "[applause]", "[laughter]"):
            continue
        start = item["start"]
        if bucket_start is None:
            bucket_start = start
        if start - bucket_start >= window_seconds and bucket_texts:
            grouped.append((bucket_start, " ".join(bucket_texts)))
            bucket_start = start
            bucket_texts = [text]
        else:
            bucket_texts.append(text)

    if bucket_start is not None and bucket_texts:
        grouped.append((bucket_start, " ".join(bucket_texts)))

    if grep_pattern:
        regex = re.compile(grep_pattern, re.IGNORECASE)
        matched_indices: set[int] = set()
        for idx, (_, txt) in enumerate(grouped):
            if regex.search(txt):
                for neighbor in (idx - 1, idx, idx + 1):
                    if 0 <= neighbor < len(grouped):
                        matched_indices.add(neighbor)
        grouped = [grouped[i] for i in sorted(matched_indices)]

    return [f"[{format_timestamp(ts)}] {txt}" for ts, txt in grouped]


def fetch_video_info(target: str, include_comments: bool = True) -> list[dict]:
    """Use yt-dlp to extract metadata and top comments for a URL, video ID, or ytsearch query."""
    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    if include_comments:
        ydl_opts["getcomments"] = True
        ydl_opts["extractor_args"] = {
            "youtube": {
                "comment_sort": ["top"],
                "max_comments": ["12,10,2,0"],
            }
        }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=False)
        if not info:
            return []
        if "entries" in info:
            return [entry for entry in info["entries"] if entry]
        return [info]


def render_video_markdown(
    info: dict,
    max_transcript_lines: int = 120,
    section_query: str | None = None,
    time_range: tuple[float, float] | None = None,
    grep_pattern: str | None = None,
    storyboard_dir: Path | None = None,
) -> str:
    video_id = info.get("id") or ""
    title = info.get("title") or "Untitled Video"
    url = info.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
    channel = info.get("uploader") or info.get("channel") or "Unknown Channel"
    duration = format_timestamp(info.get("duration"))

    lines = [
        f"## {title}",
        f"- **URL**: {url}",
        f"- **Channel**: {channel} | **Duration**: {duration}",
    ]

    chapters = info.get("chapters") or []
    if chapters:
        lines.append("\n### Video Chapters")
        for ch in chapters:
            start_ts = format_timestamp(ch.get("start_time"))
            ch_title = ch.get("title") or "Section"
            lines.append(f"- `{start_ts}` — {ch_title}")

    description = (info.get("description") or "").strip()
    if description:
        lines.append("\n### Description Notes")
        desc_excerpt = description[:2500]
        if len(description) > 2500:
            desc_excerpt += "\n... [description truncated]"
        lines.append("```text")
        lines.append(desc_excerpt)
        lines.append("```")

    comments = info.get("comments") or []
    if comments:
        lines.append("\n### Top / Pinned Community Comments")
        shown_comments = 0
        for c in comments:
            c_text = (c.get("text") or "").strip()
            if not c_text:
                continue
            author = c.get("author") or "user"
            likes = c.get("like_count") or 0
            pinned = " [PINNED]" if c.get("is_pinned") else ""
            shown_comments += 1
            lines.append(f"- **{author}{pinned} ({likes} likes):** {c_text[:600]}")
            if shown_comments >= 8:
                break

    start_sec: float | None = None
    end_sec: float | None = None
    window_label = ""

    if time_range:
        start_sec, end_sec = time_range
        window_label = f" ({format_timestamp(start_sec)} – {format_timestamp(end_sec)})"
    elif section_query:
        resolved = resolve_section_window(info, section_query)
        if resolved:
            start_sec, end_sec, matched_title = resolved
            window_label = (
                f" (Matched Section: '{matched_title}' | "
                f"{format_timestamp(start_sec)} – {format_timestamp(end_sec)})"
            )
        else:
            if not grep_pattern:
                # Short keywords like "1-2" are too noisy alone; fall back to the whole phrase.
                grep_pattern = "|".join(
                    re.escape(k) for k in section_query.split() if len(k) >= 3
                ) or re.escape(section_query.strip())
                window_label = f" (Filtered by keywords: '{section_query}')"

    if storyboard_dir is not None:
        sb_notes = save_storyboard_sheets(
            info,
            storyboard_dir,
            start_sec=start_sec,
            end_sec=end_sec,
        )
        if sb_notes:
            lines.append("\n### Saved Storyboard Frame Grids (open with Read)")
            lines.extend(sb_notes)

    if video_id:
        segments = fetch_transcript_segments(
            video_id,
            start_sec=start_sec,
            end_sec=end_sec,
            grep_pattern=grep_pattern,
        )
        lines.append(f"\n### Spoken Transcript{window_label}")
        if segments:
            shown = segments[:max_transcript_lines]
            lines.extend(shown)
            if len(segments) > max_transcript_lines:
                lines.append(
                    f"... [{len(segments) - max_transcript_lines} additional segments omitted; "
                    f"use --section, --time-range, --grep, or --max-lines to inspect]"
                )
        else:
            lines.append(
                "_No matching spoken captions found (either a no-commentary video or no match in range; "
                "use --save-storyboards <dir> to inspect visual frame grids with Read)._"
            )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract walkthrough metadata, chapters, top comments, transcripts, and storyboards from YouTube."
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="YouTube video URLs (including search redirect URLs) or 11-character video IDs.",
    )
    parser.add_argument(
        "--search",
        "-s",
        help="Search YouTube for matching walkthrough videos and extract their text.",
    )
    parser.add_argument(
        "--section",
        help="Slice transcript to the chapter or description timestamp matching this level/puzzle name.",
    )
    parser.add_argument(
        "--time-range",
        nargs=2,
        metavar=("START", "END"),
        help="Slice transcript/storyboards to a timestamp range (e.g., --time-range 03:30 07:00).",
    )
    parser.add_argument(
        "--grep",
        "-g",
        help="Filter transcript to segments matching this regex/keyword (+/- 20s context).",
    )
    parser.add_argument(
        "--save-storyboards",
        metavar="DIR",
        help="Save 3x3 storyboard JPEG sheets for the target time window to DIR for visual inspection with Read.",
    )
    parser.add_argument(
        "--no-comments",
        action="store_true",
        help="Skip fetching top YouTube comments.",
    )
    parser.add_argument(
        "--max-results",
        "-n",
        type=int,
        default=2,
        help="Number of search results to process when using --search (default: 2).",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=120,
        help="Max timestamped transcript blocks per video (default: 120).",
    )
    args = parser.parse_args()

    if not args.targets and not args.search:
        parser.print_help(sys.stderr)
        return 1

    parsed_range: tuple[float, float] | None = None
    if args.time_range:
        try:
            parsed_range = (
                parse_timestamp_str(args.time_range[0]),
                parse_timestamp_str(args.time_range[1]),
            )
        except ValueError as exc:
            print(f"--time-range: {exc} (use SECONDS, MM:SS or HH:MM:SS)", file=sys.stderr)
            return 2
    if args.grep:
        try:
            re.compile(args.grep)
        except re.error as exc:
            print(f"--grep: invalid regex {args.grep!r}: {exc}", file=sys.stderr)
            return 2

    storyboard_dir = Path(args.save_storyboards).resolve() if args.save_storyboards else None
    if storyboard_dir and not storyboard_dir.is_relative_to(STORYBOARD_ROOT):
        # game-guide: the skill's permissions only cover /tmp/game-guide, so refuse to write elsewhere.
        print(f"--save-storyboards must be inside {STORYBOARD_ROOT}", file=sys.stderr)
        return 2
    include_comments = not args.no_comments
    videos: list[dict] = []

    if args.search:
        query = f"ytsearch{max(1, args.max_results)}:{args.search}"
        try:
            videos.extend(fetch_video_info(query, include_comments=include_comments))
        except Exception as exc:
            print(f"Error searching YouTube for '{args.search}': {exc}", file=sys.stderr)

    for target in args.targets:
        vid = extract_video_id(target)
        if not vid:
            # game-guide: only YouTube videos, so a URL from an untrusted page can't send yt-dlp anywhere else.
            print(f"Not a YouTube video: {target}", file=sys.stderr)
            continue
        lookup = f"https://www.youtube.com/watch?v={vid}"
        try:
            videos.extend(fetch_video_info(lookup, include_comments=include_comments))
        except Exception as exc:
            if vid:
                segments = fetch_transcript_segments(
                    vid,
                    start_sec=parsed_range[0] if parsed_range else None,
                    end_sec=parsed_range[1] if parsed_range else None,
                    grep_pattern=args.grep,
                )
                if segments:
                    print(f"## Video {vid} (Transcript Only)")
                    print("\n".join(segments[: args.max_lines]))
                    continue
            print(f"Error fetching '{target}': {exc}", file=sys.stderr)

    if not videos:
        print("No walkthrough videos or transcripts found.", file=sys.stderr)
        return 1

    output_blocks = [
        render_video_markdown(
            video,
            max_transcript_lines=args.max_lines,
            section_query=args.section,
            time_range=parsed_range,
            grep_pattern=args.grep,
            storyboard_dir=storyboard_dir,
        )
        for video in videos
    ]
    print("\n\n---\n\n".join(output_blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
