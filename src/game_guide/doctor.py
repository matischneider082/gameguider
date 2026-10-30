"""What this machine can do with GameGuider, and the walkthrough that connects it to the user's AI: the notes folder,
the AI CLIs that research guides (installed? signed in?), and the optional media tools, each with its fix. Shared by
`game-guide setup` (interactive in a terminal) and the app's Setup panel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

from .backends import BACKENDS
from .notes import games_dir

# The AIs GameGuider can use, with their official installers and sign-in commands.
AI = {
    "claude": {"label": "Claude", "app": "Claude Code", "plan": "a Claude Pro or Max plan, or an Anthropic account",
               "install": "curl -fsSL https://claude.ai/install.sh | bash", "sign_in": "claude auth login",
               "docs": "https://code.claude.com/docs/en/setup"},
    "codex": {"label": "ChatGPT", "app": "the Codex CLI", "plan": "a ChatGPT Plus, Pro or Business plan",
              "install": "curl -fsSL https://chatgpt.com/codex/install.sh | sh", "sign_in": "codex login",
              "docs": "https://github.com/openai/codex"},
}


def _package_fix(name: str) -> str:
    return f"brew install {name}" if sys.platform == "darwin" else f"Install {name} with your package manager, e.g. sudo apt install {name}"


MEDIA = {  # tool: (what it enables, how to install)
    "yt-dlp": ("Video playback and still frames", "uv tool install yt-dlp"),
    "ffmpeg": ("Video playback and still frames", _package_fix("ffmpeg")),
    "mpv": ("Opening videos in an external player", _package_fix("mpv")),
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = False


def signed_in(backend: str) -> bool | None:
    """Whether the backend's CLI is signed in; None when it isn't installed or can't say (an older CLI).
    Only the yes/no is read: never the account or any token."""
    exe = shutil.which(backend)
    if not exe:
        return None
    try:
        if backend == "claude":
            result = subprocess.run([exe, "auth", "status", "--json"], capture_output=True, text=True, timeout=15)
            return bool(json.loads(result.stdout).get("loggedIn"))
        return subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


def ai_status() -> list[dict]:
    return [{"id": b, **AI[b], "installed": bool(shutil.which(b)), "signed_in": signed_in(b)} for b in BACKENDS]


def checks(ai: list[dict] | None = None) -> list[Check]:
    ai = ai if ai is not None else ai_status()
    folder = games_dir()
    writable = folder.is_dir() and os.access(folder, os.W_OK) or not folder.exists() and os.access(_existing_parent(folder), os.W_OK)
    usable = [a for a in ai if a["installed"] and a["signed_in"] is not False]
    result = [Check("Notes folder", writable, str(folder) if writable else f"{folder} is not writable",
                    "" if writable else "Choose another folder with `game-guide setup --notes-dir PATH`.", required=True)]
    for a in ai:
        ok = a["installed"] and a["signed_in"] is not False
        detail = ("not installed" if not a["installed"] else "installed, not signed in" if a["signed_in"] is False
                  else "signed in" if a["signed_in"] else "installed")
        fix = "" if ok else (f"Sign in with `{a['sign_in']}`." if a["installed"] else
                             f"Install {a['app']} with `{a['install']}`, then sign in with `{a['sign_in']}`.")
        # One AI is enough, so a missing one only blocks when no other is usable.
        result.append(Check(f"{a['label']} ({a['app']})", ok, detail, fix, required=not usable))
    for tool, (use, fix) in MEDIA.items():
        path = shutil.which(tool)
        result.append(Check(tool, bool(path), path or f"not found; needed for: {use.lower()}", "" if path else fix))
    return result


def ready(found: list[Check]) -> bool:
    """Whether every required check passes: a writable notes folder and at least one AI that's ready."""
    return all(c.ok for c in found if c.required)


def as_json() -> dict:
    ai = ai_status()
    found = checks(ai)
    return {"notes_dir": str(games_dir()), "ready": ready(found), "checks": [asdict(c) for c in found], "ai": ai,
            "can_open_terminal": sys.platform == "darwin"}


def report(found: list[Check] | None = None) -> str:
    found = found if found is not None else checks()
    lines = [f"{'ok ' if c.ok else ('!! ' if c.required else '-- ')} {c.name}: {c.detail}" + (f"\n     fix: {c.fix}" if c.fix else "")
             for c in found]
    lines.append("Ready: ask questions and build walkthroughs." if ready(found) else
                 "Not ready: fix the items marked !! first. Items marked -- are optional.")
    return "\n".join(lines)


def open_in_terminal(backend: str, step: str) -> None:
    """Open Terminal running one of the walkthrough's fixed commands (macOS), so a sign-in can ask its questions."""
    if backend not in AI or step not in ("install", "sign_in"):
        raise ValueError("Unknown setup step.")
    if sys.platform != "darwin":
        raise ValueError("Copy the command into a terminal and run it there.")
    command = AI[backend][step].replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", 'tell application "Terminal" to activate',
                    "-e", f'tell application "Terminal" to do script "{command}"'], check=True, timeout=15)


def walkthrough(ask=input, run=subprocess.run, say=print) -> bool:
    """Connect an AI from a terminal: pick Claude or ChatGPT, install its CLI with the official installer, sign in.
    Returns whether an AI is ready afterwards."""
    say("\nGameGuider researches guides with an AI you already use, through its command-line app.")
    for number, backend in enumerate(BACKENDS, 1):
        say(f"  {number}) {AI[backend]['label']} ({AI[backend]['app']}), for {AI[backend]['plan']}")
    say(f"  {len(BACKENDS) + 1}) Skip for now")
    choice = ask(f"Which do you use? [1-{len(BACKENDS) + 1}] ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(BACKENDS):
        say("Skipped. Run `game-guide setup` when you're ready, or use Setup in the app.")
        return False
    backend = BACKENDS[int(choice) - 1]
    info = AI[backend]
    if not shutil.which(backend):
        say(f"\nStep 1 of 2: install {info['app']} with its official installer:\n    {info['install']}")
        if ask("Install it now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            say(f"Not installed. You can run that command yourself; see {info['docs']}")
            return False
        run(info["install"], shell=True)  # a fixed command from AI, never user input
        # The installers put the CLI in ~/.local/bin, which this process's PATH may not have yet.
        os.environ["PATH"] = os.pathsep.join([os.path.expanduser("~/.local/bin"), os.environ.get("PATH", "")])
        if not shutil.which(backend):
            say(f"{info['app']} didn't install. Open a new terminal and run `game-guide setup` again, or see {info['docs']}")
            return False
    if signed_in(backend) is not True:
        say(f"\nStep 2 of 2: sign in to {info['label']}. Your browser opens; finish there and come back.\n    {info['sign_in']}")
        run(info["sign_in"].split())
    if signed_in(backend) is False:
        say(f"Not signed in yet. Run `{info['sign_in']}` and try again.")
        return False
    say(f"\n{info['label']} is connected. GameGuider will use it to research guides.")
    return True


def _existing_parent(path):
    while not path.exists() and path != path.parent:
        path = path.parent
    return path
