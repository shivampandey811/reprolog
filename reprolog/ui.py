"""Live viewer: `reprolog ui` then open http://127.0.0.1:7999

- live feed of captured requests (server-sent events)
- span waterfall per request; click a span to see its logs, attributes and calls
- Replay button that re-runs the request in a subprocess and shows the verdict
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import storage
from .export import to_curl

_HERE = Path(__file__).parent
PAGES = {
    "/": "site.html",           # landing page
    "/app": "app.html",         # app shell: sidebar with Activity, Tokens, Setup
    "/app/activity": "viewer.html",
}


def _page(name: str) -> str:
    return (_HERE / name).read_text(encoding="utf-8")  # read per request so edits show on reload

SETTINGS = {"app_path": "main:app", "db": None, "cwd": os.getcwd()}


def run_replay(capture_id: str, headers: list, live_outbound: bool) -> dict:
    fd, out = tempfile.mkstemp(prefix="reprolog-", suffix=".json")
    os.close(fd)
    cmd = [sys.executable, "-m", "reprolog", "--db", SETTINGS["db"], "replay", capture_id,
           "--app", SETTINGS["app_path"], "--save", "--out-json", out]
    for h in headers:
        cmd += ["-H", h]
    if live_outbound:
        cmd.append("--live-outbound")
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=SETTINGS["cwd"], capture_output=True, text=True, timeout=180)
        output = (proc.stdout or "") + (proc.stderr or "")
        payload = {}
        if os.path.getsize(out):
            with open(out) as fh:
                payload = json.load(fh)
        if not payload:
            payload = {"failed": True, "message": "Replay did not finish. See the output below."}
        payload["output_tail"] = output[-6000:]
    except subprocess.TimeoutExpired:
        payload = {"failed": True, "message": "Replay took longer than 180 seconds and was stopped."}
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    payload["seconds"] = round(time.time() - started, 2)
    payload.get("replayed", {}).pop("request", None)  # the viewer reloads the saved capture instead
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "reprolog"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _host_ok(self) -> bool:
        # Blocks DNS-rebinding pages from talking to the viewer.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in {"127.0.0.1", "localhost", "::1"}

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, json.dumps({"error": "open the viewer at http://127.0.0.1"}))
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path.rstrip("/") in PAGES or url.path == "/":
            return self._send(200, _page(PAGES[url.path.rstrip("/") or "/"]), "text/html")
        if url.path.startswith("/api/") and not self._authorized("read"):
            return self._send(401, json.dumps({"error": "invalid token"}))
        if url.path == "/api/tokens":
            return self._send(200, json.dumps(storage.list_tokens()))
        if url.path == "/api/info":
            return self._send(200, json.dumps({"app": SETTINGS["app_path"], "db": SETTINGS["db"],
                                               "cwd": SETTINGS["cwd"], "count": storage.count()}))
        if url.path == "/api/captures":
            limit = int(query.get("limit", ["300"])[0])
            return self._send(200, json.dumps(storage.list_captures(limit=limit)))
        if url.path.startswith("/api/captures/"):
            try:
                rec = storage.get(url.path.rsplit("/", 1)[-1])
            except LookupError as err:
                return self._send(400, json.dumps({"error": str(err)}))
            if rec is None:
                return self._send(404, json.dumps({"error": "not found"}))
            rec["_curl"] = to_curl(rec)
            return self._send(200, json.dumps(rec, default=str))
        if url.path == "/api/stream":
            return self._stream(int(query.get("after", ["0"])[0]))
        return self._send(404, json.dumps({"error": "not found"}))

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else None

    def _authorized(self, need: str) -> bool:
        """No token: the local browser (POSTs also need the X-Reprolog header).
        With `Authorization: Bearer rpl_...`: read tokens may GET, write tokens may also POST."""
        token = self._bearer()
        if token is None:
            return need == "read" or self.headers.get("X-Reprolog") == "1"
        scope = storage.check_token(token)
        return scope == "write" or (scope == "read" and need == "read")

    def do_POST(self):
        if not self._host_ok() or not self._authorized("write"):
            return self._send(403, json.dumps({"error": "forbidden"}))
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if url.path.startswith("/api/tokens"):
            if self._bearer() is not None:  # tokens can't mint or revoke tokens
                return self._send(403, json.dumps({"error": "manage tokens from the app"}))
            if url.path == "/api/tokens":
                try:
                    tok = storage.create_token(str(body.get("name") or ""), str(body.get("scope") or "read"))
                except ValueError as err:
                    return self._send(400, json.dumps({"error": str(err)}))
                return self._send(200, json.dumps(tok))
            if url.path.endswith("/revoke"):
                ok = storage.revoke_token(url.path.split("/")[-2])
                return self._send(200 if ok else 404, json.dumps({"revoked": ok}))
        if url.path.startswith("/api/replay/"):
            cid = url.path.rsplit("/", 1)[-1]
            headers = [h for h in (body.get("headers") or []) if isinstance(h, str) and ":" in h]
            result = run_replay(cid, headers, bool(body.get("live_outbound")))
            return self._send(200, json.dumps(result, default=str))
        return self._send(404, json.dumps({"error": "not found"}))

    def _stream(self, after: int):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_ping = time.time()
        try:
            while True:
                rows = storage.list_since(after)
                for row in rows:
                    after = row["seq"]
                    self.wfile.write(f"data: {json.dumps(row)}\n\n".encode())
                if not rows and time.time() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    last_ping = time.time()
                self.wfile.flush()
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def serve(port: int = 7999, app_path: str = "main:app", db: str | None = None, open_browser: bool = True) -> None:
    SETTINGS.update(app_path=app_path, db=db or storage.db_path(), cwd=os.getcwd())
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}"
    print(f"reprolog at {url}  (app: {url}/app)")
    print(f"  reading {SETTINGS['db']}")
    print(f"  Replay button runs: reprolog replay <id> --app {app_path}  (in {SETTINGS['cwd']})")
    print("  Ctrl+C to stop")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
