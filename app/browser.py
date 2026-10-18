"""Live Wowhead access via a real headed Edge browser (CDP over stdlib sockets).

Wowhead's CloudFront WAF blocks non-browser clients; a genuine browser session
passes. We drive Edge through the Chrome DevTools Protocol with a minimal
stdlib WebSocket client - no third-party dependencies, per project rules.

The sold-by signal is structural, never text-matched: item pages embed
``new Listview({template: 'npc', id: 'sold-by', ... data: [...]})`` while
drop-only pages use ``id: 'dropped-by'``.
"""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BLOCK_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform")

# On-disk caches Edge recreates freely; Cookies/Local Storage stay untouched
# because Cloudflare clearance and our logins must survive between runs.
_PURGE_DIRS = ("Cache", "Code Cache", "GPUCache", "Service Worker",
               "DawnGraphiteCache", "DawnWebGPUCache")


def purge_profile_cache(profile_dir):
    """Delete browser cache directories under profile_dir. Best-effort."""
    removed = 0
    for name in _PURGE_DIRS:
        path = os.path.join(profile_dir, name)
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


def detect_block(html):
    """Return 'blocked' when the page is a Cloudflare/browser challenge."""
    low = html[:20000].lower()
    for marker in BLOCK_MARKERS:
        if marker in low:
            return "blocked"
    return None


def _find_data_array(html, start):
    """Bracket-match the ``data: [ ... ]`` array starting at/after ``start``."""
    d = html.find("data: [", start)
    if d < 0:
        return None
    j = d + 5  # at '['
    depth = 0
    started = False
    n = len(html)
    while j < n:
        c = html[j]
        if c == "[":
            depth += 1
            started = True
        elif c == "]":
            depth -= 1
            if started and depth == 0:
                return html[d + 6:j]
        elif c in ("'", '"'):
            j += 1
            while j < n and html[j] != c:
                if html[j] == "\\":
                    j += 1
                j += 1
        j += 1
    return None


def _js_list_to_json(raw):
    """Convert a JS object-literal array to JSON: quote bare keys, single-quote strings."""
    out = []
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == '"':
            j = i + 1
            while j < n:
                if raw[j] == "\\":
                    j += 2
                    continue
                if raw[j] == '"':
                    break
                j += 1
            out.append(raw[i:j + 1])
            i = j + 1
        elif c == "'":
            j = i + 1
            buf = []
            while j < n:
                ch = raw[j]
                if ch == "\\" and j + 1 < n:
                    buf.append(raw[j + 1])
                    j += 2
                    continue
                if ch == "'":
                    break
                buf.append(ch)
                j += 1
            out.append('"%s"' % "".join(buf).replace('"', '\\"'))
            i = j + 1
        elif c in "{,":
            k = i + 1
            while k < n and raw[k] in " \t\r\n":
                k += 1
            m = k
            while m < n and (raw[m].isalnum() or raw[m] == "_"):
                m += 1
            if m > k and m < n and raw[m] == ":":
                out.append(c + raw[i + 1:k] + '"%s"' % raw[k:m])
                i = m
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_sold_by_html(html):
    """Extract the sold-by NPC list from a Wowhead item page's HTML.

    Returns ``{"npcs": [{"id", "name", "cost", "stock"}, ...]}`` or None when
    the page has no sold-by Listview (either no vendor, or not an item page).
    """
    for marker in ("id: 'sold-by'", 'id: "sold-by"'):
        i = html.find(marker)
        if i >= 0:
            break
    if i < 0:
        return None
    raw = _find_data_array(html, i)
    if raw is None:
        return None
    try:
        arr = json.loads(_js_list_to_json(raw) + "]")
    except ValueError:
        return None
    npcs = []
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        npcs.append({
            "id": entry.get("id"),
            "name": entry.get("name") or "",
            "cost": entry.get("cost"),
            "stock": entry.get("stock"),
        })
    return {"npcs": npcs}


def trustworthy_not_sold(html):
    """A page with no sold-by Listview is only a real "not sold" when the
    page still has *some* Listview (drop-only pages use `dropped-by`);
    blank/stalled loads must not be persisted as verified not-sold rows.
    """
    return "Listview(" in (html or "")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _WsConn:
    """Minimal RFC6455 client for the CDP page socket (text frames only)."""

    def __init__(self, host, port, path, timeout):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\n"
                     "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                     "Sec-WebSocket-Key: %s\r\n"
                     "Sec-WebSocket-Version: 13\r\n\r\n") % (path, host, port, key)
        self.sock.sendall(handshake.encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise ConnectionError("handshake rejected: %r" % head[:120])

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk

    def send_text(self, payload):
        data = payload.encode()
        mask = os.urandom(4)
        n = len(data)
        hdr = b"\x81"
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(hdr + mask + masked)

    def _send_control(self, opcode, payload=b""):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        hdr = bytes([0x80 | opcode, 0x80 | len(payload)])
        self.sock.sendall(hdr + mask + masked)

    def recv_frame(self):
        self._need(2)
        b0, b1 = self.buf[0], self.buf[1]
        opcode = b0 & 0x0F
        ln = b1 & 0x7F
        off = 2
        if ln == 126:
            self._need(4)
            ln = struct.unpack(">H", self.buf[2:4])[0]
            off = 4
        elif ln == 127:
            self._need(10)
            ln = struct.unpack(">Q", self.buf[2:10])[0]
            off = 10
        self._need(off + ln)
        data = self.buf[off:off + ln]
        self.buf = self.buf[off + ln:]
        return b0 >> 4, opcode, data

    def recv_json(self, deadline):
        """Read frames until a text message arrives; answer pings; JSON-decode."""
        parts = []
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("cdp read deadline")
            self.sock.settimeout(max(0.5, remaining))
            fin, opcode, data = self.recv_frame()
            if opcode == 0x9:
                self._send_control(0xA, data)
                continue
            if opcode == 0x8:
                raise ConnectionError("peer closed")
            if opcode in (0x1, 0x0):
                parts.append(data)
                if fin:
                    return json.loads(b"".join(parts).decode("utf-8", "replace"))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _cdp_handshake_and_recv(host, port, path, timeout=10):
    """Open a page socket and return the first id-bearing message (tests use this)."""
    conn = _WsConn(host, port, path, timeout)
    deadline = time.time() + timeout
    try:
        while True:
            msg = conn.recv_json(deadline)
            if isinstance(msg, dict) and "id" in msg:
                return msg
    finally:
        conn.close()


def _http_json(method, url, timeout=10):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class Browser:
    """One headed Edge instance with a dedicated profile, driven over CDP.

    A single tab is opened once and reused for every item (navigate, read,
    repeat) - opening a tab per item would leak hundreds of Wowhead pages
    and hang the machine.
    """

    def __init__(self, profile_dir, port=None, headless=False, settle=4.0):
        self.profile_dir = profile_dir
        self.port = port or _free_port()
        self.headless = headless
        self.settle = settle
        self.proc = None
        self._cmd_id = 0
        self._conn = None
        self._target_id = None

    def launch(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        os.makedirs(self.profile_dir, exist_ok=True)
        args = [
            EDGE_PATH,
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=" + self.profile_dir,
            "--no-first-run",
            "--no-default-browser-check",
            "--disk-cache-size=1",
            "--media-cache-size=1",
            "--disable-application-cache",
            "--disable-background-networking",
            "--window-size=1100,800",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                _http_json("GET", "http://127.0.0.1:%d/json/version" % self.port,
                           timeout=3)
                return
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.5)
        raise RuntimeError("Edge CDP did not come up on port %d" % self.port)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def relaunch_if_dead(self):
        if not self.alive():
            self.launch()

    def close(self):
        self._drop_page(close_tab=False)  # process teardown kills tabs anyway
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            self.proc = None
            # only safe after Edge has exited (files were locked while alive)
            purge_profile_cache(self.profile_dir)

    def __enter__(self):
        self.launch()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _open_page(self, url=None):
        base = "http://127.0.0.1:%d" % self.port
        target_url = url or "about:blank"
        try:
            target = _http_json("PUT", base + "/json/new?" + target_url)
        except urllib.error.HTTPError:
            target = _http_json("POST", base + "/json/new?" + target_url)
        self._target_id = target.get("id")
        ws = target["webSocketDebuggerUrl"]
        parts = ws.split("/")
        hostport = parts[2]
        host, port = hostport.split(":")
        path = "/" + "/".join(parts[3:])
        return _WsConn(host, int(port), path, 30)

    def _ensure_page(self):
        """The shared tab's socket; opened on first use, reused after that."""
        if self._conn is not None:
            return self._conn
        self._conn = self._open_page()
        return self._conn

    def _drop_page(self, close_tab=True):
        """Forget a dead tab socket; close the tab itself when possible."""
        conn, self._conn = self._conn, None
        target_id, self._target_id = self._target_id, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if close_tab and target_id:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/json/close/%s"
                    % (self.port, target_id))
                urllib.request.urlopen(req, timeout=3).read()
            except (urllib.error.URLError, OSError, ValueError):
                pass

    _PROBE_EXPR = (
        "(function () {"
        " var h = document.documentElement.innerHTML;"
        " return JSON.stringify({rs: document.readyState, len: h.length,"
        "  sb: h.indexOf(\"id: 'sold-by'\") >= 0,"
        "  db: h.indexOf(\"id: 'dropped-by'\") >= 0,"
        "  lv: h.indexOf('Listview(') >= 0});"
        "})()"
    )

    def _command(self, conn, method, params=None, timeout=30):
        self._cmd_id += 1
        mid = self._cmd_id
        conn.send_text(json.dumps({"id": mid, "method": method,
                                   "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            msg = conn.recv_json(deadline)
            if isinstance(msg, dict) and msg.get("id") == mid:
                return msg

    def extract_sold_by(self, item_id, timeout=45.0):
        """Fetch one Wowhead item page and return the sold-by verdict.

        Uses the shared tab: navigate, probe, read. On a dead socket the tab
        is dropped (and closed) so the next call opens a fresh one.
        """
        url = "https://www.wowhead.com/item=%d" % item_id
        try:
            conn = self._ensure_page()
        except (OSError, KeyError, ValueError, urllib.error.URLError) as exc:
            return {"status": "error", "detail": "open failed: %s" % exc}
        try:
            try:
                self._command(conn, "Page.navigate", {"url": url}, timeout=timeout)
            except (OSError, ConnectionError, TimeoutError, ValueError) as exc:
                self._drop_page()
                return {"status": "error", "detail": "navigate failed: %s" % exc}
            deadline = time.time() + timeout
            stable = 0
            last_len = -1
            state = {}
            while time.time() < deadline:
                time.sleep(1.0)
                try:
                    res = self._command(
                        conn, "Runtime.evaluate",
                        {"expression": self._PROBE_EXPR, "returnByValue": True},
                        timeout=10)
                    state = json.loads(res["result"]["result"].get("value") or "{}")
                except (OSError, ConnectionError, TimeoutError, KeyError, ValueError):
                    continue
                if state.get("sb") or state.get("db"):
                    break
                length = state.get("len", 0)
                if state.get("rs") == "complete" and length > 50000 \
                        and length == last_len:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                last_len = length
            time.sleep(max(0.5, self.settle / 4))
            try:
                res = self._command(
                    conn, "Runtime.evaluate",
                    {"expression": "document.documentElement.innerHTML",
                     "returnByValue": True},
                    timeout=timeout)
                html = res["result"]["result"].get("value") or ""
            except (OSError, ConnectionError, TimeoutError, KeyError,
                    ValueError) as exc:
                self._drop_page()
                return {"status": "error", "detail": "evaluate failed: %s" % exc}
        except (OSError, ConnectionError, TimeoutError, ValueError) as exc:
            self._drop_page()
            return {"status": "error", "detail": "page read failed: %s" % exc}

        if detect_block(html):
            return {"status": "blocked"}
        parsed = parse_sold_by_html(html)
        if parsed is None:
            if not trustworthy_not_sold(html):
                return {"status": "error",
                        "detail": "page had no Listview (load failed?)"}
            return {"sold": 0, "npcs": [], "status": "ok"}
        return {"sold": 1, "npcs": parsed["npcs"], "status": "ok"}
