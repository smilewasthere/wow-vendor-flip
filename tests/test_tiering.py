"""M4: vendor tiering per PLAN section 5.3 (soft hints only, never exclude)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import tiering


class VendorTierTest(unittest.TestCase):
    def row(self, **kw):
        base = {
            "purchase_price": 100,
            "synthetic_multiple": 0,
            "is_equippable": 0,
            "item_class": "Tradeskill",
        }
        base.update(kw)
        return base

    def test_likely_when_no_soft_hints(self):
        out = tiering.vendor_tier(self.row())
        self.assertEqual(out["tier"], "likely")
        self.assertEqual(out["chips"], [])

    def test_unverified_on_synthetic_multiple(self):
        out = tiering.vendor_tier(self.row(synthetic_multiple=1))
        self.assertEqual(out["tier"], "unverified")
        self.assertTrue(any("4-5" in chip for chip in out["chips"]),
                        out["chips"])

    def test_unverified_when_equippable(self):
        out = tiering.vendor_tier(self.row(is_equippable=1))
        self.assertEqual(out["tier"], "unverified")
        self.assertTrue(any("equippable" in chip for chip in out["chips"]),
                        out["chips"])

    def test_unverified_when_class_outside_whitelist(self):
        out = tiering.vendor_tier(self.row(item_class="Armor"))
        self.assertEqual(out["tier"], "unverified")
        self.assertTrue(any("Armor" in chip for chip in out["chips"]),
                        out["chips"])

    def test_all_three_hints_listed(self):
        out = tiering.vendor_tier(self.row(synthetic_multiple=1,
                                           is_equippable=1,
                                           item_class="Weapon"))
        self.assertEqual(out["tier"], "unverified")
        self.assertEqual(len(out["chips"]), 3)

    def test_unknown_price_when_purchase_is_one(self):
        out = tiering.vendor_tier(self.row(purchase_price=1,
                                           synthetic_multiple=1,
                                           is_equippable=1))
        self.assertEqual(out["tier"], "unknown-price")

    def test_no_price_when_purchase_zero(self):
        out = tiering.vendor_tier(self.row(purchase_price=0))
        self.assertEqual(out["tier"], "no-price")

    def test_vendor_goods_classes_get_no_class_chip(self):
        for cls in ("Tradeskill", "Consumable", "Miscellaneous", "Recipe",
                    "Profession", "Glyph", "Container", "Gem",
                    "Item Enhancement", "Housing"):
            out = tiering.vendor_tier(self.row(item_class=cls))
            self.assertEqual(out["tier"], "likely", cls)
            self.assertEqual(out["chips"], [], cls)

    def test_gear_and_quest_classes_chip_outside_whitelist(self):
        for cls in ("Armor", "Weapon", "Quest"):
            out = tiering.vendor_tier(self.row(item_class=cls))
            self.assertEqual(out["tier"], "unverified", cls)
            self.assertTrue(any(cls in chip for chip in out["chips"]), cls)

    def test_empty_item_class_gets_no_class_chip(self):
        out = tiering.vendor_tier(self.row(item_class=""))
        self.assertEqual(out["tier"], "likely")
        self.assertEqual(out["chips"], [])

    def test_accepts_sqlite_row_style_mapping(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT 100 AS purchase_price, 0 AS synthetic_multiple,"
                " 0 AS is_equippable, 'Tradeskill' AS item_class").fetchone()
            out = tiering.vendor_tier(row)
            self.assertEqual(out["tier"], "likely")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
