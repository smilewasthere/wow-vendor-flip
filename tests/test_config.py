import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigDefaultsTest(unittest.TestCase):
    def test_defaults_are_complete(self):
        self.assertEqual(config.defaults()["host"], "127.0.0.1")
        self.assertEqual(config.defaults()["port"], 8008)
        self.assertEqual(config.defaults()["region"], "eu")
        self.assertNotIn("realm_slug", config.defaults())
        self.assertEqual(config.defaults()["daily_budget"], 50000)
        self.assertEqual(config.defaults()["commodity_share"], 0.02)
        self.assertEqual(config.defaults()["min_profit"], 5.0)
        self.assertEqual(config.defaults()["min_roi"], 0.30)
        self.assertEqual(config.defaults()["min_sold_per_day"], 0.5)
        self.assertEqual(config.defaults()["min_sale_rate"], 0.02)
        self.assertEqual(config.defaults()["client_id"], "")
        self.assertEqual(config.defaults()["client_secret"], "")
        self.assertTrue(config.defaults()["tsm_boot_refresh"],
                        "M15: boot refreshes TSM CSVs by default")
        self.assertTrue(config.defaults()["auctions_boot_refresh"],
                        "M16: boot refreshes the commodity wall by default")

    def test_missing_local_file_returns_defaults(self):
        cfg = config.load(os.path.join(ROOT, "does-not-exist.json"))
        self.assertEqual(cfg, config.defaults())


class ConfigOverrideTest(unittest.TestCase):
    def test_local_file_overrides_defaults(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump({"daily_budget": 12345, "client_id": "abc"}, fh)
            path = fh.name
        try:
            cfg = config.load(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg["daily_budget"], 12345)
        self.assertEqual(cfg["client_id"], "abc")
        self.assertEqual(cfg["min_roi"], 0.30)

    def test_invalid_json_raises_config_error(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("{not json")
            path = fh.name
        try:
            with self.assertRaises(config.ConfigError):
                config.load(path)
        finally:
            os.unlink(path)

    def test_utf8_bom_written_by_powershell_or_notepad_is_accepted(self):
        path = None
        try:
            path = os.path.join(tempfile.gettempdir(), "vf-bom-config.json")
            with open(path, "w", encoding="utf-8-sig") as fh:
                json.dump({"daily_budget": 777, "client_id": "x"}, fh)
            cfg = config.load(path)
        finally:
            if path and os.path.exists(path):
                os.unlink(path)
        self.assertEqual(cfg["daily_budget"], 777)
        self.assertEqual(cfg["client_id"], "x")


class SecretHandlingTest(unittest.TestCase):
    def _cfg(self, secret="hunter2-super-secret"):
        return dict(config.defaults(), client_secret=secret)

    def test_redacted_hides_client_secret(self):
        cfg = self._cfg()
        red = config.redacted(cfg)
        self.assertEqual(red["client_secret"], "***")
        self.assertNotIn("hunter2-super-secret", json.dumps(red))

    def test_redacted_keeps_non_secret_fields(self):
        red = config.redacted(self._cfg())
        self.assertEqual(red["client_id"], "")
        self.assertEqual(red["daily_budget"], 50000)

    def test_local_config_is_gitignored(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            self.assertIn("config.local.json", fh.read())


class ExampleConfigTest(unittest.TestCase):
    def test_example_has_blank_credentials(self):
        example = os.path.join(ROOT, "config.local.json.example")
        with open(example, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["client_id"], "")
        self.assertEqual(data["client_secret"], "")
        self.assertIn("region", data)
        self.assertNotIn("realm_slug", data)


if __name__ == "__main__":
    unittest.main()
