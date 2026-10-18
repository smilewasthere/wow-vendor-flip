import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, httpd


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wowvf-srv-")
        cfg = config.defaults()
        cfg["port"] = 0
        cfg["db_path"] = os.path.join(cls.tmp, "test.sqlite")
        cfg["cache_dir"] = cls.tmp
        cls.server = httpd.build_server(cfg)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            finally:
                exc.close()
            return exc.code, body, exc.headers

    def test_index_page_served(self):
        status, body, headers = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("<title>", body)
        self.assertIn("text/html", headers.get("Content-Type", ""))

    def test_health_endpoint(self):
        status, body, _ = self._get("/api/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("version", payload)

    def test_unknown_route_is_404(self):
        status, _, _ = self._get("/nope")
        self.assertEqual(status, 404)

    def _post(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        req = urllib.request.Request(url, data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            finally:
                exc.close()
            return exc.code, body

    def test_extract_status_endpoint(self):
        status, body, _ = self._get("/api/extract/status")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        for key in ("phase", "running", "cursor", "scanned", "candidates",
                    "not_found", "last_error"):
            self.assertIn(key, payload)
        self.assertIsInstance(payload["scanned"], int)

    def test_extract_start_delegates_to_extractor(self):
        with mock.patch("app.extract.start_async",
                        return_value={"phase": "idle",
                                      "running": False}) as fake:
            status, body = self._post("/api/extract/start")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"phase": "idle", "running": False})
        self.assertEqual(fake.call_count, 1)
        self.assertFalse(fake.call_args.kwargs.get("restart"))

    def test_extract_start_honours_restart_flag(self):
        # M8R: the UI Re-extract button sends restart=true so a completed
        # extraction actually refetches vendor data.
        with mock.patch("app.extract.start_async",
                        return_value={"phase": "scanning",
                                      "running": True}) as fake:
            status, _ = self._raw_request(
                "POST", "/api/extract/start",
                headers={"Content-Type": "application/json"},
                body=b'{"restart": true}')
        self.assertEqual(status, 200)
        self.assertTrue(fake.call_args.kwargs.get("restart"))

    def test_unknown_post_is_404(self):
        status, body = self._post("/api/nope")
        self.assertEqual(status, 404)

    def _raw_post(self, content_length_header):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/extract/start")
            conn.putheader("Content-Length", content_length_header)
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_post_with_malformed_content_length_is_400(self):
        status, body = self._raw_post("not-a-number")
        self.assertEqual(status, 400)

    def test_post_with_oversized_body_is_413(self):
        status, body = self._raw_post("999999999")
        self.assertEqual(status, 413)

    def test_static_asset_served(self):
        status, body, headers = self._get("/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("css", headers.get("Content-Type", ""))
        self.assertTrue(body.strip())

    def _raw_request(self, method, path, headers=None, body=b"{}"):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_post_with_foreign_origin_is_403(self):
        status, body = self._raw_request(
            "POST", "/api/item/notes",
            headers={"Content-Type": "application/json",
                     "Origin": "http://evil.test"})
        self.assertEqual(status, 403)
        self.assertIn(b"bad origin", body)

    def test_get_with_foreign_host_is_403(self):
        # DNS rebinding: a page pointed at us via an attacker Host.
        status, body = self._raw_request(
            "GET", "/api/health", headers={"Host": "evil.test"},
            body=b"")
        self.assertEqual(status, 403)

    def test_internal_error_is_json_500_not_connection_drop(self):
        from app.routes import api as routes_api

        def boom(cfg, query=None):
            raise RuntimeError("boom")

        with mock.patch.dict(routes_api.ROUTES, {"/api/health": boom}):
            status, body = self._raw_request("GET", "/api/health",
                                             body=b"")
        self.assertEqual(status, 500)
        json.loads(body.decode("utf-8"))  # must be valid JSON


if __name__ == "__main__":
    unittest.main()
