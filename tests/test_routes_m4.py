"""M4 routes: GET /api/icon (lazy, cached) and POST /api/item/verify."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, httpd


class RoutesM4Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wowvf-m4-")
        cls.db_path = os.path.join(cls.tmp, "test.sqlite")
        conn = db.connect(cls.db_path)
        db.init_schema(conn)
        conn.execute(
            "INSERT INTO candidate_items"
            " (id, name, purchase_price, purchase_quantity, sell_price,"
            "  quality, is_equippable, item_class, item_subclass,"
            "  synthetic_multiple, vendor_verified, excluded, flagged_risky,"
            "  notes, extracted_at)"
            " VALUES (30817, 'Simple Flour', 10, 1, 2, 'common', 0,"
            "  'Tradeskill', '', 1, 0, 1, 1, 'keep', '2026-10-13T00:00:00')")
        conn.commit()
        conn.close()

        cfg = config.defaults()
        cfg["port"] = 0
        cfg["db_path"] = cls.db_path
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
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            finally:
                exc.close()
            return exc.code, body

    def _post(self, path, data):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                out = exc.read().decode("utf-8", "replace")
            finally:
                exc.close()
            return exc.code, out

    def test_icon_endpoint_returns_icon_and_url(self):
        with mock.patch("app.icons.fetch_icon_name",
                        return_value="inv_flour") as fake:
            status, body = self._get("/api/icon?id=30817")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["icon"], "inv_flour")
        self.assertIn("inv_flour", payload["url"])
        self.assertIn("render.worldofwarcraft.com", payload["url"])
        self.assertEqual(fake.call_count, 1)

    def test_icon_endpoint_second_call_hits_cache(self):
        with mock.patch("app.icons.fetch_icon_name",
                        return_value="inv_cached") as fake:
            status, body = self._get("/api/icon?id=424242")
            self.assertEqual(status, 200)
            status, body = self._get("/api/icon?id=424242")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["icon"], "inv_cached")
        self.assertEqual(fake.call_count, 1)

    def test_icon_endpoint_missing_id_is_400(self):
        status, body = self._get("/api/icon")
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_icon_endpoint_non_numeric_id_is_400(self):
        status, body = self._get("/api/icon?id=abc")
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_icon_endpoint_transient_blizzard_error_is_502(self):
        err = blizzard_error_500()
        with mock.patch("app.icons.fetch_icon_name", side_effect=err):
            status, body = self._get("/api/icon?id=555")
        self.assertEqual(status, 502)
        self.assertFalse(json.loads(body)["ok"])

    def test_verify_roundtrip_persists(self):
        status, body = self._post("/api/item/verify",
                                  {"id": 30817, "vendor_verified": True})
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["vendor_verified"], 1)

        status, body = self._post("/api/item/verify",
                                  {"id": 30817, "vendor_verified": False})
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["vendor_verified"], 0)

    def test_verify_preserves_other_manual_flags_after_a_real_write(self):
        status, _ = self._post("/api/item/verify",
                               {"id": 30817, "vendor_verified": True})
        self.assertEqual(status, 200)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT vendor_verified, excluded, flagged_risky, notes FROM"
            " candidate_items WHERE id = 30817").fetchone()
        conn.close()
        self.assertEqual(row, (1, 1, 1, "keep"))

    def test_verify_huge_id_is_400_not_a_crash(self):
        status, body = self._post("/api/item/verify",
                                  {"id": 99999999999999999999,
                                   "vendor_verified": True})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_icon_huge_id_is_400_not_a_crash(self):
        status, body = self._get("/api/icon?id=99999999999999999999")
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_icon_repeated_id_takes_first_value(self):
        with mock.patch("app.icons.fetch_icon_name",
                        return_value="inv_first"):
            status, body = self._get("/api/icon?id=888&id=999")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], 888)

    def test_icon_502_body_never_echoes_upstream_text(self):
        from app import blizzard
        err = blizzard.BlizzardError(
            "api request failed: HTTP 500: SECRETVALUE123 client_secret")
        err.status = 500
        with mock.patch("app.icons.fetch_icon_name", side_effect=err):
            status, body = self._get("/api/icon?id=777")
        self.assertEqual(status, 502)
        self.assertNotIn("SECRETVALUE123", body)
        self.assertNotIn("client_secret", body)
        self.assertFalse(json.loads(body)["ok"])

    def test_verify_unknown_id_is_404(self):
        status, body = self._post("/api/item/verify",
                                  {"id": 999999999, "vendor_verified": True})
        self.assertEqual(status, 404)
        self.assertFalse(json.loads(body)["ok"])

    def test_verify_missing_id_is_400(self):
        status, body = self._post("/api/item/verify",
                                  {"vendor_verified": True})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_verify_non_boolean_is_400(self):
        status, body = self._post("/api/item/verify",
                                  {"id": 30817, "vendor_verified": "yes"})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_verify_malformed_json_is_400(self):
        status, body = self._post("/api/item/verify", b"not json at all")
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])


def blizzard_error_500():
    from app import blizzard
    err = blizzard.BlizzardError("api request failed: HTTP 500: boom")
    err.status = 500
    return err


if __name__ == "__main__":
    unittest.main()
