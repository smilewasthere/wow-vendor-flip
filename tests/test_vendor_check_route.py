"""M9 routes: /api/vendor-check/status|start|cancel delegate to the job."""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, httpd

STATUS_SNAPSHOT = {
    "phase": "idle", "error": "", "running": False, "done": 0, "total": 0,
    "current": 0, "sold": 0, "not_sold": 0, "blocked": 0,
    "needs_attention": False,
}


class VendorCheckRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wowvf-vcroute-")
        cls.db_path = os.path.join(cls.tmp, "test.sqlite")
        conn = db.connect(cls.db_path)
        db.init_schema(conn)
        conn.close()
        cfg = config.defaults()
        cfg["port"] = 0
        cfg["db_path"] = cls.db_path
        cfg["cache_dir"] = cls.tmp
        cls.server = httpd.build_server(cfg)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            finally:
                exc.close()
            return exc.code, body

    def _post(self, path, body=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            finally:
                exc.close()
            return exc.code, payload

    def test_get_status_delegates_to_job(self):
        with mock.patch("app.vendor_check.status",
                        return_value=dict(STATUS_SNAPSHOT)) as fake:
            status, payload = self._get("/api/vendor-check/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["phase"], "idle")
        self.assertIn("running", payload)
        self.assertIn("needs_attention", payload)
        self.assertEqual(fake.call_count, 1)

    def test_post_start_maps_restart_to_keep_existing(self):
        with mock.patch("app.vendor_check.start_async",
                        return_value=dict(STATUS_SNAPSHOT, running=True)) as fake:
            status, payload = self._post("/api/vendor-check/start",
                                         {"restart": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["running"])
        kwargs = fake.call_args[1]
        self.assertFalse(kwargs["keep_existing"])

    def test_post_start_default_keeps_existing(self):
        with mock.patch("app.vendor_check.start_async",
                        return_value=dict(STATUS_SNAPSHOT)) as fake:
            status, _ = self._post("/api/vendor-check/start")
        self.assertEqual(status, 200)
        self.assertTrue(fake.call_args[1]["keep_existing"])

    def test_post_cancel_returns_cancelled_flag(self):
        with mock.patch("app.vendor_check.cancel",
                        return_value=True) as fake:
            status, payload = self._post("/api/vendor-check/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"cancelled": True})
        self.assertEqual(fake.call_count, 1)


if __name__ == "__main__":
    unittest.main()
