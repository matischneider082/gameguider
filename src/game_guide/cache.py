"""Process-local cache for notes-folder reads.

An entry is reused only while that path's mtime, size and inode still match. Text read
while a save is landing is returned and not stored, so the new mtime can never point at
the old bytes. A missing file is not remembered as empty. Obsidian and the library both
replace files, which changes the mtime (and, on replace, the inode).
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path

MISS = object()
ABSENT = ("absent",)  # stamp for a path that is not there; distinct from a raced read
_MAX_FILES = 256
_MAX_BYTES = 2_000_000
_lock = threading.Lock()
_files: OrderedDict[Path, tuple[tuple, str]] = OrderedDict()


def file_stamp(path: Path) -> tuple[int, int, int] | None:
    """`(mtime_ns, size, inode)`, or None when `path` cannot be stat'd (missing or unreadable).

    `Path.exists` treats any stat error as absence, and the readers used to do the same.
    """
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def load(path: Path) -> tuple[str, tuple | None]:
    """`(text, stamp)` for `path`.

    `stamp` is `ABSENT` when the file is missing, None when the bytes are not that version
    (a write raced the read, or the file could not be read), and the file's `(mtime_ns, size, inode)`
    when `text` is exactly that version. Invalid UTF-8 is kept with replacement characters, the way an
    editor mid-save leaves it. An unreadable file is not remembered: restoring the mode keeps the same stamp.
    """
    path = Path(path)
    stamp = file_stamp(path)
    if stamp is None:
        with _lock:
            _files.pop(path, None)
        return "", ABSENT
    if not os.access(path, os.R_OK):
        # stat succeeds for the owner of a mode-0 file. A cached copy would skip the permission check,
        # and remembering "" would still match after the mode comes back.
        with _lock:
            _files.pop(path, None)
        return "", None
    with _lock:
        hit = _files.get(path)
        if hit is not None and hit[0] == stamp:
            _files.move_to_end(path)
            return hit[1], stamp
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        with _lock:
            _files.pop(path, None)
        return "", None
    if file_stamp(path) != stamp:
        return text, None  # changed mid-read; the caller must not remember work derived from it
    if stamp[1] <= _MAX_BYTES:
        with _lock:
            _files[path] = (stamp, text)
            _files.move_to_end(path)
            while len(_files) > _MAX_FILES:
                _files.popitem(last=False)
    return text, stamp


def read_text(path: Path) -> str | None:
    """The file's text, or None when it does not exist. A raced read is still returned."""
    text, stamp = load(path)
    return None if stamp == ABSENT else text


def recall(store: OrderedDict, key, lock: threading.Lock):
    """The cached value, or `MISS` when `key` is absent. `None` is a real cached value."""
    with lock:
        if key not in store:
            return MISS
        store.move_to_end(key)
        return store[key]


def remember(store: OrderedDict, key, value, limit: int, lock: threading.Lock) -> None:
    with lock:
        store[key] = value
        store.move_to_end(key)
        while len(store) > limit:
            store.popitem(last=False)


def clear() -> None:
    """Drop remembered file text. Derived caches key on stamps, so they miss on their own."""
    with _lock:
        _files.clear()
