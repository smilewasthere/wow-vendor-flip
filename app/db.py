"""SQLite storage: schema, extraction state, candidate upserts.

`data/vendorflips.sqlite` is created here (first milestone that needs it, M3).
Tables: `meta` (app metadata), `extraction_state` (resumable extraction
progress), `candidate_items`, `icon_cache`, `settings` — PLAN section 3.
Manual columns (`vendor_verified`, `excluded`, `flagged_risky`, `notes`) are
never touched by a re-extraction upsert — they are the user's ground truth.
"""

import json
import os
import sqlite3

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS extraction_state (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS candidate_items (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  purchase_price INTEGER NOT NULL DEFAULT 0,
  purchase_quantity INTEGER NOT NULL DEFAULT 0,
  sell_price INTEGER NOT NULL DEFAULT 0,
  quality TEXT NOT NULL DEFAULT '',
  is_equippable INTEGER NOT NULL DEFAULT 0,
  item_class TEXT NOT NULL DEFAULT '',
  item_subclass TEXT NOT NULL DEFAULT '',
  synthetic_multiple INTEGER NOT NULL DEFAULT 0,
  vendor_verified INTEGER NOT NULL DEFAULT 0,
  excluded INTEGER NOT NULL DEFAULT 0,
  flagged_risky INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  extracted_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS icon_cache (
  id INTEGER PRIMARY KEY,
  icon TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS scanned_ids (
  id INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS vendor_sources (
  item_id INTEGER PRIMARY KEY,
  sold INTEGER NOT NULL,
  npcs TEXT NOT NULL DEFAULT '[]',
  wowhead_cost INTEGER,
  checked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL DEFAULT '',
  client_id TEXT NOT NULL UNIQUE,
  client_secret TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  cooldown_until TEXT,
  created_at TEXT NOT NULL
);
"""

UPSERT = """
INSERT INTO candidate_items (
  id, name, purchase_price, purchase_quantity, sell_price, quality,
  is_equippable, item_class, item_subclass, synthetic_multiple, extracted_at
) VALUES (
  :id, :name, :purchase_price, :purchase_quantity, :sell_price, :quality,
  :is_equippable, :item_class, :item_subclass, :synthetic_multiple, :extracted_at
)
ON CONFLICT(id) DO UPDATE SET
  name = excluded.name,
  purchase_price = excluded.purchase_price,
  purchase_quantity = excluded.purchase_quantity,
  sell_price = excluded.sell_price,
  quality = excluded.quality,
  is_equippable = excluded.is_equippable,
  item_class = excluded.item_class,
  item_subclass = excluded.item_subclass,
  synthetic_multiple = excluded.synthetic_multiple,
  extracted_at = excluded.extracted_at
"""


def connect(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    # WAL: readers (the UI) keep working while extraction holds a write
    # transaction; unsupported (and unnecessary) for :memory:.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    if meta_get(conn, "schema_version") is None:
        meta_set(conn, "schema_version", SCHEMA_VERSION)
    conn.commit()


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def state_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM extraction_state WHERE key = ?",
                       (key,)).fetchone()
    return row[0] if row else default


def state_set(conn, key, value):
    conn.execute(
        "INSERT INTO extraction_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def state_bulk(conn, mapping):
    for key, value in mapping.items():
        state_set(conn, key, value)


def state_dict(conn):
    return {row[0]: row[1]
            for row in conn.execute("SELECT key, value FROM extraction_state "
                                    "ORDER BY key")}


def upsert_candidate(conn, row):
    payload = dict(row)
    for key, default in (("name", ""), ("quality", ""), ("item_class", ""),
                         ("item_subclass", ""), ("extracted_at", "")):
        payload.setdefault(key, default)
    for key in ("purchase_price", "purchase_quantity", "sell_price",
                "is_equippable", "synthetic_multiple"):
        payload.setdefault(key, 0)
    conn.execute(UPSERT, payload)


def expire_candidate(conn, item_id, extracted_at):
    """An id that vanished upstream (404): drop it unless the user marked it."""
    cur = conn.execute(
        "DELETE FROM candidate_items WHERE id = ? AND vendor_verified = 0 "
        "AND excluded = 0 AND flagged_risky = 0 AND notes = ''",
        (item_id,))
    if cur.rowcount == 0:
        conn.execute(
            "UPDATE candidate_items SET purchase_price = 0, "
            "purchase_quantity = 0, sell_price = 0, extracted_at = ? "
            "WHERE id = ?",
            (extracted_at, item_id))


def mark_scanned(conn, ids):
    conn.executemany(
        "INSERT OR IGNORE INTO scanned_ids (id) VALUES (?)",
        ((int(i),) for i in ids))


def scanned_id_set(conn):
    return {row[0] for row in conn.execute("SELECT id FROM scanned_ids")}


def truncate_scanned(conn):
    conn.execute("DELETE FROM scanned_ids")


def count_candidates(conn):
    return conn.execute("SELECT COUNT(*) FROM candidate_items").fetchone()[0]


def candidate_ids(conn):
    return {row[0] for row in conn.execute("SELECT id FROM candidate_items")}


def all_candidates(conn):
    """Every candidate row as a dict (M5 scoring works on dicts)."""
    cur = conn.execute("SELECT * FROM candidate_items ORDER BY id")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def set_vendor_verified(conn, item_id, value):
    """Persist only the vendor_verified flag; other manual columns untouched."""
    cur = conn.execute(
        "UPDATE candidate_items SET vendor_verified = ? WHERE id = ?",
        (1 if value else 0, item_id))
    conn.commit()
    return cur.rowcount > 0


def set_notes(conn, item_id, notes):
    """Persist only the notes column (empty string clears)."""
    cur = conn.execute(
        "UPDATE candidate_items SET notes = ? WHERE id = ?",
        (notes, item_id))
    conn.commit()
    return cur.rowcount > 0


def upsert_vendor_source(conn, item_id, sold, npcs, wowhead_cost,
                         checked_at, status="ok"):
    """Record one Wowhead verification result (M9 vendor-check job)."""
    conn.execute(
        "INSERT INTO vendor_sources "
        "(item_id, sold, npcs, wowhead_cost, checked_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET "
        "  sold = excluded.sold, npcs = excluded.npcs, "
        "  wowhead_cost = excluded.wowhead_cost, "
        "  checked_at = excluded.checked_at, status = excluded.status",
        (int(item_id), 1 if sold else 0, json.dumps(npcs or []),
         wowhead_cost, checked_at, status))
    conn.commit()


def vendor_source_map(conn, item_ids=None):
    """{item_id: {sold, npcs, wowhead_cost, checked_at, status}} (all rows if ids None)."""
    if item_ids is None:
        cur = conn.execute("SELECT * FROM vendor_sources")
    else:
        ids = [int(i) for i in item_ids]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        cur = conn.execute(
            "SELECT * FROM vendor_sources WHERE item_id IN (%s)" % marks, ids)
    cols = [d[0] for d in cur.description]
    out = {}
    for row in cur.fetchall():
        rec = dict(zip(cols, row))
        out[rec["item_id"]] = {
            "sold": rec["sold"],
            "npcs": json.loads(rec["npcs"] or "[]"),
            "wowhead_cost": rec["wowhead_cost"],
            "checked_at": rec["checked_at"],
            "status": rec["status"],
        }
    return out


def vendor_sources_stats(conn):
    """Tallies for the vendor-check UI: sold / not_sold / blocked / error / total."""
    row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN sold = 1 THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN sold = 0 AND status = 'ok' THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), "
        "  COUNT(*) FROM vendor_sources").fetchone()
    return {
        "sold": row[0] or 0,
        "not_sold": row[1] or 0,
        "blocked": row[2] or 0,
        "error": row[3] or 0,
        "total": row[4] or 0,
    }
