import datetime
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auctions, db, quantity
from app.routes import api


NOW = datetime.datetime.now().isoformat(timespec="seconds")

REGION_HEADER = ("itemId,name,marketValue,historical,avgSalePrice,saleRate,"
                 "soldPerDay,updatedAt")
COMMODITIES_HEADER = "itemId,name,marketValue,minBuyout,recent,historical,updatedAt"


class QuantityRouteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tsm_dir = os.path.join(self.tmp.name, "tsm")
        self.a_dir = os.path.join(self.tmp.name, "auctions")
        os.makedirs(self.tsm_dir)
        os.makedirs(self.a_dir)
        self.db_path = os.path.join(self.tmp.name, "vf.db")
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        conn.close()
        self.cfg = {"region": "eu", "db_path": self.db_path,
                    "cache_dir": self.tsm_dir, "auctions_dir": self.a_dir}
        self.kicks = []
        original = auctions.maybe_refresh_async

        def record(eff, **kwargs):
            self.kicks.append(eff)
            return False
        auctions.maybe_refresh_async = record
        self.addCleanup(setattr, auctions, "maybe_refresh_async", original)

    def write_tsm(self, region_rows=(), commodities_rows=()):
        with open(os.path.join(self.tsm_dir, "region-items.csv"), "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(REGION_HEADER + "\n")
            for row in region_rows:
                fh.write(",".join(str(v) for v in row) + "\n")
        with open(os.path.join(self.tsm_dir, "commodities.csv"), "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(COMMODITIES_HEADER + "\n")
            for row in commodities_rows:
                fh.write(",".join(str(v) for v in row) + "\n")

    def write_auctions(self, ladder, fetched_at=NOW):
        with open(os.path.join(self.a_dir, "commodities-eu.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"region": "eu", "items": ladder}, fh)
        with open(os.path.join(self.a_dir, "commodities-eu.meta.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"fetched_at": fetched_at, "items": len(ladder),
                       "levels": 1, "units": 1}, fh)

    def call(self, **params):
        query = {key: [value] for key, value in params.items()}
        return api.item_quantity(self.cfg, query)


class ValidationTest(QuantityRouteBase):
    def test_bad_id_is_400(self):
        for raw in ("", "abc", "0", "-5", "1.5", str(10 ** 12)):
            status, body = self.call(id=raw, price="100", confidence="0.95")
            self.assertEqual(status, 400, raw)
            self.assertFalse(body["ok"])

    def test_bad_price_is_400(self):
        for raw in ("", "0", "-1", "abc", "12.5"):
            status, body = self.call(id="7", price=raw, confidence="0.95")
            self.assertEqual(status, 400, raw)
            self.assertFalse(body["ok"])

    def test_bad_confidence_is_400(self):
        for raw in ("", "0.5", "0.8", "1.0", "abc", "nan", "0.950001", "95"):
            status, body = self.call(id="7", price="100", confidence=raw)
            self.assertEqual(status, 400, raw)
            self.assertFalse(body["ok"])

    def test_bad_qty_is_400(self):
        for raw in ("", "0", "-3", "abc", "1.5", str(10 ** 10)):
            status, body = self.call(id="7", price="100", confidence="0.95",
                                     qty=raw)
            self.assertEqual(status, 400, raw)
            self.assertFalse(body["ok"])

    def test_accepted_confidence_values(self):
        self.write_auctions({"7": [[100, 3]]})
        self.write_tsm(region_rows=[(7, "M", 150, 140, 145, "0.4", "4.0", NOW)])
        for raw in ("0.9", "0.95", "0.99", "0.90"):
            result = self.call(id="7", price="100", confidence=raw)
            if isinstance(result, tuple):
                self.fail("unexpected %r for %r" % (result, raw))
            self.assertEqual(result["confidence"], float(raw))


class WarmingTest(QuantityRouteBase):
    def test_empty_cache_is_503_and_kicks_refresh(self):
        status, body = self.call(id="7", price="100", confidence="0.95")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "auction_data_warming_up")
        self.assertEqual(len(self.kicks), 1)

    def test_no_realm_count_file_needed(self):
        # write_auctions no longer writes a realm-count file - the ladder
        # alone must be enough (TSM demand is region-wide, no realm scaling)
        self.write_auctions({"7": [[100, 3]]})
        self.write_tsm(region_rows=[(7, "M", 150, 140, 145, "0.4", "4.0", NOW)])
        result = self.call(id="7", price="100", confidence="0.95")
        if isinstance(result, tuple):
            self.fail("expected 200 payload, got %r" % (result,))
        self.assertTrue(result["ok"])

    def test_stale_but_present_serves_data_and_kicks(self):
        self.write_auctions({"7": [[100, 3]]}, fetched_at="2026-01-01T00:00:00")
        self.write_tsm(region_rows=[(7, "M", 150, 140, 145, "0.4", "4.0", NOW)])
        result = self.call(id="7", price="100", confidence="0.95")
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.kicks), 1)


class CommodityAndSalesTest(QuantityRouteBase):
    def test_non_commodity_item_gets_commodities_only_message(self):
        self.write_auctions({"7": [[100, 3]]})
        self.write_tsm(region_rows=[(42, "Big Sword", 5000, 4800, 4900,
                                     "0.1", "0.5", NOW)])
        result = self.call(id="42", price="100", confidence="0.95")
        self.assertTrue(result["ok"])
        self.assertFalse(result["commodity"])
        self.assertEqual(result["name"], "Big Sword")
        self.assertEqual(result["message"],
                         "commodities only - no sold/day data for this item")

    def test_commodity_without_tsm_row_is_no_sales_data(self):
        self.write_auctions({"55": [[100, 3]]})
        self.write_tsm()
        result = self.call(id="55", price="100", confidence="0.95")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_sales_data")


class HappyPathTest(QuantityRouteBase):
    def setUp(self):
        super().setUp()
        self.write_auctions({"7": [[100, 4], [200, 8]]})
        self.write_tsm(
            region_rows=[(7, "Test Mat", 150, 140, 145, "0.4", "400.0", NOW)],
            commodities_rows=[(7, "Test Mat", 150, 150, 148, 140, NOW)])

    def test_full_shape_and_math_composition(self):
        result = self.call(id="7", price="150", confidence="0.95")
        self.assertTrue(result["ok"])
        self.assertTrue(result["commodity"])
        self.assertEqual(result["id"], 7)
        self.assertEqual(result["name"], "Test Mat")
        self.assertEqual(result["price"], 150)
        self.assertEqual(result["confidence"], 0.95)
        self.assertEqual(result["market_value"], 150)
        # region-wide TSM sold/day is the demand rate itself - no realm
        # multiply (the old sold_per_ah x realms inflation is gone)
        self.assertEqual(result["sold_per_day"], 400.0)
        self.assertNotIn("sold_per_ah", result)
        self.assertNotIn("realm_count", result)
        self.assertNotIn("lam_day", result)
        self.assertEqual(result["stretch"], 1.0)
        self.assertEqual(result["lam_eff"], 400.0)
        self.assertEqual(result["ahead_units"], 4)
        self.assertNotIn("same_price_units", result)
        self.assertNotIn("levels", result)
        self.assertIsNone(result["stack"])
        self.assertEqual(result["tsm_updated_at"], NOW)
        self.assertTrue(result["auctions_fetched_at"])
        self.assertFalse(result["refreshing"])
        lam = result["lam_eff"]
        ahead = result["ahead_units"]
        conf = result["confidence"]
        self.assertGreaterEqual(
            quantity.achieved_confidence(lam, ahead, result["max_qty"]),
            conf - 1e-9)
        self.assertLess(
            quantity.achieved_confidence(lam, ahead, result["max_qty"] + 1),
            conf)
        self.assertEqual(result["max_qty"],
                         result["max_total"] - ahead)
        json.dumps(result)

    def test_same_price_units_are_not_ahead(self):
        result = self.call(id="7", price="200", confidence="0.95")
        self.assertEqual(result["ahead_units"], 4)
        self.assertNotIn("same_price_units", result)
        json.dumps(result)

    def test_price_above_market_halves_demand(self):
        result = self.call(id="7", price="300", confidence="0.95")
        self.assertEqual(result["stretch"], 0.5)
        self.assertEqual(result["lam_eff"], 200.0)
        json.dumps(result)

    def test_stricter_confidence_never_recommends_more(self):
        loose = self.call(id="7", price="150", confidence="0.9")
        strict = self.call(id="7", price="150", confidence="0.99")
        self.assertLessEqual(strict["max_qty"], loose["max_qty"])
        self.assertEqual(strict["confidence"], 0.99)

    def test_zero_demand_returns_zero_and_json_safe(self):
        self.write_tsm(region_rows=[(7, "Test Mat", 150, 140, 145, "0.0",
                                     "0", NOW)])
        result = self.call(id="7", price="150", confidence="0.95")
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_qty"], 0)
        self.assertEqual(result["lam_eff"], 0.0)
        self.assertIsNone(result["expected_days"])
        text = json.dumps(result)
        self.assertNotIn("Infinity", text)
        self.assertNotIn("NaN", text)

    def test_ahead_beyond_demand_recommends_zero(self):
        self.write_auctions({"7": [[10, 99999]]})
        self.write_tsm(region_rows=[(7, "Test Mat", 150, 140, 145, "0.4",
                                     "0.01", NOW)])
        result = self.call(id="7", price="150", confidence="0.95")
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_qty"], 0)
        json.dumps(result)

    def test_lowest_price_present_but_levels_absent(self):
        result = self.call(id="7", price="150", confidence="0.95")
        self.assertEqual(result["lowest_price"], 100)
        self.assertNotIn("levels", result)
        json.dumps(result)

    def test_stack_block_for_your_quantity(self):
        result = self.call(id="7", price="150", confidence="0.95", qty="20")
        stack = result["stack"]
        self.assertEqual(stack["qty"], 20)
        self.assertGreaterEqual(stack["achieved"], 0.95 - 1e-9)
        self.assertGreater(stack["typical_days"], 0)
        self.assertLess(stack["typical_days"], 1)
        self.assertGreater(stack["days_95"], 0)
        self.assertLess(stack["days_95"], 1)
        json.dumps(result)

    def test_stack_on_slow_item_reports_low_confidence_and_days(self):
        # 0.5/day region demand: one unit is NOT 95% sold in 24h
        self.write_auctions({"7": [[100, 5]]})
        self.write_tsm(region_rows=[(7, "Test Mat", 150, 140, 145, "0.4",
                                     "0.5", NOW)])
        result = self.call(id="7", price="100", confidence="0.95", qty="1")
        stack = result["stack"]
        self.assertEqual(stack["qty"], 1)
        self.assertLess(stack["achieved"], 0.95)
        self.assertAlmostEqual(stack["typical_days"], 2.0, delta=0.01)
        self.assertAlmostEqual(stack["days_95"], math.log(20.0) / 0.5,
                               delta=0.05)
        text = json.dumps(result)
        self.assertNotIn("Infinity", text)

    def test_stack_zero_demand_is_null_days(self):
        self.write_tsm(region_rows=[(7, "Test Mat", 150, 140, 145, "0.0",
                                     "0", NOW)])
        result = self.call(id="7", price="150", confidence="0.95", qty="5")
        stack = result["stack"]
        self.assertEqual(stack["achieved"], 0.0)
        self.assertIsNone(stack["typical_days"])
        self.assertIsNone(stack["days_95"])
        text = json.dumps(result)
        self.assertNotIn("Infinity", text)


class DispatchTest(unittest.TestCase):
    def test_route_is_registered(self):
        self.assertIs(api.ROUTES.get("/api/item/quantity"),
                      api.item_quantity)


if __name__ == "__main__":
    unittest.main()
