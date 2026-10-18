"""M5 route: GET /api/items - scored candidates with counts and filters."""

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

REGION_HEADER = ("itemId,name,marketValue,historical,avgSalePrice,saleRate,"
                 "soldPerDay,updatedAt\n")
COMM_HEADER = ("itemId,name,marketValue,minBuyout,recent,historical,"
               "updatedAt\n")
STAMP = "2026-10-12T02:47:41Z"


class ItemsRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wowvf-items-")
        cls.db_path = os.path.join(cls.tmp, "test.sqlite")
        conn = db.connect(cls.db_path)
        db.init_schema(conn)
        cols = ("id, name, purchase_price, purchase_quantity, sell_price,"
                " quality, is_equippable, item_class, item_subclass,"
                " synthetic_multiple, vendor_verified, excluded,"
                " flagged_risky, notes, extracted_at")
        conn.execute(
            "INSERT INTO candidate_items (%s) VALUES"
            " (1, 'Good Thing', 10000, 1, 1000, 'common', 0,"
            "  'Tradeskill', '', 0, 1, 0, 0, '', 'now'),"
            " (2, 'Slow Thing', 10000, 1, 1000, 'common', 0,"
            "  'Tradeskill', '', 0, 0, 1, 0, 'mine', 'now')" % cols)
        conn.commit()
        conn.close()

        with open(os.path.join(cls.tmp, "region-items.csv"),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(REGION_HEADER)
            fh.write("1,Good Thing,70000,70000,70000,0.5,100,%s\n" % STAMP)
            fh.write("2,Slow Thing,70000,70000,70000,0.5,10,%s\n" % STAMP)
        with open(os.path.join(cls.tmp, "commodities.csv"),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(COMM_HEADER)

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

    def test_shape_counts_and_item_fields(self):
        status, payload = self._get("/api/items")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["counts"]["GOOD"], 1)
        self.assertEqual(payload["counts"]["NOT WORTH IT"], 1)
        item = payload["items"][0]
        for key in ("id", "name", "wowhead", "verdict", "reason",
                    "tier", "chips", "score", "net_profit_unit", "roi",
                    "expected_sold_per_day", "confidence", "sell_price",
                    "purchase_price", "commodity", "suspicious",
                    "vendor_verified"):
            self.assertIn(key, item)
        self.assertEqual(item["wowhead"],
                         "https://www.wowhead.com/item=%d" % item["id"])
        # M12: Undermine links removed (region-only, no realm)
        self.assertNotIn("undermine", item)
        # excluded item carries its note through
        excluded = [i for i in payload["items"] if i["id"] == 2][0]
        self.assertEqual(excluded["notes"], "mine")

    def test_default_sort_is_score_descending(self):
        status, payload = self._get("/api/items")
        scores = [i["score"] for i in payload["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_verdict_filter(self):
        status, payload = self._get("/api/items?verdict=GOOD")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["verdict"], "GOOD")
        # counts always describe the whole scored set
        self.assertEqual(payload["counts"]["NOT WORTH IT"], 1)

    def test_sort_by_roi(self):
        status, payload = self._get("/api/items?sort=roi")
        rois = [i["roi"] for i in payload["items"]]
        self.assertEqual(rois, sorted(rois, reverse=True))

    def test_sort_by_profit_and_sold(self):
        status, payload = self._get("/api/items?sort=profit")
        profits = [i["net_profit_unit"] for i in payload["items"]]
        self.assertEqual(profits, sorted(profits, reverse=True))
        status, payload = self._get("/api/items?sort=sold")
        solds = [i["expected_sold_per_day"] for i in payload["items"]]
        self.assertEqual(solds, sorted(solds, reverse=True))

    def test_verdict_url_encoded(self):
        from urllib.parse import quote
        status, payload = self._get(
            "/api/items?verdict=" + quote("NOT WORTH IT"))
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["verdict"], "NOT WORTH IT")

    def test_limit(self):
        status, payload = self._get("/api/items?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["total"], 2)

    def test_limit_never_hides_verdicts_users_act_on(self):
        # M6R: GOOD/CHECK VENDOR rows always ship; limit only trims others.
        status, payload = self._get("/api/items?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["verdict"], "GOOD")
        status, payload = self._get("/api/items?limit=0")
        self.assertEqual(status, 400)  # still validated first

    def test_invalid_params_are_400(self):
        for qs in ("?sort=banana", "?verdict=WEIRD", "?limit=abc",
                   "?limit=0", "?limit=99999999"):
            status, payload = self._get("/api/items" + qs)
            self.assertEqual(status, 400, qs)
            self.assertFalse(payload["ok"], qs)

    def test_items_route_never_fetches_icons(self):
        with mock.patch("app.icons.fetch_icon_name") as fake:
            status, _ = self._get("/api/items")
        self.assertEqual(status, 200)
        self.assertEqual(fake.call_count, 0)

    def test_settings_threshold_rows_never_affect_scoring(self):
        # M12: thresholds are hard-set; a stale settings row must be inert.
        conn = db.connect(self.db_path)
        conn.execute("INSERT OR REPLACE INTO settings (key, value)"
                     " VALUES ('min_profit', '999999999')")
        conn.commit()
        conn.close()
        try:
            status, payload = self._get("/api/items")
        finally:
            conn = db.connect(self.db_path)
            conn.execute("DELETE FROM settings WHERE key = 'min_profit'")
            conn.commit()
            conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"].get("GOOD"), 1)

    def test_missing_tsm_cache_returns_503(self):
        empty = os.path.join(self.tmp, "no-tsm-here")
        os.makedirs(empty, exist_ok=True)
        cfg = dict(config.defaults(), db_path=self.db_path, cache_dir=empty)
        from app.routes import api
        status, payload = api.items(cfg, {})
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])

    def test_corrupt_tsm_cache_returns_503(self):
        bad = os.path.join(self.tmp, "corrupt-tsm")
        os.makedirs(bad, exist_ok=True)
        for name in ("region-items.csv", "commodities.csv"):
            with open(os.path.join(bad, name), "wb") as fh:
                fh.write(b"\xff\xfe\x00bad")
        cfg = dict(config.defaults(), db_path=self.db_path, cache_dir=bad)
        from app.routes import api
        status, payload = api.items(cfg, {})
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])


class ItemsVendorFieldsTest(unittest.TestCase):
    """M9: vendor_sources surfaces as row fields + NOT VENDOR-SOLD counts."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wowvf-items-v-")
        cls.db_path = os.path.join(cls.tmp, "test.sqlite")
        conn = db.connect(cls.db_path)
        db.init_schema(conn)
        cols = ("id, name, purchase_price, purchase_quantity, sell_price,"
                " quality, is_equippable, item_class, item_subclass,"
                " synthetic_multiple, vendor_verified, excluded,"
                " flagged_risky, notes, extracted_at")
        conn.execute(
            "INSERT INTO candidate_items (%s) VALUES"
            " (11, 'Live Vendor', 10000, 1, 1000, 'common', 0,"
            "  'Tradeskill', '', 0, 0, 0, 0, '', 'now'),"
            " (12, 'Vendor Gone', 10000, 1, 1000, 'common', 0,"
            "  'Tradeskill', '', 0, 0, 0, 0, '', 'now'),"
            " (13, 'Unknown Yet', 10000, 1, 1000, 'common', 0,"
            "  'Tradeskill', '', 0, 0, 0, 0, '', 'now')" % cols)
        db.upsert_vendor_source(
            conn, 11, 1, [{"id": 958, "name": "Tharynn Bouden",
                           "cost": [[320]], "stock": 20}],
            320, "2026-10-14T00:00:00Z")
        db.upsert_vendor_source(conn, 12, 0, [], None,
                                "2026-10-14T00:00:00Z")
        conn.commit()
        conn.close()

        with open(os.path.join(cls.tmp, "region-items.csv"),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(REGION_HEADER)
            for item_id in (11, 12, 13):
                fh.write("%d,Thing%d,70000,70000,70000,0.5,100,%s\n"
                         % (item_id, item_id, STAMP))
        with open(os.path.join(cls.tmp, "commodities.csv"),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(COMM_HEADER)

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
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status, json.loads(resp.read())

    def test_vendor_fields_and_not_vendor_sold_count(self):
        status, payload = self._get("/api/items")
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"].get("NOT VENDOR-SOLD"), 1)
        self.assertEqual(payload["counts"].get("GOOD"), 1)
        self.assertEqual(payload["counts"].get("CHECK VENDOR"), 1)
        by_id = {item["id"]: item for item in payload["items"]}
        live = by_id[11]
        self.assertEqual(live["verdict"], "GOOD")
        self.assertIn("sold by", live["reason"])
        self.assertEqual(live["vendor_sold"], 1)
        self.assertEqual(live["vendor_npcs"][0]["name"], "Tharynn Bouden")
        self.assertEqual(live["vendor_checked_at"], "2026-10-14T00:00:00Z")
        gone = by_id[12]
        self.assertEqual(gone["verdict"], "NOT VENDOR-SOLD")
        self.assertEqual(gone["vendor_sold"], 0)
        self.assertEqual(gone["score"], 0)
        unknown = by_id[13]
        self.assertEqual(unknown["verdict"], "CHECK VENDOR")
        self.assertIsNone(unknown["vendor_sold"])
        self.assertEqual(unknown["vendor_npcs"], [])


if __name__ == "__main__":
    unittest.main()
