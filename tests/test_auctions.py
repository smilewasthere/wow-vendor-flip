import datetime
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auctions
from app import blizzard


def _cfg(region="eu", extra=None):
    cfg = {"region": region, "client_id": "cid", "client_secret": "csecret"}
    if extra:
        cfg.update(extra)
    return cfg


def _token_post(url, data, headers):
    return 200, json.dumps({"access_token": "tok-1",
                            "expires_in": 3600}).encode("utf-8")


def _bad_token_post(url, data, headers):
    return 400, b'{"error":"invalid_client"}'


class BuildLadderTest(unittest.TestCase):
    def test_aggregates_and_sorts(self):
        rows = [
            {"item": {"id": 7}, "quantity": 3, "unit_price": 200},
            {"item": {"id": 7}, "quantity": 4, "unit_price": 100},
            {"item": {"id": 7}, "quantity": 5, "unit_price": 200},
        ]
        ladder, stats = auctions.build_ladder(rows)
        self.assertEqual(ladder[7], [[100, 4], [200, 8]])
        self.assertEqual(stats["seen"], 3)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["items"], 1)
        self.assertEqual(stats["levels"], 2)
        self.assertEqual(stats["units"], 12)

    def test_skips_malformed_rows(self):
        rows = [
            {"item": {"id": 7}, "quantity": 3, "unit_price": 100},
            {"item": {}, "quantity": 3, "unit_price": 100},
            {"item": {"id": 8}, "quantity": 0, "unit_price": 100},
            {"item": {"id": 8}, "quantity": 3, "unit_price": 0},
            {"item": {"id": 8}, "quantity": -2, "unit_price": 100},
            {"item": {"id": 8}, "quantity": 3, "unit_price": -5},
            "not-a-dict",
            {"item": {"id": "oops"}, "quantity": 3, "unit_price": 100},
        ]
        ladder, stats = auctions.build_ladder(rows)
        self.assertEqual(ladder, {7: [[100, 3]]})
        self.assertEqual(stats["seen"], 8)
        self.assertEqual(stats["skipped"], 7)
        self.assertEqual(stats["items"], 1)

    def test_empty_input(self):
        ladder, stats = auctions.build_ladder([])
        self.assertEqual(ladder, {})
        self.assertEqual(stats["seen"], 0)
        self.assertEqual(stats["units"], 0)


class UrlTest(unittest.TestCase):
    def test_commodities_url_uses_dynamic_namespace(self):
        url = auctions.commodities_url(_cfg("eu"))
        self.assertIn("eu.api.blizzard.com", url)
        self.assertIn("/data/wow/auctions/commodities", url)
        self.assertIn("namespace=dynamic-eu", url)
        url = auctions.commodities_url(_cfg("us"))
        self.assertIn("us.api.blizzard.com", url)
        self.assertIn("namespace=dynamic-us", url)


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.cfg = _cfg(extra={"auctions_dir": self.dir})
        self.gets = []

    def _make_get(self, commodities, fail=None):
        def get(url, headers):
            self.gets.append((url, headers))
            if "/connected-realm/index" in url:
                raise AssertionError("realm index must not be fetched")
            if fail == "commodities":
                err = blizzard.BlizzardError("api request failed: HTTP 500")
                err.status = 500
                raise err
            return 200, json.dumps(commodities).encode("utf-8")
        return get

    def _commodities(self):
        return {"auctions": [
            {"item": {"id": 7}, "quantity": 3, "unit_price": 100},
            {"item": {"id": 9}, "quantity": 1, "unit_price": 50},
        ]}

    def test_happy_path_writes_ladder_and_meta_only(self):
        sleeps = []
        out = auctions.refresh(
            self.cfg,
            get=self._make_get(self._commodities()),
            post=_token_post, sleep=sleeps.append)
        self.assertEqual(set(out), {"commodities"})
        self.assertEqual(out["commodities"]["items"], 2)
        self.assertEqual(sleeps, [])
        for url, _headers in self.gets:
            self.assertNotIn("/connected-realm/index", url)
        with open(os.path.join(self.dir, "commodities-eu.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["items"]["7"], [[100, 3]])
        meta = auctions.load_meta(self.dir, "eu")
        self.assertEqual(meta["items"], 2)
        self.assertEqual(meta["units"], 4)
        self.assertTrue(meta["fetched_at"])
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "realm-count-eu.json")))

    def test_requests_carry_bearer_token(self):
        auctions.refresh(
            self.cfg,
            get=self._make_get(self._commodities()),
            post=_token_post)
        self.assertTrue(self.gets)
        for url, headers in self.gets:
            self.assertEqual(headers.get("Authorization"), "Bearer tok-1")

    def test_empty_commodities_refused_and_old_ladder_kept(self):
        old = {"region": "eu", "items": {"7": [[100, 3]]}}
        with open(os.path.join(self.dir, "commodities-eu.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(old, fh)
        with self.assertRaises(auctions.AuctionsError):
            auctions.refresh(
                self.cfg,
                get=self._make_get({"auctions": []}),
                post=_token_post)
        with open(os.path.join(self.dir, "commodities-eu.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), old)

    def test_commodities_fetch_failure_writes_nothing(self):
        sleeps = []
        with self.assertRaises(blizzard.BlizzardError):
            auctions.refresh(
                self.cfg,
                get=self._make_get(None, fail="commodities"),
                post=_token_post, sleep=sleeps.append)
        self.assertEqual(sleeps, [2, 4])
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "commodities-eu.json")))

    def test_429_retried_with_backoff_then_succeeds(self):
        calls = {"n": 0}
        sleeps = []

        def get(url, headers):
            if "/connected-realm/index" in url:
                raise AssertionError("realm index must not be fetched")
            calls["n"] += 1
            if calls["n"] == 1:
                err = blizzard.BlizzardError("HTTP 429")
                err.status = 429
                raise err
            return 200, json.dumps(self._commodities()).encode("utf-8")

        out = auctions.refresh(self.cfg, get=get, post=_token_post,
                               sleep=sleeps.append)
        self.assertEqual(out["commodities"]["items"], 2)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(sleeps, [2])

    def test_persistent_500_gives_up_after_three_attempts(self):
        calls = {"n": 0}
        sleeps = []

        def get(url, headers):
            calls["n"] += 1
            err = blizzard.BlizzardError("HTTP 500")
            err.status = 500
            raise err

        with self.assertRaises(blizzard.BlizzardError):
            auctions.refresh(self.cfg, get=get, post=_token_post,
                             sleep=sleeps.append)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [2, 4])

    def test_transport_error_retried(self):
        calls = {"n": 0}

        def get(url, headers):
            calls["n"] += 1
            if calls["n"] == 1:
                raise blizzard.BlizzardError("cannot reach host")
            return 200, json.dumps(self._commodities()).encode("utf-8")

        out = auctions.refresh(self.cfg, get=get, post=_token_post,
                               sleep=lambda s: None)
        self.assertEqual(out["commodities"]["items"], 2)
        self.assertEqual(calls["n"], 2)

    def test_non_retryable_status_raises_immediately(self):
        calls = {"n": 0}

        def get(url, headers):
            calls["n"] += 1
            err = blizzard.BlizzardError("HTTP 403")
            err.status = 403
            raise err

        with self.assertRaises(blizzard.BlizzardError):
            auctions.refresh(self.cfg, get=get, post=_token_post,
                             sleep=lambda s: None)
        self.assertEqual(calls["n"], 1)

    def test_token_failure_never_calls_get(self):
        def get(url, headers):
            raise AssertionError("get must not be called")

        with self.assertRaises(blizzard.BlizzardError):
            auctions.refresh(self.cfg, get=get, post=_bad_token_post)
        self.assertEqual(self.gets, [])


class GetLadderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.cfg = _cfg(extra={"auctions_dir": self.dir})

    def test_missing_file_returns_none(self):
        self.assertIsNone(auctions.get_ladder(self.cfg))

    def test_roundtrip_after_refresh(self):
        rows = [{"item": {"id": 7}, "quantity": 3, "unit_price": 100}]

        def get(url, headers):
            if "/connected-realm/index" in url:
                raise AssertionError("realm index must not be fetched")
            return 200, json.dumps({"auctions": rows}).encode("utf-8")
        auctions.refresh(self.cfg, get=get, post=_token_post)
        got = auctions.get_ladder(self.cfg)
        self.assertIsNotNone(got)
        ladder, meta = got
        self.assertEqual(ladder, {7: [[100, 3]]})
        self.assertEqual(meta["items"], 1)

    def test_corrupt_json_returns_none(self):
        with open(os.path.join(self.dir, "commodities-eu.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(auctions.get_ladder(self.cfg))

    def test_reload_on_file_change(self):
        def write(payload):
            with open(os.path.join(self.dir, "commodities-eu.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.utime(os.path.join(self.dir, "commodities-eu.json"),
                     (time.time() + 10, time.time() + 10))
        write({"region": "eu", "items": {"7": [[100, 3]]}})
        first, _ = auctions.get_ladder(self.cfg)
        self.assertEqual(first, {7: [[100, 3]]})
        write({"region": "eu", "items": {"9": [[50, 1]]}})
        second, _ = auctions.get_ladder(self.cfg)
        self.assertEqual(second, {9: [[50, 1]]})

    def test_region_isolation(self):
        with open(os.path.join(self.dir, "commodities-eu.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"region": "eu", "items": {"7": [[1, 1]]}}, fh)
        us_cfg = _cfg("us", extra={"auctions_dir": self.dir})
        self.assertIsNone(auctions.get_ladder(us_cfg))

    def test_corrupt_keys_skipped(self):
        with open(os.path.join(self.dir, "commodities-eu.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"region": "eu",
                       "items": {"bad": [[1, 1]], "7": [[100, 3]]}}, fh)
        ladder, _ = auctions.get_ladder(self.cfg)
        self.assertEqual(ladder, {7: [[100, 3]]})


class AheadUnitsTest(unittest.TestCase):
    def setUp(self):
        self.levels = [[100, 5], [200, 10], [200, 7], [300, 3]]

    def test_at_existing_level_counts_same_price_separately(self):
        self.assertEqual(auctions.ahead_units(self.levels, 200), (5, 17))

    def test_below_all_levels(self):
        self.assertEqual(auctions.ahead_units(self.levels, 99), (0, 0))

    def test_between_levels(self):
        self.assertEqual(auctions.ahead_units(self.levels, 150), (5, 0))

    def test_above_all_levels(self):
        self.assertEqual(auctions.ahead_units(self.levels, 1000), (25, 0))

    def test_empty(self):
        self.assertEqual(auctions.ahead_units([], 100), (0, 0))


class LadderStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.cfg = _cfg(extra={"auctions_dir": self.dir})

    def _write(self, fetched_at):
        with open(os.path.join(self.dir, "commodities-eu.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"region": "eu", "items": {"7": [[100, 3]]}}, fh)
        with open(os.path.join(self.dir, "commodities-eu.meta.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"fetched_at": fetched_at, "items": 1, "levels": 1,
                       "units": 3}, fh)

    def test_no_files_is_warming(self):
        st = auctions.ladder_status(self.cfg)
        self.assertTrue(st["warming"])
        self.assertIsNone(st["age_s"])
        self.assertNotIn("realm_count", st)

    def test_fresh_data_is_clean(self):
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self._write(now)
        st = auctions.ladder_status(self.cfg)
        self.assertFalse(st["warming"])
        self.assertFalse(st["stale"])
        self.assertLess(st["age_s"], 60)
        self.assertNotIn("realm_count", st)
        self.assertNotIn("realm_stale", st)

    def test_old_data_is_stale(self):
        self._write("2026-01-01T00:00:00")
        st = auctions.ladder_status(self.cfg)
        self.assertFalse(st["warming"])
        self.assertTrue(st["stale"])
        self.assertGreater(st["age_s"], auctions.LADDER_TTL)

    def test_ladder_alone_is_not_warming(self):
        # no realm-count file exists at all - the ladder alone is enough
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self._write(now)
        st = auctions.ladder_status(self.cfg)
        self.assertFalse(st["warming"])
        self.assertNotIn("realm_count", st)

    def test_corrupt_meta_is_stale_but_usable(self):
        with open(os.path.join(self.dir, "commodities-eu.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"region": "eu", "items": {"7": [[100, 3]]}}, fh)
        with open(os.path.join(self.dir, "commodities-eu.meta.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{broken")
        st = auctions.ladder_status(self.cfg)
        self.assertFalse(st["warming"])
        self.assertTrue(st["stale"])
        self.assertIsNone(st["age_s"])


class RefreshAsyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.cfg = _cfg(extra={"auctions_dir": self.dir})

    def _wait_idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not auctions.refresh_status()["running"]:
                return
            time.sleep(0.02)
        self.fail("refresh thread did not finish")

    def test_runner_success_records_stats(self):
        def runner(cfg, cache_dir):
            return {"items": 1}
        self.assertTrue(auctions.refresh_async(self.cfg, self.dir,
                                               runner=runner))
        self._wait_idle()
        st = auctions.refresh_status()
        self.assertEqual(st["error"], "")
        self.assertEqual(st["stats"], {"items": 1})
        self.assertTrue(st["finished_at"])

    def test_runner_error_is_captured_never_raised(self):
        def runner(cfg, cache_dir):
            raise RuntimeError("boom")
        self.assertTrue(auctions.refresh_async(self.cfg, self.dir,
                                               runner=runner))
        self._wait_idle()
        st = auctions.refresh_status()
        self.assertEqual(st["error"], "boom")
        self.assertIsNone(st["stats"])

    def test_single_flight_while_running(self):
        started = threading.Event()
        release = threading.Event()

        def runner(cfg, cache_dir):
            started.set()
            release.wait(5)
            return {"ok": 1}
        self.assertTrue(auctions.refresh_async(self.cfg, self.dir,
                                               runner=runner))
        self.assertTrue(started.wait(5))
        self.assertFalse(auctions.refresh_async(self.cfg, self.dir,
                                                runner=runner))
        release.set()
        self._wait_idle()
        self.assertTrue(auctions.refresh_async(self.cfg, self.dir,
                                               runner=lambda c, d: {}))
        self._wait_idle()

    def test_sweep_tmp_only_removes_tmp(self):
        keep = os.path.join(self.dir, "commodities-eu.json")
        junk = os.path.join(self.dir, "commodities-eu.json.tmp")
        for path in (keep, junk):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{}")
        removed = auctions.sweep_tmp(self.dir)
        self.assertEqual(removed, 1)
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(junk))


class MaybeRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.cfg = _cfg(extra={"auctions_dir": self.dir})
        self.calls = []

        def runner(cfg, cache_dir):
            self.calls.append(1)
            return {}
        self.runner = runner
        self._reset(thread=None, error="", finished_at="")

    def _reset(self, **state):
        with auctions._REFRESH_LOCK:
            auctions._REFRESH.update(state)

    def _wait_idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not auctions.refresh_status()["running"]:
                return
            time.sleep(0.02)
        self.fail("refresh thread did not finish")

    def test_running_short_circuits(self):
        release = threading.Event()
        alive = threading.Thread(target=release.wait, args=(5,), daemon=True)
        alive.start()
        self._reset(thread=alive)
        try:
            self.assertFalse(auctions.maybe_refresh_async(
                self.cfg, self.dir, runner=self.runner))
            self.assertEqual(self.calls, [])
        finally:
            release.set()
            alive.join(5)

    def test_fresh_error_is_cooled_down(self):
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self._reset(thread=None, error="boom", finished_at=now)
        self.assertFalse(auctions.maybe_refresh_async(
            self.cfg, self.dir, runner=self.runner))
        self.assertEqual(self.calls, [])

    def test_old_error_retries(self):
        old = (datetime.datetime.now()
               - datetime.timedelta(seconds=120)).isoformat(timespec="seconds")
        self._reset(thread=None, error="boom", finished_at=old)
        self.assertTrue(auctions.maybe_refresh_async(
            self.cfg, self.dir, runner=self.runner))
        self._wait_idle()
        self.assertEqual(auctions.refresh_status()["error"], "")

    def test_clean_state_refreshes_even_if_recently_finished(self):
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self._reset(thread=None, error="", finished_at=now)
        self.assertTrue(auctions.maybe_refresh_async(
            self.cfg, self.dir, runner=self.runner))
        self._wait_idle()


class DefaultDirTest(unittest.TestCase):
    def test_default_dir_lives_under_data(self):
        self.assertTrue(auctions.default_auctions_dir().replace("\\", "/")
                        .endswith("data/auctions"))


if __name__ == "__main__":
    unittest.main()
