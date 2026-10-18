"""M11: multiple Blizzard API keys with automatic rotation on HTTP 429.

Keys live in the local database; `config.local.json` stays valid as the
fallback when the table is empty. `apply(conn, cfg)` injects the active
key's credentials into a copy of cfg, so every existing call site keeps
working unchanged. On rate-limit, `mark_cooldown` parks the current key
and `apply` returns the next one.
"""

import datetime
import sqlite3

from app import blizzard

COOLDOWN_MINUTES = 10


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _mask(secret):
    if not secret:
        return ""
    tail = secret[-4:] if len(secret) >= 4 else secret
    return "****" + tail


def add(conn, label, client_id, client_secret):
    """Insert a key. Raises ValueError on blanks or a duplicate client_id."""
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required")
    try:
        cur = conn.execute(
            "INSERT INTO api_keys (label, client_id, client_secret,"
            " created_at) VALUES (?, ?, ?, ?)",
            ((label or "").strip(), client_id, client_secret,
             _now().isoformat()))
    except sqlite3.IntegrityError:
        raise ValueError("that client_id is already stored")
    return {"id": cur.lastrowid, "label": (label or "").strip(),
            "client_id": client_id}


def remove(conn, key_id):
    conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))


def list_masked(conn):
    rows = conn.execute(
        "SELECT id, label, client_id, client_secret, enabled,"
        " cooldown_until FROM api_keys ORDER BY id").fetchall()
    active = active_key(conn)
    active_id = active["id"] if active else None
    out = []
    for row in rows:
        out.append({
            "id": row[0], "label": row[1], "client_id": row[2],
            "secret_masked": _mask(row[3]),
            "enabled": bool(row[4]), "cooldown_until": row[5],
            "active": row[0] == active_id,
        })
    return out


def active_key(conn):
    """First enabled key whose cooldown has passed, else None."""
    now = _now().isoformat()
    row = conn.execute(
        "SELECT id, label, client_id, client_secret FROM api_keys"
        " WHERE enabled = 1"
        " AND (cooldown_until IS NULL OR cooldown_until <= ?)"
        " ORDER BY id LIMIT 1", (now,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "label": row[1],
            "client_id": row[2], "client_secret": row[3]}


def available(conn):
    return active_key(conn) is not None


def apply(conn, cfg):
    """cfg copy with the active key's credentials, or cfg unchanged."""
    key = active_key(conn)
    if key is None:
        return cfg
    out = dict(cfg)
    out["client_id"] = key["client_id"]
    out["client_secret"] = key["client_secret"]
    return out


def mark_cooldown(conn, client_id, minutes=COOLDOWN_MINUTES):
    """Park a key for a while (default 10 minutes)."""
    if not client_id:
        return
    until = (_now() + datetime.timedelta(minutes=minutes)).isoformat()
    conn.execute("UPDATE api_keys SET cooldown_until = ?"
                 " WHERE client_id = ?", (until, client_id))


def test_credentials(client_id, client_secret, get_token=None):
    """Live token exchange. Returns (ok, message) - never echoes secrets."""
    fn = get_token or blizzard.get_token
    probe = {"client_id": (client_id or "").strip(),
             "client_secret": (client_secret or "").strip()}
    try:
        fn(probe)
    except Exception as exc:  # noqa: BLE001 - any failure is "not working"
        message = str(exc)
        secret = probe["client_secret"]
        if secret:
            message = message.replace(secret, "****")
        return False, message
    return True, "token acquired"
