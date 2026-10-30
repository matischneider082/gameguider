"""Thumbnails, still frames and playback for YouTube sources. Needs yt-dlp, ffmpeg and mpv on PATH."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path

from PIL import Image

from . import procs

CACHE = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "game-guide"
DOWNLOADS = {}  # video id -> lock: p and f racing two yt-dlp runs would write the same .part file
STORYBOARDS = {}  # video id -> lock: serialize storyboard metadata lookups per video


FEATURES = {  # feature -> (the tools it runs, why it can't); yt-dlp needs ffmpeg to merge video and audio
    "frames": (("yt-dlp", "ffmpeg"), "Still frames are unavailable on this installation."),
    "video": (("yt-dlp", "ffmpeg"), "Video playback is unavailable on this installation."),
    "play": (("yt-dlp", "ffmpeg", "mpv"), "External playback is unavailable on this installation."),
}


def missing(*tools):
    return [t for t in tools if not shutil.which(t)]


def unavailable(feature):
    """Why `feature` ("frames", "video" or "play") can't run here, offering YouTube instead; None when it can."""
    tools, reason = FEATURES[feature]
    return f"{reason} You can watch the video on YouTube instead." if missing(*tools) else None


def short_error(e):
    """The last line yt-dlp or ffmpeg printed, instead of an exception that quotes a huge signed URL."""
    lines = (getattr(e, "stderr", None) or "").strip().splitlines()
    return lines[-1][:200] if lines else str(e)[:200]


def thumbnail(video):
    out = CACHE / f"{video.id}.jpg"
    if not out.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"https://i.ytimg.com/vi/{video.id}/hqdefault.jpg", timeout=10) as response:
            data = response.read()
        # Renamed into place only when complete, so a dropped connection can't leave a truncated cache hit.
        with tempfile.NamedTemporaryFile(dir=CACHE, suffix=".part", delete=False) as tmp:
            tmp.write(data)
        os.replace(tmp.name, out)
    return out


def _storyboard_spec(video_id: str) -> dict | None:
    """Fetch and cache the highest-resolution YouTube storyboard spec for video_id."""
    CACHE.mkdir(parents=True, exist_ok=True)
    meta_file = CACHE / f"{video_id}.storyboard.json"
    with STORYBOARDS.setdefault(video_id, threading.Lock()):
        if meta_file.exists():
            try:
                return json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if missing("yt-dlp"):
            return None
        try:
            raw = procs.run(
                [
                    "yt-dlp",
                    "--ignore-config",
                    "-q",
                    "--no-warnings",
                    "--skip-download",
                    "-J",
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                timeout=20,
            )
            info = json.loads(raw)
        except Exception:
            return None
        storyboards = [
            f for f in (info.get("formats") or [])
            if f.get("format_note") == "storyboard" and f.get("fragments")
        ]
        if not storyboards:
            return None
        sb = storyboards[-1]
        spec = {
            "rows": int(sb.get("rows") or 3),
            "cols": int(sb.get("columns") or 3),
            "fragments": sb.get("fragments") or [],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CACHE, suffix=".part", delete=False) as tmp:
            json.dump(spec, tmp)
        os.replace(tmp.name, meta_file)
        return spec


def timestamp_thumbnail(video):
    """Return a timestamp-specific thumbnail cropped from the video's storyboard at `video.at`, falling back to `thumbnail(video)`."""
    at = max(0, int(getattr(video, "at", 0) or 0))
    if at <= 0:
        return thumbnail(video)
    out = CACHE / f"{video.id}-thumb-{at}.jpg"
    if out.exists():
        return out
    spec = _storyboard_spec(video.id)
    if not spec or not spec.get("fragments"):
        return thumbnail(video)
    rows = max(1, int(spec.get("rows") or 3))
    cols = max(1, int(spec.get("cols") or 3))
    fragments = spec["fragments"]
    cur = 0.0
    for idx, frag in enumerate(fragments):
        dur = max(1.0, float(frag.get("duration") or 18.0))
        if cur + dur >= at or idx == len(fragments) - 1:
            frag_url = frag.get("url")
            if not frag_url:
                break
            sheet_cache = CACHE / f"{video.id}-sb-{idx}.jpg"
            try:
                if not sheet_cache.exists():
                    with urllib.request.urlopen(frag_url, timeout=10) as resp:
                        sheet_bytes = resp.read()
                    with tempfile.NamedTemporaryFile(dir=CACHE, suffix=".part", delete=False) as tmp:
                        tmp.write(sheet_bytes)
                    os.replace(tmp.name, sheet_cache)
                with Image.open(sheet_cache) as sheet:
                    cells = rows * cols
                    cell_idx = min(cells - 1, max(0, int((at - cur) / (dur / cells))))
                    r, c = divmod(cell_idx, cols)
                    cw, ch = sheet.width // cols, sheet.height // rows
                    crop = sheet.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
                    buf = io.BytesIO()
                    crop.convert("RGB").save(buf, format="JPEG", quality=90)
                with tempfile.NamedTemporaryFile(dir=CACHE, suffix=".part", delete=False) as tmp:
                    tmp.write(buf.getvalue())
                os.replace(tmp.name, out)
                return out
            except Exception:
                break
        cur += dur
    return thumbnail(video)


def download(video):
    """The whole video at up to 720p, cached.

    YouTube rejects ffmpeg and mpv reading its stream URLs (403), and yt-dlp's section download runs at
    about real time, but its own chunked downloader fetches a full video in seconds.
    """
    # shortcut: downloads are kept forever so frames and replays are instant; add cleanup if ~/.cache/game-guide gets big.
    with DOWNLOADS.setdefault(video.id, threading.Lock()):
        # yt-dlp renames into the final <id>.video.<ext> only once complete; .part, .fNNN and .temp leftovers aren't hits.
        if cached := next((p for p in CACHE.glob(f"{video.id}.video.*") if p.name == f"{video.id}.video{p.suffix}"), None):
            return cached
        CACHE.mkdir(parents=True, exist_ok=True)
        # --ignore-config: a yt-dlp.conf (say with --no-part) could make an interrupted download look complete.
        out = procs.run(["yt-dlp", "--ignore-config", "-q", "--no-warnings", "--no-simulate", "--print",
                         "after_move:filepath", "-f", "bv*[height<=720]+ba/b[height<=720]/b",
                         "-o", str(CACHE / f"{video.id}.video.%(ext)s"), f"https://www.youtube.com/watch?v={video.id}"],
                        timeout=600)
        return Path(out.strip().splitlines()[0])


def frames(video, offsets=(-2, 0, 2)):
    """Still frames around the source's timestamp, so you can see the moment without watching."""
    times = sorted({max(0, video.at + o) for o in offsets})
    outs = [CACHE / f"{video.id}-{t}.jpg" for t in times]
    if all(o.exists() for o in outs):
        return outs
    source, error = download(video), None
    for t, out in zip(times, outs):
        try:
            # Keep ffmpeg off the TUI's terminal: it reads keys from stdin, and short_error needs its stderr.
            procs.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(t), "-i", str(source), "-frames:v", "1",
                       "-q:v", "3", str(out)], timeout=60)
        except subprocess.CalledProcessError as e:  # e.g. a time past the end of the video: show the frames that exist
            if e.returncode < 0:  # killed (the app is quitting): don't start the next ffmpeg
                raise
            error = e
    if found := [o for o in outs if o.exists()]:
        return found
    raise error or RuntimeError(f"ffmpeg wrote no frames: is {video.at}s past the end of the video?")


def play(video):
    """Open the video at its timestamp in an mpv window. Blocks while the video downloads the first time."""
    path = download(video)
    subprocess.Popen(["mpv", f"--start={video.at}", f"--title={video.label}", str(path)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **procs.GROUP)
    return path
