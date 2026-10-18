"""M5 pricing engine: PLAN section 5 - values, suspicion, verdicts.

M12: realm table dropped. Pool = region + commodities values; thresholds
come from config/defaults only (settings table no longer layers in).
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import pricing, tsm

REGION_HEADER = ("itemId,name,marketValue,historical,avgSalePrice,saleRate,"
                 "soldPerDay,updatedAt\n")
COMM_HEADER = ("itemId,name,marketValue,minBuyout,recent,historical,"
               "updatedAt\n")
STAMP = "2026-10-12T02:47:41Z"


def candidate(**kw):
    base = {
        "id": 1, "name": "Thing", "purchase_price": 10000,
        "purchase_quantity": 1, "sell_price": 1000, "quality": "common",
        "is_equippable": 0, "item_class": "Tradeskill",
        "item_subclass": "", "synthetic_multiple": 0,
        "vendor_verified": 0, "excluded": 0, "flagged_risky": 0,
        "notes": "",
    }
    base.update(kw)
    return base


class PricingBase(unittest.TestCase):
    """Fixture: tiny TSM cache in a tmp dir + a context built from it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowvf-m5-")
        self.region = {}       # itemId -> (mv, hist, avgSale, saleRate, sold)
        self.commodities = {}  # itemId -> (mv, minBuyout, recent, hist)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self):
        with open(os.path.join(self.tmp, "region-items.csv"),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(REGION_HEADER)
            for item_id, (mv, hist, avg, rate, sold) in self.region.items():
                fh.write("%d,Name%d,%d,%d,%d,%s,%s,%s\n"
                         % (item_id, item_id, mv, hist, avg, rate, sold,
                            STAMP))
        with open(os.path.join(self.tmp, "commodities.csv"),
                  "w", encoding="utf-8", newline="") as fh:
            fh.write(COMM_HEADER)
            for item_id, (mv, mb, rec, hist) in self.commodities.items():
                fh.write("%d,Name%d,%d,%d,%d,%d,%s\n"
                         % (item_id, item_id, mv, mb, rec, hist, STAMP))
        return pricing.context({}, self.tmp)

    def happy_tables(self, item_id=1):
        """Region row that passes every suspicion rule cleanly."""
        self.region[item_id] = (70000, 70000, 70000, 0.5, 100)


class ContextTest(PricingBase):
    def test_indexes_rows_by_item_id(self):
        self.happy_tables(7)
        self.commodities[7] = (50000, 40000, 50000, 45000)
        ctx = self.build()
        self.assertEqual(ctx["region"][7]["marketValue"], 70000)
        self.assertEqual(ctx["commodities"][7]["historical"], 45000)
        self.assertIn(7, ctx["commodity_ids"])
        self.assertNotIn("realm", ctx)

    def test_context_cached_until_a_file_changes(self):
        self.happy_tables(7)
        ctx1 = self.build()
        ctx2 = pricing.context({}, self.tmp)
        self.assertIs(ctx1, ctx2)
        # bump mtime explicitly (mtime resolution safety)
        self.region[7] = (80000, 80000, 80000, 0.5, 100)
        self.build()
        path = os.path.join(self.tmp, "region-items.csv")
        future = time.time() + 5
        os.utime(path, (future, future))
        ctx3 = pricing.context({}, self.tmp)
        self.assertIsNot(ctx1, ctx3)
        self.assertEqual(ctx3["region"][7]["marketValue"], 80000)


class SellPriceTest(PricingBase):
    def test_min_positive_of_pool_values(self):
        # pool = region (mv, hist) + commodity (mv, hist); min positive wins
        self.region[1] = (70000, 60000, 70000, 0.5, 100)
        self.commodities[1] = (55000, 0, 55000, 50000)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx, pricing.default_thresholds())
        self.assertEqual(out["sell_price"], 50000)

    def test_commodity_values_join_the_pool(self):
        self.happy_tables(1)
        self.commodities[1] = (50000, 40000, 50000, 42000)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx, pricing.default_thresholds())
        self.assertEqual(out["sell_price"], 42000)
        self.assertTrue(out["commodity"])

    def test_min_buyout_is_never_used(self):
        self.happy_tables(1)
        self.commodities[1] = (70000, 1, 70000, 70000)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx, pricing.default_thresholds())
        self.assertEqual(out["sell_price"], 70000)

    def test_no_tsm_rows_means_no_ah_price(self):
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx, pricing.default_thresholds())
        self.assertEqual(out["sell_price"], 0)
        self.assertEqual(out["verdict"], "NOT WORTH IT")
        self.assertIn("no AH price data", out["reason"])


class DerivedValuesTest(PricingBase):
    def setUp(self):
        super().setUp()
        self.happy_tables(1)

    def test_vendor_cost_unit_divides_by_quantity(self):
        ctx = self.build()
        out = pricing.score_item(
            candidate(purchase_price=1000, purchase_quantity=4),
            ctx, pricing.default_thresholds())
        self.assertEqual(out["vendor_cost_unit"], 250)

    def test_zero_quantity_is_treated_as_one(self):
        ctx = self.build()
        out = pricing.score_item(
            candidate(purchase_price=1000, purchase_quantity=0),
            ctx, pricing.default_thresholds())
        self.assertEqual(out["vendor_cost_unit"], 1000)

    def test_units_cap_wins_when_budget_is_tiny(self):
        ctx = self.build()
        # budget 1 gold = 10000copper -> cap = 10000/10000 = 1/day
        th = dict(pricing.default_thresholds(), daily_budget=1)
        out = pricing.score_item(candidate(), ctx, th)
        self.assertAlmostEqual(out["expected_sold_per_day"], 1.0)

    def test_units_cap_budget_is_gold_converted_to_copper(self):
        ctx = self.build()
        # default budget 50000 gold, cost 10000c -> cap 50000, volume wins
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertAlmostEqual(out["expected_sold_per_day"], 2.0)

    def test_non_commodity_volume_divides_by_ah_count(self):
        ctx = self.build()
        th = dict(pricing.default_thresholds(), ah_count=2)
        out = pricing.score_item(candidate(), ctx, th)
        # (100 / 2) * 0.02 = 1.0 (commodity path would give 2.0)
        self.assertAlmostEqual(out["expected_sold_per_day"], 1.0)

    def test_commodity_volume_not_divided(self):
        self.commodities[1] = (70000, 70000, 70000, 70000)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertAlmostEqual(out["expected_sold_per_day"], 2.0)

    def test_confidence_clamps(self):
        cases = [(0.0, 0.2), (0.05, 0.5), (0.1, 1.0), (9.0, 1.0)]
        for rate, want in cases:
            self.region[1] = (70000, 70000, 70000, rate, 100)
            ctx = self.build()
            out = pricing.score_item(candidate(), ctx,
                                     pricing.default_thresholds())
            self.assertAlmostEqual(out["confidence"], want, msg=str(rate))

    def test_score_is_net_times_expected_times_confidence(self):
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        # pinned literals (not self-referential): sell 70000 * 0.95 - 10000
        # = 56500; volume 100 * 0.02 = 2 (cap 50000g -> 500000000c);
        # rate 0.5 -> conf 1.0; score 56500 * 2 * 1.0 = 113000
        self.assertAlmostEqual(out["net_profit_unit"], 56500.0)
        self.assertAlmostEqual(out["expected_sold_per_day"], 2.0)
        self.assertAlmostEqual(out["confidence"], 1.0)
        self.assertAlmostEqual(out["score"], 113000.0)


class SuspicionCommodityTest(PricingBase):
    def commodity(self, mv, hist, avg, rate, sold, recent):
        self.region[1] = (mv, hist, avg, rate, sold)
        self.commodities[1] = (mv, recent, recent, hist)

    def test_rule1_market_value_spike_flags_and_falls_back_to_region(self):
        # commodity MV 200k vs hist 100k -> > 1.75x; fallback = region pair
        self.commodity(200000, 100000, 100000, 0.5, 100, 200000)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertTrue(out["suspicious"])
        self.assertIn("far above 60-day history", out["suspicious_reason"])
        # fallback = min positive region values (mv 200000, hist 100000)
        self.assertEqual(out["sell_price"], 100000)

    def test_rule2_listed_above_real_sale_prices(self):
        self.commodity(60000, 60000, 30000, 0.5, 100, 60000)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertTrue(out["suspicious"])
        self.assertIn("real sale prices", out["suspicious_reason"])

    def test_rule3_sharp_recent_move(self):
        self.commodity(100000, 100000, 100000, 0.5, 100, 100)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertTrue(out["suspicious"])
        self.assertIn("moved sharply", out["suspicious_reason"])

    def test_rule4_no_sales(self):
        self.commodity(70000, 70000, 70000, 0.5, 0, 70000)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertTrue(out["suspicious"])
        self.assertIn("no sales", out["suspicious_reason"])

    def test_rule5_undercut_volume(self):
        self.commodity(70000, 70000, 70000, 0.01, 25, 70000)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertTrue(out["suspicious"])
        self.assertIn("undercut", out["suspicious_reason"])

    def test_clean_commodity_is_not_flagged(self):
        self.commodity(70000, 70000, 70000, 0.5, 100, 70000)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertFalse(out["suspicious"])

    def test_suspicious_commodity_without_region_has_no_sell_price(self):
        self.commodity(200000, 100000, 100000, 0.5, 100, 200000)
        del self.region[1]
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        # no region fallback exists -> no trustworthy sell price (rule 2),
        # NOT the spiky commodity marketValue
        self.assertTrue(out["suspicious"])
        self.assertEqual(out["sell_price"], 0)
        self.assertEqual(out["verdict"], "NOT WORTH IT")
        self.assertIn("no AH price data", out["reason"])


class SuspicionNonCommodityTest(PricingBase):
    """M12: non-commodities only carry region rows; rules 4/5 still apply."""

    def test_no_sales_flags(self):
        self.region[1] = (70000, 70000, 70000, 0.5, 0)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertTrue(out["suspicious"])
        self.assertIn("no sales", out["suspicious_reason"])

    def test_undercut_volume_flags(self):
        self.region[1] = (70000, 70000, 70000, 0.01, 25)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertTrue(out["suspicious"])
        self.assertIn("undercut", out["suspicious_reason"])

    def test_fractional_sold_per_day_is_not_no_sales(self):
        self.region[1] = (70000, 70000, 70000, 0.5, 0.75)
        ctx = self.build()
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        # TSM soldPerDay is a float; int() truncation would fake "no sales"
        self.assertAlmostEqual(out["sold_per_day"], 0.75)
        self.assertFalse(out["suspicious"])
        self.assertAlmostEqual(out["expected_sold_per_day"], 0.015)


class VerdictTest(PricingBase):
    """First-match matrix for all 13 rules (PLAN section 5.6)."""

    def happy(self):
        self.happy_tables(1)

    def score(self, cand=None, th=None, ctx=None):
        ctx = ctx if ctx is not None else self.build()
        return pricing.score_item(cand or candidate(), ctx,
                                  th or pricing.default_thresholds())

    def test_rule1_excluded(self):
        self.happy()
        out = self.score(candidate(excluded=1))
        self.assertEqual(out["verdict"], "NOT WORTH IT")
        self.assertIn("excluded by you", out["reason"])

    def test_rule2_no_ah_price(self):
        out = self.score()  # no TSM rows at all
        self.assertIn("no AH price data", out["reason"])

    def test_rule3_no_buy_price(self):
        self.happy()
        out = self.score(candidate(purchase_price=0))
        self.assertEqual(out["verdict"], "NOT WORTH IT")
        self.assertIn("no buy price", out["reason"])

    def test_rule4_price_of_one_is_check_in_game(self):
        self.happy()
        out = self.score(candidate(purchase_price=1))
        self.assertEqual(out["verdict"], "CHECK IN GAME")

    def test_rule5_sells_for_less_than_vendor_price(self):
        self.happy()
        out = self.score(candidate(purchase_price=66500))
        # sell 70000 * 0.95 = 66500 -> net 0
        self.assertEqual(out["verdict"], "NOT WORTH IT")
        self.assertIn("less than the vendor price", out["reason"])

    def test_rule6_profit_below_min(self):
        self.region[1] = (60000, 60000, 60000, 0.5, 100)
        out = self.score(candidate(purchase_price=50000))
        self.assertEqual(out["verdict"], "NOT WORTH IT")
        self.assertIn("profit too low", out["reason"])

    def test_rule7_roi_below_min(self):
        self.region[1] = (270000, 270000, 270000, 0.5, 100)
        out = self.score(candidate(purchase_price=200000))
        # net 56500 (>= 5g) but roi 0.2825 < 0.30
        self.assertEqual(out["verdict"], "NOT WORTH IT")
        self.assertIn("return too low", out["reason"])

    def test_rule8_too_slow(self):
        self.region[1] = (70000, 70000, 70000, 0.5, 10)
        out = self.score()
        self.assertEqual(out["verdict"], "NOT WORTH IT")
        self.assertIn("sells too slowly", out["reason"])

    def test_rule9_suspicious_is_risky(self):
        # commodity spike (mv 300k vs hist 70k) is the M12 suspicion path
        self.region[1] = (70000, 70000, 70000, 0.5, 100)
        self.commodities[1] = (300000, 70000, 300000, 70000)
        out = self.score()
        self.assertEqual(out["verdict"], "RISKY")
        self.assertIn("far above 60-day history", out["reason"])

    def test_rule10_low_sale_rate_is_risky(self):
        # reachable when min_sold_per_day is loose enough that rule 8
        # does not shadow it (rule 5 needs sold > 20; rule 8 needs >= 25)
        self.region[1] = (70000, 70000, 70000, 0.01, 10)
        th = dict(pricing.default_thresholds(), min_sold_per_day=0.01)
        out = self.score(th=th)
        self.assertEqual(out["verdict"], "RISKY")
        self.assertIn("very low sale rate", out["reason"])

    def test_rule11_manual_risky_flag(self):
        self.happy()
        out = self.score(candidate(flagged_risky=1, notes="check forum"))
        self.assertEqual(out["verdict"], "RISKY")
        self.assertIn("your note: check forum", out["reason"])

    def test_rule12_unverified_is_check_vendor_with_chips(self):
        self.happy()
        out = self.score(candidate(synthetic_multiple=1))
        self.assertEqual(out["verdict"], "CHECK VENDOR")
        self.assertIn("confirm a vendor", out["reason"])
        self.assertIn("4-5", out["reason"])
        self.assertEqual(len(out["chips"]), 1)

    def test_rule13_verified_is_good(self):
        self.happy()
        out = self.score(candidate(vendor_verified=1))
        self.assertEqual(out["verdict"], "GOOD")
        self.assertIn("profit", out["reason"])


class BoundaryTest(PricingBase):
    """Equality at a rule boundary must NOT fire the rule (strict <)."""

    def happy(self):
        self.happy_tables(1)

    def score(self, cand=None, th=None):
        return pricing.score_item(cand or candidate(), self.build(),
                                  th or pricing.default_thresholds())

    def test_net_equal_to_min_profit_passes_rule6(self):
        self.happy()
        out = self.score(candidate(purchase_price=16500))
        # net = 70000 * 0.95 - 16500 = 50000 -> exactly 5.00 gold
        self.assertAlmostEqual(out["net_profit_unit"], 50000.0)
        self.assertEqual(out["verdict"], "CHECK VENDOR")

    def test_roi_equal_to_min_roi_passes_rule7(self):
        self.happy()
        base = self.score()
        th = dict(pricing.default_thresholds(), min_roi=base["roi"])
        out = self.score(th=th)
        self.assertEqual(out["verdict"], "CHECK VENDOR")

    def test_expected_equal_to_min_sold_passes_rule8(self):
        self.happy()
        base = self.score()
        th = dict(pricing.default_thresholds(),
                  min_sold_per_day=base["expected_sold_per_day"])
        out = self.score(th=th)
        self.assertEqual(out["verdict"], "CHECK VENDOR")

    def test_sale_rate_equal_to_min_rate_passes_rule10(self):
        self.region[1] = (70000, 70000, 70000, 0.02, 100)
        th = dict(pricing.default_thresholds(), min_sold_per_day=0.01)
        out = self.score(th=th)
        # undercut rule 5 needs rate < 0.02; rule 10 needs rate < min
        self.assertFalse(out["suspicious"])
        self.assertEqual(out["verdict"], "CHECK VENDOR")

    def test_mv_equal_to_1_75_hist_not_flagged(self):
        self.region[1] = (175000, 100000, 175000, 0.5, 100)
        self.commodities[1] = (175000, 175000, 175000, 100000)
        out = self.score()
        self.assertTrue(out["commodity"])
        self.assertFalse(out["suspicious"])

    def test_sold_equal_20_not_undercut_flagged(self):
        self.region[1] = (70000, 70000, 70000, 0.01, 20)
        out = self.score()
        # undercut rule needs sold > 20; 20 exactly stays clean
        self.assertFalse(out["suspicious"])


class ThresholdsTest(PricingBase):
    def test_defaults_match_the_plan(self):
        th = pricing.default_thresholds()
        self.assertEqual(th["min_profit"], 5.0)
        self.assertEqual(th["min_roi"], 0.30)
        self.assertEqual(th["min_sold_per_day"], 0.5)
        self.assertEqual(th["min_sale_rate"], 0.02)
        self.assertEqual(th["commodity_share"], 0.02)
        self.assertEqual(th["daily_budget"], 50000)
        self.assertEqual(th["ah_count"], 1)

    def test_cfg_overrides_defaults(self):
        th = pricing.thresholds({"min_profit": 9.0, "ah_count": 3})
        self.assertEqual(th["min_profit"], 9.0)
        self.assertEqual(th["ah_count"], 3)
        self.assertEqual(th["min_roi"], 0.30)

    def test_settings_table_is_ignored(self):
        # M12: the settings table never stores thresholds anymore; even if
        # a stale row survived, thresholds() must not read the table.
        import sqlite3
        from app import db
        conn = sqlite3.connect(":memory:")
        db.init_schema(conn)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES"
            " ('min_profit', '999999'), ('ah_count', '3')")
        conn.commit()
        th = pricing.thresholds({"min_profit": 5.0, "ah_count": 1}, conn)
        conn.close()
        self.assertEqual(th["min_profit"], 5.0)
        self.assertEqual(th["ah_count"], 1)

    def test_non_finite_cfg_values_fall_back_to_defaults(self):
        th = pricing.thresholds({"min_profit": float("nan"),
                                 "min_roi": float("inf"),
                                 "ah_count": float("nan")})
        self.assertEqual(th["min_profit"], 5.0)
        self.assertEqual(th["min_roi"], 0.30)
        self.assertEqual(th["ah_count"], 1)


class VendorSourceVerdictTest(PricingBase):
    """M9: vendor_sources folds into verdicts (NOT VENDOR-SOLD, GOOD)."""

    def happy(self):
        self.happy_tables(1)

    def score(self, vsrc, cand=None, th=None):
        self.happy()
        ctx = self.build()
        if vsrc is not None:
            ctx["vendor_sources"] = vsrc
        return pricing.score_item(cand or candidate(), ctx,
                                  th or pricing.default_thresholds())

    def test_vendor_sold_zero_is_not_vendor_sold_with_zero_score(self):
        out = self.score({1: {"sold": 0, "npcs": [], "status": "ok",
                              "wowhead_cost": None,
                              "checked_at": "2026-10-14T00:00:00Z"}})
        self.assertEqual(out["verdict"], "NOT VENDOR-SOLD")
        self.assertIn("no vendor sells", out["reason"])
        self.assertEqual(out["score"], 0)
        self.assertEqual(out["vendor_sold"], 0)

    def test_vendor_sold_one_auto_promotes_to_good(self):
        out = self.score({1: {"sold": 1, "status": "ok",
                              "npcs": [{"id": 958, "name": "Tharynn Bouden"}],
                              "wowhead_cost": 320,
                              "checked_at": "2026-10-14T00:00:00Z"}})
        self.assertEqual(out["verdict"], "GOOD")
        self.assertIn("sold by", out["reason"])
        self.assertIn("Tharynn Bouden", out["reason"])
        self.assertEqual(out["vendor_sold"], 1)
        self.assertEqual(out["vendor_npcs"][0]["name"], "Tharynn Bouden")
        self.assertEqual(out["vendor_checked_at"], "2026-10-14T00:00:00Z")

    def test_unknown_vendor_stays_check_vendor(self):
        out = self.score(None)
        self.assertEqual(out["verdict"], "CHECK VENDOR")
        self.assertIsNone(out["vendor_sold"])
        self.assertEqual(out["vendor_npcs"], [])
        self.assertIsNone(out["vendor_checked_at"])

    def test_blocked_or_error_rows_are_unknown(self):
        out = self.score({1: {"sold": 0, "npcs": [], "status": "blocked",
                              "wowhead_cost": None,
                              "checked_at": "2026-10-14T00:00:00Z"}})
        self.assertEqual(out["verdict"], "CHECK VENDOR")
        self.assertIsNone(out["vendor_sold"])

    def test_manual_verified_still_good_without_vendor_row(self):
        out = self.score(None, cand=candidate(vendor_verified=1))
        self.assertEqual(out["verdict"], "GOOD")

    def test_risky_beats_vendor_sold_one(self):
        self.region[1] = (70000, 70000, 70000, 0.5, 100)
        self.commodities[1] = (300000, 70000, 300000, 70000)
        ctx = self.build()
        ctx["vendor_sources"] = {
            1: {"sold": 1, "npcs": [{"id": 9, "name": "Vendor"}],
                "status": "ok", "wowhead_cost": None,
                "checked_at": "2026-10-14T00:00:00Z"}}
        out = pricing.score_item(candidate(), ctx,
                                 pricing.default_thresholds())
        self.assertEqual(out["verdict"], "RISKY")

    def test_cost_mismatch_chip_over_ten_percent(self):
        out = self.score(
            {1: {"sold": 1, "npcs": [{"id": 9, "name": "Vendor"}],
                 "status": "ok", "wowhead_cost": 14500,
                 "checked_at": "2026-10-14T00:00:00Z"}},
            cand=candidate(purchase_price=16500))
        self.assertIn("cost mismatch", out["chips"])

    def test_small_cost_difference_is_not_a_mismatch(self):
        out = self.score(
            {1: {"sold": 1, "npcs": [{"id": 9, "name": "Vendor"}],
                 "status": "ok", "wowhead_cost": 10500,
                 "checked_at": "2026-10-14T00:00:00Z"}})
        self.assertNotIn("cost mismatch", out["chips"])


class EventVendorFoldTest(PricingBase):
    """M10: event/DEV vendors (Herbataur & co) do not vouch for items."""

    HERBATAUR = {"id": 224342, "name": "Herbataur"}
    THARYNN = {"id": 958, "name": "Tharynn Bouden"}

    def score(self, npcs, cand=None):
        self.happy_tables(1)
        ctx = self.build()
        ctx["vendor_sources"] = {
            1: {"sold": 1, "npcs": npcs, "status": "ok",
                "wowhead_cost": 320,
                "checked_at": "2026-10-15T00:00:00Z"}}
        return pricing.score_item(cand or candidate(), ctx,
                                  pricing.default_thresholds())

    def test_event_vendor_ids_cover_the_verified_block_list(self):
        expected = {224342, 60977, 223884, 234555,
                    219033, 220359, 220360, 220438, 222186, 185861}
        self.assertEqual(set(pricing.EVENT_VENDOR_IDS), expected)

    def test_sole_event_vendor_is_not_vendor_sold(self):
        out = self.score([self.HERBATAUR])
        self.assertEqual(out["verdict"], "NOT VENDOR-SOLD")
        self.assertIn("event/DEV vendors", out["reason"])
        self.assertEqual(out["vendor_sold"], 0)
        self.assertEqual(out["vendor_npcs"], [])
        self.assertEqual(out["score"], 0)

    def test_mixed_event_and_real_keeps_only_the_real_npc(self):
        out = self.score([self.HERBATAUR, self.THARYNN])
        self.assertEqual(out["verdict"], "GOOD")
        self.assertEqual(out["vendor_sold"], 1)
        self.assertEqual(out["vendor_npcs"], [self.THARYNN])
        self.assertIn("Tharynn Bouden", out["reason"])
        self.assertNotIn("Herbataur", out["reason"])

    def test_plain_real_vendor_is_untouched(self):
        out = self.score([self.THARYNN])
        self.assertEqual(out["verdict"], "GOOD")
        self.assertEqual(out["vendor_npcs"], [self.THARYNN])
        self.assertIn("sold by Tharynn Bouden", out["reason"])


if __name__ == "__main__":
    unittest.main()
