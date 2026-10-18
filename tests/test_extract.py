import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import api_keys, blizzard, config, db, extract, tsm


def item_payload(item_id, purchase_price=500, **extra):
    payload = {
        "id": item_id,
        "name": "Item %d" % item_id,
        "purchase_price": purchase_price,
        "sell_price": 100,
        "purchase_quantity": 1,
        "quality": "COMMON",
        "is_equippable": False,
        "item_class": {"name": "Tradeskill"},
        "item_subclass": {"name": "Other"},
    }
    payload.update(extra)
    return payload


class UniverseTest(unittest.TestCase):
    def test_union_of_all_tables_sorted_and_deduped(self):
        tables = {
            "realm": [{"itemId": 30}, {"itemId": 10}],
            "region": [{"itemId": 20}, {"itemId": 10}],
            "commodities": [{"itemId": 40}],
        }
        self.assertEqual(extract.universe_from_tables(tables), [10, 20, 30, 40])

    def test_zero_and_missing_ids_are_dropped(self):
        tables = {"realm": [{"itemId": 0}, {"itemId": 5}, {}]}
        self.assertEqual(extract.universe_from_tables(tables), [5])


class FetchItemTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def get(self, status, body):
        def opener(url, headers):
            self.calls.append((url, headers))
            return status, body
        return opener

    def test_200_returns_payload_and_targets_the_item_url(self):
        body = json.dumps({"id": 5, "name": "X", "purchase_price": 10}).encode()
        payload = extract.fetch_item(config.defaults(), "tok", 5,
                                     get=self.get(200, body))
        self.assertEqual(payload["id"], 5)
        url, headers = self.calls[0]
        self.assertIn("/data/wow/item/5", url)
        self.assertIn("namespace=static-eu", url)
        self.assertEqual(headers.get("Authorization"), "Bearer tok")

    def test_404_raises_item_not_found(self):
        with self.assertRaises(extract.ItemNotFound):
            extract.fetch_item(config.defaults(), "tok", 5, get=self.get(404, b""))

    def test_429_raises_rate_limited(self):
        with self.assertRaises(extract.RateLimited):
            extract.fetch_item(config.defaults(), "tok", 5, get=self.get(429, b""))

    def test_401_raises_token_expired(self):
        with self.assertRaises(extract.TokenExpired):
            extract.fetch_item(config.defaults(), "tok", 5, get=self.get(401, b""))

    def test_500_raises_blizzard_error(self):
        with self.assertRaises(blizzard.BlizzardError):
            extract.fetch_item(config.defaults(), "tok", 5, get=self.get(500, b"err"))


class ExtractHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "vendorflips.sqlite")
        self.universe = [1, 2, 3]
        self.payloads = {}
        self.fetch_calls = []
        self.token_calls = []
        self.sleeps = []

    def fetch(self, cfg, token, item_id):
        self.fetch_calls.append(item_id)
        action = self.payloads.get(item_id)
        if callable(action):
            return action(token)
        if isinstance(action, Exception):
            raise action
        if action is None:
            raise extract.ItemNotFound(item_id)
        return action

    def get_token(self, cfg):
        self.token_calls.append(len(self.token_calls) + 1)
        return "tok%d" % len(self.token_calls), 3600

    def do_run(self, **kwargs):
        defaults = dict(cfg=config.defaults(), db_path=self.db_path,
                        universe=self.universe, fetch_item_fn=self.fetch,
                        get_token_fn=self.get_token, sleep=self.sleeps.append,
                        pace=0.05, clock=lambda: 0.0, batch=2, max_retries=3,
                        now=lambda: "2026-10-12T00:00:00")
        defaults.update(kwargs)
        return extract.run_extraction(**defaults)

    def state(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            from app import db
            return db.state_dict(conn), db.candidate_ids(conn)
        finally:
            conn.close()

class RunExtractionTest(ExtractHarness):
    def test_extracts_only_items_with_positive_purchase_price(self):
        self.payloads = {1: item_payload(1, 500),
                         2: item_payload(2, 0),
                         3: item_payload(3, 1)}
        status = self.do_run()
        state, ids = self.state()
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["scanned"], 3)
        self.assertEqual(status["candidates"], 2)
        self.assertEqual(status["not_found"], 0)
        self.assertEqual(status["cursor"], 3)
        self.assertEqual(ids, {1, 3})
        self.assertEqual(state["phase"], "complete")

    def test_crash_mid_batch_then_resume_without_duplicates(self):
        self.universe = [1, 2, 3, 4, 5]
        for i in self.universe:
            self.payloads[i] = item_payload(i, 500)
        self.payloads[3] = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.do_run(batch=2)
        state, ids = self.state()
        self.assertEqual(state["phase"], "scanning")
        self.assertEqual(state["cursor"], "2")
        self.assertEqual(state["scanned"], "2")
        self.assertEqual(ids, {1, 2})

        self.payloads[3] = item_payload(3, 500)
        self.fetch_calls = []
        status = self.do_run(batch=2)
        state, ids = self.state()
        self.assertEqual(self.fetch_calls, [3, 4, 5])
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["scanned"], 5)
        self.assertEqual(len(ids), 5)
        self.assertEqual(ids, {1, 2, 3, 4, 5})

    def test_404_counts_as_not_found_and_still_completes(self):
        self.universe = [1, 2]
        self.payloads = {1: item_payload(1, 500)}
        status = self.do_run()
        self.assertEqual(status["not_found"], 1)
        self.assertEqual(status["candidates"], 1)
        self.assertEqual(status["phase"], "complete")

    def test_rate_limited_retries_with_backoff(self):
        self.payloads = {1: item_payload(1, 500), 2: item_payload(2, 500)}
        calls = {"n": 0}

        def flaky(token):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise extract.RateLimited(1)
            return item_payload(1, 500)

        self.payloads[1] = flaky
        status = self.do_run()
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["retries"], 2)
        self.assertEqual(status["candidates"], 2)
        backoffs = [s for s in self.sleeps if s >= 1]
        self.assertEqual(backoffs, [1, 2])

    def test_persistent_blizzard_error_aborts_with_last_error(self):
        self.universe = [1]
        self.payloads = {1: blizzard.BlizzardError("HTTP 500 flaky")}
        with self.assertRaises(blizzard.BlizzardError):
            self.do_run(max_retries=2, batch=1)
        state, ids = self.state()
        self.assertEqual(state["phase"], "scanning")
        self.assertEqual(state["cursor"], "0")
        self.assertIn("flaky", state["last_error"])
        self.assertEqual(ids, set())

    def test_pace_sleeps_before_every_fetch(self):
        self.universe = [1, 2, 3]
        for i in self.universe:
            self.payloads[i] = item_payload(i, 500)
        self.do_run(pace=0.05)
        self.assertEqual(self.sleeps, [0.05, 0.05, 0.05])

    def test_pace_shrinks_when_requests_started_recently(self):
        self.universe = [1, 2]
        for i in self.universe:
            self.payloads[i] = item_payload(i, 500)
        values = iter([0.0, 0.02, 0.02, 0.05, 0.05])
        self.do_run(pace=0.05, clock=lambda: next(values))
        self.assertEqual(len(self.sleeps), 2)
        self.assertAlmostEqual(self.sleeps[0], 0.03, places=9)
        self.assertAlmostEqual(self.sleeps[1], 0.02, places=9)

    def test_completed_run_is_noop_until_restart(self):
        for i in self.universe:
            self.payloads[i] = item_payload(i, 500)
        first = self.do_run()
        self.assertEqual(first["phase"], "complete")

        import sqlite3
        from app import db
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE candidate_items SET vendor_verified=1, notes='mine' "
                     "WHERE id=1")
        conn.commit()
        conn.close()

        self.fetch_calls = []
        again = self.do_run()
        self.assertEqual(self.fetch_calls, [])
        self.assertEqual(again["phase"], "complete")

        self.payloads[2] = item_payload(2, 777, name="Renamed")
        status = self.do_run(restart=True)
        self.assertEqual(self.fetch_calls, [1, 2, 3])
        self.assertEqual(status["scanned"], 3)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT vendor_verified, notes, purchase_price, name "
                               "FROM candidate_items WHERE id=1").fetchone()
            self.assertEqual(row, (1, "mine", 500, "Item 1"))
            row2 = conn.execute("SELECT purchase_price, name FROM candidate_items "
                                "WHERE id=2").fetchone()
            self.assertEqual(row2, (777, "Renamed"))
        finally:
            conn.close()

    def test_limit_stays_scanning_and_resume_finishes(self):
        self.universe = [1, 2, 3, 4, 5]
        for i in self.universe:
            self.payloads[i] = item_payload(i, 500)
        partial = self.do_run(limit=2)
        self.assertEqual(partial["phase"], "scanning")
        self.assertEqual(partial["scanned"], 2)
        self.assertEqual(self.fetch_calls, [1, 2])

        self.fetch_calls = []
        done = self.do_run()
        self.assertEqual(done["phase"], "complete")
        self.assertEqual(done["scanned"], 5)
        self.assertEqual(self.fetch_calls, [3, 4, 5])

    def test_token_refresh_on_401_then_succeeds(self):
        self.universe = [1]

        def needs_fresh_token(token):
            if token == "tok1":
                raise extract.TokenExpired(1)
            return item_payload(1, 500)

        self.payloads[1] = needs_fresh_token
        status = self.do_run()
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["candidates"], 1)
        self.assertEqual(len(self.token_calls), 2)

    def test_status_is_idle_when_database_file_is_missing(self):
        missing = os.path.join(self.dir, "nope.sqlite")
        status = extract.status(config.defaults(), db_path=missing)
        self.assertEqual(status["phase"], "idle")
        self.assertFalse(status["running"])
        self.assertEqual(status["scanned"], 0)
        self.assertFalse(os.path.exists(missing))


class StatusTsmBlockTest(unittest.TestCase):
    """M15 T2: status carries local TSM freshness + the re-extract nag.
    Never opens the network - metas and state are written directly."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "flip.sqlite")
        self.cfg = dict(config.defaults(), cache_dir=self.dir,
                        db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_meta(self, key, fetched_at, rows=2):
        with open(os.path.join(self.dir, "%s.meta.json" % key), "w",
                  encoding="utf-8") as fh:
            json.dump({"rows": rows, "fetched_at": fetched_at}, fh)

    def write_state(self, phase, updated_at):
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        db.state_bulk(conn, {"phase": phase, "updated_at": updated_at})
        conn.commit()
        conn.close()

    def block(self):
        return extract.status(self.cfg, db_path=self.db_path)["tsm"]

    def test_status_includes_tsm_block_shape(self):
        block = self.block()
        for key in ("fetched_at", "rows", "running", "error",
                    "newer_than_extract"):
            self.assertIn(key, block)
        self.assertEqual(block["fetched_at"], "")
        self.assertEqual(block["rows"], 0)
        self.assertFalse(block["newer_than_extract"])

    def test_newer_when_tsm_fetched_after_complete(self):
        self.write_meta("region", "2026-10-15T09:00:00")
        self.write_meta("commodities", "2026-10-15T08:00:00")
        self.write_state("complete", "2026-10-15T07:00:00")
        block = self.block()
        self.assertEqual(block["fetched_at"], "2026-10-15T09:00:00")
        self.assertEqual(block["rows"], 2)
        self.assertTrue(block["newer_than_extract"],
                        "fresher TSM than last extract must nag")

    def test_not_newer_when_extract_is_later(self):
        self.write_meta("region", "2026-10-14T09:00:00")
        self.write_state("complete", "2026-10-15T07:00:00")
        self.assertFalse(self.block()["newer_than_extract"])

    def test_never_nags_while_extract_running(self):
        self.write_meta("region", "2026-10-15T09:00:00")
        self.write_state("scanning", "2026-10-15T07:00:00")
        self.assertFalse(self.block()["newer_than_extract"])

    def test_never_nags_before_first_completed_extract(self):
        self.write_meta("region", "2026-10-15T09:00:00")
        self.write_state("idle", "")
        self.assertFalse(self.block()["newer_than_extract"])

    def test_refresh_state_flows_into_status(self):
        gate = threading.Event()
        try:
            tsm.refresh_async(self.cfg, self.dir,
                              runner=lambda cfg, cache_dir: gate.wait(2))
            block = self.block()
            self.assertTrue(block["running"],
                            "live refresh worker must show as running")
        finally:
            gate.set()
            thread = tsm._REFRESH.get("thread")
            if thread:
                thread.join(timeout=3)


class StartAsyncTest(unittest.TestCase):
    def test_runs_in_background_and_reports_running(self):
        started = threading.Event()
        release = threading.Event()

        def runner(cfg, **kwargs):
            started.set()
            release.wait(5)

        status = extract.start_async(config.defaults(), runner=runner)
        self.assertTrue(started.wait(5))
        self.assertTrue(extract.status(config.defaults())["running"])
        release.set()
        extract._join_for_test(timeout=5)
        self.assertFalse(extract.status(config.defaults())["running"])

    def test_second_start_while_running_is_ignored(self):
        release = threading.Event()
        calls = []

        def runner(cfg, **kwargs):
            calls.append(1)
            release.wait(5)

        extract.start_async(config.defaults(), runner=runner)
        extract.start_async(config.defaults(), runner=runner)
        release.set()
        extract._join_for_test(timeout=5)
        self.assertEqual(len(calls), 1)


class UniverseChurnTest(unittest.TestCase):
    """Review Important 1: resume must not skip ids below the old cursor."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "vendorflips.sqlite")
        self.fetch_calls = []
        self.sleeps = []

    def fetch(self, cfg, token, item_id):
        self.fetch_calls.append(item_id)
        return item_payload(item_id, 500)

    def do_run(self, universe, **kwargs):
        defaults = dict(cfg=config.defaults(), db_path=self.db_path,
                        universe=universe, fetch_item_fn=self.fetch,
                        get_token_fn=lambda c: ("tok", 3600),
                        sleep=self.sleeps.append, pace=0, batch=2,
                        now=lambda: "T")
        defaults.update(kwargs)
        return extract.run_extraction(**defaults)

    def test_new_ids_below_cursor_are_fetched_after_tsm_refresh(self):
        status = self.do_run([20, 30], limit=1)
        self.assertEqual(status["cursor"], 20)
        self.fetch_calls = []
        status = self.do_run([15, 20, 30])
        self.assertEqual(self.fetch_calls, [15, 30])
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["scanned"], 3)

    def test_legacy_cursor_state_seeds_scanned_ids(self):
        conn = extract.db.connect(self.db_path)
        extract.db.init_schema(conn)
        extract.db.state_bulk(conn, {
            "phase": "scanning", "cursor": "2", "scanned": "2",
            "candidates": "2", "not_found": "0", "retries": "0",
            "last_error": "", "started_at": "T", "updated_at": "T",
            "universe_size": "3",
        })
        conn.commit()
        conn.close()

        status = self.do_run([1, 2, 3])
        self.assertEqual(self.fetch_calls, [3])
        self.assertEqual(status["scanned"], 3)
        self.assertEqual(status["phase"], "complete")

    def test_restart_truncates_scanned_ids(self):
        self.do_run([1, 2])
        self.fetch_calls = []
        status = self.do_run([1, 2], restart=True)
        self.assertEqual(self.fetch_calls, [1, 2])
        self.assertEqual(status["scanned"], 2)


class StaleCandidateTest(ExtractHarness):
    """Review Important 2: re-extraction must refresh rows that went stale."""

    def test_price_drop_to_zero_updates_row_on_restart(self):
        for i in (1, 2):
            self.payloads[i] = item_payload(i, 500)
        self.do_run()
        self.payloads[1] = item_payload(1, 0)
        self.payloads[2] = item_payload(2, 777)
        status = self.do_run(restart=True)
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT purchase_price FROM candidate_items WHERE id=1").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 0)
        self.assertEqual(status["candidates"], 1)

    def test_404_on_restart_expires_rows_with_and_without_flags(self):
        for i in (1, 2, 3):
            self.payloads[i] = item_payload(i, 500)
        self.do_run()
        import sqlite3
        from app import db
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE candidate_items SET vendor_verified=1 WHERE id=2")
        conn.commit()
        conn.close()

        self.payloads = {1: item_payload(1, 500)}   # 2 and 3 now 404
        status = self.do_run(restart=True)
        conn = sqlite3.connect(self.db_path)
        try:
            flagged = conn.execute(
                "SELECT purchase_price, vendor_verified FROM candidate_items "
                "WHERE id=2").fetchone()
            gone = conn.execute(
                "SELECT COUNT(*) FROM candidate_items WHERE id=3").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(flagged, (0, 1))
        self.assertEqual(gone, 0)
        self.assertEqual(status["not_found"], 2)
        self.assertEqual(status["candidates"], 1)


class EmptyUniverseTest(ExtractHarness):
    """Review Important 3: an empty universe must never record `complete`."""

    def test_empty_universe_raises_and_leaves_state_untouched(self):
        with self.assertRaises(extract.ExtractionError):
            self.do_run(universe=[])
        status = extract.status(config.defaults(), db_path=self.db_path)
        self.assertEqual(status["phase"], "idle")
        self.assertEqual(status["scanned"], 0)


class LastErrorLifecycleTest(ExtractHarness):
    """Review Important 5: last_error must not outlive a successful run."""

    def test_abort_then_resume_clears_last_error(self):
        self.universe = [1]
        self.payloads = {1: extract.RateLimited(1)}
        with self.assertRaises(extract.ExtractionError):
            self.do_run(max_retries=1)
        state, _ = self.state()
        self.assertIn("rate limited", state["last_error"])

        self.payloads = {1: item_payload(1, 500)}
        status = self.do_run()
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["last_error"], "")

    def test_rate_limit_exhaustion_records_error_and_stays_scannable(self):
        self.universe = [1]
        self.payloads = {1: extract.RateLimited(1)}
        with self.assertRaises(extract.ExtractionError):
            self.do_run(max_retries=1, batch=1)
        state, ids = self.state()
        self.assertIn("rate limited on item 1", state["last_error"])
        self.assertEqual(state["cursor"], "0")
        self.assertEqual(ids, set())

    def test_token_refresh_cap_persists_last_error(self):
        def always_expired(token):
            raise extract.TokenExpired(1)

        self.universe = [1]
        self.payloads = {1: always_expired}
        with self.assertRaises(extract.ExtractionError):
            self.do_run()
        state, _ = self.state()
        self.assertIn("token", state["last_error"])
        self.assertEqual(len(self.token_calls), 6)


class LockTest(unittest.TestCase):
    """Review Important 4: one extraction at a time, across processes."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "vendorflips.sqlite")

    def test_second_acquire_is_refused_until_release(self):
        holder = extract.acquire_lock(self.db_path)
        self.assertIsNotNone(holder)
        self.assertEqual(extract.owner_pid(self.db_path), os.getpid())
        self.assertIsNone(extract.acquire_lock(self.db_path))
        extract.release_lock(holder)
        holder2 = extract.acquire_lock(self.db_path)
        self.assertIsNotNone(holder2)
        extract.release_lock(holder2)

    def test_status_reports_running_while_lock_is_held(self):
        holder = extract.acquire_lock(self.db_path)
        try:
            st = extract.status(config.defaults(), db_path=self.db_path)
            self.assertTrue(st["running"])
            self.assertEqual(st["owner_pid"], os.getpid())
        finally:
            extract.release_lock(holder)
        st = extract.status(config.defaults(), db_path=self.db_path)
        self.assertFalse(st["running"])

    def test_run_extraction_refuses_when_lock_is_held(self):
        holder = extract.acquire_lock(self.db_path)
        try:
            with self.assertRaises(extract.ExtractionError) as caught:
                extract.run_extraction(
                    config.defaults(), db_path=self.db_path, universe=[1],
                    fetch_item_fn=None, sleep=lambda s: None, pace=0)
            self.assertIn("already running", str(caught.exception))
            self.assertFalse(os.path.exists(self.db_path))
        finally:
            extract.release_lock(holder)


class TsmRefreshTest(ExtractHarness):
    """M8: universe=None refreshes TSM first, falls back to cache on failure."""

    def setUp(self):
        super().setUp()
        self.cache_dir = os.path.join(self.dir, "tsm")
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(os.path.join(self.cache_dir, "commodities.csv"), "w",
                  encoding="utf-8") as fh:
            fh.write("itemId,name,marketValue,minBuyout,recent,historical,"
                     "updatedAt\n1,A,10,10,10,10,now\n"
                     "2,B,10,10,10,10,now\n")
        with open(os.path.join(self.cache_dir, "region-items.csv"), "w",
                  encoding="utf-8") as fh:
            fh.write("itemId,name,marketValue,historical,avgSalePrice,"
                     "saleRate,soldPerDay,updatedAt\n"
                     "1,A,10,10,10,0.5,2,now\n"
                     "2,B,10,10,10,0.5,2,now\n")

    def run_universe_none(self):
        return self.do_run(universe=None, cache_dir=self.cache_dir)

    def test_universe_none_downloads_tsm_first(self):
        self.payloads = {1: item_payload(1, 500)}
        calls = []

        def fake_download(cfg, cache_dir, log=None, **kwargs):
            calls.append(cache_dir)
            return {}

        with mock.patch.object(tsm, "download", side_effect=fake_download):
            status = self.run_universe_none()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], self.cache_dir)
        self.assertEqual(status["universe_size"], 2)
        self.assertEqual(status["phase"], "complete")

    def test_download_failure_falls_back_to_cached_tables(self):
        self.payloads = {1: item_payload(1, 500)}

        def boom(cfg, cache_dir, log=None, **kwargs):
            raise tsm.TsmError("network down")

        with mock.patch.object(tsm, "download", side_effect=boom):
            status = self.run_universe_none()
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["scanned"], 2)

    def test_status_carries_tsm_skipped_rows(self):
        self.payloads = {1: item_payload(1, 500)}

        def fake_download(cfg, cache_dir, log=None, **kwargs):
            return {"commodities": {"skipped": 3},
                    "region": {"skipped": 0}}

        with mock.patch.object(tsm, "download", side_effect=fake_download):
            status = self.run_universe_none()
        self.assertEqual(status["tsm_skipped"], 3)
        state, _ = self.state()
        self.assertEqual(state.get("tsm_skipped"), "3")

    def test_fallback_reads_skipped_from_meta(self):
        # M8R: a failed refresh must not zero a previously reported count.
        # M12: KEYS = region + commodities -> two meta files, 2 + 2 = 4.
        self.payloads = {1: item_payload(1, 500)}
        import json
        for key in ("region", "commodities"):
            with open(os.path.join(self.cache_dir, "%s.meta.json" % key),
                      "w", encoding="utf-8") as fh:
                json.dump({"skipped": 2}, fh)

        def boom(cfg, cache_dir, log=None, **kwargs):
            raise tsm.TsmError("network down")

        with mock.patch.object(tsm, "download", side_effect=boom):
            status = self.run_universe_none()
        self.assertEqual(status["tsm_skipped"], 4)

    def test_resume_accumulates_tsm_skipped(self):
        self.payloads = {1: item_payload(1, 500), 2: item_payload(2, 500)}
        calls = [{"skipped": 3}, {"skipped": 2}]

        def fake_download(cfg, cache_dir, log=None, **kwargs):
            return {"commodities": {"skipped": calls[0]["skipped"]},
                    "region": {"skipped": 0}}

        with mock.patch.object(tsm, "download", side_effect=fake_download):
            self.do_run(universe=None, cache_dir=self.cache_dir, limit=1)
            state, _ = self.state()
            self.assertEqual(state.get("tsm_skipped"), "3")
            calls[0] = {"skipped": 2}
            status = self.do_run(universe=None, cache_dir=self.cache_dir)
        self.assertEqual(status["tsm_skipped"], 5)
        state, _ = self.state()
        self.assertEqual(state.get("tsm_skipped"), "5")


class JoinWorkerTest(unittest.TestCase):
    def test_join_without_thread_is_true(self):
        extract._STATE["thread"] = None
        self.assertTrue(extract.join_worker(timeout=0))

    def test_join_waits_for_running_worker(self):
        release = threading.Event()

        def runner(cfg, **kwargs):
            release.wait(5)

        extract.start_async(config.defaults(), runner=runner)
        release.set()
        self.assertTrue(extract.join_worker(timeout=5))
        self.assertFalse(extract.status(config.defaults())["running"])

    def test_join_times_out_on_still_running_worker(self):
        release = threading.Event()

        def runner(cfg, **kwargs):
            release.wait(5)

        extract.start_async(config.defaults(), runner=runner)
        try:
            self.assertFalse(extract.join_worker(timeout=0.05),
                             "still-running worker must report False")
        finally:
            release.set()
            extract.join_worker(timeout=5)


class StatusSetupBlockTest(unittest.TestCase):
    """First-run wizard: status carries setup {credentials, extract_done}."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "flip.sqlite")
        self.cfg = dict(config.defaults(), cache_dir=self.dir,
                        db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def status(self, cfg=None):
        return extract.status(cfg if cfg is not None else self.cfg,
                              db_path=self.db_path)

    def test_setup_block_shape_when_fresh_clone(self):
        st = self.status()
        self.assertIn("setup", st)
        self.assertFalse(st["setup"]["credentials"])
        self.assertFalse(st["setup"]["extract_done"])

    def test_credentials_true_from_config_file_creds(self):
        cfg = dict(self.cfg, client_id="ID", client_secret="SEC")
        self.assertTrue(self.status(cfg)["setup"]["credentials"])

    def test_config_needs_both_id_and_secret(self):
        cfg = dict(self.cfg, client_id="ID", client_secret="")
        self.assertFalse(self.status(cfg)["setup"]["credentials"])

    def test_credentials_true_from_stored_db_key(self):
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        api_keys.add(conn, "main", "ID", "SEC")
        conn.commit()
        conn.close()
        self.assertTrue(self.status()["setup"]["credentials"])

    def test_extract_done_true_when_phase_complete(self):
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        db.state_bulk(conn, {"phase": "complete",
                             "updated_at": "2026-10-18T00:00:00"})
        conn.commit()
        conn.close()
        st = self.status()
        self.assertTrue(st["setup"]["extract_done"])
        self.assertFalse(st["setup"]["credentials"])

    def test_status_missing_db_file_never_raises(self):
        missing = os.path.join(self.dir, "absent.sqlite")
        st = extract.status(config.defaults(), db_path=missing)
        self.assertFalse(st["setup"]["credentials"])
        self.assertFalse(st["setup"]["extract_done"])


if __name__ == "__main__":
    unittest.main()
