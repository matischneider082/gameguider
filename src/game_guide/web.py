"""Local browser Web UI for game-guide: full walkthroughs, saved maps/images, and locally cached videos.

GuideRequestHandler is the HTTP adapter over the actions module: route tables map paths to actions, whose payload
goes out as JSON and whose ActionError becomes {"error": message} with its status. Only what needs the socket stays
here: the page, image and video bytes with byte ranges, and the NDJSON ask stream."""

from __future__ import annotations

import json
import mimetypes
import threading
import urllib.parse
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import actions, media, procs, themes
from .actions import ActionError
from .conversation import Conversation, Done, Error, Status, Text
from .notes import Video, games_dir
from .render import render

# path -> action(query or body): its return value is the 200 JSON response
GET_ACTIONS = {"/api/state": actions.state, "/api/setup": actions.setup, "/api/search": actions.search}
POST_ACTIONS = {
    "/api/setup": actions.save_setup,
    "/api/setup/terminal": actions.setup_terminal,
    "/api/theme": actions.save_theme,
    "/api/updates/check": actions.check_updates,
    "/api/games/artwork": actions.refresh_artwork,
    "/api/scores/refresh": actions.refresh_score,
    "/api/backlog": actions.add_backlog,
    "/api/games/reorder": actions.reorder,
    "/api/games/save": actions.save,
    "/api/games/spot": actions.set_spot,
    "/api/games/notes": actions.edit_notes,
    "/api/games/delete": actions.delete,
    "/api/play": actions.play,
}


class GuideHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address):
        super().__init__(server_address, GuideRequestHandler)
        self.conversation = Conversation()

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def server_close(self):
        self.conversation.cancel()
        super().server_close()


class GuideRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _send_json(self, status: int, data: dict):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, status: int, content_type: str, data: bytes, cache: str = "no-cache"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file_range(self, path: Path, content_type: str):
        size = path.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200

        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            if "-" in spec:
                left, right = spec.split("-", 1)
                try:
                    if left == "":
                        suffix = int(right)
                        start = max(0, size - suffix)
                    else:
                        start = int(left)
                        if right:
                            end = min(size - 1, int(right))
                    if 0 <= start <= end < size:
                        status = 206
                    else:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                except ValueError:
                    pass

        length = max(0, end - start + 1) if size > 0 else 0
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()

        if length <= 0:
            return
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def _foreign(self, post: bool) -> str | None:
        """Why a request didn't come from this app's own page, or None. Any website the user has open can reach a
        local port, and a POST here can start their AI CLI, so: the Host must be ours (a DNS-rebinding page names its
        own domain), a browser's Origin must be ours, and a POST must be JSON (which needs a CORS preflight that
        this server never grants, unlike a form or a text/plain fetch)."""
        host, port = self.server.server_address[:2]
        named = (self.headers.get("Host") or "").lower()
        if host in ("127.0.0.1", "localhost", "::1") and named not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return "Unknown host"
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() != f"http://{named}":
            return "Cross-site request refused"
        if post and (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return "Send JSON with Content-Type: application/json"
        return None

    def do_GET(self):
        if reason := self._foreign(post=False):
            self._send_json(403, {"error": reason})
            return
        parsed = urllib.parse.urlparse(self.path)
        query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        self._route(parsed.path, query, GET_ACTIONS, self.GET_ROUTES, self.GET_PREFIX_ROUTES)

    def do_POST(self):
        if reason := self._foreign(post=True):
            self.close_connection = True  # the unread body would otherwise be parsed as the next request
            self._send_json(403, {"error": reason})
            return
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        self._route(urllib.parse.urlparse(self.path).path, body, POST_ACTIONS, self.POST_ROUTES, {})

    def _route(self, path: str, arg: dict, json_actions: dict, routes: dict, prefix_routes: dict):
        """Answer with the action's payload, or let a route send its own page, bytes or stream."""
        try:
            if action := json_actions.get(path):
                self._send_json(200, action(arg))
            elif route := routes.get(path):
                route(self, arg)
            elif prefix := next((p for p in prefix_routes if path.startswith(p)), None):
                prefix_routes[prefix](self, path[len(prefix):], arg)
            else:
                raise ActionError(404, "Not found")
        except ActionError as e:
            self._send_json(e.status, {"error": e.message})

    def _send_page(self, _query: dict):
        # themes.current() only returns names from its own closed list, so it's safe inside the attribute.
        page = HTML_PAGE.replace('<html lang="en">', f'<html lang="en" data-theme="{themes.current()}">', 1)
        self._send_bytes(200, "text/html; charset=utf-8", page.encode("utf-8"), cache="no-store")

    def _send_share(self, query: dict):
        filename, content = actions.share(query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_image(self, rel: str, _query: dict):
        rel = urllib.parse.unquote(rel).lstrip("/")
        images_root = (games_dir() / "images").resolve()
        target = (images_root / rel).resolve()
        if target == images_root or not target.is_relative_to(images_root):
            raise ActionError(403, "Refusing image path outside images/")
        if not target.is_file():
            raise ActionError(404, "Image not found")
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send_bytes(200, mime, target.read_bytes())

    def _send_thumbnail(self, video_id: str, query: dict):
        actions.check_video_id(video_id)
        try:
            at = max(0, int(float(query.get("at") or "0")))
        except (ValueError, OverflowError):
            at = 0
        try:
            thumb = (
                media.timestamp_thumbnail(Video(video_id, at, video_id))
                if at > 0
                else media.thumbnail(Video(video_id, 0, video_id))
            )
        except Exception as e:
            raise ActionError(502, f"thumbnail failed: {e}") from e
        self._send_bytes(200, "image/jpeg", thumb.read_bytes(), cache="max-age=3600")

    def _send_frames(self, video_id: str, query: dict):
        self._send_json(200, actions.frames(video_id, query))

    def _send_frame(self, rest: str, _query: dict):
        parts = rest.split("/")
        if len(parts) != 2 or not actions.VIDEO_ID_RE.fullmatch(parts[0]) or not parts[1].isdigit():
            raise ActionError(400, "Invalid frame path")
        frame_file = media.CACHE / f"{parts[0]}-{parts[1]}.jpg"
        if not frame_file.is_file():
            raise ActionError(404, "Frame not found")
        self._send_bytes(200, "image/jpeg", frame_file.read_bytes(), cache="max-age=3600")

    def _send_video(self, video_id: str, _query: dict):
        actions.check_video_id(video_id)
        if not actions.is_video_cached(video_id) and (reason := media.unavailable("video")):
            raise ActionError(400, reason)
        try:
            video_path = media.download(Video(video_id, 0, video_id))
        except Exception as e:
            raise ActionError(502, f"video download failed: {media.short_error(e)}") from e
        mime = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
        self._send_file_range(video_path, mime)

    def _new_chat(self, _body: dict):
        self.server.conversation.reset()
        self._send_json(200, {"ok": True})

    def _ask(self, body: dict):
        self._stream_updates(*actions.ask(self.server.conversation, body))

    def _walkthrough(self, body: dict):
        self._stream_updates(*actions.walkthrough(self.server.conversation, body))

    def _stream_updates(self, backend: str, stream):
        """Deliver a Conversation's updates as NDJSON, one event per line, until the answer or error."""
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def write_event(payload: dict) -> bool:
            raw = (json.dumps(payload) + "\n").encode("utf-8")
            try:
                self.wfile.write(raw)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        with closing(stream):
            for update in stream:
                if isinstance(update, Status):
                    payload = {"kind": "status", "text": f"{backend}: {update.text}"}
                elif isinstance(update, Text):
                    payload = {"kind": "text", "text": update.text}
                elif isinstance(update, Done):
                    payload = {"kind": "done", "text": update.text, "rendered": update.markdown, "html": render(update.markdown, games_dir()),
                               "session": update.session}
                else:
                    payload = {
                        "kind": "error",
                        "text": update.detail,
                        "reason": update.reason,
                        "message": update.message,
                        "rendered": f"**{backend} failed:**\n\n```\n{update.detail}\n```",
                    }
                if not write_event(payload) or isinstance(update, (Done, Error)):
                    break

    # Routes that send their own response; a prefix route also gets the rest of the path after its prefix.
    GET_ROUTES = {"/": _send_page, "/index.html": _send_page, "/api/share": _send_share}
    GET_PREFIX_ROUTES = {"/images/": _send_image, "/api/thumbnail/": _send_thumbnail, "/api/frames/": _send_frames,
                         "/api/frame/": _send_frame, "/api/video/": _send_video}
    POST_ROUTES = {"/api/new-chat": _new_chat, "/api/ask": _ask, "/api/walkthrough": _walkthrough}


def start_server(host: str = "127.0.0.1", port: int = 0) -> GuideHTTPServer:
    server = GuideHTTPServer((host, port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run(host: str = "127.0.0.1", port: int = 0, open_browser: bool = True):
    server = GuideHTTPServer((host, port))
    url = server.url
    print(f"GameGuider Web UI running at {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        procs.stop_all()


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GameGuider — Your games and walkthroughs</title>
<style>
  :root {
    --bg: #0e1014;
    --surface-1: #14171d;
    --surface-2: #1b1f27;
    --surface-3: #232833;
    --border: #262c38;
    --border-strong: #353d4d;
    --text: #ece8df;
    --text-secondary: #b4b9c6;
    --muted: #9ea5b5;
    --accent: #bd9bff;
    --accent-hover: #cfb7ff;
    --accent-soft: rgba(189, 155, 255, 0.14);
    --accent-border: rgba(189, 155, 255, 0.38);
    --green: #38b28b;
    --green-soft: rgba(56, 178, 139, 0.14);
    --green-border: rgba(56, 178, 139, 0.38);
    --blue: #5c95d6;
    --blue-soft: rgba(92, 149, 214, 0.14);
    --plum: #c28799;
    --plum-soft: rgba(194, 135, 153, 0.16);
    --danger: #f08360;
    --danger-soft: rgba(217, 107, 67, 0.15);
    --on-accent: #121316;
    --track: rgba(255, 255, 255, 0.07);
    --bar: rgba(14, 16, 20, 0.92);
    --bar-soft: rgba(14, 16, 20, 0.45);
    color-scheme: dark;
    --radius: 8px;
    --radius-sm: 6px;
    --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", system-ui, sans-serif;
    --font-mono: "SF Mono", ui-monospace, Menlo, Monaco, Consolas, monospace;
  }
  /*__THEMES__*/
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-sans);
    font-size: 15px;
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
  }
  button, input, select, textarea {
    font-family: inherit;
  }
  input::placeholder, textarea::placeholder {
    color: var(--muted);
    opacity: 1;
  }
  .icon {
    width: 15px;
    height: 15px;
    stroke: currentColor;
    stroke-width: 1.85;
    fill: none;
    stroke-linecap: round;
    stroke-linejoin: round;
    flex-shrink: 0;
  }
  .icon-sm {
    width: 13px;
    height: 13px;
  }
  .app-shell {
    display: grid;
    grid-template-columns: 300px 1fr;
    grid-template-rows: 1fr auto;
    height: 100vh;
  }

  /* Left Library Sidebar */
  .sidebar {
    grid-row: 1 / 3;
    background: var(--surface-1);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .brand-bar {
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .brand-title {
    font-weight: 700;
    font-size: 15px;
    letter-spacing: -0.01em;
    display: flex;
    align-items: center;
    gap: 9px;
    color: var(--text);
  }
  .brand-mark {
    width: 26px;
    height: 26px;
    border-radius: 6px;
    background: var(--accent-soft);
    border: 1px solid var(--accent-border);
    color: var(--accent);
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .sidebar-actions {
    padding: 10px 12px;
    display: flex;
    gap: 8px;
    border-bottom: 1px solid var(--border);
  }
  .update-panel { padding: 0 12px 10px; border-bottom: 1px solid var(--border); }
  .update-panel { display: flex; flex-wrap: wrap; gap: 6px; }
  .update-panel button { flex: 1; }
  .update-panel .update-result { flex-basis: 100%; }
  .guide-hits h2 { margin:28px 0 10px; font-size:16px; }
  .guide-hits p { color:var(--text-secondary); font-size:14px; }
  .guide-hits ul { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
  .guide-hit { display:grid; gap:4px; width:100%; text-align:left; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:var(--surface-1); color:var(--text); cursor:pointer; font:inherit; }
  .guide-hit:hover, .guide-hit:focus-visible { border-color:var(--accent-border); }
  .guide-hit-place { font-weight:600; font-size:13px; }
  .guide-hit-snippet { color:var(--text-secondary); font-size:13px; line-height:1.5; }
  .setup-banner { display:flex; flex-wrap:wrap; align-items:center; gap:10px 14px; margin:0 0 18px; padding:12px 14px; border:1px solid var(--accent-border); border-radius:8px; background:var(--accent-soft); font-size:14px; }
  .setup-banner[hidden] { display:none; }
  .ai-connect { display:grid; gap:10px; padding-bottom:14px; border-bottom:1px solid var(--border); }
  .ai-connect h3 { margin:0; font-size:15px; }
  .ai-connect p { margin:0; font-size:13px; color:var(--text-secondary); }
  .ai-options { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:8px; }
  .ai-option { display:grid; gap:2px; text-align:left; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:var(--surface-1); color:var(--text); font:inherit; cursor:pointer; }
  .ai-option span { font-size:12px; color:var(--text-secondary); }
  .ai-option.active { border-color:var(--accent); background:var(--accent-soft); }
  .ai-steps { list-style:none; margin:0; padding:0; display:grid; gap:10px; }
  .ai-steps li { display:grid; grid-template-columns:24px 1fr; gap:8px; font-size:13px; }
  .ai-steps .mark { display:grid; place-items:center; width:22px; height:22px; border-radius:50%; border:1px solid var(--border); font-size:12px; font-weight:700; }
  .ai-steps .done .mark { border-color:var(--accent); color:var(--accent); }
  .ai-steps .cmd { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-top:6px; }
  .ai-steps code { font-family:var(--font-mono); font-size:12px; background:var(--surface-2); padding:3px 6px; border-radius:4px; user-select:all; overflow-wrap:anywhere; }
  .ai-actions { display:flex; align-items:center; gap:12px; font-size:13px; }
  .setup-list { list-style:none; margin:0; padding:0; display:grid; gap:10px; }
  .setup-list li { display:grid; grid-template-columns:22px 1fr; gap:2px 8px; font-size:13px; }
  .setup-list .mark { grid-row:span 3; font-weight:700; }
  .setup-list .ok .mark { color:var(--accent); }
  .setup-list .missing.required .mark { color:var(--danger); }
  .setup-list .detail { color:var(--text-secondary); overflow-wrap:anywhere; }
  .setup-list .fix { overflow-wrap:anywhere; }
  .setup-list code { font-family:var(--font-mono); font-size:12px; background:var(--surface-2); padding:1px 5px; border-radius:4px; user-select:all; }
  .update-result { color: var(--text-secondary); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
  .update-result:empty { display: none; }
  .update-result p { margin: 8px 0 0; }
  .update-result a { color: var(--accent); text-underline-offset: 3px; }
  .status-filters {
    padding: 8px 12px;
    display: flex;
    gap: 4px;
    overflow-x: auto;
    border-bottom: 1px solid var(--border);
    scrollbar-width: none;
  }
  .status-filters::-webkit-scrollbar { display: none; }
  .filter-chip {
    background: transparent;
    color: var(--text-secondary);
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    padding: 4px 9px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }
  .filter-chip:hover {
    color: var(--text);
    background: var(--surface-2);
  }
  .filter-chip.active {
    background: var(--accent-soft);
    color: var(--accent);
    border-color: var(--accent-border);
  }

  /* Buttons */
  .btn {
    text-decoration: none;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 6px 12px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    white-space: nowrap;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
  }
  .btn:hover:not(:disabled) {
    background: var(--surface-3);
    border-color: var(--border-strong);
  }
  .btn:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
  .btn:disabled {
    opacity: 0.42;
    cursor: not-allowed;
  }
  .btn-primary {
    background: var(--accent);
    color: var(--on-accent);
    border-color: var(--accent);
    font-weight: 600;
  }
  .btn-primary:hover:not(:disabled) {
    background: var(--accent-hover);
    border-color: var(--accent-hover);
  }
  .btn-success {
    background: var(--green-soft);
    color: var(--green);
    border-color: var(--green-border);
    font-weight: 600;
  }
  .btn-danger {
    background: var(--danger-soft);
    color: var(--danger);
    border-color: rgba(217, 107, 67, 0.36);
  }
  .btn-ghost {
    background: transparent;
    border-color: transparent;
    color: var(--text-secondary);
  }
  .btn-ghost:hover:not(:disabled) {
    background: var(--surface-2);
    color: var(--text);
    border-color: var(--border);
  }
  .btn-sm {
    padding: 5px 10px;
    font-size: 12px;
  }

  .visually-hidden { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
  .library-search { padding: 12px 12px 4px; }
  .library-search input { width:100%; background:var(--bg); color:var(--text); border:1px solid var(--border-strong); border-radius:var(--radius-sm); padding:8px 10px; }
  .game-card:focus-visible, .chapter-item:focus-visible, .map-image-button:focus-visible, .tab-btn:focus-visible, .filter-chip:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

  /* Games List */
  .games-list {
    flex: 1;
    overflow-y: auto;
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .game-card {
    padding: 10px;
    border-radius: var(--radius);
    border: 1px solid transparent;
    background: transparent;
    cursor: pointer;
    text-align: left;
    transition: background 0.15s, border-color 0.15s;
  }
  .game-card:hover {
    background: var(--surface-2);
    border-color: var(--border);
  }
  .game-card.active {
    background: var(--surface-2);
    border-color: var(--accent-border);
  }
  .game-card-main {
    display: grid;
    grid-template-columns: 42px minmax(0, 1fr);
    gap: 10px;
    align-items: start;
  }
  .game-card-art, .game-card-art-placeholder {
    width: 42px;
    height: 56px;
    border-radius: 5px;
    border: 1px solid var(--border);
  }
  .game-card-art {
    object-fit: cover;
    background: var(--surface-3);
  }
  .game-card-art-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--surface-2);
    color: var(--text-secondary);
    font-size: 18px;
    font-weight: 700;
  }
  .game-card-details { min-width: 0; }
  .game-card-queue {
    margin-top: 8px;
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .queue-position {
    margin-right: auto;
    color: var(--text-secondary);
    font-family: var(--font-mono);
    font-size: 12px;
  }
  .game-card-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-weight: 600;
    font-size: 14px;
    color: var(--text);
  }
  .game-card-title-wrap {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }
  .game-card-title-wrap span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .active-indicator {
    width: 7px;
    height: 7px;
    border-radius: 999px;
    background: var(--accent);
    flex-shrink: 0;
  }
  .game-card-meta {
    margin-top: 5px;
    font-size: 12px;
    color: var(--text-secondary);
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }
  .game-card-spot {
    margin-top: 6px;
    font-size: 12px;
    color: var(--accent);
    font-family: var(--font-mono);
    display: flex;
    align-items: center;
    gap: 5px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .game-progress-wrap {
    margin-top: 8px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .game-progress-bar {
    flex: 1;
    height: 5px;
    background: var(--track);
    border-radius: 999px;
    overflow: hidden;
  }
  .game-progress-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 999px;
    transition: width 0.25s ease;
  }
  .game-progress-label {
    font-size: 12px;
    font-family: var(--font-mono);
    color: var(--text-secondary);
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  /* Pills & Badges */
  .pill {
    font-size: 12px;
    font-family: var(--font-mono);
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--surface-3);
    color: var(--text-secondary);
    white-space: nowrap;
  }
  .pill.playing { background: var(--green-soft); color: var(--green); }
  .pill.backlog { background: var(--blue-soft); color: var(--blue); }
  .pill.paused { background: var(--accent-soft); color: var(--accent); }
  .pill.beaten { background: var(--plum-soft); color: var(--plum); }
  .pill.dropped { background: var(--danger-soft); color: var(--danger); }
  .pill.spot { background: var(--accent-soft); color: var(--accent); }
  .pill.walk { background: var(--blue-soft); color: var(--blue); }

  /* Main Workspace */
  .main-pane {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }
  .topbar {
    background: var(--surface-1);
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-direction: column;
  }
  .share-controls { padding: 0 24px 10px; }
  .share-controls summary { cursor: pointer; width: fit-content; }
  .share-form { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; padding: 12px 0 4px; }
  .share-form label { display: flex; gap: 6px; align-items: center; font-size: 13px; }
  .share-help { flex-basis: 100%; margin: 0; color: var(--text-secondary); font-size: 12px; line-height: 1.5; }
  .share-error { color: var(--danger); font-size: 12px; }
  .topbar-primary {
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  .game-header-info {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    min-width: 0;
  }
  .game-header-art {
    width: 64px;
    height: 86px;
    object-fit: cover;
    border-radius: 5px;
    border: 1px solid var(--border-strong);
    background: var(--surface-2);
  }
  .game-header-title {
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.015em;
    margin: 0;
    color: var(--text);
  }
  .topbar-spot-bar {
    padding: 8px 24px 10px;
    background: var(--bar-soft);
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
  }
  .spot-bar-left {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    flex: 1;
    min-width: 280px;
  }
  .spot-bar-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .status-select, .chapter-jump-select {
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  .chapter-jump-select {
    max-width: 260px;
    font-weight: 500;
  }
  .spot-editor-wrap {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 2px 4px 2px 10px;
  }
  .spot-editor-input {
    background: transparent;
    border: none;
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: 12px;
    min-width: 180px;
  }
  .spot-editor-input:focus { outline: none; }
  .controls-group {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .control-cluster {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .control-label {
    font-size: 12px;
    color: var(--muted);
    font-weight: 500;
  }
  .segmented {
    display: inline-flex;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 2px;
  }
  .segmented button {
    background: transparent;
    color: var(--text-secondary);
    border: none;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
    border-radius: 4px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    transition: background 0.15s, color 0.15s;
  }
  .segmented button:hover {
    color: var(--text);
  }
  .segmented button.active {
    background: var(--surface-3);
    color: var(--accent);
  }
  .kbd-hint {
    font-family: var(--font-mono);
    font-size: 12px;
    color: inherit;
  }

  /* Navigation Tabs */
  .tabs-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 0 24px;
    background: var(--surface-1);
    border-bottom: 1px solid var(--border);
  }
  .tabs-group {
    display: flex;
    gap: 6px;
  }
  .tab-btn {
    background: transparent;
    color: var(--text-secondary);
    border: none;
    border-bottom: 2px solid transparent;
    padding: 10px 14px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    transition: color 0.15s, border-color 0.15s;
  }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }
  .tab-count {
    font-size: 12px;
    font-family: var(--font-mono);
    background: var(--surface-2);
    color: var(--text-secondary);
    padding: 1px 7px;
    border-radius: 999px;
    font-variant-numeric: tabular-nums;
  }
  .shortcuts-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    color: var(--muted);
    font-family: var(--font-mono);
  }

  /* Tab Content Views */
  .tab-view {
    flex: 1;
    overflow: hidden;
    display: none;
  }
  .tab-view.active {
    display: flex;
  }
  .scroll-body {
    flex: 1;
    overflow-y: auto;
    padding: 28px 36px;
    max-width: 980px;
  }

  /* Walkthrough Split View */
  .walk-layout {
    display: grid;
    grid-template-columns: 300px 1fr;
    width: 100%;
    height: 100%;
    overflow: hidden;
  }
  .walk-sidebar {
    background: var(--surface-1);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .walk-sidebar-top {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .walk-filter {
    width: 100%;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 6px 10px;
    font-size: 13px;
  }
  .chapters-list {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .chapter-item {
    width: 100%; background: transparent; text-align: left; font-family: inherit;
    padding: 8px 10px;
    border-radius: var(--radius-sm);
    border: 1px solid transparent;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-size: 13px;
    color: var(--text-secondary);
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .chapter-item:hover {
    background: var(--surface-2);
    color: var(--text);
  }
  .chapter-item.active {
    background: var(--surface-3);
    border-color: var(--border-strong);
    color: var(--text);
    font-weight: 600;
  }
  .chapter-item.current-spot {
    background: var(--accent-soft);
    border-color: var(--accent-border);
    color: var(--text);
  }
  .chapter-item-left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .chapter-num {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-secondary);
    min-width: 24px;
    font-variant-numeric: tabular-nums;
  }
  .chapter-title-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .spot-dot {
    width: 7px;
    height: 7px;
    border-radius: 999px;
    background: var(--accent);
    flex-shrink: 0;
  }
  .chapter-badges {
    display: inline-flex;
    gap: 6px;
    font-size: 12px;
    color: var(--text-secondary);
    font-family: var(--font-mono);
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
  }
  .walk-reader {
    overflow-y: auto;
    padding: 0 36px 36px;
    scroll-behavior: smooth;
  }
  .chapter-nav {
    position: sticky;
    top: 0;
    z-index: 20;
    background: var(--bar);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 0;
    margin-bottom: 20px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }

  /* Notes Tracker Bar & Editor */
  .notes-toolbar {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 16px;
    margin-bottom: 24px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .notes-toolbar-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  .quick-note-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .quick-note-input {
    flex: 1;
    min-width: 220px;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 7px 12px;
    font-size: 13px;
  }
  .notes-editor-area {
    width: 100%;
    min-height: 360px;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 14px;
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 1.55;
    resize: vertical;
  }

  /* Modal Dialog */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(10, 12, 16, 0.82);
    backdrop-filter: blur(4px);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 950;
    padding: 20px;
  }
  .modal-backdrop.open { display: flex; }
  .modal-card {
    width: 100%;
    max-width: 560px;
    background: var(--surface-1);
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.65);
    overflow: hidden;
  }
  .modal-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .modal-header h2 {
    margin: 0;
    font-size: 17px;
  }
  .modal-body {
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    max-height: 78vh;
    overflow-y: auto;
  }
  .form-field {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .form-field label {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
  }
  .form-input, .form-select, .form-textarea {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 8px 12px;
    font-size: 14px;
  }
  .form-row-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  .preset-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 3px;
  }
  .preset-chip {
    font-size: 12px;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--text-secondary);
    cursor: pointer;
  }
  .preset-chip:hover { color: var(--text); border-color: var(--accent); }
  .modal-footer {
    padding: 14px 20px;
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  /* Editorial Walkthrough Typography & Level Blocks */
  .md-content p,
  .md-content ul,
  .md-content ol,
  .md-content blockquote {
    max-width: 58ch;
  }
  .md-content p {
    margin: 0 0 14px;
  }
  .md-content ul, .md-content ol {
    margin: 0 0 16px;
    padding-left: 22px;
  }
  .md-content li {
    margin-bottom: 6px;
  }
  .md-content h1, .md-content h2, .md-content h3 {
    line-height: 1.28;
    letter-spacing: -0.015em;
    color: var(--text);
  }
  .md-content h1 {
    font-size: 23px;
    margin: 28px 0 12px;
  }
  .md-content h2 {
    font-size: 20px;
    margin: 32px 0 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .md-content h3 {
    font-size: 16px;
    margin: 26px 0 8px;
    color: var(--accent);
  }
  .md-content > h1:first-child,
  .md-content > h2:first-child,
  .md-content > h3:first-child {
    margin-top: 0;
  }
  .level-heading {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    font-family: var(--font-mono);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 4px 10px;
    border-radius: 4px;
    margin: 24px 0 10px;
    font-weight: 700;
  }
  .level-hint { background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent-border); }
  .level-strategy { background: var(--blue-soft); color: var(--blue); border: 1px solid rgba(92, 149, 214, 0.35); }
  .level-answer { background: var(--green-soft); color: var(--green); border: 1px solid var(--green-border); }
  .redacted-box {
    max-width: 70ch;
    padding: 12px 16px;
    border-radius: var(--radius-sm);
    border: 1px dashed var(--border-strong);
    background: var(--surface-1);
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin: 10px 0 18px;
  }
  .md-content pre {
    max-width: 780px;
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 12px 14px;
    overflow-x: auto;
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 1.45;
  }
  .md-content code {
    font-family: var(--font-mono);
    background: var(--surface-2);
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 0.92em;
  }
  .md-content pre code { background: none; padding: 0; }
  .md-content a { color: var(--blue); }
  .md-content blockquote {
    margin: 14px 0;
    padding: 10px 16px;
    border-left: 2px solid var(--accent);
    background: rgba(189, 155, 255, 0.06);
    border-radius: 0;
    color: var(--text-secondary);
  }
  .md-content table {
    border-collapse: collapse;
    width: 100%;
    max-width: 780px;
    margin: 16px 0;
    font-size: 14px;
    font-variant-numeric: tabular-nums;
  }
  .md-content th, .md-content td {
    border: 1px solid var(--border);
    padding: 8px 12px;
    text-align: left;
    vertical-align: top;
  }
  .md-content th {
    background: var(--surface-2);
    font-weight: 600;
    color: var(--text);
  }

  /* Inline Walkthrough Figures & Video Cards */
  .guide-figure {
    margin: 18px 0;
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    max-width: 760px;
  }
  .guide-figure img {
    display: block;
    width: 100%;
    max-height: 460px;
    object-fit: contain;
    background: var(--bg);
    cursor: zoom-in;
  }
  .guide-figure figcaption {
    padding: 8px 12px;
    font-size: 12px;
    color: var(--text-secondary);
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .video-card {
    margin: 16px 0;
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    max-width: 760px;
  }
  .video-card-header {
    display: flex;
    gap: 16px;
    padding: 14px;
    align-items: center;
  }
  .video-thumb-wrap {
    position: relative;
    width: 216px;
    height: 122px;
    flex-shrink: 0;
    border-radius: var(--radius-sm);
    overflow: hidden;
    background: var(--bg);
    cursor: pointer;
  }
  .video-thumb-wrap img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform 0.2s ease;
  }
  .video-thumb-wrap:hover img {
    transform: scale(1.03);
  }
  .video-play-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(14, 16, 20, 0.28);
    transition: background 0.15s;
  }
  .video-thumb-wrap:hover .video-play-overlay {
    background: rgba(14, 16, 20, 0.48);
  }
  .video-play-circle {
    width: 40px;
    height: 40px;
    border-radius: 999px;
    background: var(--accent);
    color: var(--on-accent);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 15px;
    font-weight: 800;
  }
  .video-time-pill {
    position: absolute;
    bottom: 6px;
    right: 6px;
    background: var(--bar);
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
    font-variant-numeric: tabular-nums;
  }
  .video-card-info {
    flex: 1;
    min-width: 0;
  }
  .video-card-title {
    font-weight: 600;
    font-size: 15px;
    margin-bottom: 4px;
    color: var(--text);
  }
  .video-card-sub {
    font-size: 12px;
    color: var(--text-secondary);
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    margin-bottom: 10px;
  }
  .video-card-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .video-player-slot {
    border-top: 1px solid var(--border);
    background: var(--bg);
    display: none;
  }
  .video-player-slot.open { display: block; }
  .video-player-slot video {
    width: 100%;
    max-height: 440px;
    display: block;
  }
  .frames-strip {
    display: none;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
    padding: 12px 14px;
    border-top: 1px solid var(--border);
    background: var(--bg);
  }
  .frames-strip.open { display: grid; }
  .frame-tile {
    overflow: hidden;
  }
  .frame-tile img {
    width: 100%;
    height: 128px;
    object-fit: cover;
    display: block;
    border-radius: 4px;
    cursor: zoom-in;
  }
  .frame-tile span {
    display: block;
    padding: 4px 4px 0;
    font-size: 12px;
    font-family: var(--font-mono);
    color: var(--text-secondary);
    text-align: center;
    font-variant-numeric: tabular-nums;
  }

  /* Chapter Media Strip (Unbordered container so thumb cards are never nested cards) */
  .chapter-media-strip {
    margin: 0 0 24px;
    padding: 0 0 18px;
    border-bottom: 1px solid var(--border);
  }
  .chapter-media-strip-title {
    font-size: 12px;
    font-family: var(--font-mono);
    color: var(--text-secondary);
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .chapter-media-row {
    display: flex;
    gap: 12px;
    overflow-x: auto;
    padding: 4px 12px 8px;
  }
  .chapter-thumb-card {
    flex: 0 0 204px;
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    overflow: hidden;
    cursor: pointer;
    transition: border-color 0.15s, transform 0.15s;
  }
  .chapter-thumb-card:hover {
    border-color: var(--accent);
    transform: translateY(-1px);
  }
  .chapter-thumb-img-wrap {
    position: relative;
    width: 100%;
    height: 114px;
    background: var(--bg);
  }
  .chapter-thumb-img-wrap img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .chapter-thumb-caption {
    padding: 6px 9px;
    font-size: 12px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Media Gallery Grid */
  .media-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 16px;
    margin-top: 12px;
  }

  .maps-body { max-width:1200px; }
  .maps-intro { color:var(--text-secondary); max-width:70ch; }
  .maps-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:16px; }
  .map-card { background:var(--surface-1); border:1px solid var(--border); border-radius:10px; overflow:hidden; }
  .map-image-button { display:block; width:100%; padding:0; border:0; background:var(--surface-2); cursor:zoom-in; }
  .map-image-button img { display:block; width:100%; height:180px; object-fit:contain; }
  .map-details { display:flex; flex-direction:column; align-items:flex-start; gap:6px; padding:12px; }
  .map-details span { color:var(--text-secondary); font-size:13px; }

  /* Bottom Ask Bar */
  .ask-bar {
    grid-column: 2;
    background: var(--surface-1);
    border-top: 1px solid var(--border);
    padding: 10px 24px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .ask-row {
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .ask-input {
    flex: 1;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 8px 14px;
    font-size: 14px;
  }
  .status-line {
    font-size: 12px;
    font-family: var(--font-mono);
    color: var(--text-secondary);
    min-height: 16px;
  }

  /* Toast Notification */
  .toast {
    position: fixed;
    bottom: 68px;
    right: 24px;
    background: var(--surface-3);
    color: var(--text);
    border: 1px solid var(--accent-border);
    border-radius: var(--radius-sm);
    padding: 8px 14px;
    font-size: 13px;
    font-weight: 500;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
    z-index: 980;
    opacity: 0;
    transform: translateY(8px);
    pointer-events: none;
    transition: opacity 0.18s ease, transform 0.18s ease;
  }
  .toast.visible {
    opacity: 1;
    transform: translateY(0);
  }

  /* Lightbox Gallery */
  .lightbox {
    position: fixed;
    inset: 0;
    background: rgba(10, 12, 16, 0.92);
    backdrop-filter: blur(6px);
    display: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 24px;
  }
  .lightbox.open { display: flex; }
  .lightbox-stage {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    max-width: 94vw;
    max-height: 82vh;
  }
  .lightbox img {
    max-width: 88vw;
    max-height: 80vh;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-strong);
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.7);
  }
  .lightbox-stage.zoomed { width:94vw; height:75vh; overflow:auto; display:block; }
  .lightbox-stage.zoomed img { display:block; max-width:none; max-height:none; width:200%; height:auto; }
  @media (max-width: 900px) {
    .app-shell { grid-template-columns: 230px minmax(0,1fr); }
    .walk-layout { grid-template-columns: 200px minmax(0,1fr); }
    .topbar-primary, .scroll-body, .ask-bar { padding-left:16px; padding-right:16px; }
    .shortcuts-bar { display:none; }
  }
  @media (max-width: 640px) {
    html, body { overflow:auto; }
    .app-shell { display:flex; flex-direction:column; height:auto; min-height:100vh; }
    .sidebar { max-height:300px; border-right:0; border-bottom:1px solid var(--border); }
    .games-list { min-height:100px; }
    .main-pane { min-height:65vh; overflow:visible; }
    .walk-layout { grid-template-columns:1fr; height:auto; overflow:visible; }
    .walk-sidebar { max-height:220px; border-right:0; }
    .walk-reader, .scroll-body { overflow:visible; }
    .tabs-bar { overflow-x:auto; }
    .tabs-group { flex-shrink:0; }
    .ask-bar { position:sticky; bottom:0; }
    .ask-row { flex-wrap:wrap; }
    .ask-input { min-width:160px; }
    .lightbox-bar { flex-wrap:wrap; justify-content:center; }
  }
  .lightbox-bar {
    margin-top: 14px;
    display: flex;
    align-items: center;
    gap: 14px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 6px 14px;
    font-size: 13px;
    color: var(--text);
  }
  .lightbox-counter {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  /* Cover-led library */
  .shelf-nav { margin: 10px 12px 0; padding: 9px 11px; text-align:left; color:var(--text); background:var(--surface-2); border:1px solid var(--border); border-radius:var(--radius-sm); cursor:pointer; font-weight:600; }
  .shelf-view { display:none; }
  .library-mode .shelf-view { display:block; flex:1; overflow:auto; padding:36px clamp(20px, 4vw, 56px) 64px; }
  .library-mode .topbar, .library-mode .tabs-bar, .library-mode .tab-view, .library-mode .ask-bar { display:none; }
  .library-mode .shelf-nav { color:var(--accent); border-color:var(--accent-border); }
  .shelf-heading { display:flex; justify-content:space-between; align-items:end; gap:20px; flex-wrap:wrap; padding-bottom:22px; border-bottom:1px solid var(--border); }
  .shelf-heading h1 { margin:0 0 6px; font-size:clamp(28px, 3vw, 38px); letter-spacing:-.03em; }
  .shelf-heading p, .shelf-empty, .shelf-help { color:var(--text-secondary); font-size:13px; margin:0; }
  .shelf-controls { display:flex; align-items:center; gap:9px; font-size:13px; color:var(--text-secondary); }
  .shelf-controls select { background:var(--surface-2); color:var(--text); border:1px solid var(--border-strong); border-radius:var(--radius-sm); padding:9px 32px 9px 10px; max-width:220px; }
  .shelf-section { margin-top:32px; }
  .shelf-section h2 { margin:0 0 16px; font-size:18px; letter-spacing:-.01em; }
  .shelf-section h2 span { color:var(--text-secondary); font-size:13px; font-weight:400; margin-left:8px; }
  .shelf-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(130px, 1fr)); gap:22px 18px; padding:0 0 20px; border-bottom:3px solid var(--border-strong); }
  .shelf-game { min-width:0; padding:0; border:0; background:none; text-align:left; color:var(--text); cursor:pointer; font:inherit; }
  .shelf-cover { aspect-ratio:3 / 4; width:100%; display:flex; align-items:center; justify-content:center; overflow:hidden; border-radius:8px; background:var(--surface-2); border:1px solid var(--border); color:var(--text-secondary); font-size:36px; font-weight:700; box-shadow:0 6px 14px rgba(0,0,0,.18); }
  .shelf-cover img { width:100%; height:100%; object-fit:cover; }
  .shelf-game:hover .shelf-cover { border-color:var(--accent); }
  .shelf-game:focus-visible, .shelf-nav:focus-visible, .shelf-controls select:focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
  .shelf-title { display:block; font-size:14px; font-weight:650; line-height:1.35; margin-top:10px; overflow-wrap:anywhere; }
  .shelf-meta { display:flex; flex-wrap:wrap; gap:5px 9px; margin-top:5px; font-size:12px; color:var(--text-secondary); }
  .score-label { font-size:12px; color:var(--text-secondary); }
  .shelf-empty { padding:36px 0; line-height:1.6; }
  .shelf-empty h2 { margin:0 0 6px; color:var(--text); font-size:18px; }
  .shelf-empty p { margin:0 0 16px; }
  .modal-status { margin:0; font-size:13px; color:var(--text-secondary); }
  .modal-status.error { color:var(--danger); }
  .modal-status:empty { display:none; }
  ::selection { background:var(--accent-soft); color:var(--text); }
  .shelf-help { margin-top:26px; line-height:1.6; }
  @media (max-width:640px) {
    .library-mode .sidebar { max-height:none; min-width:0; }
    .app-shell, .main-pane, .sidebar { min-width:0; }
    .sidebar-actions { flex-wrap:wrap; }
    .shelf-heading { display:block; }
    .shelf-controls { margin-top:16px; }
    .library-mode .games-list { display:none; }
    .library-mode .shelf-view { padding:24px 18px 48px; overflow:visible; }
    .shelf-grid { grid-template-columns:repeat(auto-fill,minmax(112px,1fr)); gap:18px 12px; }
    .shelf-heading { align-items:start; }
  }
</style>
</head>
<body>
<div class="app-shell library-mode">
  <aside class="sidebar">
    <div class="brand-bar">
      <div class="brand-title">
        <span class="brand-mark" aria-hidden="true">
          <svg class="icon" viewBox="0 0 24 24"><path d="M6 12h4m-2-2v4m7-1h.01M18 11h.01M4 7h16a2 2 0 0 1 2 2v7a3 3 0 0 1-3 3h-2l-2-2H9l-2 2H5a3 3 0 0 1-3-3V9a2 2 0 0 1 2-2Z"/></svg>
        </span>
        <span>GameGuider</span>
      </div>
    </div>
    <div class="sidebar-actions">
      <button class="btn btn-primary btn-sm" id="track-game-btn" style="flex:1" onclick="openGameModal(false)">
        <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
        <span>Add game</span>
      </button>
      <button class="btn btn-sm" id="backlog-list-btn" onclick="openBacklogModal()" title="Add several games at once">Add a list</button>
      <button class="btn btn-sm" id="reload-btn" title="Reload library" aria-label="Reload library">
        <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/></svg>
      </button>
    </div>
    <div class="update-panel">
      <button type="button" class="btn btn-sm" id="open-setup" onclick="openSetupModal()">Setup</button>
      <button type="button" class="btn btn-sm" id="check-updates" onclick="checkForUpdates()">Check for updates</button>
      <div id="update-result" class="update-result" role="status" aria-live="polite"></div>
    </div>
    <button type="button" class="shelf-nav" onclick="showShelf()">&#8592; Your shelf</button>
    <div class="library-search"><label class="visually-hidden" for="library-search">Search your games</label><input type="search" id="library-search" placeholder="Search your games and guides"></div>
    <div class="status-filters" id="status-filters"></div>
    <div class="games-list" id="games-list"></div>
  </aside>

  <main class="main-pane">
    <section class="shelf-view" id="shelf-view" aria-labelledby="shelf-title">
      <div class="shelf-heading"><div><h1 id="shelf-title">Your games</h1><p id="shelf-summary">Your collection, ready when you are.</p></div>
        <div class="shelf-controls"><label for="console-filter">Console</label><select id="console-filter" aria-label="Filter by console"><option value="all">All consoles</option></select></div>
      </div>
      <div class="setup-banner" id="setup-banner" role="status" hidden></div>
      <div id="shelf-content"></div>
      <div class="guide-hits" id="guide-hits" aria-live="polite"></div>
    </section>
    <header class="topbar">
      <div class="topbar-primary">
        <div class="game-header-info" id="game-header-info">
          <h1 class="game-header-title">Select or add a game</h1>
        </div>
        <div class="controls-group">
          <div class="control-cluster">
            <span class="control-label">Spoiler</span>
            <div class="segmented" id="spoiler-control" title="Spoiler Level (Keys 1, 2, 3)">
              <button data-spoiler="hint" class="active">Hint <span class="kbd-hint">1</span></button>
              <button data-spoiler="strategy">Strategy <span class="kbd-hint">2</span></button>
              <button data-spoiler="answer">Answer <span class="kbd-hint">3</span></button>
            </div>
          </div>
          <div class="control-cluster">
            <span class="control-label">Agent</span>
            <div class="segmented" id="backend-control" title="AI Backend">
              <button data-backend="claude" class="active">Claude</button>
              <button data-backend="codex">Codex</button>
            </div>
          </div>
          <button class="btn btn-primary btn-sm" id="build-walkthrough-btn">Build walkthrough</button>
        </div>
      </div>
      <details class="share-controls" id="share-controls" ontoggle="prepareShare()">
        <summary class="btn btn-sm">Share guide</summary>
        <form class="share-form" onsubmit="downloadShare(event)">
          <label for="share-spoiler">Spoiler level
            <select id="share-spoiler" class="status-select"><option value="hint">Hint</option><option value="strategy">Strategy</option><option value="answer">Answer</option></select>
          </label>
          <label><input type="checkbox" id="share-notes"> Include personal notes</label>
          <button type="submit" class="btn btn-primary btn-sm" id="share-download">Download guide</button>
          <p class="share-help">Creates an HTML file with saved images and video links. Personal notes are optional. Unlabelled content may contain spoilers.</p>
          <span id="share-error" class="share-error" role="alert"></span>
        </form>
      </details>
      <div class="topbar-spot-bar" id="topbar-spot-bar" style="display:none"></div>
    </header>

    <nav class="tabs-bar">
      <div class="tabs-group">
        <button class="tab-btn active" data-tab="walk-tab">
          <span>Walkthrough</span>
          <span class="tab-count" id="walk-count">0</span>
        </button>
        <button class="tab-btn" data-tab="notes-tab">
          <span>Notes</span>
        </button>
        <button class="tab-btn" data-tab="answer-tab">
          <span>Answers</span>
        </button>
        <button class="tab-btn" data-tab="maps-tab">
          <span>Maps</span>
          <span class="tab-count" id="maps-count">0</span>
        </button>
        <button class="tab-btn" data-tab="media-tab">
          <span>Media</span>
          <span class="tab-count" id="media-count">0</span>
        </button>
      </div>
      <div class="shortcuts-bar" title="Keyboard shortcuts">
        <span>[ / ] Chapter</span>
        <span>·</span>
        <span>s Set Spot</span>
        <span>·</span>
        <span>/ Filter</span>
      </div>
    </nav>

    <section class="tab-view active" id="walk-tab">
      <div class="walk-layout">
        <div class="walk-sidebar">
          <div class="walk-sidebar-top">
            <label class="visually-hidden" for="chapter-filter">Search chapters</label>
            <input type="search" class="walk-filter" id="chapter-filter" placeholder="Search chapters">
            <div id="jump-current-spot-slot"></div>
          </div>
          <div class="chapters-list" id="chapters-list"></div>
        </div>
        <div class="walk-reader" id="walk-reader">
          <div id="walk-chapter-content" class="md-content"></div>
        </div>
      </div>
    </section>

    <section class="tab-view" id="notes-tab">
      <div class="scroll-body" id="notes-tab-body">
        <div id="notes-tracker-controls"></div>
        <div class="md-content" id="notes-content"></div>
      </div>
    </section>

    <section class="tab-view" id="answer-tab">
      <div class="scroll-body md-content" id="answer-content">
        <p style="color:var(--text-secondary)">Ask a question below or build a walkthrough for this game.</p>
      </div>
    </section>

    <section class="tab-view" id="maps-tab">
      <div class="scroll-body maps-body"><div id="maps-content"></div></div>
    </section>

    <section class="tab-view" id="media-tab">
      <div class="scroll-body" style="max-width:1200px">
        <div id="media-content"></div>
      </div>
    </section>
  </main>

  <footer class="ask-bar">
    <form class="ask-row" id="ask-form">
      <label class="visually-hidden" for="question-input">Ask about this game</label>
      <input type="text" class="ask-input" id="question-input" placeholder="Where are you stuck?" autocomplete="off">
      <button type="submit" class="btn btn-primary">Ask Guide</button>
      <button type="button" class="btn" id="new-chat-btn" title="Reset conversation context">New Chat</button>
    </form>
    <div class="status-line" id="status-line" role="status" aria-live="polite"></div>
  </footer>
</div>

<!-- Add / Edit Tracked Game Modal -->
<div class="modal-backdrop" id="game-modal">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="game-modal-title" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h2 id="game-modal-title">Track a Game</h2>
      <button type="button" class="btn btn-sm" onclick="closeGameModal()" aria-label="Close modal">Close</button>
    </div>
    <form id="game-modal-form" onsubmit="submitGameModal(event)">
      <div class="modal-body">
        <input type="hidden" id="modal-game-slug" value="">
        <div class="form-field">
          <label for="modal-game-name">Game Title *</label>
          <input type="text" id="modal-game-name" class="form-input" placeholder="e.g. Fire Emblem: Fortune's Weave" required>
        </div>
        <div class="form-row-2">
          <div class="form-field">
            <label for="modal-game-platform">Platform</label>
            <input type="text" id="modal-game-platform" class="form-input" placeholder="e.g. Switch 2, PC, PS5">
            <div class="preset-chips">
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-platform','Switch 2')">Switch 2</button>
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-platform','Switch')">Switch</button>
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-platform','PC / Steam')">PC / Steam</button>
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-platform','PS5')">PS5</button>
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-platform','Steam Deck')">Deck</button>
            </div>
          </div>
          <div class="form-field">
            <label for="modal-game-genre">Genre</label>
            <input type="text" id="modal-game-genre" class="form-input" placeholder="e.g. Tactical RPG, Metroidvania">
            <div class="preset-chips">
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-genre','Tactical RPG')">Tactical RPG</button>
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-genre','RPG')">RPG</button>
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-genre','Metroidvania')">Metroidvania</button>
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-genre','Action')">Action</button>
              <button type="button" class="preset-chip" onclick="setModalField('modal-game-genre','Puzzle')">Puzzle</button>
            </div>
          </div>
        </div>
        <div class="form-row-2">
          <div class="form-field">
            <label for="modal-game-status">Status</label>
            <select id="modal-game-status" class="form-select">
              <option value="Playing">Playing</option>
              <option value="Backlog">Backlog</option>
              <option value="Paused">Paused</option>
              <option value="Beaten">Beaten</option>
              <option value="Dropped">Dropped</option>
            </select>
          </div>
          <div class="form-field">
            <label for="modal-game-spot">Current Spot / Chapter / Boss</label>
            <input type="text" id="modal-game-spot" class="form-input" placeholder="e.g. Cai 5: Cai's Battle">
          </div>
        </div>
        <div class="form-field" id="modal-notes-field-wrap">
          <label for="modal-game-notes">Starting Notes / Build / Goals (optional)</label>
          <textarea id="modal-game-notes" class="form-textarea" rows="3" placeholder="e.g. Playing on Maddening mode, focusing on Blaze Arts & Spear units..."></textarea>
        </div>
        <label id="modal-auto-walk-wrap" style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer">
          <input type="checkbox" id="modal-auto-walkthrough">
          <span>Automatically build full chapter-by-chapter walkthrough after saving</span>
        </label>
        <p id="game-modal-status" class="modal-status" role="status" aria-live="polite"></p>
      </div>
      <div class="modal-footer">
        <div>
          <button type="button" class="btn btn-danger btn-sm" id="modal-delete-btn" style="display:none" onclick="deleteCurrentGame()">Untrack Game</button>
        </div>
        <div style="display:flex;gap:8px">
          <button type="button" class="btn" onclick="closeGameModal()">Cancel</button>
          <button type="submit" class="btn btn-primary" id="modal-save-btn">Save Game</button>
        </div>
      </div>
    </form>
  </div>
</div>

<!-- Setup Modal -->
<div class="modal-backdrop" id="setup-modal">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="setup-modal-title" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h2 id="setup-modal-title">Setup</h2>
      <button type="button" class="btn btn-sm" onclick="closeSetupModal()" aria-label="Close modal">Close</button>
    </div>
    <form id="setup-form" onsubmit="saveNotesFolder(event)">
      <div class="modal-body">
        <section class="ai-connect" id="ai-connect" aria-live="polite"></section>
        <ul class="setup-list" id="setup-list"></ul>
        <div class="form-field">
          <label for="theme-select">Theme</label>
          <select id="theme-select" class="form-input" onchange="setTheme(this.value)"><!--__THEME_OPTIONS__--></select>
        </div>
        <div class="form-field">
          <label for="setup-notes-dir">Notes folder</label>
          <input type="text" id="setup-notes-dir" class="form-input" spellcheck="false" autocomplete="off" placeholder="~/Documents/GameGuider">
          <span style="font-size:12px;color:var(--text-secondary)">Your games, notes, walkthroughs and saved images live here. A folder in your Obsidian vault works too.</span>
        </div>
        <p id="setup-modal-status" class="modal-status" role="status" aria-live="polite"></p>
      </div>
      <div class="modal-footer">
        <span style="font-size:12px;color:var(--text-secondary)">The same checks run with <code>game-guide setup</code> in Terminal.</span>
        <div style="display:flex;gap:8px">
          <button type="button" class="btn" onclick="closeSetupModal()">Close</button>
          <button type="submit" class="btn btn-primary">Save folder</button>
        </div>
      </div>
    </form>
  </div>
</div>

<!-- Bulk Backlog List Modal -->
<div class="modal-backdrop" id="backlog-modal">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="backlog-modal-title" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h2 id="backlog-modal-title">Add a list of games</h2>
      <button type="button" class="btn btn-sm" onclick="closeBacklogModal()" aria-label="Close modal">Close</button>
    </div>
    <form id="backlog-modal-form" onsubmit="submitBacklogModal(event)">
      <div class="modal-body">
        <div class="form-field">
          <label for="backlog-games-input">Games (one per line)</label>
          <textarea id="backlog-games-input" class="form-textarea" rows="8" required placeholder="Celeste&#10;Hades II | PC | Roguelike&#10;3. Hollow Knight: Silksong | Switch 2 | Metroidvania"></textarea>
          <span style="font-size:12px;color:var(--text-secondary)">Paste plain titles, bullets, or numbered lists. Optional format: Game | Platform | Genre | Notes.</span>
        </div>
        <div class="form-row-2">
          <div class="form-field">
            <label for="backlog-default-platform">Default platform</label>
            <input type="text" id="backlog-default-platform" class="form-input" placeholder="e.g. Switch 2 or PC">
          </div>
          <div class="form-field">
            <label for="backlog-default-genre">Default genre</label>
            <input type="text" id="backlog-default-genre" class="form-input" placeholder="e.g. RPG">
          </div>
        </div>
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer">
          <input type="checkbox" id="backlog-fetch-artwork">
          <span>Fetch cover artwork now (slower for long lists)</span>
        </label>
        <p id="backlog-modal-status" class="modal-status" role="status" aria-live="polite"></p>
      </div>
      <div class="modal-footer">
        <span style="font-size:12px;color:var(--text-secondary)">You can reorder the queue with the arrows on each card.</span>
        <div style="display:flex;gap:8px">
          <button type="button" class="btn" onclick="closeBacklogModal()">Cancel</button>
          <button type="submit" class="btn btn-primary">Add to Backlog</button>
        </div>
      </div>
    </form>
  </div>
</div>

<!-- Lightbox Gallery -->
<div class="lightbox" id="lightbox" onclick="closeLightbox()">
  <div class="lightbox-stage" id="lightbox-stage" onclick="event.stopPropagation()"></div>
  <div class="lightbox-bar" onclick="event.stopPropagation()">
    <button type="button" class="btn btn-sm" id="lightbox-prev" onclick="stepLightbox(-1)">Prev</button>
    <span id="lightbox-caption">Map / Screenshot</span>
    <span class="lightbox-counter" id="lightbox-counter">1 / 1</span>
    <button type="button" class="btn btn-sm" id="lightbox-next" onclick="stepLightbox(1)">Next</button>
    <button type="button" class="btn btn-sm" id="lightbox-zoom" onclick="toggleLightboxZoom()" aria-pressed="false">Zoom in</button>
    <button type="button" class="btn btn-sm" onclick="closeLightbox()">Close (Esc)</button>
  </div>
</div>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const state = {
  games: [],
  selected: null,
  spoiler: "hint",
  backend: "claude",
  statusFilter: "all",
  consoleFilter: "all",
  librarySearch: "",
  guideHits: null,
  aiChoice: null,  // the AI picked in Setup's walkthrough; null shows the connected one  // /api/search results for librarySearch, shown under the shelf
  maps: [],
  walkthrough: [],
  currentChapter: null,
  selectedChapterIdx: 0,
  chapterFilter: "",
  notesHtml: "",
  rawNotes: "",
  editingNotes: false,
  media: [],
  lightboxItems: [],
  lightboxIdx: 0,
};

let toastTimer = null;
function showToast(msg) {
  const el = document.getElementById("toast");
  if (!el || !msg) return;
  el.textContent = msg;
  el.classList.add("visible");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("visible"), 2600);
}

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Click targets carry their call as data (run by the ACTIONS listener), never as inline JS: entities are decoded before an inline handler runs, so escaping cannot keep guide text out of it.
function act(name, ...args) {
  return `data-action="${name}" data-args="${esc(JSON.stringify(args))}"`;
}

function fmtTime(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  if (hrs > 0) return `${hrs}:${String(mins).padStart(2, "0")}:${String(rem).padStart(2, "0")}`;
  return `${mins}:${String(rem).padStart(2, "0")}`;
}

function statusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "playing") return "playing";
  if (s === "backlog") return "backlog";
  if (s === "paused") return "paused";
  if (s === "beaten") return "beaten";
  if (s === "dropped") return "dropped";
  return "";
}

function thumbUrlFor(id, at) {
  const sec = Math.max(0, Math.floor(Number(at) || 0));
  return sec > 0
    ? `/api/thumbnail/${encodeURIComponent(id)}?at=${sec}`
    : `/api/thumbnail/${encodeURIComponent(id)}`;
}

function videoCardHtml(id, at, label) {
  const cardId = `vid-${id}-${at}-${Math.random().toString(36).slice(2, 7)}`;
  const timeStr = fmtTime(at);
  const thumbSrc = thumbUrlFor(id, at);
  return `
    <div class="video-card" id="${cardId}" data-vid="${esc(id)}" data-at="${Number(at) || 0}">
      <div class="video-card-header">
        <div class="video-thumb-wrap" ${act("playInlineVideo", cardId)} title="Play from ${timeStr}">
          <img src="${thumbSrc}" alt="${esc(label)}" loading="lazy">
          <div class="video-play-overlay"><div class="video-play-circle">▶</div></div>
          <span class="video-time-pill">${timeStr}</span>
        </div>
        <div class="video-card-info">
          <div class="video-card-title">${esc(label || id)}</div>
          <div class="video-card-sub">
            <span>YouTube · starts at ${timeStr}</span>
          </div>
          <div class="video-card-actions">
            <button type="button" class="btn btn-primary btn-sm" ${act("playInlineVideo", cardId)}>
              Play (${timeStr})
            </button>
            <button type="button" class="btn btn-sm" ${act("loadStillFrames", cardId)}>
              Show still frames
            </button>
            <button type="button" class="btn btn-sm" ${act("playMpv", id, Number(at) || 0, String(label ?? ""))}>
              Open in external player
            </button>
            <a class="btn btn-sm" href="https://www.youtube.com/watch?v=${encodeURIComponent(id)}&amp;t=${Number(at) || 0}s" target="_blank" rel="noopener noreferrer">Watch on YouTube</a>
          </div>
        </div>
      </div>
      <div class="video-player-slot" id="${cardId}-player"></div>
      <div class="frames-strip" id="${cardId}-frames"></div>
    </div>
  `;
}

function figureHtml(alt, src) {
  const safeAlt = esc(alt || "Walkthrough Map / Screenshot");
  return `
    <figure class="guide-figure">
      <img src="${esc(src)}" alt="${safeAlt}" loading="lazy" ${act("openLightbox", src, alt || "Walkthrough Map / Screenshot")}>
      <figcaption>
        <span>${safeAlt}</span>
        <span style="font-family:var(--font-mono);font-size:12px">Click to inspect</span>
      </figcaption>
    </figure>
  `;
}

// Guide Markdown arrives rendered by the server (render.py); this only attaches behaviour to its data-* placeholders.
function hydrate(root) {
  root.querySelectorAll("[data-video]").forEach(el => { el.outerHTML = videoCardHtml(el.dataset.video, Number(el.dataset.at) || 0, el.dataset.label); });
  root.querySelectorAll(".guide-figure img").forEach(img => {
    img.dataset.action = "openLightbox";
    img.dataset.args = JSON.stringify([img.getAttribute("src"), img.alt]);
  });
  root.querySelectorAll("[data-reveal]").forEach(el => {
    const level = el.dataset.reveal;
    el.insertAdjacentHTML("beforeend", `<button type="button" class="btn btn-sm" ${act("setSpoiler", level)}>Reveal ${esc(level.charAt(0).toUpperCase() + level.slice(1))}</button>`);
  });
  return root;
}
window.hydrate = hydrate;

// Fixed and error messages go in as DOM text, never as HTML.
function showMessage(el, title, detail) {
  const p = document.createElement("p"), strong = document.createElement("strong");
  strong.textContent = title;
  p.append(strong, ` ${detail ?? ""}`);
  el.replaceChildren(p);
}

window.playInlineVideo = function(cardId) {
  const card = document.getElementById(cardId);
  if (!card) return;
  const vid = card.dataset.vid;
  const at = Number(card.dataset.at || 0);
  const slot = document.getElementById(`${cardId}-player`);
  if (!slot) return;
  slot.classList.add("open");
  setStatus(`Loading video at ${fmtTime(at)}…`);
  slot.innerHTML = `<video controls autoplay preload="auto" src="/api/video/${encodeURIComponent(vid)}#t=${at}"></video>`;
  const videoEl = slot.querySelector("video");
  if (videoEl) {
    videoEl.addEventListener("loadedmetadata", () => {
      if (at > 0 && Math.abs(videoEl.currentTime - at) > 1) {
        videoEl.currentTime = at;
      }
      setStatus("");
    }, { once: true });
    videoEl.addEventListener("error", () => {
      setStatus("Could not play this video here. You can watch it on YouTube instead.");
    });
  }
};

window.loadStillFrames = async function(cardId) {
  const card = document.getElementById(cardId);
  if (!card) return;
  const vid = card.dataset.vid;
  const at = Number(card.dataset.at || 0);
  const strip = document.getElementById(`${cardId}-frames`);
  if (!strip) return;
  strip.classList.add("open");
  strip.innerHTML = `<div style="grid-column:1/-1;color:var(--text-secondary);font-size:13px">Extracting still frames around ${fmtTime(at)}…</div>`;
  setStatus(`Grabbing frames around ${fmtTime(at)}…`);
  try {
    const resp = await fetch(`/api/frames/${encodeURIComponent(vid)}?at=${at}`);
    const data = await resp.json();
    if (!resp.ok || data.error) {
      strip.innerHTML = `<div style="grid-column:1/-1;color:var(--danger);font-size:13px">Could not load still frames. You can watch the video on YouTube instead.</div>`;
      setStatus("Could not load still frames.");
      return;
    }
    strip.innerHTML = (data.frames || []).map(f => `
      <div class="frame-tile">
        <img src="${esc(f.url)}" alt="Frame at ${fmtTime(f.time)}" ${act("openLightbox", f.url, `Frame at ${fmtTime(f.time)}`)}>
        <span>${fmtTime(f.time)}</span>
      </div>
    `).join("");
    setStatus("");
  } catch (err) {
    strip.innerHTML = `<div style="grid-column:1/-1;color:var(--danger)">Could not load still frames. Please try again.</div>`;
    setStatus("");
  }
};

window.playMpv = async function(vid, at, label) {
  setStatus(`Opening ${label} in an external player…`);
  try {
    const resp = await fetch("/api/play", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: vid, at, label }),
    });
    const data = await resp.json();
    setStatus(resp.ok ? "" : "Could not open the external player. You can watch the video on YouTube instead.");
  } catch (err) {
    setStatus("Could not open the external player. You can watch the video on YouTube instead.");
  }
};

function collectVisibleGalleryItems() {
  const ch = state.walkthrough[state.selectedChapterIdx];
  const chImgs = ch && Array.isArray(ch.images)
    ? ch.images.filter(i => i.exists !== false).map(i => ({ src: i.src, label: i.label || "Map / Screenshot" }))
    : [];
  if (chImgs.length) return chImgs;
  return state.media.filter(m => m.type === "image").map(i => ({ src: i.src, label: i.label || "Map / Screenshot" }));
}

function updateLightboxView() {
  document.getElementById("lightbox-stage").classList.remove("zoomed");
  const zoom = document.getElementById("lightbox-zoom");
  zoom.textContent = "Zoom in";
  zoom.setAttribute("aria-pressed", "false");
  const item = state.lightboxItems[state.lightboxIdx];
  if (!item) return;
  const stage = document.getElementById("lightbox-stage");
  if (stage) stage.innerHTML = `<img id="lightbox-img" src="${esc(item.src)}" alt="${esc(item.label || "Map / Screenshot")}">`;
  document.getElementById("lightbox-caption").textContent = item.label || "Map / Screenshot";
  document.getElementById("lightbox-counter").textContent = `${state.lightboxIdx + 1} / ${state.lightboxItems.length}`;
  document.getElementById("lightbox-prev").disabled = state.lightboxItems.length <= 1;
  document.getElementById("lightbox-next").disabled = state.lightboxItems.length <= 1;
}

window.openLightbox = function(src, label = "") {
  const items = collectVisibleGalleryItems();
  const foundIdx = items.findIndex(i => i.src === src);
  if (foundIdx !== -1) {
    state.lightboxItems = items;
    state.lightboxIdx = foundIdx;
  } else {
    state.lightboxItems = [{ src, label: label || "Map / Screenshot" }, ...items];
    state.lightboxIdx = 0;
  }
  updateLightboxView();
  document.getElementById("lightbox").classList.add("open");
};

window.stepLightbox = function(delta) {
  if (!state.lightboxItems.length) return;
  state.lightboxIdx = (state.lightboxIdx + delta + state.lightboxItems.length) % state.lightboxItems.length;
  updateLightboxView();
};

window.toggleLightboxZoom = function() {
  const stage = document.getElementById("lightbox-stage");
  const zoomed = stage.classList.toggle("zoomed");
  const button = document.getElementById("lightbox-zoom");
  button.textContent = zoomed ? "Fit to screen" : "Zoom in";
  button.setAttribute("aria-pressed", String(zoomed));
};

window.closeLightbox = function() {
  document.getElementById("lightbox").classList.remove("open");
};

function setStatus(text) {
  document.getElementById("status-line").textContent = text || "";
}

window.setSpoiler = function(spoiler) {
  state.spoiler = spoiler;
  document.querySelectorAll("#spoiler-control button").forEach(b => {
    b.classList.toggle("active", b.dataset.spoiler === spoiler);
  });
  loadState(state.selected?.slug, false);
};

function applyStateData(data, switchedGame = false) {
  state.games = data.games || [];
  state.selected = data.selected;
  state.walkthrough = data.walkthrough || [];
  state.currentChapter = data.current_chapter;
  state.notesHtml = data.notes_html || "";
  state.rawNotes = data.raw_notes || "";
  state.media = data.media || [];
  state.maps = data.maps || [];

  if (switchedGame) {
    state.editingNotes = false;
    state.selectedChapterIdx = state.currentChapter != null ? state.currentChapter : 0;
  } else if (state.selectedChapterIdx >= state.walkthrough.length) {
    state.selectedChapterIdx = Math.max(0, state.walkthrough.length - 1);
  }
  renderUI();
}

async function loadState(gameSlug = null, switchedGame = true) {
  const params = new URLSearchParams({ spoiler: state.spoiler });
  if (gameSlug) params.set("game", gameSlug);
  const resp = await fetch(`/api/state?${params}`);
  const data = await resp.json();
  applyStateData(data, switchedGame);
}

window.setStatusFilter = function(filter) {
  state.statusFilter = filter;
  renderLibrary();
};

function renderStatusFilters() {
  const el = document.getElementById("status-filters");
  const counts = { all: state.games.length };
  for (const g of state.games) {
    const key = (g.status || "playing").toLowerCase();
    counts[key] = (counts[key] || 0) + 1;
  }
  const tabs = [
    { key: "all", label: "All" },
    { key: "playing", label: "Playing" },
    { key: "backlog", label: "Backlog" },
    { key: "paused", label: "Paused" },
    { key: "beaten", label: "Beaten" },
    { key: "dropped", label: "Dropped" },
  ];
  el.innerHTML = tabs.map(t => {
    const c = counts[t.key] || 0;
    const active = state.statusFilter === t.key ? "active" : "";
    return `<button type="button" class="filter-chip ${active}" ${act("setStatusFilter", t.key)}>${t.label} (${c})</button>`;
  }).join("");
}


function gameConsoles(platform) {
  const raw = String(platform || "").trim();
  if (!raw) return ["Unspecified"];
  return [...new Set(raw.split(/\s*(?:,|;|\s+\/\s+|\s+&\s+)\s*/).map(value => {
    const part = value.trim();
    if (/^(pc|steam)$/i.test(part)) return "PC";
    if (/^(playstation 5|ps5)$/i.test(part)) return "PS5";
    if (/^(playstation 4|ps4)$/i.test(part)) return "PS4";
    if (/^(nintendo switch|switch)$/i.test(part)) return "Switch";
    if (/^(nintendo switch 2|switch 2)$/i.test(part)) return "Switch 2";
    return part;
  }).filter(Boolean))];
}
function renderShelf(filteredGames) {
  const select = document.getElementById("console-filter");
  const consoles = [...new Set(state.games.flatMap(g => gameConsoles(g.platform)))].sort((a, b) => a.localeCompare(b));
  if (state.consoleFilter !== "all" && !consoles.includes(state.consoleFilter)) state.consoleFilter = "all";
  select.innerHTML = `<option value="all">All consoles</option>` + consoles.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  select.value = state.consoleFilter;
  const visible = filteredGames.filter(g => state.consoleFilter === "all" || gameConsoles(g.platform).includes(state.consoleFilter));
  document.getElementById("shelf-summary").textContent = `${visible.length} ${visible.length === 1 ? "game" : "games"} in this view`;
  const groups = state.consoleFilter === "all" ? consoles : [state.consoleFilter];
  const content = document.getElementById("shelf-content");
  if (!visible.length) {
    content.innerHTML = state.games.length
      ? `<div class="shelf-empty"><h2>No games found</h2><p>Try a different search or clear your filters.</p><button type="button" class="btn" onclick="clearLibraryFilters()">Clear filters</button></div>`
      : `<div class="shelf-empty"><h2>Your shelf is empty</h2><p>Add a game to keep its walkthrough and notes together.</p><button type="button" class="btn btn-primary" onclick="openGameModal(false)">Add your first game</button></div>`;
    return;
  }
  content.innerHTML = groups.map(consoleName => {
    const games = visible.filter(g => gameConsoles(g.platform).includes(consoleName));
    if (!games.length) return "";
    return `<section class="shelf-section"><h2>${esc(consoleName)} <span>${games.length}</span></h2><div class="shelf-grid">${games.map(g => {
      const art = g.artwork ? `<img src="${esc(g.artwork)}" alt="" loading="lazy">` : esc((g.name || "?").charAt(0).toUpperCase());
      const score = g.score ? `<span title="${esc(scoreSummary(g.score))}">Critics ${Math.round(g.score.score)}/100</span>` : "";
      return `<button type="button" class="shelf-game" ${act("selectGame", g.slug)} aria-label="Open ${esc(g.name)}"><span class="shelf-cover" aria-hidden="true">${art}</span><span class="shelf-title">${esc(g.name)}</span><span class="shelf-meta"><span>${esc(g.status || "Untracked")}</span>${score}</span></button>`;
    }).join("")}</div></section>`;
  }).join("") + `<p class="shelf-help">Critic scores come from OpenCritic and Metacritic through Wikidata. Open a game and choose Get critic score to fetch one.</p>`;
}
document.getElementById("console-filter").addEventListener("change", e => { state.consoleFilter = e.target.value; renderLibrary(); });
window.clearLibraryFilters = function() {
  state.statusFilter = "all";
  state.consoleFilter = "all";
  state.librarySearch = "";
  document.getElementById("library-search").value = "";
  renderLibrary();
  searchGuides("");
};

function renderLibrary() {
  renderStatusFilters();

  const gamesEl = document.getElementById("games-list");
  const filteredGames = state.games.filter(g => {
    const matchesStatus = state.statusFilter === "all" || (g.status || "").toLowerCase() === state.statusFilter;
    const query = state.librarySearch.trim().toLowerCase();
    return matchesStatus && (!query || [g.name, g.platform, g.genre].some(v => (v || "").toLowerCase().includes(query)));
  });

  renderShelf(filteredGames);

  if (!filteredGames.length) {
    gamesEl.innerHTML = `
      <div style="padding:18px 14px;color:var(--text-secondary);font-size:13px;text-align:center">
        <p style="margin:0 0 10px">${state.librarySearch ? "No games match your search." : "No games in this view yet."}</p>
        ${state.games.length ? `<button type="button" class="btn btn-sm" onclick="clearLibraryFilters()">Clear filters</button>` : `<button type="button" class="btn btn-primary btn-sm" onclick="openGameModal(false)">Add your first game</button>`}
      </div>
    `;
  } else {
    gamesEl.innerHTML = filteredGames.map((g, idx) => {
      const isSelected = Boolean(state.selected && g.slug === state.selected.slug);
      const active = isSelected ? "active" : "";
      const sClass = statusClass(g.status);
      let progressHtml = "";
      if (g.chapter_count > 0) {
        const curNum = g.current_chapter_num || 0;
        const pct = g.progress_pct || 0;
        progressHtml = `
          <div class="game-progress-wrap" title="${curNum ? `Chapter ${curNum} of ${g.chapter_count} (chapter position)` : `${g.chapter_count} chapters`}">
            <div class="game-progress-bar"><div class="game-progress-fill" style="width:${pct}%"></div></div>
            <span class="game-progress-label">${curNum ? `${curNum}/${g.chapter_count}` : `${g.chapter_count} ch`}</span>
          </div>
        `;
      }
      const queueControls = state.statusFilter === "backlog" && !state.librarySearch.trim() ? `
        <div class="game-card-queue">
          <span class="queue-position">Queue #${idx + 1}</span>
          <button type="button" class="btn btn-sm" ${idx === 0 ? "disabled" : ""} ${act("moveGame", g.slug, -1)} title="Move up in Backlog" aria-label="Move ${esc(g.name)} up">↑</button>
          <button type="button" class="btn btn-sm" ${idx === filteredGames.length - 1 ? "disabled" : ""} ${act("moveGame", g.slug, 1)} title="Move down in Backlog" aria-label="Move ${esc(g.name)} down">↓</button>
        </div>
      ` : "";
      const artwork = g.artwork
        ? `<img class="game-card-art" src="${esc(g.artwork)}" alt="${esc(g.name)} cover artwork" loading="lazy">`
        : `<div class="game-card-art-placeholder" aria-hidden="true">${esc((g.name || "?").charAt(0).toUpperCase())}</div>`;
      return `
        <div class="game-card ${active}" role="button" tabindex="0" aria-label="Open ${esc(g.name)}" ${isSelected ? 'aria-current="true"' : ""} ${act("selectGame", g.slug)}>
          <div class="game-card-main">
            ${artwork}
            <div class="game-card-details">
              <div class="game-card-top">
                <div class="game-card-title-wrap">
                  ${isSelected ? `<span class="active-indicator" aria-hidden="true"></span>` : ""}
                  <span>${esc(g.name)}</span>
                </div>
                ${g.status ? `<span class="pill ${sClass}">${esc(g.status)}</span>` : ""}
              </div>
              <div class="game-card-meta">
                ${g.platform ? `<span>${esc(g.platform)}</span>` : ""}
                ${g.genre ? `<span>· ${esc(g.genre)}</span>` : ""}
              </div>
              ${g.spot ? `<div class="game-card-spot" title="Current Spot: ${esc(g.spot)}">Spot: ${esc(g.spot)}</div>` : ""}
              ${progressHtml}
              ${queueControls}
            </div>
          </div>
        </div>
      `;
    }).join("");
  }

}

function renderUI() {
  renderLibrary();
  const headerEl = document.getElementById("game-header-info");
  const spotBarEl = document.getElementById("topbar-spot-bar");
  const shareControls = document.getElementById("share-controls");
  const selectedSlug = state.selected?.slug || "";
  if (shareControls.dataset.game !== selectedSlug) {
    shareControls.open = false;
    shareControls.dataset.game = selectedSlug;
    document.getElementById("share-notes").checked = false;
    document.getElementById("share-error").textContent = "";
  }
  shareControls.hidden = !state.selected;

  if (state.selected) {
    const g = state.selected;
    const statuses = ["Playing", "Backlog", "Paused", "Beaten", "Dropped"];
    const statusOpts = statuses.map(s => {
      const sel = (g.status || "Playing").toLowerCase() === s.toLowerCase() ? "selected" : "";
      return `<option value="${s}" ${sel}>${s}</option>`;
    }).join("");

    headerEl.innerHTML = `
      ${g.artwork ? `<img class="game-header-art" src="${esc(g.artwork)}" alt="${esc(g.name)} cover artwork">` : ""}
      <h1 class="game-header-title">${esc(g.name)}</h1>
      ${g.platform ? `<span class="pill">${esc(g.platform)}</span>` : ""}
      ${g.genre ? `<span class="pill">${esc(g.genre)}</span>` : ""}
      <select class="status-select" aria-label="Game Status" title="Update Game Status" onchange="quickUpdateStatus(this.value)">
        ${statusOpts}
      </select>
      <button type="button" class="btn btn-sm" onclick="openGameModal(true)" title="Edit game details">Edit Game</button>
      <button type="button" class="btn btn-sm" id="fetch-cover" onclick="fetchCover()">${g.artwork ? "Find cover" : "Find cover artwork"}</button><span id="cover-error" class="score-label" role="alert"></span>
      ${g.score ? `<span class="score-label" title="${esc(scoreSummary(g.score))}">Critics ${Math.round(g.score.score)}/100 · ${esc(g.score.source)}${g.score.count ? ` (${Number(g.score.count)})` : ""}</span>` : `<span class="score-label">No critic score yet</span>`}
      <button type="button" class="btn btn-sm" id="refresh-score" onclick="refreshScore()">${g.score ? "Refresh score" : "Get critic score"}</button><span id="score-error" class="score-label" role="alert"></span>
    `;

    const chapterSelectOpts = state.walkthrough.length
      ? `<select class="chapter-jump-select" aria-label="Jump to chapter" onchange="onHeaderChapterSelect(this.value)">
          <option value="">Jump to chapter (${state.walkthrough.length})…</option>
          ${state.walkthrough.map((ch, i) => {
            const sel = i === state.currentChapter ? "selected" : "";
            return `<option value="${i}" ${sel}>${ch.index}. ${esc(ch.title)}</option>`;
          }).join("")}
        </select>`
      : "";

    const curNum = g.current_chapter_num || (state.currentChapter != null ? state.currentChapter + 1 : 0);
    const totalCh = g.chapter_count || state.walkthrough.length;
    const pct = g.progress_pct || (totalCh && curNum ? Math.round((curNum / totalCh) * 100) : 0);
    const progressReadout = totalCh > 0
      ? `<span class="game-progress-label">${curNum ? `Current chapter ${curNum} of ${totalCh}` : `${totalCh} chapters ready`}</span>`
      : `<span class="game-progress-label">No walkthrough chapters yet</span>`;

    spotBarEl.style.display = "flex";
    spotBarEl.innerHTML = `
      <div class="spot-bar-left">
        <span class="spot-bar-label">Current Spot</span>
        ${chapterSelectOpts}
        ${state.currentChapter != null ? `<button type="button" class="btn btn-sm" ${act("openChapter", state.currentChapter)}>Continue guide</button>` : ""}
        <form class="spot-editor-wrap" onsubmit="quickUpdateSpot(event)">
          <input type="text" id="header-spot-input" class="spot-editor-input" value="${esc(g.spot || "")}" placeholder="Custom chapter, boss, or objective…">
          <button type="submit" class="btn btn-sm" style="padding:3px 9px">Save Spot</button>
        </form>
      </div>
      <div>${progressReadout}</div>
    `;
  } else {
    headerEl.innerHTML = `
      <h1 class="game-header-title">No game selected</h1>
      <button type="button" class="btn btn-primary btn-sm" onclick="openGameModal(false)">Track Your First Game</button>
    `;
    spotBarEl.style.display = "none";
    spotBarEl.innerHTML = "";
  }

  document.getElementById("walk-count").textContent = state.walkthrough.length;
  document.getElementById("media-count").textContent = state.media.filter(m => m.type === "video" || m.exists !== false).length;
  document.getElementById("maps-count").textContent = state.maps.length;

  renderWalkthroughTab();
  renderNotesTab();
  renderMapsTab();
  renderMediaTab();
}

window.onHeaderChapterSelect = function(val) {
  if (val === "" || val == null) return;
  const idx = Number(val);
  if (Number.isNaN(idx) || !state.walkthrough[idx]) return;
  selectChapter(idx);
  switchTab("walk-tab");
};

window.showShelf = function() {
  document.querySelector(".app-shell").classList.add("library-mode");
  renderLibrary();
};
window.selectGame = function(slug) {
  document.querySelector(".app-shell").classList.remove("library-mode");
  loadState(slug, true);
};
window.checkForUpdates = async function() {
  const button = document.getElementById("check-updates");
  const result = document.getElementById("update-result");
  button.disabled = true;
  button.textContent = "Checking…";
  result.textContent = "";
  try {
    const response = await fetch("/api/updates/check", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    if (!response.ok) throw new Error("Update check failed.");
    const data = await response.json();
    const messages = {
      current: "GameGuider is up to date.",
      available: "A newer version of GameGuider is available.",
      local_ahead: "This installation contains changes that are not on GitHub.",
      diverged: "This installation and the GitHub version contain different changes.",
      unsupported: "This installation cannot check for updates automatically.",
      error: "Could not check for updates. Check your connection and repository access, then try again.",
    };
    const message = document.createElement("p");
    message.textContent = (data.status === "available" && data.latest) ? `GameGuider ${data.latest} is available.` : (messages[data.status] || messages.error);
    result.appendChild(message);
    if (data.dirty) {
      const warning = document.createElement("p");
      warning.textContent = "This installation has local changes. Review them before updating.";
      result.appendChild(warning);
    }
    if (["available", "unsupported", "local_ahead", "diverged"].includes(data.status)) {
      const help = document.createElement("p");
      const link = document.createElement("a");
      link.href = "https://github.com/matischneider082/gameguider#checking-for-updates";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "View update instructions on GitHub";
      help.appendChild(link);
      result.appendChild(help);
    }
  } catch (_) {
    result.textContent = "Could not check for updates. Please try again.";
  } finally {
    button.disabled = false;
    button.textContent = "Check for updates";
  }
};
window.prepareShare = function() {
  if (document.getElementById("share-controls").open) {
    document.getElementById("share-spoiler").value = state.spoiler;
  }
};
window.downloadShare = async function(event) {
  event.preventDefault();
  if (!state.selected) return;
  const button = document.getElementById("share-download");
  const error = document.getElementById("share-error");
  const slug = state.selected.slug;
  const params = new URLSearchParams({game: slug, spoiler: document.getElementById("share-spoiler").value});
  if (document.getElementById("share-notes").checked) params.set("notes", "1");
  button.disabled = true;
  button.textContent = "Preparing…";
  error.textContent = "";
  try {
    const response = await fetch(`/api/share?${params}`);
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error || "Could not prepare this guide.");
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `${slug}-gameguider.html`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    showToast("Guide ready to save and share");
  } catch (err) {
    error.textContent = err.message || "Could not prepare this guide. Please try again.";
  } finally {
    button.disabled = false;
    button.textContent = "Download guide";
  }
};
window.fetchCover = async function() {
  if (!state.selected) return;
  const gameSlug = state.selected.slug;
  const button = document.getElementById("fetch-cover");
  const errorEl = document.getElementById("cover-error");
  errorEl.textContent = "";
  button.disabled = true;
  button.textContent = "Finding cover…";
  try {
    const resp = await fetch("/api/games/artwork", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({game: gameSlug, spoiler: state.spoiler}) });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Could not find cover artwork.");
    if (state.selected?.slug === gameSlug) applyStateData(data, false);
    else await loadState(state.selected?.slug, false);
    showToast("Cover artwork added");
  } catch (err) {
    errorEl.textContent = err.message || "Could not find cover artwork.";
    button.disabled = false;
    button.textContent = "Find cover artwork";
  }
};
function scoreSummary(score) {
  return `${score.source} critic average` + (score.count ? ` from ${Number(score.count)} reviews` : "");
}

window.refreshScore = async function() {
  if (!state.selected) return;
  const gameSlug = state.selected.slug;
  const button = document.getElementById("refresh-score");
  const errorEl = document.getElementById("score-error");
  errorEl.textContent = "";
  button.disabled = true;
  button.textContent = "Fetching score…";
  try {
    const resp = await fetch("/api/scores/refresh", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({game: gameSlug, spoiler: state.spoiler}) });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw Object.assign(new Error(data.error || ""), { fromServer: true });  // it already says what to do
    if (state.selected?.slug === gameSlug) applyStateData(data, false);
    else await loadState(state.selected?.slug, false);
    showToast("Critic score updated");
  } catch (err) {
    errorEl.textContent = (err.fromServer && err.message) || "Could not fetch a critic score. Check your connection and try again.";
    button.disabled = false;
    button.textContent = "Get critic score";
  }
};

window.moveGame = async function(slug, delta) {
  const visible = state.games.filter(g =>
    state.statusFilter === "all" || (g.status || "").toLowerCase() === state.statusFilter
  );
  const idx = visible.findIndex(g => g.slug === slug);
  const target = idx + Number(delta);
  if (idx < 0 || target < 0 || target >= visible.length) return;
  [visible[idx], visible[target]] = [visible[target], visible[idx]];
  setStatus("Saving Backlog queue order…");
  const resp = await fetch("/api/games/reorder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ordered_slugs: visible.map(g => g.slug),
      status_filter: state.statusFilter,
      game: state.selected?.slug,
      spoiler: state.spoiler,
    }),
  });
  const data = await resp.json();
  if (resp.ok) {
    applyStateData(data, false);
    setStatus("Backlog queue order saved");
  } else {
    setStatus(data.error || "Failed to reorder games");
  }
};

window.quickUpdateStatus = async function(newStatus) {
  if (!state.selected) return;
  setStatus(`Saving status: ${newStatus}…`);
  const resp = await fetch("/api/games/spot", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      game: state.selected.slug,
      status: newStatus,
      spoiler: state.spoiler,
    }),
  });
  const data = await resp.json();
  if (resp.ok) {
    applyStateData(data, false);
    setStatus(`Saved status: ${newStatus}`);
    showToast(`Status updated to ${newStatus}`);
  } else {
    setStatus(data.error || "Failed to update status");
  }
};

window.quickUpdateSpot = async function(e) {
  if (e) e.preventDefault();
  if (!state.selected) return;
  const input = document.getElementById("header-spot-input");
  const newSpot = input ? input.value.trim() : "";
  await setGameSpot(newSpot, true);
};

window.setGameSpot = async function(newSpot, jumpToChapter = false) {
  if (!state.selected) return;
  setStatus(`Updating current spot to "${newSpot || "?"}"…`);
  const resp = await fetch("/api/games/spot", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      game: state.selected.slug,
      spot: newSpot,
      spoiler: state.spoiler,
    }),
  });
  const data = await resp.json();
  if (resp.ok) {
    applyStateData(data, jumpToChapter);
    setStatus(`Current spot saved: ${newSpot || "?"}`);
    showToast(`Current spot saved: ${newSpot || "?"}`);
  } else {
    setStatus(data.error || "Failed to save spot");
  }
};

let modalReturnFocus = null;
function setModalStatus(id, text, error = false) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.classList.toggle("error", error);
}
function restoreModalFocus() {
  if (modalReturnFocus && modalReturnFocus.isConnected) modalReturnFocus.focus();
  modalReturnFocus = null;
}
window.openGameModal = function(isEdit = false) {
  modalReturnFocus = document.activeElement;
  setModalStatus("game-modal-status", "");
  const modal = document.getElementById("game-modal");
  const titleEl = document.getElementById("game-modal-title");
  const slugEl = document.getElementById("modal-game-slug");
  const nameEl = document.getElementById("modal-game-name");
  const platEl = document.getElementById("modal-game-platform");
  const genreEl = document.getElementById("modal-game-genre");
  const statusEl = document.getElementById("modal-game-status");
  const spotEl = document.getElementById("modal-game-spot");
  const notesEl = document.getElementById("modal-game-notes");
  const notesWrap = document.getElementById("modal-notes-field-wrap");
  const autoWalkWrap = document.getElementById("modal-auto-walk-wrap");
  const autoWalkEl = document.getElementById("modal-auto-walkthrough");
  const deleteBtn = document.getElementById("modal-delete-btn");

  if (isEdit && state.selected) {
    const g = state.selected;
    titleEl.textContent = `Edit ${g.name}`;
    slugEl.value = g.slug;
    nameEl.value = g.name || "";
    platEl.value = g.platform || "";
    genreEl.value = g.genre || "";
    statusEl.value = g.status || "Playing";
    spotEl.value = g.spot || "";
    notesEl.value = "";
    notesWrap.style.display = "none";
    autoWalkWrap.style.display = "none";
    autoWalkEl.checked = false;
    deleteBtn.style.display = g.tracked ? "inline-flex" : "none";
  } else {
    titleEl.textContent = "Add a game";
    slugEl.value = "";
    nameEl.value = "";
    platEl.value = "";
    genreEl.value = "";
    statusEl.value = "Playing";
    spotEl.value = "";
    notesEl.value = "";
    notesWrap.style.display = "flex";
    autoWalkWrap.style.display = "flex";
    autoWalkEl.checked = false;
    deleteBtn.style.display = "none";
  }

  modal.classList.add("open");
  setTimeout(() => nameEl.focus(), 30);
};

window.closeGameModal = function() {
  document.getElementById("game-modal").classList.remove("open");
  restoreModalFocus();
};

document.getElementById("game-modal").addEventListener("click", () => closeGameModal());

let setupInfo = null;

function setupFixHtml(text) {
  // esc first, then mark up the two things fix texts contain: `commands` and https links
  return esc(text).replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/https:\/\/[^\s)<]+/g, url => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
}

function renderSetup() {
  const banner = document.getElementById("setup-banner");
  if (!setupInfo) return;
  const noAi = !(setupInfo.ai || []).some(a => a.installed && a.signed_in !== false);
  banner.hidden = setupInfo.ready;
  banner.innerHTML = setupInfo.ready ? "" : `<span><strong>Finish setting up GameGuider.</strong> ${noAi ? "Connect your AI (Claude or ChatGPT) so GameGuider can research guides." : "Choose a notes folder GameGuider can write to."}</span><button type="button" class="btn btn-sm btn-primary" onclick="openSetupModal()">Open setup</button>`;
  renderAiConnect();
  document.getElementById("setup-list").innerHTML = setupInfo.checks.map(c => `
    <li class="${c.ok ? "ok" : "missing"} ${c.required ? "required" : ""}">
      <span class="mark" aria-hidden="true">${c.ok ? "✓" : (c.required ? "✗" : "–")}</span>
      <strong>${esc(c.name)}${c.ok ? "" : (c.required ? " (needed)" : " (optional)")}</strong>
      <span class="detail">${esc(c.detail)}</span>
      ${c.fix ? `<span class="fix">${setupFixHtml(c.fix)}</span>` : ""}
    </li>`).join("");
}

function renderAiConnect() {
  const el = document.getElementById("ai-connect");
  const ai = setupInfo.ai || [];
  const usable = a => a.installed && a.signed_in !== false;
  const connected = ai.find(usable);
  if (connected && state.aiChoice === null) {
    el.innerHTML = `<p>✓ Connected to <strong>${esc(connected.label)}</strong> (${esc(connected.app)}). GameGuider uses it to research guides. <button type="button" class="btn btn-sm" ${act("chooseAi", "")}>Use a different AI</button></p>`;
    return;
  }
  const choice = ai.find(a => a.id === (state.aiChoice || (ai.find(a => a.installed) || {}).id));
  const step = (n, done, title, command, stepId) => `
    <li class="${done ? "done" : ""}"><span class="mark" aria-hidden="true">${done ? "✓" : n}</span>
      <div><strong>${title}</strong>${done ? " — done" : `<div class="cmd"><code>${esc(command)}</code>
        <button type="button" class="btn btn-sm" ${act("copySetupCommand", command)}>Copy</button>
        ${setupInfo.can_open_terminal ? `<button type="button" class="btn btn-sm" ${act("runSetupStep", choice.id, stepId)}>Run in Terminal</button>` : ""}</div>`}</div>
    </li>`;
  el.innerHTML = `<h3>Connect your AI</h3>
    <p>GameGuider researches guides with an AI you already use, so there's no API key or extra cost. Which do you use?</p>
    <div class="ai-options">${ai.map(a => `<button type="button" class="ai-option ${choice && choice.id === a.id ? "active" : ""}" ${act("chooseAi", a.id)}><strong>${esc(a.label)}</strong><span>${esc(a.plan)}</span></button>`).join("")}</div>
    ${choice ? `<ol class="ai-steps">
      ${step(1, choice.installed, `Install ${esc(choice.app)}`, choice.install, "install")}
      ${step(2, usable(choice), `Sign in to ${esc(choice.label)}: your browser opens, finish there`, choice.sign_in, "sign_in")}
    </ol>
    <div class="ai-actions"><button type="button" class="btn btn-sm btn-primary" ${act("recheckSetup")}>Check again</button>
      <a href="${esc(choice.docs)}" target="_blank" rel="noopener noreferrer">Help with ${esc(choice.app)}</a></div>` : ""}`;
}

function chooseAi(id) {
  state.aiChoice = id;
  renderAiConnect();
}

async function copySetupCommand(command) {
  try {
    await navigator.clipboard.writeText(command);
    showToast("Copied. Paste it into Terminal.");
  } catch (err) {
    showToast("Select the command and copy it.");
  }
}

async function runSetupStep(backend, step) {
  try {
    const resp = await fetch("/api/setup/terminal", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ backend, step }) });
    const data = await resp.json().catch(() => ({}));
    showToast(resp.ok ? "Terminal opened. Finish there, then choose Check again." : (data.error || "Could not open Terminal."));
  } catch (err) {
    showToast("Could not open Terminal. Copy the command instead.");
  }
}

async function recheckSetup() {
  await loadSetup();
  showToast(setupInfo && setupInfo.ready ? "GameGuider is ready." : "Not ready yet. Finish the steps above.");
}

async function loadSetup() {
  try {
    const resp = await fetch("/api/setup");
    if (resp.ok) setupInfo = await resp.json();
  } catch (err) { /* the banner is a hint; without it the app still works */ }
  renderSetup();
}

window.setTheme = async function(name) {
  const before = document.documentElement.dataset.theme || "dark";
  document.documentElement.dataset.theme = name;  // try it at once; undone below if it can't be saved
  try {
    const resp = await fetch("/api/theme", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ theme: name }) });
    if (!resp.ok) throw new Error();
  } catch (err) {
    document.documentElement.dataset.theme = before;
    document.getElementById("theme-select").value = before;
    showToast("Could not save the theme. Please try again.");
  }
};

window.openSetupModal = async function() {
  modalReturnFocus = document.activeElement;
  document.getElementById("theme-select").value = document.documentElement.dataset.theme || "dark";
  setModalStatus("setup-modal-status", "");
  document.getElementById("setup-modal").classList.add("open");
  await loadSetup();
  const input = document.getElementById("setup-notes-dir");
  if (setupInfo) input.value = setupInfo.notes_dir;
  setTimeout(() => input.focus(), 30);
};

window.closeSetupModal = function() {
  document.getElementById("setup-modal").classList.remove("open");
  restoreModalFocus();
};

document.getElementById("setup-modal").addEventListener("click", () => closeSetupModal());

window.saveNotesFolder = async function(e) {
  e.preventDefault();
  const path = document.getElementById("setup-notes-dir").value.trim();
  if (!path) { setModalStatus("setup-modal-status", "Enter a folder path.", true); return; }
  setModalStatus("setup-modal-status", "Saving…");
  try {
    const resp = await fetch("/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ notes_dir: path }) });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { setModalStatus("setup-modal-status", data.error || "Could not save that folder.", true); return; }
    setupInfo = data;
    renderSetup();
    document.getElementById("setup-notes-dir").value = data.notes_dir;
    setModalStatus("setup-modal-status", "Notes folder saved.");
    await loadState(null, false);
  } catch (err) {
    setModalStatus("setup-modal-status", "Could not save that folder. Please try again.", true);
  }
};

window.openBacklogModal = function() {
  modalReturnFocus = document.activeElement;
  setModalStatus("backlog-modal-status", "");
  document.getElementById("backlog-modal").classList.add("open");
  setTimeout(() => document.getElementById("backlog-games-input").focus(), 30);
};

window.closeBacklogModal = function() {
  document.getElementById("backlog-modal").classList.remove("open");
  restoreModalFocus();
};

document.getElementById("backlog-modal").addEventListener("click", () => closeBacklogModal());

window.submitBacklogModal = async function(e) {
  e.preventDefault();
  const input = document.getElementById("backlog-games-input");
  const games = input.value.trim();
  if (!games) return;
  const button = e.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  setModalStatus("backlog-modal-status", "Adding games…");
  try {
    const resp = await fetch("/api/backlog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        games,
        platform: document.getElementById("backlog-default-platform").value.trim(),
        genre: document.getElementById("backlog-default-genre").value.trim(),
        artwork: document.getElementById("backlog-fetch-artwork").checked,
        spoiler: state.spoiler,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      setModalStatus("backlog-modal-status", "Could not add these games. Check the list and try again.", true);
      return;
    }
    closeBacklogModal();
    input.value = "";
    state.statusFilter = "backlog";
    applyStateData(data, true);
    const count = (data.added || []).length;
    setStatus(`Added ${count} game${count === 1 ? "" : "s"} to Backlog`);
    showToast(`Backlog updated (${count} added)`);
  } catch (_) {
    setModalStatus("backlog-modal-status", "Could not add these games. Please try again.", true);
  } finally {
    button.disabled = false;
  }
};

window.setModalField = function(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value;
};

window.submitGameModal = async function(e) {
  e.preventDefault();
  const slug = document.getElementById("modal-game-slug").value.trim();
  const name = document.getElementById("modal-game-name").value.trim();
  const platform = document.getElementById("modal-game-platform").value.trim();
  const genre = document.getElementById("modal-game-genre").value.trim();
  const status = document.getElementById("modal-game-status").value;
  const spot = document.getElementById("modal-game-spot").value.trim();
  const notes = document.getElementById("modal-game-notes").value;
  const autoWalk = document.getElementById("modal-auto-walkthrough").checked;

  if (!name) return;
  const button = document.getElementById("modal-save-btn");
  button.disabled = true;
  setModalStatus("game-modal-status", "Saving game…");
  const payload = {
    slug: slug || undefined,
    name,
    platform,
    genre,
    status,
    spot,
    notes: (!slug && notes.trim()) ? notes : undefined,
    spoiler: state.spoiler,
  };
  try {
    const resp = await fetch("/api/games/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) {
      setModalStatus("game-modal-status", "Could not save this game. Check the details and try again.", true);
      return;
    }
    closeGameModal();
    applyStateData(data, !slug);
    setStatus(`Saved ${name}`);
    showToast(`Saved ${name}`);
    if (!slug && autoWalk) {
      document.querySelector(".app-shell").classList.remove("library-mode");
      window.triggerBuildWalkthrough();
    }
  } catch (_) {
    setModalStatus("game-modal-status", "Could not save this game. Please try again.", true);
  } finally {
    button.disabled = false;
  }
};

window.deleteCurrentGame = async function() {
  if (!state.selected) return;
  const name = state.selected.name;
  setModalStatus("game-modal-status", "Removing game…");
  try {
    const resp = await fetch("/api/games/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ game: state.selected.slug, spoiler: state.spoiler }),
    });
    const data = await resp.json();
    if (resp.ok) {
      closeGameModal();
      applyStateData(data, true);
      setStatus(`Removed ${name} from tracker (notes & walkthrough files kept safe).`);
      showToast(`Untracked ${name}`);
    } else {
      setModalStatus("game-modal-status", "Could not remove this game. Please try again.", true);
    }
  } catch (_) {
    setModalStatus("game-modal-status", "Could not remove this game. Please try again.", true);
  }
};

function renderChapterMediaStrip(ch) {
  const imgs = (ch.images || []).filter(i => i.exists !== false);
  const vids = ch.videos || [];
  if (!imgs.length && !vids.length) return "";
  const cards = [
    ...imgs.map(img => `
      <div class="chapter-thumb-card" ${act("openLightbox", img.src, String(img.label ?? ""))} title="${esc(img.label)}">
        <div class="chapter-thumb-img-wrap">
          <img src="${esc(img.src)}" alt="${esc(img.label)}" loading="lazy">
        </div>
        <div class="chapter-thumb-caption">${esc(img.label)}</div>
      </div>
    `),
    ...vids.map(v => `
      <div class="chapter-thumb-card" ${act("scrollToVideo", v.id, Number(v.at) || 0)} title="${esc(v.label)} (${fmtTime(v.at)})">
        <div class="chapter-thumb-img-wrap">
          <img src="${thumbUrlFor(v.id, v.at)}" alt="${esc(v.label)}" loading="lazy">
          <div class="video-play-overlay"><div class="video-play-circle" style="width:32px;height:32px;font-size:13px">▶</div></div>
          <span class="video-time-pill">${fmtTime(v.at)}</span>
        </div>
        <div class="chapter-thumb-caption">Video · ${esc(v.label)}</div>
      </div>
    `),
  ];
  return `
    <div class="chapter-media-strip">
      <div class="chapter-media-strip-title">
        <span>Chapter Images &amp; Video Moments (${imgs.length} image${imgs.length === 1 ? "" : "s"} · ${vids.length} video${vids.length === 1 ? "" : "s"})</span>
        <span>Click image to inspect · Click video to play video</span>
      </div>
      <div class="chapter-media-row">${cards.join("")}</div>
    </div>
  `;
}

window.scrollToVideo = function(vid, at) {
  const cards = document.querySelectorAll(`#walk-chapter-content .video-card[data-vid="${vid}"]`);
  for (const card of cards) {
    if (Number(card.dataset.at || 0) === Number(at || 0)) {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      playInlineVideo(card.id);
      return;
    }
  }
  if (cards[0]) {
    cards[0].scrollIntoView({ behavior: "smooth", block: "center" });
    playInlineVideo(cards[0].id);
  }
};

function renderWalkthroughTab() {
  const listEl = document.getElementById("chapters-list");
  const contentEl = document.getElementById("walk-chapter-content");
  const jumpSlot = document.getElementById("jump-current-spot-slot");
  const filter = state.chapterFilter.trim().toLowerCase();

  if (!state.walkthrough.length) {
    if (jumpSlot) jumpSlot.innerHTML = "";
    listEl.innerHTML = `<div style="padding:12px;color:var(--text-secondary);font-size:13px">No walkthrough built yet.</div>`;
    const gName = state.selected ? esc(state.selected.name) : "your game";
    contentEl.innerHTML = `
      <div style="padding:28px;border:1px dashed var(--border-strong);border-radius:var(--radius);max-width:620px;margin-top:24px">
        <h2 style="margin-top:0;border:none">No full walkthrough for ${gName} yet</h2>
        <p style="color:var(--text-secondary)">
          Build a chapter-by-chapter guide with hints, strategies, and answers. Maps and videos are included when available.
        </p>
        <button type="button" class="btn btn-primary" onclick="triggerBuildWalkthrough()">
          Build walkthrough
        </button>
      </div>
    `;
    return;
  }

  if (jumpSlot) {
    if (state.currentChapter != null && state.currentChapter !== state.selectedChapterIdx && state.walkthrough[state.currentChapter]) {
      const curCh = state.walkthrough[state.currentChapter];
      jumpSlot.innerHTML = `
        <button type="button" class="btn btn-sm" style="width:100%;justify-content:space-between;border-color:var(--accent-border);color:var(--accent)" ${act("selectChapter", state.currentChapter)}>
          <span>Jump to Current Spot</span>
          <span style="font-family:var(--font-mono)">Ch ${curCh.index}</span>
        </button>
      `;
    } else {
      jumpSlot.innerHTML = "";
    }
  }

  listEl.innerHTML = state.walkthrough.map((ch, idx) => {
    if (filter && !ch.title.toLowerCase().includes(filter)) return "";
    const active = idx === state.selectedChapterIdx ? "active" : "";
    const current = ch.current ? "current-spot" : "";
    const vCount = (ch.videos || []).length;
    const iCount = (ch.images || []).length;
    return `
      <button type="button" class="chapter-item ${active} ${current}" ${active ? 'aria-current="true"' : ""} ${act("selectChapter", idx)}>
        <div class="chapter-item-left">
          ${ch.current ? `<span class="spot-dot" title="Current Spot"></span>` : ""}
          <span class="chapter-num">${ch.index}.</span>
          <span class="chapter-title-text">${esc(ch.title)}</span>
        </div>
        <span class="chapter-badges">
          ${iCount ? `<span title="${iCount} map(s) / screenshot(s)">${iCount}p</span>` : ""}
          ${vCount ? `<span title="${vCount} video(s)">${vCount}v</span>` : ""}
        </span>
      </button>
    `;
  }).join("");

  if (!listEl.innerHTML.trim()) {
    listEl.innerHTML = `<div class="shelf-empty" style="padding:12px"><p>No chapters match your search.</p><button type="button" class="btn btn-sm" onclick="clearChapterFilter()">Clear search</button></div>`;
  }

  const ch = state.walkthrough[state.selectedChapterIdx] || state.walkthrough[0];
  const prevDisabled = state.selectedChapterIdx <= 0 ? "disabled" : "";
  const nextDisabled = state.selectedChapterIdx >= state.walkthrough.length - 1 ? "disabled" : "";

  const spotBtnHtml = ch.current
    ? `<span class="btn btn-success btn-sm" style="cursor:default">Current Spot</span>`
    : `<button type="button" class="btn btn-sm" ${act("setGameSpot", ch.title, false)} title="Save this chapter as your current spot (Shortcut: s)">Set as Current Spot</button>`;

  contentEl.innerHTML = `
    <div class="chapter-nav">
      <div style="display:flex;gap:8px;align-items:center">
        <button type="button" class="btn btn-sm" ${prevDisabled} ${act("selectChapter", state.selectedChapterIdx - 1)} title="Previous chapter ([)">← Prev</button>
        <span style="font-family:var(--font-mono);font-size:12px;color:var(--text-secondary)">
          Chapter ${ch.index} of ${state.walkthrough.length}
        </span>
        <button type="button" class="btn btn-sm" ${nextDisabled} ${act("selectChapter", state.selectedChapterIdx + 1)} title="Next chapter (])">Next →</button>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        ${spotBtnHtml}
        <button type="button" class="btn btn-sm" ${act("askAboutChapter", ch.title)}>
          Ask about Chapter
        </button>
      </div>
    </div>
    ${renderChapterMediaStrip(ch)}
    ${ch.html}
  `;
  hydrate(contentEl);
}

window.selectChapter = function(idx) {
  if (idx < 0 || idx >= state.walkthrough.length) return;
  state.selectedChapterIdx = idx;
  renderWalkthroughTab();
  const reader = document.getElementById("walk-reader");
  if (reader) reader.scrollTop = 0;
};

window.clearChapterFilter = function() {
  state.chapterFilter = "";
  document.getElementById("chapter-filter").value = "";
  renderWalkthroughTab();
  document.getElementById("chapter-filter").focus();
};

window.askAboutChapter = function(title) {
  const input = document.getElementById("question-input");
  input.value = `Help with ${title}: `;
  input.focus();
};

function renderNotesTab() {
  const controlsEl = document.getElementById("notes-tracker-controls");
  const el = document.getElementById("notes-content");
  if (!state.selected) {
    controlsEl.innerHTML = "";
    el.innerHTML = `<p style="color:var(--text-secondary)">Select or add a game to track your notes and progress.</p>`;
    return;
  }

  const g = state.selected;
  if (state.editingNotes) {
    controlsEl.innerHTML = `
      <div class="notes-toolbar">
        <div class="notes-toolbar-top">
          <strong>Editing notes for ${esc(g.name)}</strong>
          <div style="display:flex;gap:8px">
            <button type="button" class="btn btn-sm" onclick="toggleEditNotes(false)">Cancel</button>
            <button type="button" class="btn btn-primary btn-sm" onclick="saveRawNotes()">Save Notes</button>
          </div>
        </div>
        <label class="visually-hidden" for="raw-notes-textarea">Notes for ${esc(g.name)}</label>
        <textarea id="raw-notes-textarea" class="notes-editor-area">${esc(state.rawNotes || "")}</textarea>
      </div>
    `;
    el.innerHTML = "";
    return;
  }

  controlsEl.innerHTML = `
    <div class="notes-toolbar">
      <div class="notes-toolbar-top">
        <span style="font-size:13px;color:var(--text-secondary)">
          Keep reminders, builds, and progress for <strong>${esc(g.name)}</strong>.
        </span>
        <button type="button" class="btn btn-sm" onclick="toggleEditNotes(true)">Edit all notes</button>
      </div>
      <form class="quick-note-row" onsubmit="submitQuickNote(event)">
        <label class="visually-hidden" for="quick-note-spot">Chapter or location (optional)</label>
        <input type="text" id="quick-note-spot" class="quick-note-input" style="max-width:220px" value="${esc(g.spot || "")}" placeholder="Chapter or location (optional)">
        <label class="visually-hidden" for="quick-note-text">Your note</label>
        <input type="text" id="quick-note-text" class="quick-note-input" placeholder="Add a note or reminder…" required>
        <button type="submit" class="btn btn-primary btn-sm">Save Progress Note</button>
      </form>
    </div>
  `;
  el.innerHTML = state.notesHtml || "<p><em>No notes yet. Add a progress note above or ask a question below.</em></p>";
  hydrate(el);
}

window.toggleEditNotes = function(editing) {
  state.editingNotes = Boolean(editing);
  renderNotesTab();
};

window.saveRawNotes = async function() {
  if (!state.selected) return;
  const textarea = document.getElementById("raw-notes-textarea");
  if (!textarea) return;
  setStatus("Saving notes…");
  const resp = await fetch("/api/games/notes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      game: state.selected.slug,
      notes: textarea.value,
      spoiler: state.spoiler,
    }),
  });
  const data = await resp.json();
  if (resp.ok) {
    state.editingNotes = false;
    applyStateData(data, false);
    setStatus("Notes saved");
    showToast("Saved notes");
  } else {
    setStatus(data.error || "Failed to save notes");
  }
};

window.submitQuickNote = async function(e) {
  e.preventDefault();
  if (!state.selected) return;
  const spotEl = document.getElementById("quick-note-spot");
  const textEl = document.getElementById("quick-note-text");
  const spot = spotEl ? spotEl.value.trim() : "";
  const entry = textEl ? textEl.value.trim() : "";
  if (!entry) return;
  setStatus("Saving progress note…");
  const resp = await fetch("/api/games/notes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      game: state.selected.slug,
      spot: spot || undefined,
      heading: spot || state.selected.spot || "Progress Log",
      entry: `- ${entry}`,
      spoiler: state.spoiler,
    }),
  });
  const data = await resp.json();
  if (resp.ok) {
    applyStateData(data, false);
    setStatus("Progress note saved!");
    showToast("Progress note added");
  } else {
    setStatus(data.error || "Failed to save progress note");
  }
};

function renderMapsTab() {
  const el = document.getElementById("maps-content");
  if (!state.maps.length) {
    el.innerHTML = `<h2>Maps</h2><p class="maps-intro">No saved maps are visible at the ${esc(state.spoiler)} spoiler level. Build a walkthrough or raise the spoiler level to see available maps.</p>`;
    return;
  }
  el.innerHTML = `<h2>Maps</h2><p class="maps-intro">Saved maps from your guide. Open a map to zoom in, or jump to its chapter.</p><div class="maps-grid">` + state.maps.map((map, i) => `
    <article class="map-card">
      <button type="button" class="map-image-button" ${act("openMap", i)} aria-label="Open ${esc(map.label)}">
        <img src="${esc(map.src)}" alt="${esc(map.label)}" loading="lazy">
      </button>
      <div class="map-details"><strong>${esc(map.label)}</strong><span>${esc(map.chapter || "Notes")}</span>
        ${map.chapter_index != null ? `<button type="button" class="btn btn-sm" ${act("openChapter", Number(map.chapter_index))}>Open chapter</button>` : ""}
      </div>
    </article>`).join("") + `</div>`;
}
window.openMap = function(index) {
  if (!state.maps[index]) return;
  state.lightboxItems = state.maps.map(m => ({src: m.src, label: m.label}));
  state.lightboxIdx = index;
  updateLightboxView();
  document.getElementById("lightbox").classList.add("open");
};
window.openChapter = function(index) { selectChapter(index); switchTab("walk-tab"); };

function renderMediaTab() {
  const el = document.getElementById("media-content");
  const imgs = state.media.filter(m => m.type === "image" && m.exists !== false);
  const vids = state.media.filter(m => m.type === "video");

  if (!imgs.length && !vids.length) {
    el.innerHTML = `<p style="color:var(--text-secondary)">No visible maps or videos at the <code>${esc(state.spoiler)}</code> spoiler tier yet. Raise the spoiler level or build a walkthrough.</p>`;
    return;
  }

  let html = "";
  if (imgs.length) {
    html += `<h2 style="margin-top:0">Saved Maps &amp; Walkthrough Screenshots (${imgs.length})</h2>`;
    html += `<div class="media-grid">` + imgs.map(img => figureHtml(img.label, img.src)).join("") + `</div>`;
  }
  if (vids.length) {
    html += `<h2 style="margin-top:${imgs.length ? "32px" : "0"}">Walkthrough Videos (${vids.length})</h2>`;
    html += `<div class="media-grid">` + vids.map(v => videoCardHtml(v.id, v.at, v.label)).join("") + `</div>`;
  }
  el.innerHTML = html;
}

function switchTab(tabId) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tabId));
  document.querySelectorAll(".tab-view").forEach(v => v.classList.toggle("active", v.id === tabId));
}

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

document.querySelectorAll("#spoiler-control button").forEach(btn => {
  btn.addEventListener("click", () => setSpoiler(btn.dataset.spoiler));
});

document.querySelectorAll("#backend-control button").forEach(btn => {
  btn.addEventListener("click", () => {
    state.backend = btn.dataset.backend;
    document.querySelectorAll("#backend-control button").forEach(b => {
      b.classList.toggle("active", b.dataset.backend === state.backend);
    });
  });
});

document.getElementById("library-search").addEventListener("input", e => {
  state.librarySearch = e.target.value;
  renderLibrary();
  searchGuides(e.target.value);
});

let guideSearchTimer = null, guideSearchSeq = 0;
function searchGuides(text) {
  clearTimeout(guideSearchTimer);
  const query = text.trim();
  if (query.length < 3) { state.guideHits = null; renderGuideHits(); return; }
  guideSearchTimer = setTimeout(async () => {
    const seq = ++guideSearchSeq;
    let hits = null;
    try {
      const resp = await fetch(`/api/search?${new URLSearchParams({ q: query, spoiler: state.spoiler })}`);
      if (resp.ok) hits = await resp.json();
    } catch (err) { /* searching guides is a bonus on top of the title filter */ }
    if (seq !== guideSearchSeq) return;  // a newer search is on its way
    state.guideHits = hits;
    renderGuideHits();
  }, 250);
}

function renderGuideHits() {
  const el = document.getElementById("guide-hits");
  const data = state.guideHits;
  if (!data) { el.innerHTML = ""; return; }
  el.innerHTML = `<h2>In your guides</h2>` + (data.results.length
    ? `<ul>${data.results.map(h => `<li><button type="button" class="guide-hit" ${act("openGuideHit", h.slug, h.chapter_index)}><span class="guide-hit-place">${esc(h.game)} · ${esc(h.place)}</span><span class="guide-hit-snippet">${esc(h.snippet)}</span></button></li>`).join("")}</ul>`
    : `<p>Nothing in your saved guides or notes matches “${esc(data.query)}” at the ${esc(state.spoiler)} spoiler level.</p>`);
}

async function openGuideHit(slug, chapterIndex) {
  document.querySelector(".app-shell").classList.remove("library-mode");
  await loadState(slug, true);
  if (chapterIndex == null) switchTab("notes-tab");
  else openChapter(chapterIndex);
}

document.getElementById("chapter-filter").addEventListener("input", e => {
  state.chapterFilter = e.target.value;
  renderWalkthroughTab();
});

document.getElementById("reload-btn").addEventListener("click", () => loadState(state.selected?.slug, false));

const ACTIONS = { chooseAi, copySetupCommand, runSetupStep, recheckSetup, openGuideHit, playInlineVideo, loadStillFrames, playMpv, openLightbox, setSpoiler, setStatusFilter, selectGame, moveGame, selectChapter, openChapter, scrollToVideo, setGameSpot, askAboutChapter, openMap };
// Only the innermost action runs, so a queue button never also opens the game card around it.
document.addEventListener("click", e => {
  const el = e.target.closest("[data-action]");
  if (el && !el.disabled && Object.hasOwn(ACTIONS, el.dataset.action)) ACTIONS[el.dataset.action](...JSON.parse(el.dataset.args));
});
document.addEventListener("keydown", e => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches('[role="button"][data-action]')) { e.preventDefault(); e.target.click(); }
});

// Global keyboard shortcuts for fast hands-on-controller operation
document.addEventListener("keydown", e => {
  const lbOpen = document.getElementById("lightbox").classList.contains("open");
  const gameModalOpen = document.getElementById("game-modal").classList.contains("open");
  const backlogModalOpen = document.getElementById("backlog-modal").classList.contains("open");
  const setupModalOpen = document.getElementById("setup-modal").classList.contains("open");
  const modalOpen = gameModalOpen || backlogModalOpen || setupModalOpen;
  if (e.key === "Escape") {
    if (lbOpen) { closeLightbox(); e.preventDefault(); return; }
    if (gameModalOpen) { closeGameModal(); e.preventDefault(); return; }
    if (backlogModalOpen) { closeBacklogModal(); e.preventDefault(); return; }
    if (setupModalOpen) { closeSetupModal(); e.preventDefault(); return; }
  }
  if (modalOpen && e.key === "Tab") {
    const modal = document.getElementById(gameModalOpen ? "game-modal" : backlogModalOpen ? "backlog-modal" : "setup-modal");
    const controls = [...modal.querySelectorAll('button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), a[href]')].filter(el => el.getClientRects().length);
    if (controls.length) {
      if (e.shiftKey && document.activeElement === controls[0]) { controls[controls.length - 1].focus(); e.preventDefault(); }
      else if (!e.shiftKey && document.activeElement === controls[controls.length - 1]) { controls[0].focus(); e.preventDefault(); }
    }
    return;
  }
  if (lbOpen) {
    if (e.key === "ArrowLeft") { stepLightbox(-1); e.preventDefault(); return; }
    if (e.key === "ArrowRight") { stepLightbox(1); e.preventDefault(); return; }
    return;
  }
  const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : "";
  if (modalOpen || tag === "input" || tag === "textarea" || tag === "select" || e.metaKey || e.ctrlKey || e.altKey) {
    return;
  }
  if (e.key === "[") {
    selectChapter(state.selectedChapterIdx - 1);
    e.preventDefault();
  } else if (e.key === "]") {
    selectChapter(state.selectedChapterIdx + 1);
    e.preventDefault();
  } else if (e.key === "1") {
    setSpoiler("hint");
    e.preventDefault();
  } else if (e.key === "2") {
    setSpoiler("strategy");
    e.preventDefault();
  } else if (e.key === "3") {
    setSpoiler("answer");
    e.preventDefault();
  } else if (e.key === "s" && state.walkthrough[state.selectedChapterIdx]) {
    setGameSpot(state.walkthrough[state.selectedChapterIdx].title, false);
    e.preventDefault();
  } else if (e.key === "/") {
    const filterInput = document.querySelector(".app-shell").classList.contains("library-mode") ? document.getElementById("library-search") : document.getElementById("chapter-filter");
    if (filterInput) { filterInput.focus(); e.preventDefault(); }
  }
});

let streamSeq = 0;
async function streamEndpoint(url, payload, promptDisplay) {
  const seq = ++streamSeq;
  let answered = false;
  switchTab("answer-tab");
  const answerEl = document.getElementById("answer-content");
  answerEl.innerHTML = `<blockquote>${esc(promptDisplay)}</blockquote><p style="color:var(--text-secondary)"><em>Researching…</em></p>`;
  setStatus("Researching your guide…");

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      showMessage(answerEl, "Could not start the guide.", "Please try again.");
      setStatus("");
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.kind === "status") {
          setStatus("Researching your guide…");
        } else if (ev.kind === "error") {
          answered = true;
          showMessage(answerEl, "Guide unavailable.", ev.message);
          setStatus("");
          await loadState(state.selected?.slug, false);
        } else if (ev.kind === "done") {
          answered = true;
          answerEl.innerHTML = ev.html || `<p style="color:var(--text-secondary)"><em>No content yet.</em></p>`;
          hydrate(answerEl);
          setStatus("");
          await loadState(state.selected?.slug, false);
        }
      }
    }
    // The stream ended with no answer: New Chat stopped it. A newer question owns the pane, so leave that alone.
    if (!answered && seq === streamSeq) {
      showMessage(answerEl, "Question stopped.", "Ask again when you're ready.");
      setStatus("");
    }
  } catch (err) {
    if (seq !== streamSeq) return;
    showMessage(answerEl, "Could not complete your request.", "Check your connection and try again.");
    setStatus("");
  }
}

window.triggerBuildWalkthrough = function() {
  if (!state.selected) {
    openGameModal(false);
    return;
  }
  streamEndpoint(
    "/api/walkthrough",
    { backend: state.backend, spoiler: state.spoiler, game: state.selected.slug },
    `Build full walkthrough for ${state.selected.name} (Hint / Strategy / Answer + Pics & Videos)`
  );
};

document.getElementById("build-walkthrough-btn").addEventListener("click", () => window.triggerBuildWalkthrough());

document.getElementById("ask-form").addEventListener("submit", e => {
  e.preventDefault();
  const input = document.getElementById("question-input");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  streamEndpoint(
    "/api/ask",
    {
      backend: state.backend,
      spoiler: state.spoiler,
      game: state.selected ? state.selected.slug : "",
      question: q,
    },
    q
  );
});

document.getElementById("new-chat-btn").addEventListener("submit", e => e.preventDefault());
document.getElementById("new-chat-btn").addEventListener("click", async () => {
  await fetch("/api/new-chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  setStatus("Started a new conversation.");
  showToast("Conversation context reset");
});

const initialGame = new URLSearchParams(window.location.search).get("game");
if (initialGame) document.querySelector(".app-shell").classList.remove("library-mode");
// First run with no AI connected: open Setup so the walkthrough is the first thing you see.
Promise.all([loadState(initialGame, true), loadSetup()]).then(() => {
  if (setupInfo && !setupInfo.ready && !state.games.length) openSetupModal();
});
</script>
</body>
</html>
"""
HTML_PAGE = HTML_PAGE.replace("/*__THEMES__*/", themes.css()).replace(
    "<!--__THEME_OPTIONS__-->", "".join(f'<option value="{name}">{label}</option>' for name, label in themes.CHOICES.items()))
