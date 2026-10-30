"""A Conversation owns the lifecycle of a question above backends.ask: each backend's session, a newer question
superseding an older one (which stops and has its CLI closed), resetting for a new conversation, and a walkthrough's
best-effort artwork hooks. Its interface yields small typed updates with the user-facing wording already chosen, so
the web NDJSON stream, the TUI worker and the CLI are delivery adapters that only format them."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import closing, suppress
from dataclasses import dataclass

from .artwork import ensure_walkthrough_artwork, fetch_game_artwork
from .backends import REFUSAL, Guide, ask, framed, render
from .notes import Game

PLANTED_MESSAGE = "Unexpected instruction files were found in your notes folder. Review or remove them before trying again."
FAILED_MESSAGE = "Couldn't get an answer. Check your connection and AI provider, then try again."


def walkthrough_prompt(game_name: str) -> str:
    return (
        f"Write the full walkthrough for {game_name}, with all three spoiler levels in every chapter, "
        "using subagents in parallel to research chapters in depth, saving the game cover artwork plus key "
        "battle maps, route diagrams, and walkthrough screenshots to images/ with save_image.py, embedding "
        "the cover in the walkthrough preamble, and including timestamped YouTube video sources on dedicated "
        "bullet lines."
    )


@dataclass(frozen=True)
class Status:
    text: str


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Done:
    markdown: str  # the rendered guide, or the plain-text reply when it didn't match the schema
    guide: Guide | None
    session: str | None
    text: str  # the backend's raw reply


@dataclass(frozen=True)
class Error:
    reason: str  # "planted": refused because of instruction files in the notes folder; "failed": anything else
    message: str  # what to tell the user
    detail: str  # the backend's raw error


Update = Status | Text | Done | Error


class Conversation:
    def __init__(self, sessions: dict[str, str] | None = None):
        self.sessions = dict(sessions or {})  # backend -> session to resume; the CLI seeds it from --resume
        self._lock = threading.Lock()
        self._token = 0  # bumped by each question, reset() and cancel(); a question whose token is stale stops
        self._active = None  # the running question's backend generator, so a newer question can close it

    def ask(self, backend: str, question: str, spoiler: str, game: Game | None = None) -> Iterator[Update]:
        """Ask in this conversation. Starting supersedes the running question; this one's updates stop when a newer
        question, reset() or cancel() supersedes it. Close the iterator to stop early: that kills the CLI."""
        token = self._supersede()
        events = ask(backend, framed(question, spoiler, game), self.sessions.get(backend))
        with self._lock:
            if self._token == token:
                self._active = events
        try:
            with closing(events):
                for event in events:
                    with self._lock:  # checked and written together, so a superseded question can't write its session
                        if self._token != token:
                            return
                        if event.session:
                            self.sessions[backend] = event.session
                    if event.kind == "status":
                        yield Status(event.text)
                    elif event.kind == "text":
                        yield Text(event.text)
                    elif event.kind == "done":
                        yield Done(render(event.guide) if event.guide else event.text, event.guide,
                                   event.session or self.sessions.get(backend), event.text)
                    elif event.kind == "error":
                        planted = event.text.startswith(REFUSAL)
                        yield Error("planted" if planted else "failed", PLANTED_MESSAGE if planted else FAILED_MESSAGE,
                                    event.text)
        finally:
            with self._lock:
                if self._active is events:
                    self._active = None

    def walkthrough(self, backend: str, game: Game, spoiler: str) -> Iterator[Update]:
        """Ask for the game's full walkthrough: its cover is fetched first and embedded once the turn ends, both
        best-effort, since artwork must never block a walkthrough build."""
        with suppress(Exception):
            fetch_game_artwork(game)
        with closing(self.ask(backend, walkthrough_prompt(game.name), spoiler, game)) as updates:
            for update in updates:
                if isinstance(update, (Done, Error)):
                    with suppress(Exception):
                        ensure_walkthrough_artwork(game)
                yield update

    def reset(self):
        """A new conversation: cancel the running question and forget every backend's session."""
        self._supersede()
        self.sessions.clear()

    def cancel(self):
        """Stop the running question, as when the server closes."""
        self._supersede()

    def _supersede(self) -> int:
        with self._lock:
            self._token += 1
            token, older, self._active = self._token, self._active, None
        if older is not None:
            with suppress(Exception):  # "already executing" on its own thread: it stops at its next token check
                older.close()
        return token
