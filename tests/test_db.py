import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "vendorflips.sqlite")
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()

    def test_connect_creates_file_and_init_creates_all_plan_tables(self):
        db.init_schema(self.conn)
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("meta", "candidate_items", "icon_cache", "settings",
                      "extraction_state"):
            self.assertIn(table, names)

    def test_init_schema_is_idempotent(self):
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("candidate_items", names)

    def test_candidate_row_exposes_the_fields_the_plan_lists(self):
        db.init_schema(self.conn)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(candidate_items)")}
        for column in ("id", "name", "purchase_price", "purchase_quantity",
                       "sell_price", "quality", "is_equippable", "item_class",
                       "item_subclass", "synthetic_multiple", "vendor_verified",
                       "excluded", "flagged_risky", "notes"):
            self.assertIn(column, columns)


class StateTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_missing_key_returns_default(self):
        self.assertEqual(db.state_get(self.conn, "cursor", "0"), "0")
        self.assertIsNone(db.state_get(self.conn, "cursor"))

    def test_set_then_get_roundtrip(self):
        db.state_set(self.conn, "cursor", "12345")
        self.conn.commit()
        self.assertEqual(db.state_get(self.conn, "cursor"), "12345")

    def test_state_dict_returns_all_keys(self):
        db.state_bulk(self.conn, {"phase": "scanning", "cursor": "10"})
        self.conn.commit()
        self.assertEqual(db.state_dict(self.conn),
                         {"phase": "scanning", "cursor": "10"})


class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def row(self, item_id=100, **overrides):
        base = {
            "id": item_id, "name": "Thing", "purchase_price": 500,
            "purchase_quantity": 1, "sell_price": 100, "quality": "COMMON",
            "is_equippable": 0, "item_class": "Tradeskill",
            "item_subclass": "Other", "synthetic_multiple": 1,
            "extracted_at": "2026-10-12T00:00:00",
        }
        base.update(overrides)
        return base

    def test_upsert_inserts_and_counts(self):
        db.upsert_candidate(self.conn, self.row())
        db.upsert_candidate(self.conn, self.row(item_id=200))
        self.conn.commit()
        self.assertEqual(db.count_candidates(self.conn), 2)
        self.assertEqual(db.candidate_ids(self.conn), {100, 200})

    def test_upsert_refreshes_extracted_fields(self):
        db.upsert_candidate(self.conn, self.row(purchase_price=500))
        db.upsert_candidate(self.conn, self.row(purchase_price=9999, name="New Name"))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT name, purchase_price FROM candidate_items WHERE id=100").fetchone()
        self.assertEqual(row[0], "New Name")
        self.assertEqual(row[1], 9999)

    def test_reextract_preserves_manual_flags(self):
        db.upsert_candidate(self.conn, self.row())
        self.conn.execute(
            "UPDATE candidate_items SET vendor_verified=1, excluded=1, "
            "flagged_risky=1, notes='saw it' WHERE id=100")
        db.upsert_candidate(self.conn, self.row(purchase_price=777))
        row = self.conn.execute(
            "SELECT purchase_price, vendor_verified, excluded, flagged_risky, notes "
            "FROM candidate_items WHERE id=100").fetchone()
        self.assertEqual(row, (777, 1, 1, 1, "saw it"))

    def test_set_vendor_verified_flips_only_that_column(self):
        db.upsert_candidate(self.conn, self.row())
        self.conn.execute(
            "UPDATE candidate_items SET excluded=1, flagged_risky=1, "
            "notes='keep me' WHERE id=100")
        self.conn.commit()
        self.assertTrue(db.set_vendor_verified(self.conn, 100, True))
        row = self.conn.execute(
            "SELECT vendor_verified, excluded, flagged_risky, notes "
            "FROM candidate_items WHERE id=100").fetchone()
        self.assertEqual(row, (1, 1, 1, "keep me"))
        self.assertTrue(db.set_vendor_verified(self.conn, 100, False))
        row = self.conn.execute(
            "SELECT vendor_verified, excluded, flagged_risky, notes "
            "FROM candidate_items WHERE id=100").fetchone()
        self.assertEqual(row, (0, 1, 1, "keep me"))

    def test_set_vendor_verified_unknown_id_returns_false(self):
        self.assertFalse(db.set_vendor_verified(self.conn, 424242, True))


class VendorSourceTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_table_exists_and_idempotent(self):
        db.init_schema(self.conn)
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='vendor_sources'").fetchall()
        self.assertEqual(len(rows), 1)

    def test_upsert_and_map_roundtrip(self):
        db.upsert_vendor_source(
            self.conn, 42, 1, [{"id": 958, "name": "Vendor"}], 150,
            "2026-10-14T00:00:00Z")
        m = db.vendor_source_map(self.conn, [42])
        self.assertEqual(m[42]["sold"], 1)
        self.assertEqual(m[42]["npcs"][0]["name"], "Vendor")
        self.assertEqual(m[42]["wowhead_cost"], 150)

    def test_upsert_overwrites(self):
        db.upsert_vendor_source(self.conn, 7, 1, [], None, "2026-10-14T00:00:00Z")
        db.upsert_vendor_source(
            self.conn, 7, 0, [], None, "2026-10-15T00:00:00Z")
        m = db.vendor_source_map(self.conn)
        self.assertEqual(m[7]["sold"], 0)
        self.assertEqual(m[7]["checked_at"], "2026-10-15T00:00:00Z")

    def test_stats(self):
        db.upsert_vendor_source(self.conn, 1, 1, [], 10, "t")
        db.upsert_vendor_source(self.conn, 2, 0, [], None, "t")
        db.upsert_vendor_source(self.conn, 3, 0, [], None, "t", status="blocked")
        s = db.vendor_sources_stats(self.conn)
        self.assertEqual(s, {"sold": 1, "not_sold": 1, "blocked": 1,
                             "error": 0, "total": 3})


if __name__ == "__main__":
    unittest.main()
