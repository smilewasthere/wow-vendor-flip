"""M6 runtime settings: validation, persistence, cfg merge (app/settings.py).

M12: only Region and Icon host remain; removed keys raise on coerce/write
and are purged from the table at boot.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, settings


class CoerceTest(unittest.TestCase):
    def test_choices(self):
        self.assertEqual(settings.coerce("region", "us"), "us")
        self.assertEqual(settings.coerce("icon_host", "wowhead"), "wowhead")
        with self.assertRaises(ValueError):
            settings.coerce("region", "kr")
        with self.assertRaises(ValueError):
            settings.coerce("icon_host", "cdn.example.com")

    def test_bool_is_rejected(self):
        for key in ("region", "icon_host"):
            with self.assertRaises(ValueError):
                settings.coerce(key, True)

    def test_unknown_and_removed_keys_raise(self):
        for key in ("client_secret", "min_profit", "min_roi", "realm_slug",
                    "refresh_minutes", "ah_count", "daily_budget"):
            with self.assertRaises(ValueError):
                settings.coerce(key, "1")

    def test_non_string_choices_are_rejected(self):
        for raw in (5, 1.5, None, [], {}):
            with self.assertRaises(ValueError):
                settings.coerce("region", raw)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_write_read_roundtrip_and_none_deletes(self):
        settings.write(self.conn, {"region": "us", "icon_host": "wowhead"})
        stored = settings.stored(self.conn)
        self.assertEqual(stored["region"], "us")
        self.assertEqual(stored["icon_host"], "wowhead")
        settings.write(self.conn, {"region": None})
        self.assertNotIn("region", settings.stored(self.conn))
        self.assertIn("icon_host", settings.stored(self.conn))

    def test_write_rejects_bad_batch_before_touching_store(self):
        settings.write(self.conn, {"region": "us"})
        with self.assertRaises(ValueError):
            settings.write(self.conn, {"region": "kr", "icon_host": "wowhead"})
        stored = settings.stored(self.conn)
        self.assertNotIn("icon_host", stored)
        self.assertEqual(stored["region"], "us")

    def test_apply_layers_over_cfg(self):
        settings.write(self.conn, {"region": "us"})
        eff = settings.apply({"region": "eu", "port": 8008}, self.conn)
        self.assertEqual(eff["region"], "us")
        self.assertEqual(eff["port"], 8008)

    def test_corrupt_stored_value_is_skipped(self):
        self.conn.execute("INSERT INTO settings (key, value)"
                          " VALUES ('region', 'kr')")
        self.conn.commit()
        self.assertEqual(settings.stored(self.conn), {})

    def test_speclist_is_region_and_icon_host_only(self):
        self.assertEqual(set(settings.SPECS), {"region", "icon_host"})

    def test_removed_keys_are_purged_from_the_table(self):
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES"
            " ('min_profit', '999999'), ('realm_slug', 'twisting-nether'),"
            " ('refresh_minutes', '30'), ('region', 'us')")
        self.conn.commit()
        settings.purge_removed(self.conn)
        rows = dict(self.conn.execute("SELECT key, value FROM settings"))
        self.assertEqual(rows, {"region": "us"})


if __name__ == "__main__":
    unittest.main()
