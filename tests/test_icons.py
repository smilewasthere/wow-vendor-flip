"""M4: lazy icon filenames - fetch on first sight, LRU cache, bounded size."""

import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import blizzard, db, icons


def not_found():
    err = blizzard.BlizzardError("api request failed: HTTP 404: not found")
    err.status = 404
    return err


class IconsCacheTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(db.SCHEMA)
        self.fetches = []
        self.stamp = [0]
        self.cfg = {"region": "eu", "client_id": "x", "icon_cache_max": 3}
        icons._RESOLVE_COOLDOWN.clear()

    def tearDown(self):
        self.conn.close()

    def now(self):
        self.stamp[0] += 1
        return "2026-10-13T00:00:00.%06d" % self.stamp[0]

    def make_fetch(self, table):
        def fetch(cfg, item_id):
            self.fetches.append(item_id)
            value = table.get(item_id)
            if value is None:
                raise not_found()
            if isinstance(value, Exception):
                raise value
            return value
        return fetch

    def cached_ids(self):
        return [r[0] for r in
                self.conn.execute("SELECT id FROM icon_cache ORDER BY id")]

    def seed_icon(self, item_id, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO icon_cache (id, icon, updated_at)"
            " VALUES (?, ?, ?)", (item_id, value, self.now()))
        self.conn.commit()

    def test_miss_fetches_and_caches(self):
        fetch = self.make_fetch({1: "inv_a"})
        out = icons.get_icon(self.cfg, self.conn, 1, fetch_fn=fetch,
                             now_fn=self.now)
        self.assertEqual(out, "inv_a")
        self.assertEqual(self.fetches, [1])
        self.assertEqual(self.cached_ids(), [1])

    def test_hit_does_not_fetch_again(self):
        fetch = self.make_fetch({1: "inv_a"})
        icons.get_icon(self.cfg, self.conn, 1, fetch_fn=fetch, now_fn=self.now)
        out = icons.get_icon(self.cfg, self.conn, 1, fetch_fn=fetch,
                             now_fn=self.now)
        self.assertEqual(out, "inv_a")
        self.assertEqual(self.fetches, [1])

    def test_404_is_cached_as_empty(self):
        fetch = self.make_fetch({})
        out = icons.get_icon(self.cfg, self.conn, 9, fetch_fn=fetch,
                             now_fn=self.now)
        self.assertEqual(out, "")
        out = icons.get_icon(self.cfg, self.conn, 9, fetch_fn=fetch,
                             now_fn=self.now)
        self.assertEqual(out, "")
        self.assertEqual(self.fetches, [9])

    def test_transient_error_is_not_cached(self):
        boom = blizzard.BlizzardError("api request failed: HTTP 500: boom")
        boom.status = 500
        table = {2: boom}
        fetch = self.make_fetch(table)
        with self.assertRaises(blizzard.BlizzardError):
            icons.get_icon(self.cfg, self.conn, 2, fetch_fn=fetch,
                           now_fn=self.now)
        table[2] = "inv_ok"
        out = icons.get_icon(self.cfg, self.conn, 2, fetch_fn=fetch,
                             now_fn=self.now)
        self.assertEqual(out, "inv_ok")
        self.assertEqual(self.fetches, [2, 2])

    def test_cap_evicts_oldest(self):
        fetch = self.make_fetch({i: "inv_%d" % i for i in (1, 2, 3, 4)})
        for item_id in (1, 2, 3, 4):
            icons.get_icon(self.cfg, self.conn, item_id, fetch_fn=fetch,
                           now_fn=self.now)
        self.assertEqual(self.cached_ids(), [2, 3, 4])

    def test_hit_refreshes_recency(self):
        fetch = self.make_fetch({i: "inv_%d" % i for i in (1, 2, 3, 4)})
        for item_id in (1, 2, 3):
            icons.get_icon(self.cfg, self.conn, item_id, fetch_fn=fetch,
                           now_fn=self.now)
        icons.get_icon(self.cfg, self.conn, 1, fetch_fn=fetch, now_fn=self.now)
        icons.get_icon(self.cfg, self.conn, 4, fetch_fn=fetch, now_fn=self.now)
        self.assertEqual(self.cached_ids(), [1, 3, 4])

    def test_cache_cap_from_config_or_default(self):
        self.assertEqual(icons.cache_cap({"icon_cache_max": 5}), 5)
        self.assertEqual(icons.cache_cap({}), icons.DEFAULT_CAP)
        self.assertEqual(icons.cache_cap({"icon_cache_max": 0}),
                         icons.DEFAULT_CAP)
        self.assertEqual(icons.cache_cap({"icon_cache_max": "nope"}),
                         icons.DEFAULT_CAP)

    def test_cache_cap_clamps_absurd_values(self):
        self.assertEqual(icons.cache_cap({"icon_cache_max": 10 ** 30}),
                         icons.DEFAULT_CAP)
        self.assertEqual(icons.cache_cap({"icon_cache_max": -5}),
                         icons.DEFAULT_CAP)
        self.assertEqual(icons.cache_cap({"icon_cache_max": 50000}), 50000)

    def test_get_icon_with_absurd_cap_does_not_crash(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(db.SCHEMA)
        try:
            fetch = self.make_fetch({1: "inv_a"})
            out = icons.get_icon(self.cfg, conn, 1, fetch_fn=fetch,
                                 now_fn=self.now, cap=10 ** 30)
            self.assertEqual(out, "inv_a")
        finally:
            conn.close()

    def test_get_icon_wowhead_resolves_and_caches_name(self):
        self.cfg["icon_host"] = "wowhead"
        value = "https://render.worldofwarcraft.com/eu/icons/56/134058.jpg"
        self.seed_icon(7, value)
        asked = []

        def resolve(fileid):
            asked.append(fileid)
            return "inv_misc_food_wheat_01"

        out = icons.get_icon(self.cfg, self.conn, 7, resolve_fn=resolve)
        self.assertEqual(out, "inv_misc_food_wheat_01")
        self.assertEqual(asked, ["134058"])
        row = self.conn.execute(
            "SELECT icon FROM icon_cache WHERE id = 7").fetchone()
        self.assertEqual(row[0], "inv_misc_food_wheat_01")

    def test_get_icon_resolution_failure_keeps_value_and_sets_cooldown(self):
        self.cfg["icon_host"] = "wowhead"
        value = "https://render.worldofwarcraft.com/eu/icons/56/134058.jpg"
        self.seed_icon(7, value)
        asked = []

        def resolve(fileid):
            asked.append(fileid)
            return ""

        out = icons.get_icon(self.cfg, self.conn, 7, resolve_fn=resolve)
        self.assertEqual(out, value)
        self.assertEqual(asked, ["134058"])
        out = icons.get_icon(self.cfg, self.conn, 7, resolve_fn=resolve)
        self.assertEqual(out, value)
        self.assertEqual(asked, ["134058"])  # cooled down: no second resolve

    def test_get_icon_blizzard_mode_never_resolves(self):
        value = "https://render.worldofwarcraft.com/eu/icons/56/134058.jpg"
        asked = []

        def resolve(fileid):
            asked.append(fileid)
            return "inv_misc"

        for offset, host in enumerate((None, "blizzard")):
            item_id = 7 + offset
            self.seed_icon(item_id, value)
            cfg = {"region": "eu"}
            if host is not None:
                cfg["icon_host"] = host
            out = icons.get_icon(cfg, self.conn, item_id, resolve_fn=resolve)
            self.assertEqual(out, value)
        self.assertEqual(asked, [])


class IconUrlTest(unittest.TestCase):
    def test_blizzard_default(self):
        url = icons.icon_url({"region": "eu", "icon_host": "blizzard"},
                             "inv_flour")
        self.assertEqual(
            url,
            "https://render.worldofwarcraft.com/eu/icons/56/inv_flour.jpg")

    def test_wowhead_toggle(self):
        url = icons.icon_url({"icon_host": "wowhead"}, "inv_flour")
        self.assertEqual(
            url,
            "https://wow.zamimg.com/images/wow/icons/large/inv_flour.jpg")

    def test_empty_icon_is_empty_url(self):
        self.assertEqual(icons.icon_url({"region": "eu"}, ""), "")

    def test_icon_url_passes_through_render_cdn_https_only(self):
        good = "https://render.worldofwarcraft.com/eu/icons/56/134058.jpg"
        self.assertEqual(icons.icon_url({"region": "eu"}, good), good)
        self.assertEqual(icons.icon_url({"region": "eu",
                                         "icon_host": "blizzard"}, good),
                         good)

    def test_wowhead_numeric_render_url_passes_through(self):
        # M13: zamimg hosts no numeric file names (404) while the render CDN
        # serves them 200 - wowhead mode must pass numeric basenames through.
        src = "https://render.worldofwarcraft.com/eu/icons/56/134058.jpg"
        self.assertEqual(icons.icon_url({"icon_host": "wowhead"}, src), src)

    def test_wowhead_rewrite_keeps_name_style_files(self):
        src = "https://render.worldofwarcraft.com/us/icons/56/inv_flour.jpg"
        self.assertEqual(
            icons.icon_url({"icon_host": "wowhead"}, src),
            "https://wow.zamimg.com/images/wow/icons/large/inv_flour.jpg")

    def test_wowhead_rewrite_refuses_unsafe_filenames(self):
        for hostile in (
            "https://render.worldofwarcraft.com/eu/icons/56/x.jpg?a=1",
            "https://render.worldofwarcraft.com/eu/icons/56/",
            "https://render.worldofwarcraft.com.evil.example/x.jpg",
        ):
            self.assertEqual(icons.icon_url({"icon_host": "wowhead"},
                                            hostile), "", hostile)

    def test_icon_url_refuses_hostile_values(self):
        for hostile in (
            "http://evil.example/x.jpg",
            "https://evil.example/x.jpg",
            "https://render.worldofwarcraft.com.evil.example/x.jpg",
            "javascript:alert(1)",
            "../etc/passwd",
            "inv_ok/../x",
            "inv_bad?x=1",
            "inv_bad#frag",
            "inv bad space",
        ):
            self.assertEqual(icons.icon_url({"region": "eu"}, hostile), "",
                             hostile)


class ResolveIconNameTest(unittest.TestCase):
    def test_resolve_icon_name_parses_og_image(self):
        seen = []

        def get(url, headers):
            seen.append((url, headers))
            body = (b'<meta property="og:image" content="https://'
                    b'wow.zamimg.com/images/wow/icons/large/'
                    b'inv_misc_food_wheat_01.jpg">')
            return 200, body, "https://www.wowhead.com/icon=134058"

        out = icons.resolve_icon_name("134058", get=get)
        self.assertEqual(out, "inv_misc_food_wheat_01")
        url, headers = seen[0]
        self.assertEqual(url, "https://www.wowhead.com/icon=134058")
        # plain browser-less UA is answered 403 by wowhead
        self.assertIn("User-Agent", headers)
        self.assertIn("Sec-Fetch-Mode", headers)

    def test_wowhead_headers_are_the_verified_browser_set(self):
        # pins the exact live-verified header set (bare UA -> 403, this -> 200)
        headers = icons.WOWHEAD_HEADERS
        ua = headers["User-Agent"]
        self.assertEqual(
            ua,
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        self.assertIn("Chrome/", ua)
        lowered = {key.lower() for key in headers}
        for key in ("user-agent", "accept", "accept-language",
                    "upgrade-insecure-requests", "sec-fetch-dest",
                    "sec-fetch-mode", "sec-fetch-site"):
            self.assertIn(key, lowered, key)

    def test_resolve_icon_name_slug_fallback(self):
        def get(url, headers):
            return 200, b"<html><body>no og:image here</body></html>", \
                "https://www.wowhead.com/icon=134058/inv-misc-food"

        self.assertEqual(icons.resolve_icon_name("134058", get=get),
                         "inv_misc_food")

    def test_resolve_icon_name_rejects_bad(self):
        def forbidden(url, headers):
            return 403, b"forbidden", url

        self.assertEqual(icons.resolve_icon_name("134058", get=forbidden), "")

        def numeric_og_image(url, headers):
            body = (b"https://wow.zamimg.com/images/wow/icons/large/"
                    b"134058.jpg")
            return 200, body, "https://www.wowhead.com/icon=134058"

        self.assertEqual(icons.resolve_icon_name("134058",
                                                 get=numeric_og_image), "")

        def unsafe_slug(url, headers):
            return 200, b"", "https://www.wowhead.com/icon=134058/inv%20misc"

        self.assertEqual(icons.resolve_icon_name("134058", get=unsafe_slug),
                         "")


class FetchIconNameTest(unittest.TestCase):
    def setUp(self):
        icons._TOKEN_CACHE.clear()
        self.token_calls = []
        self.time = [1000.0]

    def fake_get_token(self, cfg):
        self.token_calls.append(cfg.get("client_id"))
        return "tok-1", 86400

    def media_get(self, payload_status=200, payload=None):
        seen = []

        def get(url, headers):
            seen.append((url, headers))
            body = json.dumps(payload if payload is not None else {
                "assets": [{"key": "icon", "value": "inv_media"}]
            }).encode("utf-8")
            return payload_status, body
        return get, seen

    def test_parses_icon_from_media_payload(self):
        get, seen = self.media_get()
        out = icons.fetch_icon_name({"client_id": "x", "region": "eu"}, 7,
                                    get=get, get_token_fn=self.fake_get_token,
                                    time_fn=lambda: self.time[0])
        self.assertEqual(out, "inv_media")
        url, headers = seen[0]
        self.assertIn("/data/wow/media/item/7", url)
        self.assertIn("namespace=static-eu", url)
        self.assertEqual(headers["Authorization"], "Bearer tok-1")
        self.assertEqual(len(self.token_calls), 1)

    def test_full_url_value_passes_through(self):
        get, _ = self.media_get(payload={
            "assets": [{"key": "icon",
                        "value": "https://render.worldofwarcraft.com/eu"
                                 "/icons/56/134058.jpg"}]})
        out = icons.fetch_icon_name({"client_id": "x", "region": "eu"}, 7,
                                    get=get, get_token_fn=self.fake_get_token,
                                    time_fn=lambda: self.time[0])
        self.assertTrue(out.startswith("https://render.worldofwarcraft.com"))
        self.assertTrue(icons.icon_url({"region": "eu"}, out).startswith(
            "https://render.worldofwarcraft.com"))

    def test_token_is_reused_until_expiry(self):
        get, _ = self.media_get()
        cfg = {"client_id": "x", "region": "eu"}
        icons.fetch_icon_name(cfg, 7, get=get,
                              get_token_fn=self.fake_get_token,
                              time_fn=lambda: self.time[0])
        self.time[0] += 3600
        icons.fetch_icon_name(cfg, 8, get=get,
                              get_token_fn=self.fake_get_token,
                              time_fn=lambda: self.time[0])
        self.assertEqual(len(self.token_calls), 1)
        self.time[0] += 90000
        icons.fetch_icon_name(cfg, 9, get=get,
                              get_token_fn=self.fake_get_token,
                              time_fn=lambda: self.time[0])
        self.assertEqual(len(self.token_calls), 2)

    def test_media_404_returns_empty(self):
        get, _ = self.media_get(payload_status=404,
                                payload={"detail": "not found"})
        out = icons.fetch_icon_name({"client_id": "x", "region": "eu"}, 7,
                                    get=get, get_token_fn=self.fake_get_token,
                                    time_fn=lambda: self.time[0])
        self.assertEqual(out, "")

    def test_media_500_propagates(self):
        get, _ = self.media_get(payload_status=500,
                                payload={"detail": "boom"})
        with self.assertRaises(blizzard.BlizzardError):
            icons.fetch_icon_name({"client_id": "x", "region": "eu"}, 7,
                                  get=get,
                                  get_token_fn=self.fake_get_token,
                                  time_fn=lambda: self.time[0])

    def test_parse_media_icon_handles_odd_payloads(self):
        self.assertEqual(blizzard.parse_media_icon({}), "")
        self.assertEqual(blizzard.parse_media_icon(None), "")
        self.assertEqual(blizzard.parse_media_icon(
            {"assets": [{"key": "other", "value": "x"}]}), "")
        self.assertEqual(blizzard.parse_media_icon(
            {"assets": [{"key": "icon", "value": "inv_ok"}]}), "inv_ok")


if __name__ == "__main__":
    unittest.main()
