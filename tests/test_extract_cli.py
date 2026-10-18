import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import blizzard, config, tsm
from tools import extract as cli

REALM_HEADER = "itemId,name,marketValue,minBuyout,recent,historical,updatedAt"
REGION_HEADER = ("itemId,name,marketValue,historical,avgSalePrice,saleRate,"
                 "soldPerDay,updatedAt")


def write_tsm_cache(cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    realm = "%s\n2320,Coarse Thread,10,10,10,10,2026-10-12T00:00:00Z\n" % REALM_HEADER
    region = ("%s\n2320,Coarse Thread,10,10,0.5,1.0,2026-10-12T00:00:00Z\n"
              % REGION_HEADER)
    for name in ("region-items.csv", "commodities.csv"):
        text = region if name == "region-items.csv" else realm
        with open(os.path.join(cache_dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.dir, "config.local.json")
        self.cache_dir = os.path.join(self.dir, "tsm")
        write_tsm_cache(self.cache_dir)

    def write_config(self, creds=True):
        data = {"region": "eu"}
        if creds:
            data["client_id"] = "test-client-id"
            data["client_secret"] = "SEKRIT-VALUE"
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            import json
            json.dump(data, fh)

    def run_check(self):
        out = io.StringIO()
        with mock.patch("app.extract.default_cache_dir",
                        return_value=self.cache_dir):
            with redirect_stdout(out):
                code = cli.main(["--config", self.cfg_path, "--check"])
        return code, out.getvalue()

    def test_check_ready_reports_universe_and_never_prints_the_secret(self):
        self.write_config(creds=True)
        code, text = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("1 ids", text)
        self.assertIn("ready", text)
        self.assertNotIn("SEKRIT-VALUE", text)
        self.assertNotIn("test-client-id", text)
        self.assertIn("***", text)

    def test_check_without_credentials_fails(self):
        self.write_config(creds=False)
        code, text = self.run_check()
        self.assertEqual(code, 2)
        self.assertIn("NOT READY", text)


class RunPathTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.dir, "config.local.json")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            fh.write('{"client_id": "x", "client_secret": "y"}')

    def test_run_without_credentials_returns_2(self):
        path = os.path.join(self.dir, "empty.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--config", path])
        self.assertEqual(code, 2)
        self.assertIn("missing credentials", out.getvalue())

    def test_keyboard_interrupt_returns_130_with_resume_hint(self):
        out = io.StringIO()
        with mock.patch("app.extract.run_extraction",
                        side_effect=KeyboardInterrupt):
            with redirect_stdout(out):
                code = cli.main(["--config", self.cfg_path])
        self.assertEqual(code, 130)
        self.assertIn("resume", out.getvalue())

    def test_blizzard_error_returns_1_with_resume_hint(self):
        out = io.StringIO()
        with mock.patch("app.extract.run_extraction",
                        side_effect=blizzard.BlizzardError("HTTP 500 flaky")):
            with redirect_stdout(out):
                code = cli.main(["--config", self.cfg_path])
        self.assertEqual(code, 1)
        text = out.getvalue()
        self.assertIn("HTTP 500 flaky", text)
        self.assertIn("resume", text)

    def test_negative_limit_is_an_argument_error(self):
        with mock.patch("app.extract.run_extraction") as fake:
            with self.assertRaises(SystemExit) as caught:
                cli.main(["--limit", "-1"])
            self.assertEqual(caught.exception.code, 2)
            fake.assert_not_called()

    def test_zero_limit_is_an_argument_error(self):
        with mock.patch("app.extract.run_extraction") as fake:
            with self.assertRaises(SystemExit) as caught:
                cli.main(["--limit", "0"])
            self.assertEqual(caught.exception.code, 2)
            fake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
