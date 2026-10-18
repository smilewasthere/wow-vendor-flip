"""M11: multiple Blizzard API keys with rotation on HTTP 429."""

import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import api_keys, blizzard, config, db
from app.routes import api


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class ApiKeysSchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowvf-keys-")
        self.path = os.path.join(self.tmp, "test.sqlite")
        self.conn = db.connect(self.path)
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_table_exists_and_is_idempotent(self):
        db.init_schema(self.conn)
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("api_keys", names)


class ApiKeysStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowvf-keys-")
        self.path = os.path.join(self.tmp, "test.sqlite")
        self.conn = db.connect(self.path)
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_then_list_masks_the_secret(self):
        key = api_keys.add(self.conn, "main", "ID-1", "SECRET-ONE")
        self.conn.commit()
        rows = api_keys.list_masked(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "main")
        self.assertEqual(rows[0]["client_id"], "ID-1")
        self.assertNotIn("SECRET-ONE", repr(rows))
        self.assertTrue(rows[0]["secret_masked"].endswith("ONE"))
        self.assertEqual(key["client_id"], "ID-1")

    def test_add_rejects_duplicate_client_id_and_blank_fields(self):
        api_keys.add(self.conn, "a", "ID-1", "S1")
        with self.assertRaises(ValueError):
            api_keys.add(self.conn, "b", "ID-1", "S2")
        with self.assertRaises(ValueError):
            api_keys.add(self.conn, "c", "", "S3")
        with self.assertRaises(ValueError):
            api_keys.add(self.conn, "d", "ID-4", "")

    def test_remove(self):
        key = api_keys.add(self.conn, "main", "ID-1", "S1")
        api_keys.remove(self.conn, key["id"])
        self.assertEqual(api_keys.list_masked(self.conn), [])

    def test_active_key_prefers_first_enabled_and_uncooled(self):
        api_keys.add(self.conn, "one", "ID-1", "S1")
        api_keys.add(self.conn, "two", "ID-2", "S2")
        self.assertEqual(api_keys.active_key(self.conn)["client_id"], "ID-1")
        api_keys.mark_cooldown(self.conn, "ID-1")
        self.assertEqual(api_keys.active_key(self.conn)["client_id"], "ID-2")
        api_keys.mark_cooldown(self.conn, "ID-2")
        self.assertIsNone(api_keys.active_key(self.conn))

    def test_cooldown_expires_naturally(self):
        key = api_keys.add(self.conn, "one", "ID-1", "S1")
        past = (utcnow() - datetime.timedelta(seconds=5)).isoformat()
        self.conn.execute("UPDATE api_keys SET cooldown_until=? WHERE id=?",
                          (past, key["id"]))
        self.conn.commit()
        self.assertEqual(api_keys.active_key(self.conn)["client_id"], "ID-1")

    def test_apply_overrides_cfg_only_with_an_active_key(self):
        cfg = {"client_id": "FILE-ID", "client_secret": "FILE-SECRET"}
        self.assertEqual(api_keys.apply(self.conn, cfg)["client_id"],
                         "FILE-ID")
        api_keys.add(self.conn, "main", "DB-ID", "DB-SECRET")
        out = api_keys.apply(self.conn, cfg)
        self.assertEqual(out["client_id"], "DB-ID")
        self.assertEqual(out["client_secret"], "DB-SECRET")
        # original cfg dict is not mutated
        self.assertEqual(cfg["client_id"], "FILE-ID")

    def test_available_reports_whether_any_key_can_be_rotated_to(self):
        self.assertFalse(api_keys.available(self.conn))
        api_keys.add(self.conn, "one", "ID-1", "S1")
        self.assertTrue(api_keys.available(self.conn))


class ApiKeysRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowvf-keys-")
        self.path = os.path.join(self.tmp, "test.sqlite")
        conn = db.connect(self.path)
        db.init_schema(conn)
        conn.close()
        self.cfg = config.defaults()
        self.cfg["db_path"] = self.path
        self.cfg["cache_dir"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, cfg, body):
        out = api.api_keys_post(cfg, body)
        if isinstance(out, tuple):
            return out
        return 200, out

    def _get_payload(self, cfg):
        out = api.api_keys_get(cfg, None)
        if isinstance(out, tuple):
            return out[1]
        return out

    def test_get_masks_and_post_add_delete_roundtrip(self):
        status, body = self._post(self.cfg, {
            "action": "add", "label": "main",
            "client_id": "ID-1", "client_secret": "SECRET-ONE"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)
        payload = self._get_payload(self.cfg)
        self.assertNotIn("SECRET-ONE", repr(payload))
        key_id = payload["keys"][0]["id"]
        status, body = self._post(self.cfg,
                                  {"action": "delete", "id": key_id})
        self.assertEqual(status, 200)
        self.assertEqual(self._get_payload(self.cfg)["keys"], [])

    def test_add_missing_fields_is_400(self):
        status, body = self._post(self.cfg, {"action": "add"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_test_action_uses_blizzard_token_exchange(self):
        with mock.patch.object(api_keys.blizzard, "get_token") as fake:
            fake.return_value = ("TOKEN", None)
            status, body = self._post(self.cfg, {
                "action": "test", "client_id": "ID-1",
                "client_secret": "S1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)
        self.assertEqual(fake.call_args[0][0]["client_id"], "ID-1")
        # the secret must never come back in the response
        self.assertNotIn("S1", repr(body))

    def test_test_action_failure_is_reported_without_the_secret(self):
        err = blizzard.BlizzardError("401 for S1-SECRET")
        err.status = 401
        with mock.patch.object(api_keys.blizzard, "get_token") as fake:
            fake.side_effect = err
            status, body = self._post(self.cfg, {
                "action": "test", "client_id": "ID-1",
                "client_secret": "S1-SECRET"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], False)
        self.assertNotIn("S1-SECRET", repr(body))


if __name__ == "__main__":
    unittest.main()
