"""M6 routes: GET/POST /api/settings, settings merges, /api/item/notes."""

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

from app import config, db, extract, settings
from app.routes import api


class SettingsRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wowvf-m6-")
        cls.db_path = os.path.join(cls.tmp, "test.sqlite")
        conn = db.connect(cls.db_path)
        db.init_schema(conn)
        conn.close()
        cls.cfg = config.defaults()
        cls.cfg["port"] = 0
        cls.cfg["db_path"] = cls.db_path
        cls.cfg["cache_dir"] = cls.tmp

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        conn = db.connect(self.db_path)
        conn.execute("DELETE FROM settings")
        conn.commit()
        conn.close()

    def _get(self, path):
        server = _serve(self.cfg)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d%s" % (port, path)
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                try:
                    body = json.loads(exc.read())
                finally:
                    exc.close()
                return exc.code, body
        finally:
            server.shutdown()
            server.server_close()

    def _post(self, path, payload, headers=None):
        server = _serve(self.cfg)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d%s" % (port, path)
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            for key, value in (headers or {}).items():
                if value == "MATCH":
                    value = "http://127.0.0.1:%d" % port
                req.add_header(key, value)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                try:
                    body = json.loads(exc.read())
                finally:
                    exc.close()
                return exc.code, body
        finally:
            server.shutdown()
            server.server_close()

    def test_get_returns_effective_defaults(self):
        status, payload = self._get("/api/settings")
        self.assertEqual(status, 200)
        s = payload["settings"]
        self.assertEqual(s["region"], "eu")
        self.assertEqual(s["icon_host"], "blizzard")
        self.assertEqual(payload["overrides"], [])
        # M12: hard-set thresholds ship read-only alongside settings
        th = payload["thresholds"]
        self.assertEqual(th["min_profit"], 5.0)
        self.assertEqual(th["daily_budget"], 50000)
        self.assertEqual(th["ah_count"], 1)

    def test_post_persists_and_get_reflects(self):
        status, payload = self._post("/api/settings", {"settings": {
            "region": "us", "icon_host": "wowhead"}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["overrides"], ["icon_host", "region"])
        status, payload = self._get("/api/settings")
        self.assertEqual(payload["settings"]["region"], "us")
        self.assertEqual(payload["settings"]["icon_host"], "wowhead")

    def test_post_null_deletes_override(self):
        self._post("/api/settings", {"settings": {"region": "us"}})
        status, payload = self._post("/api/settings",
                                     {"settings": {"region": None}})
        self.assertEqual(status, 200)
        status, payload = self._get("/api/settings")
        self.assertEqual(payload["settings"]["region"], "eu")
        self.assertEqual(payload["overrides"], [])

    def test_post_single_key_form(self):
        status, payload = self._post("/api/settings",
                                     {"key": "icon_host", "value": "wowhead"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["icon_host"], "wowhead")

    def test_invalid_posts_are_400(self):
        cases = [
            {"settings": {}},
            {"settings": {"bogus_key": 1}},
            {"settings": {"region": "kr"}},
            {"settings": {"icon_host": "evil.example"}},
            {"settings": {"region": True}},
            # M12: removed settings are rejected as unknown keys
            {"settings": {"min_profit": 1}},
            {"settings": {"min_roi": 0.5}},
            {"settings": {"realm_slug": "twisting-nether"}},
            {"settings": {"refresh_minutes": 30}},
            {"settings": {"ah_count": 2}},
            {"nope": 1},
        ]
        for body in cases:
            status, payload = self._post("/api/settings", body)
            self.assertEqual(status, 400, body)
            self.assertFalse(payload["ok"], body)

    def test_bad_batch_is_atomic(self):
        self._post("/api/settings", {"settings": {"region": "us"}})
        status, _ = self._post("/api/settings", {
            "settings": {"icon_host": "wowhead", "min_roi": -3}})
        self.assertEqual(status, 400)
        conn = db.connect(self.db_path)
        stored = settings.stored(conn)
        conn.close()
        self.assertEqual(stored, {"region": "us"})

    def test_thresholds_are_read_only_not_writable(self):
        # M12: thresholds are hard-set in pricing; the settings table never
        # stores them, and the payload exposes them separately as read-only.
        status, payload = self._post("/api/settings",
                                     {"settings": {"min_profit": 9}})
        self.assertEqual(status, 400)
        conn = db.connect(self.db_path)
        stored = settings.stored(conn)
        conn.close()
        self.assertNotIn("min_profit", stored)
        # thresholds still ship read-only on a successful GET
        status, payload = self._get("/api/settings")
        self.assertEqual(payload["thresholds"]["min_profit"], 5.0)

    def test_unhashable_single_key_is_400(self):
        status, payload = self._post("/api/settings",
                                     {"key": [1, 2], "value": 3})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        status, payload = self._post("/api/settings",
                                     {"key": None, "value": 3})
        self.assertEqual(status, 400)

    def test_foreign_origin_is_403(self):
        status, payload = self._post(
            "/api/settings", {"settings": {"region": "us"}},
            headers={"Origin": "http://evil.test"})
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

    def test_matching_origin_is_accepted(self):
        status, _ = self._post(
            "/api/settings", {"settings": {"region": "us"}},
            headers={"Origin": "MATCH"})
        self.assertEqual(status, 200)

    def test_db_busy_is_503(self):
        with mock.patch("app.routes.api.db.connect",
                        side_effect=sqlite3.OperationalError(
                            "database is locked")):
            status, payload = self._call_direct(
                api.settings_post, self.cfg,
                {"settings": {"region": "us"}})
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])

    def _call_direct(self, handler, cfg, body):
        result = handler(cfg, body)
        return result if isinstance(result, tuple) else (200, result)


def _serve(cfg):
    from app import httpd
    return httpd.build_server(cfg)


class SettingsMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowvf-m6m-")
        self.db_path = os.path.join(self.tmp, "test.sqlite")
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        settings.write(conn, {"region": "us"})
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_extraction_uses_settings_region(self):
        captured = {}

        def fake_token(cfg):
            captured["region"] = cfg.get("region")
            return "tok", 0

        def fake_fetch(cfg, token, item_id):
            raise extract.ItemNotFound(item_id)

        cfg = config.defaults()
        extract.run_extraction(
            cfg, db_path=self.db_path, cache_dir=self.tmp, universe=[1],
            fetch_item_fn=fake_fetch, get_token_fn=fake_token, pace=0)
        self.assertEqual(captured.get("region"), "us")

    def test_icon_route_uses_settings_icon_host(self):
        from app import icons
        conn = db.connect(self.db_path)
        conn.execute("INSERT INTO candidate_items"
                     " (id, name, purchase_price, purchase_quantity)"
                     " VALUES (30817, 'x', 100, 1)")
        settings.write(conn, {"icon_host": "wowhead"})
        conn.close()
        cfg = dict(config.defaults(), db_path=self.db_path,
                   cache_dir=self.tmp)
        with mock.patch("app.icons.get_icon", return_value="inv_misc_1"):
            result = api.item_icon(cfg, {"id": ["30817"]})
        status, payload = result if isinstance(result, tuple) else (200,
                                                                    result)
        self.assertEqual(status, 200)
        # wowhead icon-host mode builds the zamimg (Wowhead CDN) template
        self.assertIn("wow.zamimg.com", payload["url"])


class NotesRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowvf-m6n-")
        self.db_path = os.path.join(self.tmp, "test.sqlite")
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        conn.execute("INSERT INTO candidate_items"
                     " (id, name, purchase_price, purchase_quantity)"
                     " VALUES (42, 'Thing', 100, 1)")
        conn.commit()
        conn.close()
        self.cfg = dict(config.defaults(), db_path=self.db_path,
                        cache_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, result):
        return result if isinstance(result, tuple) else (200, result)

    def test_notes_persist_and_clear(self):
        status, payload = self._call(api.item_notes(
            self.cfg, {"id": 42, "notes": "sold out"}))
        self.assertEqual(status, 200)
        conn = db.connect(self.db_path)
        row = conn.execute("SELECT notes FROM candidate_items"
                           " WHERE id = 42").fetchone()
        conn.close()
        self.assertEqual(row[0], "sold out")
        api.item_notes(self.cfg, {"id": 42, "notes": ""})
        conn = db.connect(self.db_path)
        row = conn.execute("SELECT notes FROM candidate_items"
                           " WHERE id = 42").fetchone()
        conn.close()
        self.assertEqual(row[0], "")

    def test_notes_validation_and_404(self):
        status, _ = self._call(api.item_notes(self.cfg,
                                              {"id": 42, "notes": 5}))
        self.assertEqual(status, 400)
        status, _ = self._call(api.item_notes(
            self.cfg, {"id": 42, "notes": "x" * 501}))
        self.assertEqual(status, 400)
        status, _ = self._call(api.item_notes(self.cfg,
                                              {"id": 99, "notes": "x"}))
        self.assertEqual(status, 404)
        status, _ = self._call(api.item_notes(self.cfg,
                                              {"id": -1, "notes": "x"}))
        self.assertEqual(status, 400)


class IconErrorMappingTest(unittest.TestCase):
    """M8: upstream rate limits surface as retryable 503, not generic 502."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowvf-icon-")
        self.db_path = os.path.join(self.tmp, "test.sqlite")
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        conn.close()
        self.cfg = dict(config.defaults(), db_path=self.db_path,
                        cache_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, exc):
        from app import blizzard
        with mock.patch("app.icons.get_icon", side_effect=exc):
            result = api.item_icon(self.cfg, {"id": ["1"]})
        return result if isinstance(result, tuple) else (200, result)

    def test_upstream_429_is_503(self):
        from app import blizzard
        exc = blizzard.BlizzardError("api request failed: HTTP 429")
        exc.status = 429
        status, payload = self._call(exc)
        self.assertEqual(status, 503)
        self.assertIn("busy", payload["error"])

    def test_other_blizzard_errors_stay_502(self):
        from app import blizzard
        exc = blizzard.BlizzardError("api request failed: HTTP 500")
        exc.status = 500
        status, payload = self._call(exc)
        self.assertEqual(status, 502)
        status, payload = self._call(blizzard.BlizzardError("nope"))
        self.assertEqual(status, 502)


if __name__ == "__main__":
    unittest.main()
