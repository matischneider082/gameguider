#!/usr/bin/env python3
"""Turn a YouTube auto-caption .vtt into short timestamped paragraphs.

Usage: vtt_to_text.py FILE.vtt [--from SECONDS] [--to SECONDS] [--every SECONDS]
       vtt_to_text.py --selftest
"""
import re
import sys

CUE = re.compile(r"(\d+):(\d\d):(\d\d)\.\d+ --> ")
TAG = re.compile(r"<[^>]+>")


def seconds(match):
    h, m, s = (int(g) for g in match.groups())
    return h * 3600 + m * 60 + s


def convert(text, start=0, end=None, every=30):
    # Auto-captions repeat a cue's lines in the next cue (rolling display), so drop lines the previous cue already had;
    # a later genuine repeat is kept.
    lines, prev, cur, t = [], set(), set(), None
    for raw in text.splitlines():
        cue = CUE.match(raw)
        if cue:
            t, prev, cur = seconds(cue), cur, set()
            continue
        line = TAG.sub("", raw).strip()
        if t is None or not line:
            continue
        cur.add(line)
        if line in prev:
            continue
        if t >= start and (end is None or t <= end):
            lines.append((t, line))

    paragraphs, para_start, words = [], None, []
    for t, line in lines:
        if para_start is not None and t - para_start >= every:
            paragraphs.append((para_start, " ".join(words)))
            para_start, words = None, []
        if para_start is None:
            para_start = t
        words.append(line)
    if words:
        paragraphs.append((para_start, " ".join(words)))
    return "\n".join(f"[{p // 60}:{p % 60:02d}] {w}" for p, w in paragraphs)


SAMPLE = """WEBVTT
Kind: captions

00:00:01.000 --> 00:00:03.000 align:start position:0%
okay<00:00:01.500><c> so</c><00:00:02.000><c> this boss</c>

00:00:03.000 --> 00:00:05.000 align:start position:0%
okay so this boss
dodge<00:00:03.500><c> left</c>

00:00:40.000 --> 00:00:42.000 align:start position:0%
dodge left
then hit his back
"""


def selftest():
    out = convert(SAMPLE)
    assert out == "[0:01] okay so this boss dodge left\n[0:40] then hit his back", out
    assert convert(SAMPLE, start=30) == "[0:40] then hit his back"
    print("selftest ok")


def main(argv):
    if argv == ["--selftest"]:
        return selftest()
    opts = {"--from": 0, "--to": None, "--every": 30}
    path = None
    it = iter(argv)
    for arg in it:
        if arg in opts:
            opts[arg] = int(float(next(it)))
        else:
            path = arg
    if not path:
        sys.exit(__doc__)
    with open(path, encoding="utf-8") as f:
        print(convert(f.read(), opts["--from"], opts["--to"], opts["--every"]))


if __name__ == "__main__":
    main(sys.argv[1:])
