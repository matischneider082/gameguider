"""Ask a question through the installed `claude` or `codex` CLI, so it runs on the user's own subscription.

Never read or reuse the CLIs' login tokens: driving the official binaries is what the subscriptions allow.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import procs
from .notes import games_dir

# Editable checkouts use the live plugin; published wheels carry the same files
# inside the package so both backends work without the source checkout.
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = (
    _SOURCE_ROOT
    if (_SOURCE_ROOT / "src" / "game_guide" / "backends.py").is_file()
    and (_SOURCE_ROOT / "skills" / "game-guide" / "SKILL.md").is_file()
    else Path(__file__).resolve().parent / "_plugin"
)
SKILL = PLUGIN_ROOT / "skills" / "game-guide" / "SKILL.md"
BACKENDS = ("claude", "codex")
SPOILERS = ("hint", "strategy", "answer")


def strict(schema):
    # Codex (OpenAI strict mode) rejects a schema unless every property is required and extra keys are forbidden;
    # validation still fills the defaults, so a reply that omits a field is accepted.
    schema["required"] = list(schema["properties"])
    for prop in schema["properties"].values():
        prop.pop("default", None)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=strict)
    title: str
    url: str
    at_seconds: int | None = None


class Guide(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=strict)
    kind: Literal["guide", "tracked", "question"] = Field(
        description="guide: a researched answer; tracked: only logged or updated a game; question: needs info from the user")
    game: str
    spot: str | None = None
    spoiler: Literal["hint", "strategy", "answer"]
    summary: str = Field(description="One or two sentences: the thing to do. For a hint, the hint itself.")
    prep: list[str] = []
    steps: list[str] = []
    watch_out: list[str] = []
    sources: list[Source] = []
    follow_up: str | None = None


# Codex's web search tags its sources inline as private-use-character markers ("\ue200cite\ue202turn0view0\ue201").
CITATION = re.compile("([ \t]*)\ue200[^\ue201]*\ue201")


def strip_citations(text):
    # The space before a marker goes too ("it. **" breaks Markdown bold), unless a word follows ("5 gold") or the
    # line ends (two trailing spaces are a Markdown line break).
    def keep_space(m):
        following = m.string[m.end():m.end() + 1]
        return m.group(1) if following.isalnum() or following == "\n" else ""
    return CITATION.sub(keep_space, text)
def uncite(value):
    """Strip citation markers from every string, one string at a time so a marker can't span two fields."""
    if isinstance(value, str):
        return strip_citations(value)
    if isinstance(value, dict):
        return {k: uncite(v) for k, v in value.items()}
    return [uncite(v) for v in value] if isinstance(value, list) else value


SCHEMA = json.dumps(Guide.model_json_schema())
SCHEMA_FILE = Path(tempfile.gettempdir(), "game-guide-schema.json")  # per-user on macOS ($TMPDIR)


def parse_guide(data):
    """A Guide from the CLI's structured payload (a dict or a JSON string), or None when it doesn't validate."""
    try:
        return Guide.model_validate_json(data) if isinstance(data, str) else Guide.model_validate(data)
    except ValidationError:
        return None


def render(guide):
    if guide.kind == "tracked":
        return f"✓ **{guide.game}**{f' — {guide.spot}' if guide.spot else ''}: {guide.summary}"
    lines = [f"## {guide.game}{f' — {guide.spot}' if guide.spot else ''} `{guide.spoiler}`", "", f"**{guide.summary}**"]
    for title, items, numbered in (("Prep", guide.prep, False), ("Steps", guide.steps, True),
                                   ("Watch out", guide.watch_out, False)):
        if items:
            lines += ["", f"**{title}**", "", *(f"{i}. {x}" if numbered else f"- {x}" for i, x in enumerate(items, 1))]
    if guide.sources:
        lines += ["", "**Sources**", "", *(f"- [{s.title}]({s.url})" + (
            f" ▶ {s.at_seconds // 60}:{s.at_seconds % 60:02d}" if s.at_seconds is not None else "") for s in guide.sources)]
    if guide.follow_up:
        lines += ["", f"_{guide.follow_up}_"]
    return "\n".join(lines)


@dataclass
class Event:
    kind: str  # session | status | text | done | error
    text: str = ""
    session: str | None = None
    guide: Guide | None = None  # on `done`, when the reply matched the schema; `text` is the fallback


def framed(question, spoiler, game=None):
    where = (f" Game selected in the TUI (ignore if the user names another game): {game.name},"
             f" currently at: {game.spot or 'unknown'}.") if game else ""
    return f"{question}\n\n(Spoiler level: {spoiler}.{where})"


def ask(backend, question, session=None):
    """Run one turn and yield Events. The last one is `done` (with the answer) or `error`."""
    notes = games_dir()
    notes.mkdir(parents=True, exist_ok=True)
    if planted := [name for name in PLANTED if (notes / name).exists()]:
        yield Event("error", f"{REFUSAL} {', '.join(planted)} in {notes} would load instructions or tools "
                             "from the notes folder. Remove it if you didn't put it there.")
        return
    try:
        cmd, parse = (claude_command(question, session, notes), claude_events) if backend == "claude" \
            else (codex_command(question, session), codex_events)
    except RuntimeError as e:
        yield Event("error", str(e))
        return
    last_text, finished = "", False
    with tempfile.TemporaryFile("w+") as stderr, \
            procs.start(cmd, cwd=notes, env={**os.environ, "GAME_GUIDE_DIR": str(notes)}, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=stderr, text=True) as proc:  # own group, no tty input; tracked so quitting the TUI kills it
        try:
            for line in proc.stdout:
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                for event in parse(raw):
                    if event.kind == "text":
                        last_text = event.text
                    if event.kind == "done" and not event.text:  # codex: its last message is the JSON reply
                        event.text = strip_citations(last_text)
                        if guide := event.guide or parse_guide(last_text):
                            event.guide = Guide.model_validate(uncite(guide.model_dump()))
                        if not event.text:
                            event = Event("error", f"{backend} ended the turn without an answer")
                    finished = finished or event.kind in ("done", "error")
                    yield event
            # A non-zero exit after the CLI already reported its outcome would replace the useful message.
            if proc.wait() and not finished:
                stderr.seek(0)
                yield Event("error", stderr.read()[-2000:] or f"{backend} exited with {proc.returncode}")
        finally:
            # Closing the generator early (a superseded question) must not leave the CLI running and editing notes.
            # The whole tree, so yt-dlp/uv children the CLI spawned die with it.
            procs.untrack(proc)
            procs.kill_tree(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                procs.kill_tree(proc.pid, signal.SIGKILL)


def claude_command(question, session, notes):
    # Loads only this plugin: no user hooks, plugins or MCP servers, which keeps each turn fast and cheap.
    # acceptEdits allows writes only inside the working directory, which ask() sets to the notes folder.
    # Fetched pages and captions are untrusted, so Bash runs only the skill's own scripts and Read stays in the
    # folders the skill needs: a prompt injection can't run arbitrary commands or read ~/.ssh.
    scripts = SKILL.parent / "scripts"
    # `uv run --no-config` so a uv.toml planted in the (writable) notes folder can't swap the package index.
    # Agent/Task let Claude spawn parallel subagents for multi-chapter walkthroughs within the same sandbox.
    rules = ["WebSearch", "WebFetch", "Skill", "Agent", "Task", f"Bash(python3 {scripts}/captions.py:*)",
             f"Bash(python3 {scripts}/save_image.py:*)", f"Bash(python3 {scripts}/games_table.py)",
             f"Bash(uv run --no-config {scripts}/fetch_text_guide.py:*)",
             f"Bash(uv run --no-config {scripts}/fetch_yt_walkthrough.py:*)",
             *(f"Read(/{folder}/**)" for folder in
               (notes.resolve(), SKILL.parent, Path("/tmp/game-guide"), Path("/tmp/game-guide").resolve()))]
    # Only the first turn loads the skill; a resumed session already has it, so don't re-inject it and the table.
    prompt = question if session else f"/game-guide:game-guide {question}"
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--plugin-dir", str(PLUGIN_ROOT), "--setting-sources", "project", "--strict-mcp-config",
           "--permission-mode", "acceptEdits", "--add-dir", str(notes), "--allowedTools", ",".join(rules),
           "--disallowedTools", ",".join(f"Edit(/{notes.resolve()}/{name}{'/**' if name in ('.agents', '.claude', '.codex') else ''})"
                                         for name in PLANTED),
           "--model", os.environ.get("GAME_GUIDE_MODEL") or "sonnet", "--json-schema", SCHEMA]
    return cmd + (["--resume", session] if session else [])


# Tools that could act or send data outside the notes folder; a prompt injection in a fetched page could steer them.
CODEX_OFF = ("apps", "plugins", "hooks", "browser_use", "in_app_browser", "computer_use", "memories")
# Files in the notes folder that would make a CLI load instructions, settings, hooks or MCP servers from it. The
# notes folder is writable by both backends, so a prompt injection could plant one for the next turn.
PLANTED = (".agents", ".claude", ".codex", ".mcp.json", "AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md")
# Starts the error ask() yields when a PLANTED file is present; the Conversation classifies errors by it.
REFUSAL = "refusing to run:"


def codex_command(question, session):
    # Codex has no per-command allowlist like claude's, so its shell runs in a permission profile instead: it reads
    # only system basics, the notes folder (read-write) and the skill (read-only), with no network. A prompt
    # injection can't read ~/.ssh or upload anything; the most it could leak through web search is the notes.
    notes = games_dir().resolve()
    # The PLANTED paths stay read-only, so the shell can't leave instructions or MCP servers for the next turn.
    planted = "".join(f', {json.dumps(str(notes / name))}="read"' for name in PLANTED)
    profile = (f'permissions.game-guide.filesystem={{":minimal"="read", ":workspace_roots"="write", '
               f'{json.dumps(str(SKILL.parent))}="read"{planted}}}')
    # No project root markers: codex would otherwise climb to the enclosing git repo and fail reading its AGENTS.md.
    # Memories are off so an injected page can't read the user's codex memories or write new ones.
    config = ["-c", 'default_permissions="game-guide"', "-c", profile, "-c", 'web_search="live"',
              "-c", "project_root_markers=[]", "-c", "memories.use_memories=false",
              "-c", "memories.generate_memories=false",
              *(f for feature in CODEX_OFF for f in ("--disable", feature)),
              *(f for name in codex_mcp_servers() for f in ("-c", f"mcp_servers.{name}.enabled=false"))]
    SCHEMA_FILE.write_text(SCHEMA)
    # Unlike claude, codex keeps the user's configured model unless one is asked for.
    model = os.environ.get("GAME_GUIDE_MODEL")
    flags = ["--json", "--skip-git-repo-check", "--output-schema", str(SCHEMA_FILE), *(["-m", model] if model else [])]
    if session:
        return ["codex", "exec", *config, "resume", *flags, session, question]
    return ["codex", "exec", *config, *flags, skill_prompt(question)]


def codex_mcp_servers():
    """MCP servers in the user's codex config.toml, so each can be switched off for this run.

    Plugin-provided servers aren't listed: `--disable plugins` removes them, and `-c` can't address them anyway.
    """
    config = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"
    try:
        names = list(tomllib.loads(config.read_text(encoding="utf-8")).get("mcp_servers", {})) if config.exists() else []
    except (OSError, tomllib.TOMLDecodeError) as e:
        # Fail closed: running with the user's servers on would hand a prompt injection their tools.
        raise RuntimeError(f"couldn't read {config} to switch its MCP servers off: {e}") from e
    # `-c` takes a bare dotted path (quotes would become part of the name), so only bare-key names can be addressed.
    if odd := [n for n in names if not re.fullmatch(r"[A-Za-z0-9_-]+", n)]:
        raise RuntimeError(f"can't switch off codex MCP servers {odd}: disable them in {config}")
    return names


def skill_prompt(question):
    """Codex can't load the plugin, so inline the skill and do its `!`cat`` injection here."""
    text = SKILL.read_text(encoding="utf-8")
    body = text.split("---", 2)[2] if text.startswith("---") else text
    table = subprocess.run([sys.executable, SKILL.parent / "scripts" / "games_table.py"], capture_output=True, text=True,
                           check=True, env={**os.environ, "GAME_GUIDE_DIR": str(games_dir())}).stdout
    body = re.sub(r"^!`.*`$", lambda _: table, body, flags=re.M)
    return (f"Follow the game-guide skill below. Base directory for this skill: {SKILL.parent}\n"
            "Your shell has no network access and can read only the notes folder and this skill, so the skill's fetch, "
            "caption and save_image scripts can't run here: read guides and video pages with your web search tool "
            "instead, and skip saving images.\n"
            f"{body}\n---\n\nUser: {question}")


def claude_events(e):
    kind = e.get("type")
    if kind == "system" and e.get("subtype") == "init":
        return [Event("session", session=e.get("session_id"))]
    message = e.get("message")  # some system events carry a plain-string message
    content = message.get("content") if isinstance(message, dict) else None
    if kind == "user" and isinstance(content, str) and "<local-command-stderr>" in content:
        # A skill's !`command` failed to load; Claude ends the turn without answering, so surface why.
        return [Event("error", re.sub(r"</?local-command-stderr>", "", content))]
    if kind == "assistant":
        return [Event("status", describe(b["name"], b.get("input") or {})) if b.get("type") == "tool_use"
                else Event("text", b["text"])
                for b in (content if isinstance(content, list) else []) if b.get("type") in ("tool_use", "text")]
    if kind == "result":
        if e.get("is_error"):
            return [Event("error", e.get("result") or e.get("subtype", "error"), e.get("session_id"))]
        return [Event("done", e.get("result", ""), e.get("session_id"), parse_guide(e.get("structured_output")))]
    return []


def codex_events(e):
    kind, item = e.get("type"), e.get("item") or {}
    if kind == "thread.started":
        return [Event("session", session=e.get("thread_id"))]
    if kind == "item.started" and item.get("type") == "command_execution":
        return [Event("status", describe("Bash", {"command": item.get("command", "")}))]
    if kind == "item.started" and item.get("type") == "web_search":
        return [Event("status", describe("WebSearch", item))]
    if kind == "item.started" and item.get("type") in ("collab_tool_call", "subagent"):
        return [Event("status", describe("Agent", item))]
    if kind == "item.completed" and item.get("type") == "agent_message":
        return [Event("text", item.get("text", ""))]
    if kind == "turn.completed":
        return [Event("done")]
    if kind == "turn.failed":
        return [Event("error", (e.get("error") or {}).get("message") or "codex failed")]
    if kind == "error":  # codex reports retries it recovers from this way; a fatal one is followed by turn.failed
        return [Event("status", e.get("message", "codex error"))]
    return []


def describe(tool, args):
    if tool in ("Agent", "Task"):
        label = (args.get("description") or args.get("prompt") or "").strip()[:60]
        return f"subagent: {label}" if label else "running subagent"
    if tool == "WebSearch":
        return f"searching: {args.get('query', '')}"
    if tool == "WebFetch":
        return f"reading {args.get('url', '')}"
    if tool == "Bash":
        command = args.get("command", "")
        command = " ".join(command) if isinstance(command, list) else command
        if "captions.py" in command or "yt-dlp" in command:
            return "fetching video captions"
        return ("saving an image" if "save_image.py" in command else command)[:80]
    if tool in ("Read", "Edit", "Write"):
        return f"{tool.lower()} {Path(args.get('file_path', '')).name}"
    if tool == "Skill":
        return "loading the game-guide skill"
    return {"ToolSearch": "thinking", "StructuredOutput": "writing the answer"}.get(tool, tool)
