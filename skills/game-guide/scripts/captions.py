#!/usr/bin/env python3
"""Read a YouTube video's chapters or cleaned-up captions, running yt-dlp with fixed arguments.
Usage: captions.py chapters <youtube-url>
       captions.py text <youtube-url> [--from SECONDS] [--to SECONDS]

The skill reads untrusted pages, so this is the only way it runs yt-dlp: only the 11-character video id
from the URL reaches yt-dlp, never an option such as --exec.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from vtt_to_text import convert

OUT = Path("/tmp/game-guide")
ID = re.compile(r"[A-Za-z0-9_-]{11}")
HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com")


def video_id(url):
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        return None
    if parts.hostname == "youtu.be":
        found = parts.path[1:]
    elif parts.hostname in HOSTS and parts.path == "/watch":
        found = parse_qs(parts.query).get("v", [""])[0]
    elif parts.hostname in HOSTS and parts.path.startswith("/shorts/"):
        found = parts.path.removeprefix("/shorts/")
    else:
        return None
    return found if ID.fullmatch(found) else None


def yt_dlp(*args):
    if not shutil.which("yt-dlp"):
        sys.exit("yt-dlp is not installed; it installs with `mise use -g yt-dlp` (or `brew install yt-dlp`).")
    # --ignore-config: yt-dlp also reads a yt-dlp.conf from the working directory, which is the writable notes folder.
    done = subprocess.run(["yt-dlp", "--ignore-config", "--skip-download", *args], capture_output=True, text=True)
    if done.returncode:
        sys.exit(done.stderr[-2000:] or f"yt-dlp exited with {done.returncode}")
    return done.stdout


def main(argv):
    parser = argparse.ArgumentParser(description="Read a YouTube video's chapters or captions.")
    parser.add_argument("command", choices=("chapters", "text"))
    parser.add_argument("url")
    parser.add_argument("--from", dest="start", type=int, default=0)
    parser.add_argument("--to", dest="end", type=int)
    args = parser.parse_args(argv)
    vid = video_id(args.url)
    if not vid:
        sys.exit(f"not a YouTube video URL: {args.url}")
    url = f"https://www.youtube.com/watch?v={vid}"
    if args.command == "chapters":
        chapters = json.loads(yt_dlp("--print", "%(chapters)j", "--", url) or "null")
        print("\n".join(f"{int(c['start_time'])}-{int(c['end_time'])} {c['title']}" for c in chapters or [])
              or "No chapters.")
        return
    yt_dlp("--write-auto-subs", "--sub-langs", "en", "--sub-format", "vtt", "-o", str(OUT / "%(id)s"), "--", url)
    vtt = OUT / f"{vid}.en.vtt"
    if not vtt.exists():
        sys.exit("This video has no English captions.")
    print(convert(vtt.read_text(encoding="utf-8"), args.start, args.end))


if __name__ == "__main__":
    main(sys.argv[1:])
