import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app import blizzard

SECRET = "ZZ-TEST-SECRET-VALUE"


def fake_cfg(**over):
    cfg = config.defaults()
    cfg["client_id"] = "test-client-id"
    cfg["client_secret"] = SECRET
    cfg.update(over)
    return cfg


class CredentialsTest(unittest.TestCase):
    def test_blank_credentials_raise(self):
        cfg = config.defaults()
        with self.assertRaises(blizzard.CredentialError):
            blizzard.get_token(cfg)

    def test_credential_error_does_not_leak_secret(self):
        cfg = fake_cfg(client_id="")
        with self.assertRaises(blizzard.CredentialError) as ctx:
            blizzard.get_token(cfg)
        self.assertNotIn(SECRET, str(ctx.exception))


class TokenRequestTest(unittest.TestCase):
    def test_token_request_is_correct(self):
        req = blizzard.build_token_request(fake_cfg())
        self.assertTrue(req.full_url.startswith("https://oauth.battle.net/token"))
        self.assertNotIn(SECRET, req.full_url)
        body = req.data.decode("utf-8")
        self.assertIn("grant_type=client_credentials", body)
        # Battle.net rejects a token request that repeats client_id in the body
        # alongside Basic auth (HTTP 401 invalid_client) — credentials travel
        # only in the Authorization header.
        self.assertNotIn("test-client-id", body)
        auth = req.headers["Authorization"]
        self.assertTrue(auth.startswith("Basic "))
        self.assertIn("dGVzdC1jbGllbnQtaWQ6", auth)

    def test_token_response_parsed(self):
        payload = json.dumps(
            {"access_token": "tok-123", "token_type": "bearer", "expires_in": 86399}
        ).encode("utf-8")
        token, expires = blizzard.parse_token_response(200, payload)
        self.assertEqual(token, "tok-123")
        self.assertEqual(expires, 86399)

    def test_token_error_message_redacts_secret(self):
        body = json.dumps({"error": "invalid_client"}).encode("utf-8")
        with self.assertRaises(blizzard.BlizzardError) as ctx:
            blizzard.parse_token_response(400, body, secret=SECRET)
        msg = str(ctx.exception)
        self.assertNotIn(SECRET, msg)
        self.assertIn("400", msg)

    def test_token_error_carries_http_status(self):
        # M8R: routes map status 429 to a retryable 503; the token
        # endpoint must not lose the status the way a bare raise would.
        body = json.dumps({"error": "rate limited"}).encode("utf-8")
        with self.assertRaises(blizzard.BlizzardError) as ctx:
            blizzard.parse_token_response(429, body)
        self.assertEqual(ctx.exception.status, 429)


class ApiGetTest(unittest.TestCase):
    def test_item_url_uses_eu_host_and_static_namespace(self):
        url = blizzard.build_api_url("/data/wow/item/210796", fake_cfg(), "static-eu")
        self.assertEqual(
            url,
            "https://eu.api.blizzard.com/data/wow/item/210796"
            "?namespace=static-eu&locale=en_GB",
        )
        self.assertNotIn(SECRET, url)

    def test_search_url_carries_page_size_and_order(self):
        url = blizzard.build_api_url(
            "/data/wow/search/item",
            fake_cfg(),
            "static-eu",
            params={"_pageSize": 100, "orderby": "id"},
        )
        self.assertIn("_pageSize=100", url)
        self.assertIn("orderby=id", url)

    def test_api_error_redacts_secret(self):
        body = json.dumps({"detail": "rate limited"}).encode("utf-8")
        with self.assertRaises(blizzard.BlizzardError) as ctx:
            blizzard.parse_api_response(429, body, secret=SECRET)
        msg = str(ctx.exception)
        self.assertNotIn(SECRET, msg)
        self.assertIn("429", msg)

    def test_api_error_carries_http_status(self):
        body = json.dumps({"detail": "nope"}).encode("utf-8")
        with self.assertRaises(blizzard.BlizzardError) as ctx:
            blizzard.parse_api_response(404, body)
        self.assertEqual(ctx.exception.status, 404)
        self.assertIsNone(blizzard.BlizzardError("plain").status)


# M11: ported from tests/test_probe_m1.py before that tool was retired -
# these were the only parse_item / is_synthetic_multiple coverage.

ITEM_PAYLOAD = {
    "id": 186282,
    "name": "Sacrificer's Sacramental Cassock",
    "purchase_price": 4157482,
    "purchase_quantity": 1,
    "sell_price": 831496,
    "quality": 3,
    "is_equippable": True,
    "item_class": {"name": "Armor"},
    "item_subclass": {"name": "Cloth"},
    "preview_item": {"requirements": {"reputation": {"faction": {"name": "Test Rep"}}}},
}


class ParseItemTest(unittest.TestCase):
    def test_parses_expected_fields(self):
        row = blizzard.parse_item(ITEM_PAYLOAD)
        self.assertEqual(row["id"], 186282)
        self.assertEqual(row["name"], "Sacrificer's Sacramental Cassock")
        self.assertEqual(row["purchase_price"], 4157482)
        self.assertEqual(row["sell_price"], 831496)
        self.assertEqual(row["purchase_quantity"], 1)
        self.assertEqual(row["is_equippable"], True)
        self.assertEqual(row["item_class"], "Armor")
        self.assertEqual(row["item_subclass"], "Cloth")
        self.assertEqual(row["reputation"], "Test Rep")

    def test_ratio_is_purchase_over_sell(self):
        row = blizzard.parse_item(ITEM_PAYLOAD)
        self.assertAlmostEqual(row["ratio"], 5.0, places=2)

    def test_zero_sell_price_gives_no_ratio(self):
        payload = dict(ITEM_PAYLOAD, sell_price=0, purchase_price=100)
        row = blizzard.parse_item(payload)
        self.assertIsNone(row["ratio"])

    def test_quality_object_is_unpacked_to_its_type(self):
        # live payloads send quality as {"type": "RARE", "name": "Rare"}
        payload = dict(ITEM_PAYLOAD, quality={"type": "RARE", "name": "Rare"})
        row = blizzard.parse_item(payload)
        self.assertEqual(row["quality"], "RARE")

    def test_missing_nested_fields_are_tolerated(self):
        row = blizzard.parse_item({"id": 1, "name": "X"})
        self.assertEqual(row["purchase_price"], 0)
        self.assertEqual(row["sell_price"], 0)
        self.assertEqual(row["purchase_quantity"], 0)
        self.assertIsNone(row["ratio"])
        self.assertEqual(row["item_class"], "")
        self.assertEqual(row["reputation"], "")


class MultipleHypothesisTest(unittest.TestCase):
    def test_true_inside_4_to_5_band(self):
        self.assertTrue(blizzard.is_synthetic_multiple(2500, 625))
        self.assertTrue(blizzard.is_synthetic_multiple(4157482, 831496))

    def test_false_outside_band(self):
        self.assertFalse(blizzard.is_synthetic_multiple(5, 5))
        self.assertFalse(blizzard.is_synthetic_multiple(100, 10))
        self.assertFalse(blizzard.is_synthetic_multiple(100, 0))


if __name__ == "__main__":
    unittest.main()
