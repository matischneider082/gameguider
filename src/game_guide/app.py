"""Textual TUI: games on the left, notes / answer / media on the right, a question box at the bottom."""

import math
import threading
import webbrowser
from contextlib import closing

# Must be imported before the app starts: it queries the terminal for image support, which can't happen once Textual owns the I/O.
import textual_image.renderable  # noqa: F401
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Input, Label, ListItem, ListView, Markdown, OptionList, Select, Static, TabbedContent, TabPane
from textual.worker import get_current_worker
from textual_image.widget import Image

from . import media, procs, scores
from .backends import BACKENDS, SPOILERS
from .conversation import Conversation, Done, Error, Status, walkthrough_prompt
from .library import read_games, update_game
from .notes import Video, games_dir
from .view import game_view

TABS = ["notes-tab", "walk-tab", "answer-tab", "media-tab"]
STATUS_ICON = {"playing": "▶", "backlog": "📋", "paused": "‖", "beaten": "✓", "dropped": "✗"}
SCORE_FAILED = "Couldn't fetch a critic score. Check your connection and try again."


def shown_score(value):
    """Round a critic average the way the Web UI does (half away from zero, not banker's rounding)."""
    return math.floor(float(value) + 0.5)


def format_critic(score):
    """The header sentence for a cached score, or the prompt to fetch one."""
    if not score:
        return "No critic score yet. ctrl+g fetches one."
    reviews = f" ({score['count']})" if score.get("count") else ""
    return f"Critics {shown_score(score['score'])}/100 · {score['source']}{reviews}"


def game_list_label(game, cached):
    icon = STATUS_ICON.get(game.status.lower(), " ")
    score = scores.score_for(game, cached)
    mark = f"  {shown_score(score['score'])}/100" if score else ""
    return f"{icon} {game.name}{mark}"


class GameGuide(App):
    TITLE = "GameGuider"
    CSS = """
    #games { width: 30; border-right: solid $panel; }
    #games Label { width: 1fr; }  /* wrap long names instead of cutting them off */
    #detail { width: 1fr; }
    #critic { height: 1; padding: 0 1; }
    #main { width: 1fr; height: 1fr; }
    #media-side { width: 40; }
    #walk-side { width: 34; }
    #chapters { height: 1fr; }
    #sources { height: 1fr; }
    #media-keys, #walk-keys { padding: 0 1; color: $text-muted; }
    #preview { height: 12; }
    #frames { height: 14; }
    #frames Image { width: 1fr; }
    #ask { height: auto; }
    #ask Select { width: 16; }
    #question { width: 1fr; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        ("p", "play", "Play at timestamp"),
        ("f", "frames", "Frames"),
        ("u", "open_video", "Open on YouTube"),
        ("s", "set_spot", "Set chapter as spot"),
        ("w", "walkthrough", "Build walkthrough"),
        Binding("c", "score", "Critic score"),
        # ctrl+g reaches the action while the question box is focused; c does not, because that box takes letters.
        Binding("ctrl+g", "score", show=False),
        ("o", "open_web", "Open Web UI"),
        ("r", "reload", "Reload"),
        ("ctrl+n", "new_chat", "New conversation"),
        ("ctrl+t", "next_tab", "Next tab"),
    ]

    def __init__(self):
        super().__init__()
        self.games, self.game, self.media, self.chapters = [], None, [], []
        self.scores = {}
        self._score_gen = 0
        self._score_gen_lock = threading.Lock()
        self._score_lock = threading.Lock()  # one cache write at a time; the UI thread never takes this
        self.conversation, self.web_server = Conversation(), None

    def compose(self):
        with Horizontal():
            yield ListView(id="games")
            with Vertical(id="detail"):
                yield Static("", id="critic", markup=False)
                with TabbedContent(id="main"):
                    with TabPane("Notes", id="notes-tab"):
                        yield VerticalScroll(Markdown(id="notes"))
                    with TabPane("Walkthrough", id="walk-tab"):
                        with Horizontal():
                            with Vertical(id="walk-side"):
                                yield OptionList(id="chapters", markup=False)
                                yield Static("enter  ask about this chapter\ns      set chapter as spot\nw      build or rebuild it", id="walk-keys")
                            yield VerticalScroll(Markdown(id="chapter"))
                    with TabPane("Answer", id="answer-tab"):
                        yield VerticalScroll(Markdown("Ask something below.", id="answer"))
                    with TabPane("Media", id="media-tab"):
                        with Horizontal():
                            with Vertical(id="media-side"):
                                yield OptionList(id="sources", markup=False)
                                yield Static("p  play from the timestamp\nf  still frames around it\nu  open on YouTube", id="media-keys")
                            with VerticalScroll():
                                yield Image(id="preview")
                                yield Horizontal(id="frames")
        with Horizontal(id="ask"):
            yield Select([(s, s) for s in SPOILERS], value="hint", allow_blank=False, id="spoiler")
            yield Select([(b, b) for b in BACKENDS], value="claude", allow_blank=False, id="backend")
            yield Input(placeholder="Where are you stuck? (or: I just started Hollow Knight on Switch)", id="question")
        yield Static(id="status", markup=False)
        yield Footer()

    def on_mount(self):
        self.action_reload()
        self.query_one("#question").focus()

    def on_unmount(self):
        self.workers.cancel_all()  # first, so a worker starting a process from here on kills it at once
        if self.web_server is not None:
            self.web_server.shutdown()
            self.web_server.server_close()
            self.web_server = None
        procs.stop_all()  # quitting mustn't wait for a download or an answer to finish

    def action_open_web(self):
        if self.web_server is None:
            from .web import start_server  # the Web UI's module is large; load it only when asked for
            self.web_server = start_server()
        url = self.web_server.url
        if self.game:
            url = f"{url}/?game={self.game.slug}"
        webbrowser.open(url)
        self.set_status(f"Web UI opened at {self.web_server.url}")

    def action_reload(self):
        current = self.game.name if self.game else None
        self.games = read_games()
        self.scores = scores.read_scores(games_dir())
        view = self.query_one("#games", ListView)
        view.clear()
        view.extend(ListItem(Label(game_list_label(g, self.scores), markup=False)) for g in self.games)
        names = [g.name for g in self.games]
        if names:
            view.index = names.index(current) if current in names else 0
            self.show_game(self.games[view.index])
        else:
            self.query_one("#critic", Static).update("")
            self.query_one("#notes", Markdown).update("No games tracked yet. Tell it what you're playing below.")

    def on_tabbed_content_tab_activated(self, event):
        # p and f are typing keys, so they only work once focus leaves the question box.
        if event.pane.id in ("media-tab", "walk-tab"):
            self.query_one("#sources" if event.pane.id == "media-tab" else "#chapters").focus()

    def on_list_view_highlighted(self, event):
        if event.list_view.id == "games" and event.list_view.index is not None and self.games:
            self.show_game(self.games[event.list_view.index])

    def show_game(self, game):
        switched = not self.game or self.game.name != game.name
        if self.game and switched:
            self.workers.cancel_group(self, "preview")  # its frames belong to the previous game
        self.game = game
        view = game_view(game, self.query_one("#spoiler", Select).value)
        self.query_one("#critic", Static).update(format_critic(scores.score_for(game, self.scores)))
        header = f"# {game.name}\n\n{game.platform} · {game.genre} · {game.status} · at **{game.spot or '?'}**\n\n"
        self.query_one("#notes", Markdown).update(header + (view.notes or "_No notes yet._"))
        if switched:
            self.query_one("#notes").parent.scroll_home(animate=False)
        self.show_walkthrough(view, switched)
        self.media = view.media  # walkthrough videos and maps go in the Media tab too
        sources = self.query_one("#sources", OptionList)
        sources.clear_options()
        sources.add_options(f"▶ {m.label} · {m.at // 60}:{m.at % 60:02d}" if isinstance(m, Video) else f"▣ {m[0]}"
                            for m in self.media)
        if self.media:
            sources.highlighted = 0  # so the first thumbnail shows without a keypress

    def show_walkthrough(self, view, switched):
        self.chapters = view.chapters
        options = self.query_one("#chapters", OptionList)
        kept = options.highlighted
        options.clear_options()
        options.add_options(f"{'●' if c.current else ' '} {c.index + 1}. {c.title}" for c in self.chapters)
        if not self.chapters:
            self.query_one("#chapter", Markdown).update(
                f"No walkthrough for {view.game.name} yet.\n\nPress **w** to research the whole game, chapter by chapter "
                "(it takes a few minutes). Each chapter is saved with hint, strategy and answer levels, so the spoiler "
                "selector works here too.")
            return
        options.highlighted = (view.current or 0) if switched or kept is None or kept >= len(self.chapters) else kept
        self.show_chapter(options.highlighted)

    def show_chapter(self, index):
        self.query_one("#chapter", Markdown).update(self.chapters[index].body)
        self.query_one("#chapter").parent.scroll_home(animate=False)

    def on_option_list_option_selected(self, event):
        if event.option_list.id == "chapters" and self.chapters:
            question = self.query_one("#question", Input)
            question.value = f"Help with {self.chapters[event.option_index].title}: "
            question.focus()

    def action_set_spot(self):
        if not self.game or not self.chapters:
            return self.notify("Pick a walkthrough chapter first.")
        idx = self.query_one("#chapters", OptionList).highlighted
        if idx is None or idx >= len(self.chapters):
            return
        title = self.chapters[idx].title
        update_game(self.game.slug, spot=title)
        self.action_reload()
        self.set_status(f"Spot updated to: {title}")

    def action_walkthrough(self):
        if not self.game:
            return self.notify("Pick or add a game first.")
        # All three levels whatever the selector says: the tab redacts them, and they let you raise it later.
        self.submit(walkthrough_prompt(self.game.name), walkthrough=True)

    def action_score(self):
        """Fetch the selected game's critic score. Wikidata or IGDB, not the AI CLI, so a missing backend must not block it."""
        if not self.game:
            return self.notify("Pick or add a game first.")
        with self._score_gen_lock:
            self._score_gen += 1
            gen = self._score_gen
        self.set_status(f"Fetching critic score for {self.game.name}…")
        self.fetch_score(self.game, gen)

    @work(thread=True, exclusive=True, group="score")
    def fetch_score(self, game, gen):
        with self._score_lock:
            with self._score_gen_lock:
                if gen != self._score_gen:
                    return
            if get_current_worker().is_cancelled:
                return
            try:
                score = scores.refresh_score(game, games_dir())
                message = None
            except (ValueError, OSError) as exc:
                score, message = None, str(exc)
            except Exception:
                score, message = None, SCORE_FAILED
        if get_current_worker().is_cancelled:
            return
        self.from_worker(self._apply_score_result, game, gen, score, message)

    def _apply_score_result(self, game, gen, score, message):
        if gen != self._score_gen:  # a newer fetch owns the status line
            return
        if score is None:
            self.set_status(message)
            return
        self.action_reload()
        if self.game and self.game.slug == game.slug:
            self.set_status(format_critic(score))
        else:
            self.set_status(f"Saved a critic score for {game.name}.")

    def on_select_changed(self, event):
        if event.select.id == "spoiler" and self.game:
            self.show_game(self.game)

    def selected_media(self):
        index = self.query_one("#sources", OptionList).highlighted
        return self.media[index] if index is not None and index < len(self.media) else None

    def on_option_list_option_highlighted(self, event):
        if event.option_list.id == "chapters":
            # The list's own index, not the event's: a late event may predate a rebuild of the list for another game.
            index = event.option_list.highlighted
            return self.show_chapter(index) if index is not None and index < len(self.chapters) else None
        item = self.selected_media()
        self.workers.cancel_group(self, "preview")  # an image starts no worker, so exclusive alone won't stop the last one
        self.query_one("#frames").remove_children()
        if isinstance(item, Video):
            self.load_thumbnail(item)
        elif item:
            self.query_one("#preview", Image).image = item[1] if item[1].exists() else None

    @work(thread=True, exclusive=True, group="preview")
    def load_thumbnail(self, video):
        try:
            path = media.thumbnail(video)
        except OSError as e:
            self.from_worker(self.set_status, f"thumbnail failed: {e}")
            return
        self.from_worker(setattr, self.query_one("#preview", Image), "image", path)

    def action_play(self):
        item = self.selected_media()
        if not isinstance(item, Video):
            return self.notify("Pick a video in the Media tab first.")
        if reason := media.unavailable("play"):
            return self.notify(f"{reason} Press u to open it.", severity="warning")
        self.set_status("downloading the video for playback…")
        self.play_video(item)

    @work(thread=True, group="play")
    def play_video(self, video):
        try:
            media.play(video)
        except Exception:  # yt-dlp/mpv failures are reported, not fatal
            self.from_worker(self.set_status, "Couldn't play this video. Press u to open it on YouTube.")
            return
        self.from_worker(self.set_status, "")

    def action_frames(self):
        item = self.selected_media()
        if not isinstance(item, Video):
            return self.notify("Pick a video in the Media tab first.")
        if reason := media.unavailable("frames"):
            return self.notify(f"{reason} Press u to open it.", severity="warning")
        self.set_status(f"grabbing frames around {item.at // 60}:{item.at % 60:02d}…")
        self.load_frames(item)

    @work(thread=True, exclusive=True, group="preview")
    def load_frames(self, video):
        try:
            paths = media.frames(video)
        except Exception:  # yt-dlp/ffmpeg failures are reported, not fatal
            self.from_worker(self.set_status, "Couldn't load still frames. Press u to open the video on YouTube.")
            return
        self.from_worker(self.show_frames, paths)

    def action_open_video(self):
        item = self.selected_media()
        if not isinstance(item, Video):
            return self.notify("Pick a video in the Media tab first.")
        if not webbrowser.open(item.url):
            return self.notify("Couldn't open your browser. Please try again.", severity="warning")
        self.set_status("Video opened on YouTube.")

    def show_frames(self, paths):
        box = self.query_one("#frames")
        box.remove_children()
        box.mount_all(Image(p) for p in paths)
        self.set_status("")

    def action_next_tab(self):
        tabs = self.query_one("#main", TabbedContent)
        tabs.active = TABS[(TABS.index(tabs.active) + 1) % len(TABS)]

    def action_new_chat(self):
        self.conversation.reset()
        self.set_status("new conversation")

    def on_input_submitted(self, event):
        question = event.value.strip()
        if question:
            event.input.value = ""
            self.submit(question)

    def submit(self, question, walkthrough=False):
        backend = self.query_one("#backend", Select).value
        spoiler = self.query_one("#spoiler", Select).value
        self.query_one("#answer", Markdown).update(f"> {question}\n\n_Working…_")
        self.query_one("#main", TabbedContent).active = "answer-tab"
        # exclusive cancels the running worker, and the new question supersedes the old one in the Conversation.
        self.run_question(self.conversation.walkthrough(backend, self.game, spoiler) if walkthrough
                          else self.conversation.ask(backend, question, spoiler, self.game))

    @work(thread=True, exclusive=True, group="ask")
    def run_question(self, updates):
        answered = False
        with closing(updates):  # stopping early kills the CLI
            for update in updates:
                if get_current_worker().is_cancelled:
                    return
                if isinstance(update, Status):
                    self.from_worker(self.set_status, "Researching your guide…")
                elif isinstance(update, Done):
                    answered = True
                    self.from_worker(self.finish, update.markdown)
                elif isinstance(update, Error):
                    answered = True
                    self.from_worker(self.finish, update.message)
        if not answered:  # ctrl+n stopped the question; a newer question cancels this worker instead
            self.from_worker(self.finish, "_Question stopped. Ask again when you're ready._")

    def from_worker(self, callback, *args):
        """call_from_thread, unless the calling worker was cancelled: a superseded run must not touch the app."""
        worker = get_current_worker()
        def deliver():
            # Checked on the UI thread, where cancelling happens, so it can't race a new submit.
            if not worker.is_cancelled:
                try:
                    callback(*args)
                except NoMatches:  # the app is closing and its widgets are gone
                    pass
        try:
            self.call_from_thread(deliver)
        except RuntimeError:  # the app has already stopped
            pass

    def finish(self, text):
        self.query_one("#answer", Markdown).update(text)
        self.set_status("")
        self.action_reload()  # the skill may have added a game or saved notes

    def set_status(self, text):
        self.query_one("#status", Static).update(text)


def run():
    GameGuide().run()
