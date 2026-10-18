"""Local HTTP server: static files from web/ plus the JSON routes."""

import json
import os
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import db, extract, settings
from app.routes import api, pages

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_ROOT = os.path.join(ROOT, "web")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _normalize(result):
    """Handlers return a dict (200) or a (status, dict) tuple."""
    if isinstance(result, tuple):
        return int(result[0]), result[1]
    return 200, result

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


def build_server(cfg):
    # M4R deferral, executed in M6: schema exists before the first request.
    conn = db.connect(cfg.get("db_path") or extract.default_db_path())
    try:
        db.init_schema(conn)
        settings.purge_removed(conn)  # M12: drop retired setting rows
    finally:
        conn.close()
    handler = _make_handler(cfg)
    return ThreadingHTTPServer((cfg["host"], cfg["port"]), handler)


def _make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _host_ok(self):
            """DNS-rebinding guard: only answer requests aimed at us."""
            host = (self.headers.get("Host") or "").lower()
            name = host.rsplit(":", 1)[0]
            return name in LOCAL_HOSTS

        def _origin_ok(self):
            """CSRF guard: cross-site POSTs carry a foreign Origin."""
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            host = (self.headers.get("Host") or "").lower()
            return origin.lower() == "http://" + host

        def do_GET(self):
            try:
                self._handle_get()
            except Exception:
                traceback.print_exc()
                self._send(500, b'{"ok": false, "error": "internal error"}',
                           "application/json; charset=utf-8")

        def _handle_get(self):
            if not self._host_ok():
                self._send(403, b'{"ok": false, "error": "bad host"}',
                           "application/json; charset=utf-8")
                return
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path
            if path == "/":
                self._send(200, pages.index().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path in api.ROUTES:
                query = urllib.parse.parse_qs(parts.query)
                status, payload = _normalize(api.ROUTES[path](cfg, query))
                body = json.dumps(payload).encode("utf-8")
                self._send(status, body, "application/json; charset=utf-8")
                return
            self._static(path)

        def do_POST(self):
            try:
                self._handle_post()
            except Exception:
                traceback.print_exc()
                self._send(500, b'{"ok": false, "error": "internal error"}',
                           "application/json; charset=utf-8")

        def _handle_post(self):
            if not self._host_ok():
                self._send(403, b'{"ok": false, "error": "bad host"}',
                           "application/json; charset=utf-8")
                return
            if not self._origin_ok():
                self._send(403, b'{"ok": false, "error": "bad origin"}',
                           "application/json; charset=utf-8")
                return
            path = self.path.split("?", 1)[0]
            if path not in api.POST_ROUTES:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, b"bad content-length",
                           "text/plain; charset=utf-8")
                return
            if length < 0 or length > 65536:
                self._send(413, b"body too large",
                           "text/plain; charset=utf-8")
                return
            raw = self.rfile.read(length) if length else b""
            if raw.strip():
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    body = None
                if not isinstance(body, dict):
                    body = None
                if body is None:
                    status, payload = 400, {"ok": False,
                                            "error": "invalid JSON body"}
                    data = json.dumps(payload).encode("utf-8")
                    self._send(status, data,
                               "application/json; charset=utf-8")
                    return
            else:
                body = {}
            status, payload = _normalize(api.POST_ROUTES[path](cfg, body))
            data = json.dumps(payload).encode("utf-8")
            self._send(status, data, "application/json; charset=utf-8")

        def _static(self, path):
            target = os.path.normpath(os.path.join(WEB_ROOT, path.lstrip("/")))
            if not target.startswith(os.path.normpath(WEB_ROOT) + os.sep):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            if not os.path.isfile(target):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            ctype = CONTENT_TYPES.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
            with open(target, "rb") as fh:
                self._send(200, fh.read(), ctype)

        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler
