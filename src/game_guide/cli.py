import argparse
import os
import sys
from pathlib import Path

from . import doctor, settings
from .artwork import fetch_game_artwork
from .backends import BACKENDS, SPOILERS
from .conversation import Conversation, Done, Error, Status
from .library import append_note, batch_add_games, discover_games, find_game, read_games, remove_game, save_game, update_game
from .notes import STATUSES, Game, games_dir
from .view import game_view
from .view import search as search_guides


def main(argv=None):
    parser = argparse.ArgumentParser(prog="game-guide", description="With no command, opens the TUI (or --web for the Web UI).")
    parser.add_argument("--web", action="store_true", help="open the local browser Web UI instead of the TUI")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="print the games you're tracking")

    backlog_cmd = sub.add_parser("backlog", help="list or add games to your Backlog queue")
    backlog_cmd.add_argument("games", nargs="*", help="optional game title(s) to add to Backlog (or 'Game | Platform | Genre | Notes')")
    backlog_cmd.add_argument("--platform", "-p", default="", help="default platform for added backlog games")
    backlog_cmd.add_argument("--genre", "-g", default="", help="default genre for added backlog games")
    backlog_cmd.add_argument("--artwork", action="store_true", help="also fetch cover artwork for added games")

    track_cmd = sub.add_parser("track", aliases=["add"], help="save or update a game in GAMES.md")
    track_cmd.add_argument("game", help="game title")
    track_cmd.add_argument("--platform", "-p", default=None, help="platform (e.g. Switch 2, PC, PS5)")
    track_cmd.add_argument("--genre", "-g", default=None, help="genre (e.g. tactical RPG, metroidvania)")
    track_cmd.add_argument("--status", "-s", default=None, help=f"status ({', '.join(STATUSES)})")
    track_cmd.add_argument("--spot", default=None, help="current chapter, boss, or area")
    track_cmd.add_argument("--note", default=None, help="optional progress note to append to <slug>.md")
    track_cmd.add_argument("--artwork", action="store_true", help="also fetch cover artwork for the game")

    spot_cmd = sub.add_parser("spot", help="update your current spot (and optionally status) for a game")
    spot_cmd.add_argument("game", help="game name or slug")
    spot_cmd.add_argument("spot", help="new current spot / chapter")
    spot_cmd.add_argument("--status", "-s", default=None, help=f"optional status ({', '.join(STATUSES)})")
    spot_cmd.add_argument("--note", default=None, help="optional progress note to append to <slug>.md")

    status_cmd = sub.add_parser("status", help="update a game's status (Playing, Backlog, Paused, Beaten, Dropped)")
    status_cmd.add_argument("game", help="game name or slug")
    status_cmd.add_argument("status", help=f"new status ({', '.join(STATUSES)})")

    rm_cmd = sub.add_parser("remove", aliases=["untrack"], help="remove a game from GAMES.md (keeps notes & walkthrough files)")
    rm_cmd.add_argument("game", help="game name or slug")

    web_cmd = sub.add_parser("web", help="open the local browser Web UI (full walkthroughs, images & video)")
    web_cmd.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    web_cmd.add_argument("--port", type=int, default=0, help="bind port (default: 0, picks a free port)")
    web_cmd.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    pkg_cmd = sub.add_parser("package-app", help="build the macOS GameGuider.app bundle in out/")
    pkg_cmd.add_argument("--out-dir", help="output directory for GameGuider.app (default: out/)")
    inst_cmd = sub.add_parser("install-app", help="build GameGuider.app, install it to /Applications, and add it to the macOS Dock")
    inst_cmd.add_argument("--dest-dir", default="/Applications", help="destination directory (default: /Applications)")
    inst_cmd.add_argument("--no-dock", action="store_true", help="do not add GameGuider.app to the macOS Dock")
    inst_cmd.add_argument("--app", metavar="PATH", help="install this prebuilt GameGuider.app instead of building one")
    search_cmd = sub.add_parser("search", help="find a boss, item or area across all your saved guides and notes")
    search_cmd.add_argument("words", nargs="+")
    search_cmd.add_argument("--spoiler", choices=SPOILERS, default="hint")
    setup_cmd = sub.add_parser("setup", aliases=["doctor"], help="check what is installed, and show or choose the notes folder")
    setup_cmd.add_argument("--connect", action="store_true", help="walk through connecting an AI even if one is ready")
    setup_cmd.add_argument("--notes-dir", metavar="PATH", help="save PATH as the notes folder (e.g. your Obsidian vault's Games folder)")
    walk = sub.add_parser("walkthrough", help="read a game's saved full walkthrough")
    walk.add_argument("game")
    walk.add_argument("chapter", nargs="?", type=int, help="chapter number; without it, list the chapters")
    walk.add_argument("--spoiler", choices=SPOILERS, default="hint")
    question = sub.add_parser("ask", help="ask one question and print the answer")
    question.add_argument("question", nargs="+")
    question.add_argument("--backend", choices=BACKENDS, default="claude")
    question.add_argument("--spoiler", choices=SPOILERS, default="hint")
    question.add_argument("--resume", metavar="SESSION", help="continue a previous conversation")
    question.add_argument("--model", help="model for the backend (default: $GAME_GUIDE_MODEL, else sonnet for claude"
                                          " and your configured model for codex)")
    question.add_argument("--json", action="store_true", help="print the answer as JSON")
    args = parser.parse_args(argv)

    if args.command == "search":
        hits = search_guides(" ".join(args.words), discover_games(), args.spoiler)
        if not hits:
            sys.exit(f"Nothing in your guides matches that at the {args.spoiler} spoiler level.")
        for hit in hits:
            print(f"{hit.game.name} · {hit.place}\n    {hit.snippet}")
        return
    if args.command in ("setup", "doctor"):
        if args.notes_dir:
            try:
                settings.set_notes_dir(args.notes_dir)
            except ValueError as e:
                sys.exit(str(e))
        print(f"Notes folder: {games_dir()}")
        if os.environ.get("GAME_GUIDE_DIR"):
            print("($GAME_GUIDE_DIR is set, so it overrides the saved folder in this shell.)")
        ai = doctor.ai_status()
        print(doctor.report(doctor.checks(ai)))
        connected = any(a["installed"] and a["signed_in"] is not False for a in ai)
        if (args.connect or not connected) and sys.stdin.isatty() and sys.stdout.isatty():
            doctor.walkthrough()
        return
    if args.command == "list":
        games = read_games()
        if not games:
            print(f"No games tracked yet in {games_dir()}")
        for g in games:
            print(f"{g.status:<8} {g.name} ({g.platform}, {g.genre}) — {g.spot}")
        return
    if args.command == "backlog":
        if args.games:
            added = batch_add_games(
                args.games,
                default_status="Backlog",
                default_platform=args.platform,
                default_genre=args.genre,
            )
            for g in added:
                if args.artwork:
                    fetch_game_artwork(g)
                print(f"Added to Backlog: {g.name} ({g.platform or 'any platform'}, {g.genre or 'any genre'})")
            return
        games = [g for g in read_games() if g.status.lower() == "backlog"]
        if not games:
            print("No games in Backlog yet. Add some with: game-guide backlog \"Game Title\"")
        for idx, g in enumerate(games, 1):
            meta = ", ".join(p for p in (g.platform, g.genre) if p)
            spot_str = f" — {g.spot}" if g.spot else ""
            print(f"{idx:>2}. {g.name}" + (f" ({meta})" if meta else "") + spot_str)
        return
    if args.command in ("track", "add"):
        saved = save_game(
            name=args.game,
            platform=args.platform,
            genre=args.genre,
            status=args.status,
            spot=args.spot,
        )
        if args.note:
            append_note(saved, args.note, heading=saved.spot or None)
        if getattr(args, "artwork", False):
            fetch_game_artwork(saved)
        print(f"Saved {saved.name} [{saved.status}] — Spot: {saved.spot or '?'}")
        return
    if args.command == "spot":
        saved = update_game(args.game, spot=args.spot, status=args.status)
        if args.note:
            append_note(saved, args.note, heading=saved.spot or None)
        print(f"Updated {saved.name} [{saved.status}] — Spot: {saved.spot}")
        return
    if args.command == "status":
        saved = update_game(args.game, status=args.status)
        print(f"Updated {saved.name} status -> {saved.status}")
        return
    if args.command in ("remove", "untrack"):
        if remove_game(args.game):
            print(f"Removed {args.game} from GAMES.md")
        else:
            sys.exit(f"Game not found in GAMES.md: {args.game}")
        return
    if args.command == "web" or args.web:
        from .web import run as run_web
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 0)
        no_open = getattr(args, "no_open", False)
        run_web(host=host, port=port, open_browser=not no_open)
        return
    if args.command == "package-app":
        from . import desktop
        out_dir = Path(args.out_dir).expanduser() if args.out_dir else None
        app_path = desktop.package_app(out_dir=out_dir) if out_dir is not None else desktop.package_app()
        print(app_path)
        return
    if args.command == "install-app":
        from . import desktop
        app_path = desktop.install_app(dest_dir=Path(args.dest_dir).expanduser(), add_to_dock=not args.no_dock,
                                       app=Path(args.app).expanduser() if args.app else None)
        print(f"Installed {app_path}")
        return
    if args.command == "walkthrough":
        # A walkthrough file is readable even if its game was removed from GAMES.md.
        game = find_game(args.game) or Game(args.game)
        sections = game_view(game, args.spoiler).chapters
        if not sections:
            sys.exit(f'No walkthrough for {args.game} yet. Build one: game-guide ask "full walkthrough for {args.game}"')
        if args.chapter is None:
            print("\n".join(f"{c.index + 1:>3}. {c.title}" for c in sections))
        elif 1 <= args.chapter <= len(sections):
            print(sections[args.chapter - 1].body)
        else:
            sys.exit(f"{game.name} has chapters 1-{len(sections)}")
        return
    if args.command == "ask":
        if args.model:
            os.environ["GAME_GUIDE_MODEL"] = args.model  # read by the backend commands
        conversation = Conversation({args.backend: args.resume} if args.resume else None)
        for update in conversation.ask(args.backend, " ".join(args.question), args.spoiler):
            if isinstance(update, Status):
                print(f"… {update.text}", file=sys.stderr)
            elif isinstance(update, Done):
                if args.json and not update.guide:
                    sys.exit(update.markdown)  # the plain-text reply; stderr, exit 1
                print(update.guide.model_dump_json(indent=2) if args.json else update.markdown)
            elif isinstance(update, Error):
                sys.exit(f"{args.backend} failed: {update.detail}")
        if session := conversation.sessions.get(args.backend):
            print(f"(continue with --resume {session})", file=sys.stderr)
        return
    from .app import run
    run()
