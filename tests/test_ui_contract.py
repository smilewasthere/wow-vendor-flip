"""M6 UI contract: static assets, mount-point ids, API paths, offline-only."""

import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, httpd

try:
    from http.client import HTTPConnection
except ImportError:  # pragma: no cover
    import http.client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

REQUIRED_IDS = [
    "region-chip", "data-chip", "btn-refresh", "btn-settings",
    "summary", "count-good", "count-check-vendor", "count-risky",
    "count-not-worth-it", "count-check-in-game", "count-not-vendor-sold",
    "top-picks", "top-picks-note",
    "f-search", "f-verdict", "f-sort", "f-min-sold", "f-min-profit",
    "f-min-sale-rate", "f-min-roi", "f-only-flagged", "f-hide-excluded",
    "f-reset",
    "items-count", "items-body", "empty-state",
    "settings-panel", "settings-backdrop", "btn-settings-close",
    "s-region", "s-icon-host",
    "btn-settings-save", "btn-settings-reset", "btn-reextract",
    "extract-status", "toast", "roi-tip", "verdict-tip",
    "vendor-check-card", "vc-tally", "vc-bar", "vc-attention",
    "btn-vc-start", "btn-vc-restart", "btn-vc-cancel",
    "api-keys-list", "ak-label", "ak-id", "ak-secret",
    "btn-ak-add", "btn-ak-test", "ak-status",
    "btn-filters", "filter-pop",
    "btn-top",
    "tsm-chip",
    "qty-pop", "qty-item", "qty-price", "qty-preset-mv", "qty-preset-low",
    "qty-myqty", "qty-result", "qty-stack", "qty-age", "qty-foot",
    # First-run setup wizard (welcome popup + pending ribbon)
    "setup-overlay", "fs-title", "fs-step-welcome", "fs-step-api",
    "fs-step-extract", "fs-welcome-next", "fs-welcome-skip",
    "fs-ak-id", "fs-ak-secret", "fs-ak-status", "fs-ak-test", "fs-ak-add",
    "fs-api-next", "fs-api-skip", "fs-region", "fs-extract-start",
    "fs-extract-progress", "fs-progress-text", "fs-bar-fill",
    "fs-extract-close", "fs-finish",
    "setup-ribbon", "ribbon-text", "ribbon-progress", "ribbon-open",
]


class UiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wowvf-ui-")
        cfg = config.defaults()
        cfg["port"] = 0
        cfg["db_path"] = os.path.join(cls.tmp, "test.sqlite")
        cfg["cache_dir"] = cls.tmp
        cls.server = httpd.build_server(cfg)
        cls.port = cls.server.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        headers = dict(resp.getheaders())
        conn.close()
        return resp.status, headers, body

    def test_index_serves_with_all_mount_points(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html = body.decode("utf-8")
        for element_id in REQUIRED_IDS:
            self.assertIn('id="%s"' % element_id, html, element_id)

    def test_static_assets_served_with_types(self):
        status, headers, _ = self._get("/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))
        status, headers, _ = self._get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))

    def test_health_reports_m16(self):
        status, _, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertIn(b"0.0.1-M16", body)

    def test_app_js_only_calls_known_api_paths(self):
        from app.routes import api as routes_api
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        known = set(routes_api.ROUTES) | set(routes_api.POST_ROUTES)
        used = set(re.findall(r"/api/[A-Za-z0-9/_-]*", js))
        unknown = {path for path in used if path not in known}
        self.assertEqual(unknown, set(), "app.js calls unknown routes")

    def test_web_assets_are_offline_only(self):
        for name in ("index.html", "styles.css", "app.js"):
            with open(os.path.join(WEB, name), encoding="utf-8") as fh:
                text = fh.read()
            # the SVG xmlns in a data: URI is an identifier, not a fetch
            text = text.replace("http://www.w3.org/2000/svg", "")
            self.assertNotIn("http://", text, name)
            for match in re.findall(r"https://[A-Za-z0-9./_-]+", text):
                self.assertTrue("wowhead.com" in match or
                                "www.wowhead.com" in match,
                                "%s loads external host %s" % (name, match))
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("<script src=\"http", html)
        self.assertNotIn("href=\"http", html)

    def test_index_links_only_local_assets(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        for match in re.findall(r'(?:src|href)="([^"]+)"', html):
            if match.startswith("/"):
                status, _, _ = self._get(match)
                self.assertEqual(status, 200, match)
            else:
                self.fail("non-root asset reference: %s" % match)

    def test_app_js_ids_exist_in_index(self):
        # M6R: $("id") lookups drift silently when html ids change.
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        used = set(re.findall(r'\$\("([a-z0-9-]+)"\)', js))
        missing = used - html_ids
        self.assertEqual(missing, set(), "app.js references missing ids")


class AuctionHouseThemeTest(unittest.TestCase):
    """M9C: deep-slate + aged-gold Auction House theme contract."""

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def test_css_has_auction_house_tokens(self):
        css = self.css()
        for token in ("--bg:", "--panel:", "--panel-2:", "--gold:",
                      "--gold-dim:", "--text:", "--muted:", "--green:",
                      "--red:", "--blue:", "--purple:"):
            self.assertIn(token, css, token)

    def test_quality_ramp_and_tabular_numbers(self):
        css = self.css()
        for q in range(8):
            self.assertRegex(css, r"\.q%d\s*\{" % q, "q%d" % q)
        self.assertIn("font-variant-numeric: tabular-nums", css)
        self.assertRegex(css, r"\.num\s*\{")

    def test_drawer_and_nosale_classes_exist(self):
        css = self.css()
        for needle in (".drawer", ".drawer-open", ".drawer-backdrop",
                       ".badge.v-nosale"):
            self.assertIn(needle, css, needle)

    def test_reduced_motion_still_guarded(self):
        self.assertIn("prefers-reduced-motion", self.css())


class WowAhTextureTest(unittest.TestCase):
    """M10: stone/noise texture, quality icon frames, click shimmer."""

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def test_noise_and_stone_tokens_exist(self):
        css = self.css()
        self.assertIn("--noise: url(\"data:image/svg+xml", css)
        self.assertIn("--stone:", css)
        self.assertIn("feTurbulence", css)
        self.assertIn("body::before", css)

    def test_icons_are_large_with_quality_frames(self):
        css = self.css()
        self.assertRegex(css, r"\.icon-cell img \{[^}]*width: 36px")
        self.assertRegex(css, r"\.card \.icon-cell img[^{]*\{[^}]*44px")
        for q in range(8):
            self.assertIn(".icon-cell.q%d img" % q, css, "q%d frame" % q)

    def test_click_shimmer_keyframes_exist(self):
        css = self.css()
        for needle in ("vf-press", "vf-gleam", ".clicked",
                       "button.th-sort::after"):
            self.assertIn(needle, css, needle)

    def test_app_js_toggles_clicked_class(self):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn('classList.add("clicked")', js)
        self.assertIn("animationend", js)


class M11UiTest(unittest.TestCase):
    """M11: API-key drawer, ripple, row entrance, card reveal."""

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def js(self):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            return fh.read()

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def test_ripple_and_row_effects_exist(self):
        css = self.css()
        for needle in (".ripple", "vf-ripple", "vf-rowin", "row-enter",
                       "vf-rowflash", "vf-cardin", "vf-toastin",
                       "vf-gear", ".settings-section", ".ak-list"):
            self.assertIn(needle, css, needle)

    def test_js_spawns_ripple_and_row_enter(self):
        js = self.js()
        for needle in ('className = "ripple"', '"row-enter"',
                       "loadApiKeys", "revealCards", "btn-ak-add"):
            self.assertIn(needle, js, needle)

    def test_html_has_api_key_section_and_gear_icon(self):
        html = self.html()
        for needle in ("api-keys-list", "ak-secret", "btn-ak-add",
                       "btn-settings", "<svg", "settings-section"):
            self.assertIn(needle, html, needle)

    def test_presave_test_button_and_wiring(self):
        self.assertIn("btn-ak-test", self.html())
        js = self.js()
        for needle in ("testFormCredentials", "btn-ak-test"):
            self.assertIn(needle, js, needle)


class M13BrandHeaderTest(unittest.TestCase):
    """M13 T2: centered brand header, status chips moved to the footer."""

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def test_brand_centered_and_chips_in_footer(self):
        html = self.html()
        css = self.css()

        # .brand wraps the heading and the inline coin/flip svg
        brand = re.search(r'<[^>]*class="brand"[^>]*>.*?</div>', html, re.S)
        self.assertIsNotNone(brand, ".brand element missing from index.html")
        self.assertIn("<h1", brand.group(0), "brand has no h1")
        self.assertIn("<svg", brand.group(0), "brand has no inline svg")

        # region/data chips now live in the static footer statusbar
        footer = re.search(r'<footer class="statusbar".*?</footer>', html, re.S)
        self.assertIsNotNone(footer, "footer.statusbar missing from index.html")
        self.assertIn('id="region-chip"', footer.group(0), "region chip")
        self.assertIn('id="data-chip"', footer.group(0), "data chip")

        # topbar is a 1fr auto 1fr grid with a right-side actions container
        topbar = re.search(r"\.topbar\s*\{([^}]*)\}", css)
        self.assertIsNotNone(topbar, ".topbar rule missing")
        self.assertIn("grid-template-columns: 1fr auto 1fr", topbar.group(1))
        actions = re.search(r"\.topbar-actions\s*\{([^}]*)\}", css)
        self.assertIsNotNone(actions, ".topbar-actions rule missing")
        self.assertIn("display: flex", actions.group(1))
        self.assertIn("gap: 8px", actions.group(1))

        # .brand h1: Georgia serif + animated gold sweep clipped to the text
        h1 = re.search(r"\.brand h1\s*\{([^}]*)\}", css)
        self.assertIsNotNone(h1, ".brand h1 rule missing")
        rule = h1.group(1)
        self.assertIn("Georgia", rule, "serif stack")
        self.assertIn("background-clip: text", rule, "gradient clipped to text")
        self.assertIn("linear-gradient(110deg", rule, "sweep gold gradient")
        self.assertIn("background-size: 250%", rule, "oversized bg for sweep")

    def test_header_heavy_wow_hover_animation(self):
        """M14 T3: ornamental wings + hover shine sweep + wing glow."""
        html = self.html()
        css = self.css()
        self.assertEqual(len(re.findall(r'class="brand-wing"', html)), 2,
                         "two ornamental wings flank the brand")
        self.assertRegex(css, r"@keyframes vf-brand-shine\s*\{",
                         "shine keyframes")
        self.assertRegex(css, r"\.brand:hover h1\s*\{[^}]*"
                              r"animation: vf-brand-shine",
                         "hover animates the sweep")
        self.assertRegex(css, r"\.brand-wing\s*\{",
                         "wing base rule")
        self.assertRegex(css, r"\.brand:hover \.brand-wing\s*\{[^}]*"
                              r"drop-shadow",
                         "wings glow on hover")

        # footer.statusbar exists as a thin stone bar with a gold-dim top rule
        statusbar = re.search(r"footer\.statusbar\s*\{([^}]*)\}", css)
        self.assertIsNotNone(statusbar, "footer.statusbar rule missing")
        self.assertIn("border-top: 1px solid var(--gold-dim)",
                      statusbar.group(1))

        # offline-only: no external font (or any other) reference added
        for name, text in (("index.html", html), ("styles.css", css)):
            scrubbed = text.replace("http://www.w3.org/2000/svg", "")
            self.assertNotIn("@import", text, name)
            for url in re.findall(r"https?://[A-Za-z0-9./_-]+", scrubbed):
                self.fail("external reference in %s: %s" % (name, url))


class M13TopbarIconsTest(unittest.TestCase):
    """M13 T3: refresh button is icon-only (inline SVG, aria-label)."""

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def test_refresh_button_is_icon(self):
        html = self.html()
        # scope all checks to this button's full element so the svg match
        # cannot leak into a following element (e.g. btn-settings' svg)
        button = re.search(r'<button[^>]*id="btn-refresh"[^>]*>[\s\S]*?</button>',
                           html)
        self.assertIsNotNone(button, "btn-refresh button element missing")
        el = button.group(0)
        # mandated regex: inner content starts with an inline svg (not text)
        self.assertRegex(el, r'id="btn-refresh"[^>]*>[\s\S]*?<svg')
        # accessible name lives on the button element itself
        self.assertIn('aria-label="Refresh"', el)
        # text label fully replaced by the icon
        self.assertNotIn(">Refresh</button>", el)


class M13FilterPopoverTest(unittest.TestCase):
    """M13 T4: sliders/checks moved into the topbar filter popover."""

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def test_popover_rule_lives_in_720px_media_block(self):
        # Review Focus 3: the mobile full-width popover pin. The regex is
        # scoped to the @media (max-width: 720px) block (closed by the
        # column-0 brace) so a stray top-level .popover rule cannot pass.
        css = self.css()
        block = re.search(r"@media \(max-width: 720px\)\s*\{([\s\S]*?)^\}",
                          css, re.M)
        self.assertIsNotNone(block, "@media (max-width: 720px) block missing")
        rule = re.search(r"\.popover\s*\{([^}]*)\}", block.group(1))
        self.assertIsNotNone(rule, ".popover rule missing in the 720px block")
        for decl in ("left: 10px", "right: 10px", "width: auto"):
            self.assertIn(decl, rule.group(1), decl)

    def test_filter_popover_holds_sliders_and_checks(self):
        html = self.html()
        self.assertRegex(html, r'id="filter-pop"[\s\S]*?id="f-min-sold"')
        self.assertRegex(html, r'id="filter-pop"[\s\S]*?id="f-reset"')
        pop = re.search(r'<div id="filter-pop"[\s\S]*?</div>', html)
        self.assertIsNotNone(pop, "filter-pop element missing")
        el = pop.group(0)
        for element_id in ("f-min-sold", "f-min-profit", "f-min-sale-rate",
                           "f-min-roi", "f-only-flagged", "f-hide-excluded",
                           "f-reset"):
            self.assertIn('id="%s"' % element_id, el, element_id)

    def test_filters_bar_keeps_search_verdict_sort(self):
        html = self.html()
        self.assertRegex(html, r'id="filters"[\s\S]*?id="f-search"')
        self.assertRegex(html, r'id="filters"[\s\S]*?id="f-verdict"')
        self.assertRegex(html, r'id="filters"[\s\S]*?id="f-sort"')
        section = re.search(r'<section id="filters"[\s\S]*?</section>', html)
        self.assertIsNotNone(section, "#filters section missing")
        el = section.group(0)
        for element_id in ("f-search", "f-verdict", "f-sort"):
            self.assertIn('id="%s"' % element_id, el, element_id)
        for moved in ("f-min-sold", "f-min-profit", "f-min-sale-rate",
                      "f-min-roi", "f-only-flagged", "f-hide-excluded",
                      "f-reset"):
            self.assertNotIn('id="%s"' % moved, el, moved)


class M13DrawerSlimTest(unittest.TestCase):
    """M13 T5: drawer widened to 560px; static explanatory copy removed."""

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def test_drawer_width_and_slim_copy(self):
        css = self.css()
        html = self.html()
        self.assertIn("min(560px, 94vw)", css, "drawer width")
        for needle in (
            "Saved values override config.local.json",
            "Secrets never leave this machine",
            "Region switches to Blizzard, TSM",
            "Already-verified rows are kept",
        ):
            self.assertNotIn(needle, html, needle)
        for element_id in ("extract-status", "ak-status", "vc-attention"):
            self.assertIn('id="%s"' % element_id, html, element_id)


class M13ScrollTopTest(unittest.TestCase):
    """M13 T6: scroll-to-top button for the item list."""

    def test_scroll_top_button_wiring_and_styling(self):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            css = fh.read()

        # js needles: id lookup, scroller-bound listener, threshold,
        # reduced-motion guard on the scroll behavior
        for needle in ('$("btn-top")',
                       'document.querySelector(".table-scroll")',
                       'scroller.addEventListener("scroll"',
                       "scrollTop > 240", "prefers-reduced-motion",
                       "scrollTo("):
            self.assertIn(needle, js, needle)

        # button lives inside #table-wrap, before the list scroller
        self.assertRegex(html, r'id="table-wrap"[\s\S]*?id="btn-top"'
                               r'[\s\S]*?class="table-scroll"')
        button = re.search(r'<button[^>]*id="btn-top"[^>]*>[\s\S]*?</button>',
                           html)
        self.assertIsNotNone(button, "btn-top button element missing")
        el = button.group(0)
        self.assertRegex(el, r'<button[^>]*\bhidden\b', "hidden by default")
        self.assertIn("<svg", el, "inline svg icon")
        self.assertIn("<span>Up</span>", el, "visible label")
        self.assertIn('aria-label="Back to list top"', el)

        # positioning: wrap is the containing block, circle sits off the
        # left edge at the BOTTOM on wide screens and overlays the
        # scroller on narrow (M14: user moved it from top to bottom)
        wrap = re.search(r"#table-wrap\s*\{([^}]*)\}", css)
        self.assertIsNotNone(wrap, "#table-wrap rule missing")
        self.assertIn("position: relative", wrap.group(1))
        top = re.search(r"\.scroll-top\s*\{([^}]*)\}", css)
        self.assertIsNotNone(top, ".scroll-top rule missing")
        for rule_needle in ("44px", "left: -56px", "bottom: 12px",
                            "var(--stone)", "var(--gold)"):
            self.assertIn(rule_needle, top.group(1), rule_needle)
        self.assertRegex(css, r"@media \(max-width: 1590px\)\s*\{[^}]*"
                              r"left: 6px")
        # display:flex would otherwise beat the UA [hidden] rule
        self.assertIn(".scroll-top[hidden]", css, "[hidden] override")


class M13RowStaggerTest(unittest.TestCase):
    """M13 T7: inline stagger for first 40 rows; viewport-gated card timer."""

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def js(self):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            return fh.read()

    def test_js_staggers_only_first_40_rows_inline(self):
        js = self.js()
        for needle in ('index < 40',
                       'setProperty("--vf-i", Math.min(index, 15))'):
            self.assertIn(needle, js, needle)

    def test_js_reveal_timer_gated_to_viewport(self):
        js = self.js()
        # R5: `rect.top` already occurs in app.js (ripple), so the pin is the
        # getBoundingClientRect + innerHeight pairing inside the 700ms timer.
        needle = "getBoundingClientRect().top < window.innerHeight"
        self.assertIn(needle, js, needle)

    def test_css_rowin_kept_but_nth_child_stagger_removed(self):
        css = self.css()
        # presence: the M11 contract rules must survive the deletion
        for needle in (".row-enter", "vf-rowin",
                       "animation-delay: calc(var(--vf-i)"):
            self.assertIn(needle, css, needle)
        # absence: nth-child delay table + css-side --vf-i assignments are gone
        self.assertNotIn("nth-child", css, "nth-child --vf-i rules deleted")
        self.assertNotIn("--vf-i:", css, "--vf-i now set inline from js")


class M14VerdictBarTest(unittest.TestCase):
    """M14 T2: verdict segmented color bar - relocated, zero-text, pop,
    per-color gradients, tooltip, selected/grayscale states."""

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def js(self):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            return fh.read()

    SEGMENTS = (
        ("v-good", "count-good"),
        ("v-check", "count-check-vendor"),
        ("v-risky", "count-risky"),
        ("v-no", "count-not-worth-it"),
        ("v-game", "count-check-in-game"),
        ("v-nosale", "count-not-vendor-sold"),
    )

    def test_summary_relocated_into_filters_bar(self):
        html = self.html()
        self.assertNotIn('<section id="summary"', html,
                         "summary must become a div (nested section would "
                         "break the #filters section regex)")
        self.assertRegex(html, r'<div id="summary"')
        filters = re.search(r'<section id="filters"[\s\S]*?</section>', html)
        self.assertIsNotNone(filters, "#filters section missing")
        block = filters.group(0)
        self.assertIn('id="f-search"', block, "search row intact")
        self.assertIn('id="summary"', block, "summary lives inside #filters")
        self.assertIn('id="f-verdict"', block, "verdict select stays")
        self.assertIn('id="f-sort"', block, "sort select stays")
        self.assertLess(block.index('id="f-sort"'), block.index('id="summary"'),
                        "summary sits near/after the search row controls")
        summary = re.search(r'<div id="summary"[\s\S]*?</div>', html)
        self.assertIsNotNone(summary, "summary div missing")
        for cls, count_id in self.SEGMENTS:
            self.assertIn('id="%s"' % count_id, summary.group(0), count_id)

    def test_segments_carry_no_visible_text(self):
        html = self.html()
        for cls, count_id in self.SEGMENTS:
            pattern = (r'<button[^>]*class="count %s"[^>]*>'
                       r'\s*<span id="%s">[^<]*</span>\s*</button>'
                       % (cls, count_id))
            self.assertRegex(html, pattern,
                             "segment %s must contain only its count span "
                             "(zero visible text)" % cls)
        css = self.css()
        self.assertRegex(css, r"\.count span\s*\{[^}]*position: absolute",
                         "count spans visually hidden for screen readers")

    def test_bar_stuck_segments_sized(self):
        css = self.css()
        summary = re.search(r"#summary\s*\{([^}]*)\}", css)
        self.assertIsNotNone(summary, "#summary rule missing")
        self.assertIn("display: flex", summary.group(1), "flex row bar")
        self.assertIn("gap: 0", summary.group(1), "segments stick together")
        self.assertIn("flex: 0 0 95px", summary.group(1),
                      "definite bar width, half of the original 190px "
                      "(~15.8px per segment) so hover can take space from "
                      "the other segments")
        count = re.search(r"\.count\s*\{([^}]*)\}", css)
        self.assertIsNotNone(count, ".count rule missing")
        self.assertIn("flex: 1 1 0", count.group(1),
                      "segments share the bar equally at rest")

    def test_each_verdict_its_own_gradient(self):
        css = self.css()
        for cls, color in (("v-good", "var(--green)"),
                           ("v-check", "var(--gold)"),
                           ("v-risky", "var(--red)"),
                           ("v-no", "var(--no)"),
                           ("v-game", "var(--purple)"),
                           ("v-nosale", "#5c6675")):
            self.assertRegex(
                css, r"\.count\.%s\s*\{[^}]*linear-gradient[^}]*%s"
                     % (cls, re.escape(color)),
                "%s must carry its own %s gradient" % (cls, color))

    def test_hover_pops_wider_with_transition(self):
        css = self.css()
        count = re.search(r"\.count\s*\{([^}]*)\}", css)
        self.assertIsNotNone(count, ".count rule missing")
        self.assertRegex(count.group(1), r"transition:[^;]*flex-grow",
                         "width pop must be animated")
        self.assertRegex(css, r"\.count:hover\s*\{[^}]*flex-grow: 2\.4",
                         "hovered segment pops wider (flex-grow: 2.4)")

    def test_verdict_tooltip_markup_and_styles(self):
        html = self.html()
        self.assertRegex(html, r'<div id="verdict-tip" role="tooltip"')
        css = self.css()
        tip = re.search(r"#verdict-tip\s*\{([^}]*)\}", css)
        self.assertIsNotNone(tip, "#verdict-tip rule missing")
        self.assertIn("position: fixed", tip.group(1), "fixed tooltip")
        self.assertRegex(css, r"#verdict-tip\.on\s*\{[^}]*display: block",
                         "shown via .on class like #roi-tip")

    def test_selected_state_colors_and_others_gray(self):
        css = self.css()
        sel = re.search(
            r'#summary \.count\[aria-pressed="true"\]\s*\{([^}]*)\}', css)
        self.assertIsNotNone(sel, "selected-segment rule missing")
        self.assertNotIn("grayscale", sel.group(1),
                         "selected segment keeps its color")
        gray = re.search(
            r'#summary\.has-sel \.count:not\(\[aria-pressed="true"\]\)'
            r"\s*\{([^}]*)\}", css)
        self.assertIsNotNone(gray, "unselected gray-out rule missing")
        self.assertIn("grayscale", gray.group(1), "others go gray")
        restore = re.search(
            r'#summary\.has-sel \.count:not\(\[aria-pressed="true"\]\):hover'
            r"\s*\{([^}]*)\}", css)
        self.assertIsNotNone(restore, "hover-restore rule missing")
        self.assertIn("filter: none", restore.group(1),
                      "hovering a gray segment reveals its color")

    def test_search_input_widened_50(self):
        css = self.css()
        self.assertRegex(css, r'\.filters input\[type="search"\]\s*\{'
                              r'[^}]*min-width: 270px',
                         "search field 50% wider (180px -> 270px)")

    def test_ripple_visible_on_segments(self):
        css = self.css()
        self.assertRegex(css, r"\.count span\.ripple\s*\{"
                              r"[^}]*clip-path: none",
                         "ripple must beat the sr-only .count span rule")
        self.assertRegex(css, r"\.count span\.ripple\s*\{"
                              r"[^}]*overflow: visible",
                         "ripple overflow restored inside segments")

    def test_js_sync_tooltip_wiring(self):
        js = self.js()
        self.assertIn("function syncVerdictSegments", js)
        self.assertRegex(js, r"function resetViewFilters[\s\S]{0,700}?"
                          r"syncVerdictSegments\(\);",
                         "reset path syncs segments")
        self.assertRegex(js, r'\$\("summary"\)\.addEventListener\("click"'
                          r'[\s\S]{0,400}?syncVerdictSegments\(\);',
                         "segment click syncs")
        self.assertRegex(js, r'\$\("f-verdict"\)\.addEventListener\("change"'
                          r'[\s\S]{0,200}?syncVerdictSegments\(\);',
                         "select change syncs")
        self.assertRegex(js, r'window\.addEventListener\("scroll", '
                          r'hideVerdictTip, true\);\s*'
                          r"syncVerdictSegments\(\);",
                         "init sync right after wiring")
        self.assertIn('setAttribute("aria-pressed"', js)
        self.assertIn("verdict-tip", js)
        self.assertIn("VERDICT_TIP", js, "per-verdict explanation map")
        self.assertIn('$("summary").addEventListener("focusin"',
                      js, "tooltip also opens on keyboard focus")


class M14VaultTest(unittest.TestCase):
    """M14 T4: API Keys golden vault panel + gold/blue button variants."""

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def test_vault_panel_markup(self):
        html = self.html()
        self.assertRegex(html, r'<div class="settings-section vault"',
                         "API keys section is the vault")
        self.assertIn('class="vault-key"', html, "key icon in vault header")
        self.assertRegex(html, r'id="btn-ak-test" class="btn btn-blue"',
                         "Test button = blue accent")
        self.assertRegex(html, r'id="btn-ak-add" class="btn btn-gold"',
                         "Add key button = gold primary")

    def test_vault_panel_styles(self):
        css = self.css()
        self.assertRegex(css, r"\.settings-section\.vault\s*\{[^}]*"
                              r"linear-gradient",
                         "vault gets a lifted gold-tinted background")
        self.assertRegex(css, r"\.settings-section\.vault\s*\{[^}]*"
                              r"var\(--gold\)",
                         "vault border is gold")
        self.assertIn("vault::before", css, "top-left corner accent")
        self.assertIn("vault::after", css, "bottom-right corner accent")
        self.assertRegex(css, r"\.settings-section\.vault h3\s*\{",
                         "vault header rule")
        self.assertRegex(css, r"\.vault-key\s*\{", "key icon rule")
        self.assertRegex(css, r"\.btn-gold\s*\{[^}]*linear-gradient",
                         "gold primary button gradient")
        self.assertRegex(css, r"\.btn-blue\s*\{[^}]*linear-gradient",
                         "blue accent button gradient")


class M14VendorConsoleTest(unittest.TestCase):
    """M14 T5: vendor-check card as a mission console - icon, status pill,
    striped progress, colored actions, alert banner."""

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def js(self):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            return fh.read()

    def test_console_markup(self):
        html = self.html()
        self.assertIn('class="vc-icon"', html, "framed status icon")
        self.assertRegex(html, r'class="vc-pill"><span class="vc-tally" '
                               r'id="vc-tally"',
                         "tally sits inside the status pill")
        self.assertRegex(html, r'id="btn-vc-start"\s+class="btn btn-green"',
                         "Start = green primary")
        self.assertRegex(html, r'id="btn-vc-restart"\s+class="btn btn-orange"',
                         "Re-check = orange warn")
        self.assertRegex(html, r'id="btn-vc-cancel"\s+class="btn btn-red"',
                         "Cancel = red destructive")
        attention = re.search(r'<p class="vc-attention" id="vc-attention"'
                              r'[^>]*>[\s\S]*?</p>', html)
        self.assertIsNotNone(attention, "attention banner missing")
        self.assertIn("<svg", attention.group(0), "warning icon in banner")

    def test_console_styles(self):
        css = self.css()
        self.assertRegex(css, r"@keyframes vf-stripe\s*\{",
                         "striped progress animation")
        self.assertRegex(css, r"\.vc-progress > span\s*\{[^}]*"
                              r"repeating-linear-gradient",
                         "thick striped gold progress fill")
        self.assertRegex(css, r"\.vc-pill\.on\s*\{[^}]*animation",
                         "running pill blinks")
        self.assertRegex(css, r"\.vc-pill\.err\s*\{", "offline pill state")
        self.assertRegex(css, r"\.vc-attention\s*\{"
                              r"[^}]*background: rgba\(229, 83, 75",
                         "attention styled as red alert (M14 banner bg)")
        self.assertRegex(css, r"\.vc-attention\[hidden\]", "hidden override")
        self.assertRegex(css, r"\.vc-icon\s*\{", "icon frame rule")

    def test_js_syncs_pill_state(self):
        js = self.js()
        self.assertIn("vc-pill", js, "pill class managed from status")
        self.assertIn('st.running ? " on"', js, "running state added")
        self.assertIn('"vc-pill err"', js, "offline state added")

    def test_js_catch_resets_vc_buttons(self):
        js = self.js()
        self.assertRegex(js, r'server offline[\s\S]{0,400}?'
                          r'btn-vc-start"\)\.disabled = false',
                         "catch path re-enables Start")
        self.assertRegex(js, r'server offline[\s\S]{0,400}?'
                          r'btn-vc-cancel"\)\.hidden = true',
                         "catch path hides Cancel")


class M15Test(unittest.TestCase):
    """M15: TSM freshness chip, search suggestions, fonts."""

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def js(self):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            return fh.read()

    # ---- T3: footer TSM chip ----

    def test_tsm_chip_in_footer_statusbar(self):
        html = self.html()
        footer = re.search(r'<footer class="statusbar".*?</footer>', html,
                           re.S)
        self.assertIsNotNone(footer, "footer.statusbar missing")
        self.assertIn('id="tsm-chip"', footer.group(0),
                      "TSM chip lives with the other footer chips")

    def test_tsm_chip_js_rendering(self):
        js = self.js()
        self.assertIn("tsm-chip", js)
        self.assertIn("function agoText", js, "relative age helper")
        self.assertIn("newer_than_extract", js, "reads the status payload")
        self.assertIn("re-extract for new items", js, "nag copy")

    def test_tsm_chip_css_states(self):
        css = self.css()
        self.assertRegex(css, r"\.chip\.tsm-newer\s*\{[^}]*var\(--gold\)",
                         "fresh-than-extract chip glows gold")
        self.assertRegex(css, r"\.chip\.tsm-err\s*\{[^}]*var\(--red\)",
                         "refresh-failed chip reads red")

    # ---- T4: Chrome-style search suggestions ----

    def test_suggest_markup(self):
        html = self.html()
        self.assertRegex(html, r'<div class="search-wrap">\s*'
                               r'<input type="search" id="f-search"',
                         "f-search wrapped for absolute positioning")
        self.assertRegex(html, r'<div id="search-suggest" class="suggest" '
                               r'role="listbox" aria-label="Search '
                               r'suggestions" hidden>',
                         "listbox dropdown sits under the search input")

    def test_suggest_js_api(self):
        js = self.js()
        for fn in ("function suggestScore", "function suggestItems",
                   "function renderSuggest", "function applySuggestion",
                   "function hideSuggest"):
            self.assertIn(fn, js, "%s missing" % fn)
        self.assertIn('"ArrowDown"', js, "keyboard navigation down")
        self.assertIn('"ArrowUp"', js, "keyboard navigation up")
        self.assertRegex(js, r"toLowerCase\(\) === q\b",
                         "typing an exact full name hides the dropdown")

    def test_suggest_escape_keeps_typed_query(self):
        js = self.js()
        self.assertRegex(
            js, r'event\.key === "Escape"\) \{[\s\S]{0,160}?'
                r'event\.preventDefault\(\)[\s\S]{0,80}?hideSuggest\(\)',
            "input type=search natively clears on Escape; preventDefault "
            "keeps the typed query so the user can keep editing")

    def test_suggest_css(self):
        css = self.css()
        self.assertRegex(css, r"\.search-wrap\s*\{[^}]*position: relative",
                         "positioning context on the wrapper")
        self.assertRegex(css, r"\.suggest\s*\{[^}]*position: absolute"
                              r"[\s\S]*?z-index: 30",
                         "dropdown floats above table, below drawer (45)")
        self.assertRegex(css, r"\.suggest-row\s*\{", "row rule exists")
        self.assertRegex(css, r"\.suggest-row\.(?:active|hover)",
                         "active/hover affordance")

    # ---- T5: fonts (calm UI stack + ornament headers) ----

    def test_font_tokens(self):
        css = self.css()
        self.assertRegex(
            css,
            r'--font-ui:\s*"Bahnschrift",\s*"Segoe UI",\s*system-ui,\s*sans-serif',
            "calm Bahnschrift UI stack token")
        self.assertRegex(
            css,
            r'--font-ornament:\s*"Palatino Linotype",\s*"Book Antiqua",'
            r'\s*Georgia,\s*serif',
            "Palatino ornament stack token")
        self.assertRegex(css, r"\bbody\s*\{[^}]*font:\s*14px/1\.45 "
                              r"var\(--font-ui\)",
                         "body renders in the calm UI font")

    def test_font_ornament_settings_headers(self):
        css = self.css()
        self.assertRegex(
            css, r"\.settings-section h3\s*\{[^}]*var\(--font-ornament\)",
            "settings section headers carry the ornament font")
        self.assertRegex(
            css, r"\.settings-section h3\s*\{[^}]*font-variant:\s*small-caps",
            "ornament headers are small-caps")
        self.assertNotRegex(
            css, r"\.settings-section h3\s*\{[^}]*text-transform:\s*uppercase",
            "uppercase leaves no lowercase glyphs for small-caps to shape")


class M16QuantityAdvisorTest(unittest.TestCase):
    """M16 rework: minimal one-glance Posting Advisor + your-stack verdict."""

    def html(self):
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def css(self):
        with open(os.path.join(WEB, "styles.css"), encoding="utf-8") as fh:
            return fh.read()

    def js(self):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as fh:
            return fh.read()

    def test_qty_pop_markup(self):
        html = self.html()
        self.assertRegex(
            html, r'<div id="qty-pop" class="popover qty-pop" hidden role="dialog"'
                  r' aria-label="Posting advisor">')
        start = html.index('id="qty-pop"')
        end = html.index('id="toast"')
        block = html[start:end]
        for element_id in ("qty-item", "qty-price", "qty-preset-mv",
                           "qty-preset-low", "qty-myqty", "qty-result",
                           "qty-stack", "qty-age", "qty-foot"):
            self.assertIn('id="%s"' % element_id, block, element_id)
        self.assertIn('aria-live="polite"', block, "result announces itself")
        self.assertRegex(block, r'id="qty-price"[^>]*type="number"')
        self.assertRegex(block, r'id="qty-myqty"[^>]*type="number"',
                         "you-have quantity input is numeric")

    def test_rich_controls_are_gone(self):
        """The rework deletes confidence buttons, the breakdown table and the
        depth ladder - none may survive in markup, JS or CSS."""
        html, js, css = self.html(), self.js(), self.css()
        for needle in ("qty-conf", "qty-breakdown", "qty-ladder",
                       "qty-table", "data-conf", "realm_count",
                       "connected realms", "same_price_units"):
            self.assertNotIn(needle, html, "index.html still has " + needle)
            self.assertNotIn(needle, js, "app.js still has " + needle)
        for needle in ("qty-conf", "qty-ladder", "qty-table"):
            self.assertNotIn(needle, css, "styles.css still has " + needle)

    def test_qty_foot_carries_trust_caveats(self):
        foot = re.search(r'<p id="qty-foot"[^>]*>([\s\S]*?)</p>', self.html())
        self.assertIsNotNone(foot, "qty-foot missing")
        text = foot.group(0)
        for needle in ("estimate", "undercut race", "LIFO"):
            self.assertIn(needle, text, needle)

    def test_js_opens_renders_and_fills_popover(self):
        js = self.js()
        for needle in ('"advisor"', '"/api/item/quantity?id="', '$("qty-pop")',
                       "function openQty", "function closeQty",
                       "function loadQty", "function renderQty",
                       "function renderQtyError", "function renderQtyStack",
                       "auction_data_warming_up",
                       "no_sales_data", "data.max_qty",
                       "lam_eff", "data.stack", "days_95", "typical_days",
                       "sold_per_day", "lowest_price", "market_value",
                       "dataset.id", "Post up to ", "&qty=",
                       "&confidence=0.95", '$("qty-myqty")'):
            self.assertIn(needle, js, needle)

    def test_js_gold_to_copper_and_stale_guard(self):
        js = self.js()
        self.assertIn("10000", js, "gold input converts to copper")
        self.assertRegex(js, r"qtyState\.seq", "out-of-order responses dropped")

    def test_advisor_column_has_th_and_cell(self):
        html = self.html()
        self.assertRegex(html, r"<th scope=\"col\">Advisor</th>",
                         "table gains an Advisor column")
        js = self.js()
        self.assertRegex(js, r"function advisorCell\(item\)")

    def test_qty_pop_css(self):
        css = self.css()
        pop = re.search(r"\.qty-pop\s*\{([^}]*)\}", css)
        self.assertIsNotNone(pop, ".qty-pop rule missing")
        self.assertIn("position: fixed", pop.group(1), "JS anchors it fixed")
        for needle in (".advisor {", ".advisor:hover", ".qty-result",
                       ".qty-stack-line", "#qty-myqty", ".qty-myqty-label"):
            self.assertIn(needle, css, needle)


if __name__ == "__main__":
    unittest.main()
