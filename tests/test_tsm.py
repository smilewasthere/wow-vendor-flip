"""TSM public CSV client tests.

M12: realm CSV dropped - 2 keys (region + commodities). Commodities keep
the old realm-shaped schema.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, tsm

COMMODITIES_HEADER = "itemId,name,marketValue,minBuyout,recent,historical,updatedAt"
REGION_HEADER = "itemId,name,marketValue,historical,avgSalePrice,saleRate,soldPerDay,updatedAt"

COMMODITIES_CSV = "\n".join([
    COMMODITIES_HEADER,
    "210796,Mycobloom,27096,33800,33800,25400,2026-10-12T02:47:41Z",
    "4037,Recipe: Goggles,Parts,4344757,3380300,808830,999,2026-10-12T02:47:41Z",
    '2587,"Smith, John Shirt",100,200,300,400,2026-10-12T02:47:41Z',
    "30817,Simple Flour,,,,50,2026-10-12T02:47:41Z",
])

REGION_CSV = "\n".join([
    REGION_HEADER,
    "30817,Simple Flour,1484,100,190,0.183,79654.044,2026-10-11T00:50:23Z",
    "258123,Pattern: Enchant,Scroll,1074925212,800000000,475000000,0.06,0.24,2026-10-11T00:50:23Z",
])


class UrlTest(unittest.TestCase):
    def test_two_urls_for_eu(self):
        cfg = config.defaults()
        urls = tsm.urls(cfg)
        base = "https://public-data.tradeskillmaster.com/retail/eu"
        self.assertEqual(sorted(urls), ["commodities", "region"])
        self.assertEqual(urls["region"], base + "/region/items.csv")
        self.assertEqual(urls["commodities"], base + "/commodities.csv")

    def test_cache_paths_are_named_after_the_key(self):
        rows = tsm.cache_paths(tempfile.gettempdir())
        self.assertEqual(sorted(rows), ["commodities", "region"])
        self.assertTrue(rows["commodities"].endswith("commodities.csv"))
        self.assertTrue(rows["region"].endswith("items.csv"))


class ParseCommoditiesTest(unittest.TestCase):
    def test_normal_row_types(self):
        rows = tsm.parse_table(COMMODITIES_CSV, "commodities")
        self.assertEqual(len(rows), 4)
        row = rows[0]
        self.assertEqual(row["itemId"], 210796)
        self.assertEqual(row["name"], "Mycobloom")
        self.assertEqual(row["marketValue"], 27096)
        self.assertEqual(row["minBuyout"], 33800)
        self.assertEqual(row["recent"], 33800)
        self.assertEqual(row["historical"], 25400)
        self.assertEqual(row["updatedAt"], "2026-10-12T02:47:41Z")

    def test_unquoted_comma_in_name_is_reassembled(self):
        rows = tsm.parse_table(COMMODITIES_CSV, "commodities")
        row = rows[1]
        self.assertEqual(row["name"], "Recipe: Goggles,Parts")
        self.assertEqual(row["marketValue"], 4344757)
        self.assertEqual(row["minBuyout"], 3380300)
        self.assertEqual(row["recent"], 808830)
        self.assertEqual(row["historical"], 999)
        self.assertEqual(row["updatedAt"], "2026-10-12T02:47:41Z")

    def test_quoted_comma_in_name_survives(self):
        rows = tsm.parse_table(COMMODITIES_CSV, "commodities")
        self.assertEqual(rows[2]["name"], "Smith, John Shirt")
        self.assertEqual(rows[2]["marketValue"], 100)

    def test_blank_numerics_become_zero(self):
        row = tsm.parse_table(COMMODITIES_CSV, "commodities")[3]
        self.assertEqual(row["marketValue"], 0)
        self.assertEqual(row["minBuyout"], 0)
        self.assertEqual(row["historical"], 50)


class ParseRegionTest(unittest.TestCase):
    def test_region_types_including_floats(self):
        row = tsm.parse_table(REGION_CSV, "region")[0]
        self.assertEqual(row["itemId"], 30817)
        self.assertEqual(row["avgSalePrice"], 190)
        self.assertAlmostEqual(row["saleRate"], 0.183)
        self.assertAlmostEqual(row["soldPerDay"], 79654.044)
        self.assertEqual(row["updatedAt"], "2026-10-11T00:50:23Z")

    def test_region_unquoted_comma_name(self):
        row = tsm.parse_table(REGION_CSV, "region")[1]
        self.assertEqual(row["name"], "Pattern: Enchant,Scroll")
        self.assertEqual(row["marketValue"], 1074925212)
        self.assertAlmostEqual(row["soldPerDay"], 0.24)


class ParseErrorsTest(unittest.TestCase):
    def test_unknown_kind_raises(self):
        with self.assertRaises(tsm.TsmError):
            tsm.parse_table(COMMODITIES_CSV, "bogus")
        with self.assertRaises(tsm.TsmError):
            tsm.parse_table(COMMODITIES_CSV, "realm")  # removed in M12

    def test_empty_text_returns_no_rows(self):
        self.assertEqual(tsm.parse_table("", "commodities"), [])


class DownloadTest(unittest.TestCase):
    """Fake transport: 200 then 304 when If-None-Match is echoed back."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.calls = []
        self.logs = []
        self.serve_304 = False

    def fake_opener(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.serve_304:
            self.assertEqual(headers.get("If-None-Match"), '"etag-1"')
            return 304, b"", {}
        if url.endswith("/region/items.csv"):
            body = REGION_CSV
        else:
            body = COMMODITIES_CSV
        return (
            200,
            body.encode("utf-8"),
            {"ETag": '"etag-1"', "Last-Modified": "Tue, 12 Oct 2026 00:00:00 GMT",
             "Content-Type": "text/csv"},
        )

    def test_first_download_saves_file_meta_and_row_count(self):
        stats = tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                             log=self.logs.append)
        self.assertEqual(stats["commodities"]["status"], 200)
        self.assertEqual(stats["commodities"]["rows"], 4)
        self.assertTrue(stats["commodities"]["changed"])
        path = tsm.cache_paths(self.dir)["commodities"]
        self.assertTrue(os.path.isfile(path))
        meta = tsm.load_meta(self.dir, "commodities")
        self.assertEqual(meta["etag"], '"etag-1"')
        self.assertEqual(meta["rows"], 4)

    def test_second_download_sends_validator_and_reports_304(self):
        tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                     log=self.logs.append)
        self.serve_304 = True
        stats = tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                             log=self.logs.append)
        self.assertEqual(stats["commodities"]["status"], 304)
        self.assertFalse(stats["commodities"]["changed"])
        self.assertEqual(stats["commodities"]["rows"], 4)
        self.assertTrue(any("304" in line for line in self.logs))
        self.assertEqual(len(self.calls), 4)  # 2 files x 2 runs
        self.assertIn("If-None-Match", self.calls[3][1])

    def test_http_error_raises_tsm_error_with_url(self):
        def boom(url, headers):
            return 500, b"server on fire", {}
        with self.assertRaises(tsm.TsmError) as ctx:
            tsm.download(config.defaults(), self.dir, opener=boom,
                         log=lambda l: None, sleep=lambda s: None)
        self.assertIn("500", str(ctx.exception))

    def test_meta_file_is_json_on_disk(self):
        tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                     log=lambda l: None)
        meta_path = os.path.join(self.dir, "commodities.meta.json")
        with open(meta_path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["rows"], 4)


class CacheIntegrityTest(unittest.TestCase):
    """304 must never be believed when the cached file is missing or torn."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.serve_304 = False
        tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                     log=lambda l: None)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_opener(self, url, headers):
        if self.serve_304 and headers.get("If-None-Match"):
            return 304, b"", {}
        if url.endswith("/region/items.csv"):
            body = REGION_CSV
        else:
            body = COMMODITIES_CSV
        return (200, body.encode("utf-8"),
                {"ETag": '"etag-1"', "Last-Modified": "Tue, 12 Oct 2026 00:00:00 GMT"})

    def test_304_with_missing_file_refetches_instead_of_lying(self):
        path = tsm.cache_paths(self.dir)["commodities"]
        os.unlink(path)
        self.serve_304 = True
        stats = tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                             log=lambda l: None)
        self.assertEqual(stats["commodities"]["status"], 200,
                         "missing cache must force a full refetch, not a 304")
        self.assertTrue(os.path.isfile(path))

    def test_304_with_truncated_file_refetches(self):
        path = tsm.cache_paths(self.dir)["commodities"]
        with open(path, encoding="utf-8") as fh:
            kept = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(kept[: len(kept) // 2])
        self.serve_304 = True
        stats = tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                             log=lambda l: None)
        self.assertEqual(stats["commodities"]["status"], 200,
                         "truncated cache must force a full refetch, not a 304")
        rows = tsm.parse_table(tsm.read_cached(self.dir, "commodities"),
                               "commodities")
        self.assertEqual(len(rows), 4)

    def test_304_with_intact_file_is_accepted(self):
        self.serve_304 = True
        stats = tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                             log=lambda l: None)
        self.assertEqual(stats["commodities"]["status"], 304)
        self.assertFalse(stats["commodities"]["changed"])


class DownloadHardeningTest(unittest.TestCase):
    """M8: 429/5xx retry with backoff, single-flight, skipped-row counter."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sleeps = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def body_for(url):
        if url.endswith("/region/items.csv"):
            return REGION_CSV.encode("utf-8")
        return COMMODITIES_CSV.encode("utf-8")

    def test_429_is_retried_with_backoff_then_succeeds(self):
        failures = {"n": 1}

        def opener(url, headers):
            if failures["n"] > 0:
                failures["n"] -= 1
                return 429, b"slow down", {}
            return (200, self.body_for(url),
                    {"ETag": '"e1"'})

        stats = tsm.download(config.defaults(), self.dir, opener=opener,
                             sleep=self.sleeps.append)
        self.assertEqual(stats["commodities"]["status"], 200)
        self.assertEqual(self.sleeps, [tsm.RETRY_BACKOFF[0]])

    def test_persistent_429_raises_after_bounded_attempts(self):
        calls = []

        def opener(url, headers):
            calls.append(url)
            return 429, b"", {}

        with self.assertRaises(tsm.TsmError) as ctx:
            tsm.download(config.defaults(), self.dir, opener=opener,
                         sleep=self.sleeps.append)
        self.assertIn("429", str(ctx.exception))
        self.assertEqual(len(calls), tsm.RETRY_ATTEMPTS)
        self.assertEqual(self.sleeps,
                         list(tsm.RETRY_BACKOFF[:tsm.RETRY_ATTEMPTS - 1]))

    def test_5xx_is_retried_like_429(self):
        failures = {"n": 2}

        def opener(url, headers):
            if failures["n"] > 0:
                failures["n"] -= 1
                return 503, b"", {}
            return (200, self.body_for(url),
                    {"ETag": '"e1"'})

        stats = tsm.download(config.defaults(), self.dir, opener=opener,
                             sleep=self.sleeps.append)
        self.assertEqual(stats["commodities"]["status"], 200)
        self.assertEqual(len(self.sleeps), 2)

    def test_304_refetch_after_bad_cache_also_retries(self):
        # First pass: populate cache. Second pass: 304 + torn file forces a
        # refetch that hits a 429 once before succeeding.
        tsm.download(config.defaults(), self.dir,
                     opener=lambda u, h: (
                         200, self.body_for(u), {"ETag": '"e1"'}),
                     sleep=self.sleeps.append)
        path = tsm.cache_paths(self.dir)["commodities"]
        with open(path, encoding="utf-8") as fh:
            kept = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(kept[: len(kept) // 2])
        self.sleeps = []
        state = {"failed": False}

        def opener(url, headers):
            if headers.get("If-None-Match") and not state["failed"]:
                state["failed"] = True
                return 429, b"", {}
            if headers.get("If-None-Match"):
                return 304, b"", {}
            return (200, self.body_for(url),
                    {"ETag": '"e1"'})

        stats = tsm.download(config.defaults(), self.dir, opener=opener,
                             sleep=self.sleeps.append)
        self.assertEqual(stats["commodities"]["status"], 200)
        self.assertEqual(self.sleeps, [tsm.RETRY_BACKOFF[0]])

    def test_download_is_single_flight(self):
        import threading

        active = {"now": 0, "max": 0}
        guard = threading.Lock()
        release = threading.Event()

        def opener(url, headers):
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            release.wait(2)
            with guard:
                active["now"] -= 1
            return (200, self.body_for(url),
                    {"ETag": '"e1"'})

        results = []
        errors = []

        def worker():
            try:
                results.append(tsm.download(config.defaults(), self.dir,
                                            opener=opener,
                                            sleep=lambda s: None))
            except Exception as exc:  # surfaced below, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        import time as _time
        # Grace period: with the lock, thread B is blocked on _DOWNLOAD_LOCK
        # (never inside the opener); without it, both would be inside.
        _time.sleep(0.3)
        release.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [], errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(active["max"], 1,
                         "downloads must not overlap (single-flight)")

    def test_empty_200_is_rejected_and_cache_untouched(self):
        tsm.download(config.defaults(), self.dir,
                     opener=lambda u, h: (
                         200, self.body_for(u), {"ETag": '"e1"'}),
                     sleep=lambda s: None)
        path = tsm.cache_paths(self.dir)["commodities"]
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        meta_before = tsm.load_meta(self.dir, "commodities")

        def opener(url, headers):
            return 200, b"", {}

        with self.assertRaises(tsm.TsmError) as ctx:
            tsm.download(config.defaults(), self.dir, opener=opener,
                         sleep=lambda s: None)
        self.assertIn("empty", str(ctx.exception))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(tsm.load_meta(self.dir, "commodities"), meta_before)

    def test_transport_errors_are_retried(self):
        calls = {"n": 0}

        def opener(url, headers):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise tsm.TsmError("cannot reach host")
            if url.endswith("/region/items.csv"):
                return 200, REGION_CSV.encode("utf-8"), {"ETag": '"e"'}
            return 200, COMMODITIES_CSV.encode("utf-8"), {"ETag": '"e"'}

        stats = tsm.download(config.defaults(), self.dir, opener=opener,
                             sleep=self.sleeps.append)
        self.assertEqual(stats["commodities"]["status"], 200)
        self.assertEqual(self.sleeps,
                         list(tsm.RETRY_BACKOFF[:2]))

    def test_persistent_transport_error_raises(self):
        def opener(url, headers):
            raise tsm.TsmError("cannot reach host")

        with self.assertRaises(tsm.TsmError):
            tsm.download(config.defaults(), self.dir, opener=opener,
                         sleep=lambda s: None)

    def test_retry_after_header_raises_delay(self):
        state = {"n": 0}

        def opener(url, headers):
            state["n"] += 1
            if state["n"] == 1:
                return 429, b"", {"Retry-After": "30"}
            if url.endswith("/region/items.csv"):
                return 200, REGION_CSV.encode("utf-8"), {"ETag": '"e"'}
            return 200, COMMODITIES_CSV.encode("utf-8"), {"ETag": '"e"'}

        tsm.download(config.defaults(), self.dir, opener=opener,
                     sleep=self.sleeps.append)
        self.assertEqual(self.sleeps[0], 30)

    def test_retry_after_is_capped(self):
        state = {"n": 0}

        def opener(url, headers):
            state["n"] += 1
            if state["n"] == 1:
                return 429, b"", {"Retry-After": "99999"}
            if url.endswith("/region/items.csv"):
                return 200, REGION_CSV.encode("utf-8"), {"ETag": '"e"'}
            return 200, COMMODITIES_CSV.encode("utf-8"), {"ETag": '"e"'}

        tsm.download(config.defaults(), self.dir, opener=opener,
                     sleep=self.sleeps.append)
        self.assertEqual(self.sleeps[0], tsm.MAX_RETRY_AFTER)

    def test_304_reports_skipped_from_meta(self):
        def opener(url, headers):
            text = self.body_for(url).decode("utf-8") + "\n777\n"
            return 200, text.encode("utf-8"), {"ETag": '"e1"'}

        tsm.download(config.defaults(), self.dir, opener=opener,
                     sleep=lambda s: None)
        stats = tsm.download(
            config.defaults(), self.dir,
            opener=lambda u, h: (304, b"", {}),
            sleep=lambda s: None)
        self.assertEqual(stats["commodities"]["status"], 304)
        self.assertEqual(stats["commodities"]["skipped"], 1)

    def test_parse_table_counts_skipped_rows(self):
        stats = {}
        rows = tsm.parse_table(COMMODITIES_HEADER + "\n999\nok,row,here\n",
                               "commodities", stats=stats)
        self.assertEqual(rows, [])
        self.assertEqual(stats["skipped"], 2)

    def test_download_reports_skipped_in_stats_and_meta(self):
        def opener(url, headers):
            text = self.body_for(url).decode("utf-8") + "\n777\n"
            return 200, text.encode("utf-8"), {"ETag": '"e1"'}

        stats = tsm.download(config.defaults(), self.dir, opener=opener,
                             sleep=lambda s: None)
        self.assertEqual(stats["commodities"]["skipped"], 1)
        meta = tsm.load_meta(self.dir, "commodities")
        self.assertEqual(meta["skipped"], 1)


class LoadTablesTest(unittest.TestCase):
    def test_load_tables_reads_both_cached_files(self):
        cache = tempfile.mkdtemp()
        try:
            tsm.download(config.defaults(), cache,
                         opener=lambda u, h: (
                             200, (REGION_CSV if u.endswith("/region/items.csv")
                                   else COMMODITIES_CSV).encode("utf-8"),
                             {"ETag": '"e"'}),
                         log=lambda l: None)
            tables = tsm.load_tables(config.defaults(), cache)
            self.assertEqual(sorted(tables), ["commodities", "region"])
            self.assertEqual(len(tables["commodities"]), 4)
            self.assertEqual(len(tables["region"]), 2)
        finally:
            shutil.rmtree(cache, ignore_errors=True)

    def test_load_tables_without_cache_raises_tsm_error(self):
        cache = tempfile.mkdtemp()
        try:
            with self.assertRaises(tsm.TsmError):
                tsm.load_tables(config.defaults(), cache)
        finally:
            shutil.rmtree(cache, ignore_errors=True)


class GuardTest(unittest.TestCase):
    def test_header_drift_raises_instead_of_misparsing(self):
        with self.assertRaises(tsm.TsmError):
            tsm.parse_table("colA,colB\n1,2\n", "commodities")

    def test_short_rows_are_dropped(self):
        rows = tsm.parse_table(COMMODITIES_HEADER + "\n999\n", "commodities")
        self.assertEqual(rows, [])

    def test_corrupt_meta_degrades_to_empty_dict(self):
        cache = tempfile.mkdtemp()
        try:
            with open(os.path.join(cache, "commodities.meta.json"), "w",
                      encoding="utf-8") as fh:
                fh.write("{truncated")
            self.assertEqual(tsm.load_meta(cache, "commodities"), {})
        finally:
            shutil.rmtree(cache, ignore_errors=True)

    def test_read_error_from_transport_becomes_tsm_error(self):
        import http.client
        from unittest import mock

        class BrokenResp:
            status = 200
            headers = {}

            def read(self):
                raise http.client.IncompleteRead(b"")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", return_value=BrokenResp()):
            with self.assertRaises(tsm.TsmError):
                tsm._urlopen_get("https://example.invalid/x.csv", {})


class MetaFreshnessTest(unittest.TestCase):
    """M15 T1: fetched_at stamp, snapshot(), tmp sweep."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_opener(self, url, headers):
        if url.endswith("/region/items.csv"):
            body = REGION_CSV
        else:
            body = COMMODITIES_CSV
        return (200, body.encode("utf-8"),
                {"ETag": '"etag-1"',
                 "Last-Modified": "Tue, 12 Oct 2026 00:00:00 GMT"})

    def test_download_saves_fetched_at(self):
        tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                     log=lambda line: None)
        meta = tsm.load_meta(self.dir, "region")
        stamp = meta.get("fetched_at", "")
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
                         "fetched_at must be an ISO seconds stamp")

    def test_snapshot_reports_max_stamp_and_region_rows(self):
        tsm.download(config.defaults(), self.dir, opener=self.fake_opener,
                     log=lambda line: None)
        snap = tsm.snapshot(self.dir)
        self.assertRegex(snap["fetched_at"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertEqual(snap["rows"], 2)  # REGION_CSV has 2 data rows

    def test_snapshot_empty_cache_is_blank(self):
        self.assertEqual(tsm.snapshot(self.dir),
                         {"fetched_at": "", "rows": 0})

    def test_sweep_tmp_removes_only_tmp_leftovers(self):
        keep = os.path.join(self.dir, "region-items.csv")
        with open(keep, "w", encoding="utf-8") as fh:
            fh.write("keep")
        junk = os.path.join(self.dir, "region-items.csv.tmp")
        with open(junk, "w", encoding="utf-8") as fh:
            fh.write("crashed-write")
        removed = tsm.sweep_tmp(self.dir)
        self.assertEqual(removed, 1)
        self.assertTrue(os.path.isfile(keep), "live CSV must survive")
        self.assertFalse(os.path.isfile(junk))

    def test_sweep_tmp_on_missing_dir_is_noop(self):
        self.assertEqual(tsm.sweep_tmp(os.path.join(self.dir, "nope")), 0)


class RefreshAsyncTest(unittest.TestCase):
    """M15 T1: boot refresh worker - single-flight, error capture, stats."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        tsm._REFRESH.clear()  # fresh module state per test

    def tearDown(self):
        thread = tsm._REFRESH.get("thread")
        if thread:
            thread.join(timeout=3)
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def wait_done(timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            st = tsm.refresh_status()
            if not st["running"] and st["finished_at"]:
                return st
            time.sleep(0.02)
        return tsm.refresh_status()

    def test_refresh_runs_runner_and_records_stats(self):
        seen = {}

        def runner(cfg, cache_dir):
            seen["dir"] = cache_dir
            seen["region"] = cfg.get("region")
            return {"region": {"status": 304, "rows": 2}}

        ok = tsm.refresh_async(config.defaults(), self.dir, runner=runner)
        self.assertTrue(ok, "first refresh accepted")
        st = self.wait_done()
        self.assertFalse(st["running"])
        self.assertEqual(st["error"], "")
        self.assertEqual(st["stats"]["region"]["status"], 304)
        self.assertRegex(st["finished_at"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertEqual(seen["dir"], self.dir)
        self.assertEqual(seen["region"], "eu")

    def test_refresh_captures_runner_error_without_raising(self):
        def runner(cfg, cache_dir):
            raise tsm.TsmError("cannot reach TSM (offline)")

        tsm.refresh_async(config.defaults(), self.dir, runner=runner)
        st = self.wait_done()
        self.assertIn("cannot reach TSM", st["error"])
        self.assertFalse(st["running"])

    def test_second_refresh_while_running_is_refused(self):
        gate = threading.Event()

        def runner(cfg, cache_dir):
            gate.wait(2)
            return {}

        first = tsm.refresh_async(config.defaults(), self.dir, runner=runner)
        second = tsm.refresh_async(config.defaults(), self.dir, runner=runner)
        self.assertTrue(first)
        self.assertFalse(second, "single-flight: no second worker")
        gate.set()
        st = self.wait_done()
        self.assertFalse(st["running"])
        third = tsm.refresh_async(config.defaults(), self.dir, runner=runner)
        self.assertTrue(third, "finished worker allows a new refresh")
        gate.set()
        self.wait_done()

    def test_refresh_sweeps_tmp_before_running(self):
        marker = os.path.join(self.dir, "region-items.csv.tmp")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("stale")

        def runner(cfg, cache_dir):
            return {"swept": not os.path.isfile(marker)}

        tsm.refresh_async(config.defaults(), self.dir, runner=runner)
        st = self.wait_done()
        self.assertEqual(st["error"], "")
        self.assertTrue(st["stats"]["swept"],
                        "leftover tmp must be gone before the runner")

    def test_refresh_async_survives_thread_spawn_failure(self):
        def boom(*args, **kwargs):
            raise RuntimeError("cannot start new thread")

        with mock.patch.object(tsm.threading, "Thread", boom):
            ok = tsm.refresh_async(config.defaults(), self.dir,
                                   runner=lambda cfg, d: {})
        self.assertFalse(ok, "spawn failure reports as not-started")
        self.assertFalse(tsm.refresh_status()["running"],
                         "no worker thread may be left marked running")


if __name__ == "__main__":
    unittest.main()
