import json
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import browser


SELLER_FIXTURE = """
<html><body><script>
new Listview({template: 'npc', id: 'sold-by', name: LANG.tab_soldby,
  note: WH.sprintf(LANG.lvnote_npcsfound, 2, 200),
  data: [{"classification":0,"id":958,"location":[12],"name":"Dawn Brightstar",
          "react":[1,-1],"type":7,stock:-1,cost:[150],stack:20},
         {"classification":0,"id":960,"name":"Second Vendor",stock:-1,cost:[150]}]});
new Listview({template: 'item', id: 'contains', name: LANG.tab_contains,
  data: [{"id":1,"name":"Junk"}]});
</script></body></html>
"""

DROPONLY_FIXTURE = """
<html><body><script>
new Listview({template: 'npc', id: 'dropped-by', name: LANG.tab_droppedby,
  data: [{"classification":1,"id":69455,"name":"Zandalari Water-Binder",
          "type":7,count:1,outof:22865}]});
</script></body></html>
"""

CF_FIXTURE = ("<html><head><title>Just a moment...</title></head>"
              "<body>cf-browser-verification</body></html>")


class ParseSoldByTests(unittest.TestCase):
    def test_sold_by_fixture(self):
        out = browser.parse_sold_by_html(SELLER_FIXTURE)
        self.assertIsNotNone(out)
        names = [n["name"] for n in out["npcs"]]
        self.assertEqual(names, ["Dawn Brightstar", "Second Vendor"])
        self.assertEqual(out["npcs"][0]["id"], 958)
        self.assertEqual(out["npcs"][0]["cost"], [150])

    def test_dropped_by_only_is_not_sold(self):
        self.assertIsNone(browser.parse_sold_by_html(DROPONLY_FIXTURE))

    def test_sold_by_with_unquoted_keys(self):
        html = ("new Listview({template: 'npc', id: 'sold-by',"
                " data: [{classification:0,id:42,name:'Quartermaster',"
                "stock:-1,cost:[99]}]});")
        out = browser.parse_sold_by_html(html)
        self.assertEqual(out["npcs"][0]["name"], "Quartermaster")
        self.assertEqual(out["npcs"][0]["cost"], [99])

    def test_sold_by_double_quoted_marker(self):
        html = 'new Listview({id: "sold-by", data: [{"id":5,"name":"V"}]});'
        out = browser.parse_sold_by_html(html)
        self.assertEqual(out["npcs"][0]["id"], 5)

    def test_no_listview_returns_none(self):
        self.assertIsNone(browser.parse_sold_by_html("<html><body>meh</body></html>"))

    def test_missing_fields_become_none(self):
        html = "id: 'sold-by', data: [{\"id\":9,\"name\":\"Solo\"}]);"
        out = browser.parse_sold_by_html(html)
        self.assertIsNone(out["npcs"][0]["cost"])
        self.assertIsNone(out["npcs"][0]["stock"])


class DetectBlockTests(unittest.TestCase):
    def test_challenge_page_is_blocked(self):
        self.assertEqual(browser.detect_block(CF_FIXTURE), "blocked")

    def test_healthy_page_is_not_blocked(self):
        self.assertNotEqual(browser.detect_block(SELLER_FIXTURE), "blocked")

    def test_plain_page_is_none(self):
        self.assertIsNone(browser.detect_block("<html><body>Quick Facts</body></html>"))


class WsHandshakeTests(unittest.TestCase):
    """Handshake response and first CDP frame may arrive in ONE TCP segment."""

    def _serve(self, sock):
        req = sock.recv(4096)
        self.assertIn(b"Upgrade: websocket", req)
        head = (b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
        payload = json.dumps({"id": 1, "result": {}}).encode()
        frame = b"\x81" + bytes([len(payload)]) + payload  # unmasked server text
        sock.sendall(head + frame)  # co-sent regression

    def test_reader_survives_co_sent_frames(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def server():
            conn, _ = srv.accept()
            try:
                self._serve(conn)
            finally:
                conn.close()

        t = threading.Thread(target=server, daemon=True)
        t.start()
        msg = browser._cdp_handshake_and_recv(
            "127.0.0.1", port, "/devtools/page/x", timeout=5)
        t.join(timeout=2)
        srv.close()
        self.assertEqual(msg.get("id"), 1)


class TabReuseTest(unittest.TestCase):
    """Regression: one shared tab - a tab per item hung the user's PC."""

    def _bare_browser(self):
        b = browser.Browser.__new__(browser.Browser)
        b._conn = None
        b._target_id = None
        b.port = 1234
        return b

    def test_ensure_page_opens_once_then_reuses(self):
        b = self._bare_browser()
        opens = []
        sentinel = object()

        def fake_open(url=None):
            opens.append(url)
            return sentinel

        b._open_page = fake_open
        self.assertIs(b._ensure_page(), sentinel)
        self.assertIs(b._ensure_page(), sentinel)
        self.assertEqual(len(opens), 1)

    def test_drop_page_closes_socket_and_tab(self):
        from unittest import mock

        class FakeConn:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        b = self._bare_browser()
        conn = FakeConn()
        b._conn = conn
        b._target_id = "tid-1"
        with mock.patch("app.browser.urllib.request.urlopen") as fake:
            b._drop_page()
        self.assertTrue(conn.closed)
        self.assertIsNone(b._conn)
        self.assertIsNone(b._target_id)
        self.assertEqual(fake.call_count, 1)
        self.assertIn("/json/close/tid-1", fake.call_args[0][0].full_url)

    def test_drop_page_without_tab_skips_http(self):
        from unittest import mock
        b = self._bare_browser()
        with mock.patch("app.browser.urllib.request.urlopen") as fake:
            b._drop_page()
        fake.assert_not_called()


class NotSoldTrustTest(unittest.TestCase):
    """Review fix: a stalled/blank load must not be saved as sold=0."""

    def test_page_with_any_listview_is_a_trustworthy_not_sold(self):
        self.assertTrue(browser.trustworthy_not_sold(SELLER_FIXTURE
                                                     .replace("id: 'sold-by'",
                                                              "id: 'x'")))
        self.assertTrue(browser.trustworthy_not_sold(DROPONLY_FIXTURE))

    def test_blank_or_partial_html_is_an_error(self):
        self.assertFalse(browser.trustworthy_not_sold(""))
        self.assertFalse(browser.trustworthy_not_sold("<html></html>"))
        self.assertFalse(browser.trustworthy_not_sold("just some text" * 10))


class PurgeProfileCacheTest(unittest.TestCase):
    """M11: cache dirs are wiped; credentials survive."""

    def _make_profile(self, tmp):
        profile = os.path.join(tmp, "vf-profile")
        for name in ("Cache", "Code Cache", "GPUCache"):
            d = os.path.join(profile, name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "blob"), "w", encoding="utf-8") as fh:
                fh.write("x")
        keep = os.path.join(profile, "Default")
        os.makedirs(keep, exist_ok=True)
        with open(os.path.join(keep, "Cookies"), "w", encoding="utf-8") as fh:
            fh.write("keepme")
        return profile

    def test_cache_dirs_removed_and_credentials_kept(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            profile = self._make_profile(tmp)
            removed = browser.purge_profile_cache(profile)
            self.assertEqual(removed, 3)
            self.assertFalse(os.path.exists(os.path.join(profile, "Cache")))
            self.assertTrue(os.path.exists(
                os.path.join(profile, "Default", "Cookies")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_profile_is_a_noop(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            self.assertEqual(browser.purge_profile_cache(
                os.path.join(tmp, "nope")), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LaunchFlagsTest(unittest.TestCase):
    """M11: Edge launches with cache-busting flags (no process spawned)."""

    def test_launch_args_include_cache_limits(self):
        import types
        captured = {}
        real_subprocess = browser.subprocess
        real_http = browser._http_json

        def fake_popen(args, **kwargs):
            captured["args"] = args

            class FakeProc:
                def poll(self):
                    return 0

                def terminate(self):
                    pass

                def wait(self, timeout=None):
                    return 0

                def kill(self):
                    pass
            return FakeProc()

        try:
            # stub the module reference only - never patch the real
            # subprocess module (mock/asyncio import it during tests)
            browser.subprocess = types.SimpleNamespace(
                Popen=fake_popen,
                DEVNULL=real_subprocess.DEVNULL,
                TimeoutExpired=real_subprocess.TimeoutExpired)
            browser._http_json = lambda *a, **k: {}
            session = browser.Browser.__new__(browser.Browser)
            session.profile_dir = os.path.join(os.environ.get("TEMP", "."),
                                               "vf-launch-test")
            session.port = 9333
            session.headless = False
            session.settle = 0.0
            session.proc = None
            session._cmd_id = 0
            session._conn = None
            session._target_id = None
            session.launch()
            args = captured["args"]
            self.assertIn("--disk-cache-size=1", args)
            self.assertIn("--media-cache-size=1", args)
            self.assertIn("--disable-application-cache", args)
            self.assertIn("--disable-background-networking", args)
        finally:
            browser.subprocess = real_subprocess
            browser._http_json = real_http


if __name__ == "__main__":
    unittest.main()
