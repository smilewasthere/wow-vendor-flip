"""M9 vendor-check job: live Wowhead verification of attractive rows.

Scope = scored candidates whose verdict is not NOT WORTH IT (the ~1,303
actionable rows). One headed Edge tab, politeness delay between items,
Cloudflare pauses after 5 consecutive blocks with needs_attention.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, pricing, vendor_check

REALM_HEADER = ("itemId,name,marketValue,minBuyout,recent,historical,"
                "updatedAt\n")
REGION_HEADER = ("itemId,name,marketValue,historical,avgSalePrice,saleRate,"
                 "soldPerDay,updatedAt\n")
COMM_HEADER = ("itemId,name,marketValue,minBuyout,recent,historical,"
               "updatedAt\n")
STAMP = "2026-10-12T02:47:41Z"


class FakeBrowser:
    """Stub for app.browser.Browser: canned results per item id."""

    def __init__(self, results):
        self.results = results
        self.seen = []
        self.launched = False
        self.closed = False

    def launch(self):
        self.launched = True

    def close(self):
        self.closed = True

    def relaunch_if_dead(self):
        pass

    def extract_sold_by(self, item_id, timeout=None):
        self.seen.append(item_id)
        return self.results[item_id]


def cand(item_id, pp, sell, **kw):
    base = {
        "id": item_id, "name": "Thing%d" % item_id, "purchase_price": pp,
        "purchase_quantity": 1, "sell_price": sell, "quality": "common",
        "is_equippable": 0, "item_class": "Tradeskill",
        "item_subclass": "", "synthetic_multiple": 0,
        "vendor_verified": 0, "excluded": 0, "flagged_risky": 0,
        "notes": "",
    }
    base.update(kw)
    return base


class VendorCheckBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowvf-m9-")
        self.db_path = os.path.join(self.tmp, "vendorflips.sqlite")
        self.cache_dir = os.path.join(self.tmp, "tsm")
        os.makedirs(self.cache_dir)
        self.conn = db.connect(self.db_path)
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_tsm(self, region=None, realm=None):
        """Minimal TSM tables; default happy prices pass suspicion rules."""
        region = region if region is not None else {}
        realm = realm if realm is not None else {}
        with open(os.path.join(self.cache_dir, "region-items.csv"),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(REGION_HEADER)
            for item_id, (mv, hist, avg, rate, sold) in region.items():
                fh.write("%d,Name%d,%d,%d,%d,%s,%s,%s\n"
                         % (item_id, item_id, mv, hist, avg, rate, sold, STAMP))
        with open(os.path.join(self.cache_dir, "realm-items.csv"),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(REALM_HEADER)
            for item_id, (mv, mb, rec, hist) in realm.items():
                fh.write("%d,Name%d,%d,%d,%d,%d,%s\n"
                         % (item_id, item_id, mv, mb, rec, hist, STAMP))
        with open(os.path.join(self.cache_dir, "commodities.csv"),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(COMM_HEADER)

    def run_job(self, browser, **kw):
        return vendor_check.run_check(
            {}, db_path=self.db_path, cache_dir=self.cache_dir,
            browser=browser, delay=0.0, **kw)


class ScopeTest(VendorCheckBase):
    def test_not_worth_it_rows_are_skipped(self):
        # 1: profitable (market 70g vs cost 10g) -> CHECK VENDOR
        db.upsert_candidate(self.conn, cand(1, 10000, 1000))
        # 2: no TSM prices -> NOT WORTH IT (rule 2: no AH price)
        db.upsert_candidate(self.conn, cand(2, 100, 0))
        self.conn.commit()
        self.write_tsm(region={1: (70000, 70000, 70000, 0.5, 100)},
                       realm={1: (70000, 70000, 70000, 70000)})
        fake = FakeBrowser({1: {"sold": 0, "npcs": [], "status": "ok"}})
        self.run_job(fake)
        self.assertEqual(fake.seen, [1])

    def test_scope_includes_all_non_not_worth_it_verdicts(self):
        # RISKY (huge margin) + CHECK VENDOR + CHECK IN GAME-ish all in scope
        db.upsert_candidate(self.conn, cand(1, 10000, 1000))   # CHECK VENDOR
        db.upsert_candidate(self.conn, cand(2, 100, 0))        # NOT WORTH IT
        self.conn.commit()
        self.write_tsm(region={
            1: (70000, 70000, 70000, 0.5, 100),
            2: (50000, 50000, 50000, 0.5, 100),
        }, realm={
            1: (70000, 70000, 70000, 70000),
            2: (50000, 50000, 50000, 50000),
        })
        # item 2's market equals cost -> roi 0 -> NOT WORTH IT
        fake = FakeBrowser({1: {"sold": 0, "npcs": [], "status": "ok"},
                            2: {"sold": 0, "npcs": [], "status": "ok"}})
        self.run_job(fake)
        self.assertEqual(fake.seen, [1])


class ResultRecordingTest(VendorCheckBase):
    def test_sold_result_stores_npcs_and_nested_wowhead_cost(self):
        db.upsert_candidate(self.conn, cand(3371, 2000, 1000))
        self.conn.commit()
        self.write_tsm(region={3371: (70000, 70000, 70000, 0.5, 100)},
                       realm={3371: (70000, 70000, 70000, 70000)})
        fake = FakeBrowser({3371: {"sold": 1, "status": "ok", "npcs": [
            {"id": 958, "name": "Vendor", "cost": [[320]], "stock": 20}]}})
        out = self.run_job(fake)
        row = db.vendor_source_map(self.conn)[3371]
        self.assertEqual(row["sold"], 1)
        self.assertEqual(row["wowhead_cost"], 320)
        self.assertEqual(row["npcs"][0]["name"], "Vendor")
        self.assertEqual(out["sold"], 1)
        self.assertEqual(out["done"], 1)

    def test_not_sold_and_error_results_recorded(self):
        db.upsert_candidate(self.conn, cand(1, 10000, 1000))
        db.upsert_candidate(self.conn, cand(3, 10000, 1000))
        self.conn.commit()
        self.write_tsm(region={
            1: (70000, 70000, 70000, 0.5, 100),
            3: (70000, 70000, 70000, 0.5, 100),
        }, realm={
            1: (70000, 70000, 70000, 70000),
            3: (70000, 70000, 70000, 70000),
        })
        fake = FakeBrowser({
            1: {"sold": 0, "npcs": [], "status": "ok"},
            3: {"status": "error", "detail": "boom"},
        })
        out = self.run_job(fake)
        rows = db.vendor_source_map(self.conn)
        self.assertEqual(rows[1]["status"], "ok")
        self.assertEqual(rows[3]["status"], "error")
        self.assertEqual(out["not_sold"], 1)
        self.assertEqual(out["blocked"], 0)


class BlockedPauseTest(VendorCheckBase):
    def test_five_consecutive_blocks_pause_with_needs_attention(self):
        ids = []
        for i in range(1, 9):
            item_id = 100 + i
            ids.append(item_id)
            db.upsert_candidate(self.conn, cand(item_id, 10000, 1000))
        self.conn.commit()
        self.write_tsm(region={
            i: (70000, 70000, 70000, 0.5, 100) for i in ids},
            realm={i: (70000, 70000, 70000, 70000) for i in ids})
        fake = FakeBrowser({i: {"status": "blocked"} for i in ids})
        out = self.run_job(fake)
        self.assertTrue(out["needs_attention"])
        self.assertEqual(out["blocked"], 5)
        self.assertEqual(len(fake.seen), 5)  # stopped, did not burn the rest
        self.assertEqual(out["done"], 5)

    def test_keep_existing_ok_rows_are_not_rechecked(self):
        db.upsert_candidate(self.conn, cand(1, 10000, 1000))
        db.upsert_vendor_source(self.conn, 1, 1, [{"id": 9, "name": "Old"}],
                                320, "2026-10-14T00:00:00Z")
        self.conn.commit()
        self.write_tsm(region={1: (70000, 70000, 70000, 0.5, 100)},
                       realm={1: (70000, 70000, 70000, 70000)})
        fake = FakeBrowser({1: {"sold": 0, "npcs": [], "status": "ok"}})
        out = self.run_job(fake)
        self.assertEqual(fake.seen, [])
        self.assertEqual(out["total"], 0)
        self.assertEqual(db.vendor_source_map(self.conn)[1]["npcs"][0]["name"],
                         "Old")

    def test_keep_existing_false_rechecks_everything(self):
        db.upsert_candidate(self.conn, cand(1, 10000, 1000))
        db.upsert_vendor_source(self.conn, 1, 1, [{"id": 9, "name": "Old"}],
                                320, "2026-10-14T00:00:00Z")
        self.conn.commit()
        self.write_tsm(region={1: (70000, 70000, 70000, 0.5, 100)},
                       realm={1: (70000, 70000, 70000, 70000)})
        fake = FakeBrowser({1: {"sold": 0, "npcs": [], "status": "ok"}})
        out = self.run_job(fake, keep_existing=False)
        self.assertEqual(fake.seen, [1])
        self.assertEqual(db.vendor_source_map(self.conn)[1]["npcs"], [])


class CancelTest(VendorCheckBase):
    def test_cancel_stops_at_item_boundary(self):
        import threading
        started = threading.Event()
        gate = threading.Event()

        class SlowBrowser(FakeBrowser):
            def extract_sold_by(self, item_id, timeout=None):
                self.seen.append(item_id)
                started.set()
                gate.wait(5)
                return {"sold": 0, "npcs": [], "status": "ok"}

        for i in range(1, 4):
            db.upsert_candidate(self.conn, cand(100 + i, 10000, 1000))
        self.conn.commit()
        self.write_tsm(region={
            i: (70000, 70000, 70000, 0.5, 100) for i in (101, 102, 103)},
            realm={i: (70000, 70000, 70000, 70000) for i in (101, 102, 103)})
        fake = SlowBrowser({})
        vendor_check.start_async(
            {}, db_path=self.db_path, cache_dir=self.cache_dir,
            browser=fake, delay=0.0)
        self.assertTrue(started.wait(5))
        self.assertTrue(vendor_check.cancel())
        gate.set()
        self.assertTrue(vendor_check.join_worker(timeout=10))
        st = vendor_check.status({})
        self.assertEqual(st["phase"], "cancelled")
        self.assertFalse(st["running"])
        self.assertEqual(st["error"], "")
        self.assertLessEqual(len(fake.seen), 2)  # cancelled at a boundary
        rows = db.vendor_source_map(self.conn)
        self.assertLessEqual(len(rows), 2)

    def test_cancel_idle_returns_false(self):
        self.assertFalse(vendor_check.cancel())


class StartAsyncTest(VendorCheckBase):
    def test_start_async_runs_runner_and_status_reports_done(self):
        db.upsert_candidate(self.conn, cand(1, 10000, 1000))
        self.conn.commit()
        self.write_tsm(region={1: (70000, 70000, 70000, 0.5, 100)},
                       realm={1: (70000, 70000, 70000, 70000)})
        fake = FakeBrowser({1: {"sold": 1, "npcs": [], "status": "ok"}})
        vendor_check.start_async(
            {}, db_path=self.db_path, cache_dir=self.cache_dir,
            browser=fake, delay=0.0)
        self.assertTrue(vendor_check.join_worker(timeout=10))
        st = vendor_check.status({})
        self.assertFalse(st["running"])
        self.assertEqual(st["error"], "")
        self.assertEqual(st["done"], 1)
        self.assertEqual(st["sold"], 1)

    def test_second_start_while_running_is_ignored(self):
        import threading
        gate = threading.Event()

        def blocker(cfg, **kw):
            gate.wait(5)

        vendor_check.start_async({}, runner=blocker)
        first = vendor_check._STATE.get("thread")
        st = vendor_check.start_async({}, runner=blocker)
        self.assertIs(vendor_check._STATE.get("thread"), first)
        self.assertTrue(st["running"])
        gate.set()
        self.assertTrue(vendor_check.join_worker(timeout=5))


class WowheadCostTest(unittest.TestCase):
    def test_nested_single_gold_amount(self):
        self.assertEqual(
            vendor_check.wowhead_cost([{"cost": [[320]]}]), 320)

    def test_flat_amount(self):
        self.assertEqual(vendor_check.wowhead_cost([{"cost": [150]}]), 150)

    def test_multi_currency_points_are_not_copper(self):
        # [points, currencyId] is not gold - must not become a mismatch chip
        self.assertIsNone(
            vendor_check.wowhead_cost([{"cost": [[5, 123]]}]))

    def test_missing_or_empty_costs(self):
        self.assertIsNone(vendor_check.wowhead_cost([]))
        self.assertIsNone(vendor_check.wowhead_cost([{"cost": None}]))
        self.assertIsNone(vendor_check.wowhead_cost([{"cost": [[]]}]))


if __name__ == "__main__":
    unittest.main()
