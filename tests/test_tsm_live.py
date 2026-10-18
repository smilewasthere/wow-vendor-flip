"""Live check against TSM public data: real counts, comma names, then 304s.

Opt-in: set WOWVF_LIVE=1 to run (real download); otherwise the class skips
so the default suite stays hermetic (zero real network). When run, skips
only if the host is unreachable; any HTTP error still fails the test.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, tsm

# Floors a few percent under the counts measured 2026-10-12 (31145 / 12149),
# so ordinary data churn does not break the suite but a truncated file does.
# M12: realm CSV dropped - only region + commodities download.
FLOORS = {"region": 30000, "commodities": 11000}


class TsmLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("WOWVF_LIVE") != "1":
            raise unittest.SkipTest(
                "live TSM tests are opt-in: set WOWVF_LIVE=1")
        cls.dir = tempfile.mkdtemp(prefix="vf-tsm-")
        cls.logs = []
        try:
            cls.first = tsm.download(config.defaults(), cls.dir, log=cls.logs.append)
        except tsm.TsmError as exc:
            shutil.rmtree(cls.dir, ignore_errors=True)
            if str(exc).startswith("cannot reach"):
                raise unittest.SkipTest("TSM unreachable: %s" % exc)
            raise
        cls.tables = {key: tsm.parse_table(tsm.read_cached(cls.dir, key), key)
                      for key in tsm.KEYS}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_all_files_download_with_plausible_counts(self):
        for key, floor in FLOORS.items():
            with self.subTest(key=key):
                self.assertEqual(self.first[key]["status"], 200)
                self.assertTrue(self.first[key]["changed"])
                self.assertGreaterEqual(self.first[key]["rows"], floor)
                self.assertEqual(self.first[key]["rows"],
                                 len(self.tables[key]))

    def test_region_contains_known_item_with_sales(self):
        by_id = {row["itemId"]: row for row in self.tables["region"]}
        flour = by_id.get(30817)
        self.assertIsNotNone(flour, "Simple Flour missing from region file")
        self.assertEqual(flour["name"], "Simple Flour")
        self.assertGreater(flour["marketValue"], 0)
        self.assertGreater(flour["soldPerDay"], 0)
        self.assertGreaterEqual(flour["saleRate"], 0)

    def test_unquoted_commas_in_names_survive(self):
        for key in ("region", "commodities"):
            with self.subTest(key=key):
                comma_rows = [r for r in self.tables[key] if "," in r["name"]]
                self.assertGreaterEqual(
                    len(comma_rows), 5,
                    "%s: expected TSM's unquoted-comma rows, got %d"
                    % (key, len(comma_rows)))

    def test_rows_all_have_ids_and_names_are_usually_present(self):
        # TSM itself ships some rows with an empty name (raw line "271434,,...");
        # those items get their name from Blizzard's item API in M4.
        for key in tsm.KEYS:
            with self.subTest(key=key):
                rows = self.tables[key]
                no_id = [r["itemId"] for r in rows if r["itemId"] <= 0]
                self.assertEqual(no_id[:3], [], "%s: %d rows without an itemId"
                                 % (key, len(no_id)))
                empty = sum(1 for r in rows if not r["name"].strip())
                self.assertLess(
                    empty, len(rows) * 0.03,
                    "%s: %d/%d rows without a name looks like a parse failure"
                    % (key, empty, len(rows)))

    def test_commodity_classification_rule_holds(self):
        commodity_ids = {r["itemId"] for r in self.tables["commodities"]}
        self.assertIn(180445, commodity_ids, "Skystrider Glider is a commodity")
        self.assertNotIn(2587, commodity_ids, "Gray Woolen Shirt is gear, not a commodity")

    def test_second_run_is_304_for_every_file(self):
        before = {key: self.tables[key] for key in tsm.KEYS}
        logs = []
        second = tsm.download(config.defaults(), self.dir, log=logs.append)
        for key in tsm.KEYS:
            with self.subTest(key=key):
                self.assertEqual(second[key]["status"], 304)
                self.assertFalse(second[key]["changed"])
                self.assertEqual(second[key]["rows"], len(before[key]))
                self.assertTrue(any("304" in line and key in line for line in logs),
                                "no 304 log line for %s" % key)
                self.assertEqual(len(tsm.parse_table(tsm.read_cached(self.dir, key), key)),
                                 len(before[key]))


if __name__ == "__main__":
    unittest.main()
