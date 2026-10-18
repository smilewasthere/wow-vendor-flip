"""JSON API routes. GET handlers receive (cfg, query), POST handlers (cfg, body).

Handlers return a dict (HTTP 200) or a (status, dict) tuple.
"""

import math
import sqlite3

from app import (api_keys, auctions, blizzard, db, extract, icons, pricing,
                 quantity, settings, tsm, vendor_check)

VERSION = "0.0.1-M16"
MAX_ITEM_ID = 10 ** 9  # sqlite int64 is the real ceiling; ids today are < 1e6
MAX_NOTES = 500
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
SORTS = {
    "score": "score",
    "profit": "net_profit_unit",
    "roi": "roi",
    "sold": "expected_sold_per_day",
}
KEEP_VERDICTS = ("GOOD", "CHECK VENDOR")


def _db_path(cfg):
    return cfg.get("db_path") or extract.default_db_path()


def _cache_dir(cfg):
    return cfg.get("cache_dir") or extract.default_cache_dir()


def _parse_item_id(raw):
    """Positive int within bounds, else 0 (callers turn 0 into a 400)."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if 0 < value <= MAX_ITEM_ID else 0


def health(cfg, query=None):
    return {"status": "ok", "version": VERSION}


def extract_status(cfg, query=None):
    return extract.status(cfg, db_path=_db_path(cfg))


def extract_start(cfg, body=None):
    body = body or {}
    restart = bool(body.get("restart"))
    return extract.start_async(cfg, db_path=_db_path(cfg), restart=restart)


def vendor_check_status(cfg, query=None):
    return vendor_check.status(cfg, db_path=_db_path(cfg))


def vendor_check_start(cfg, body=None):
    body = body or {}
    restart = bool(body.get("restart"))
    return vendor_check.start_async(cfg, db_path=_db_path(cfg),
                                    cache_dir=_cache_dir(cfg),
                                    keep_existing=not restart)


def vendor_check_cancel(cfg, body=None):
    return {"cancelled": vendor_check.cancel()}


def item_icon(cfg, query):
    item_id = _parse_item_id(((query or {}).get("id") or [""])[0])
    if not item_id:
        return 400, {"ok": False, "error": "id must be a positive integer"}
    conn = None
    try:
        conn = db.connect(_db_path(cfg))
        eff = settings.apply(cfg, conn)
        eff = api_keys.apply(conn, eff)  # stored keys override the file
        try:
            name = icons.get_icon(eff, conn, item_id)
        except blizzard.BlizzardError as exc:
            # Upstream text stays out of the response (and any secrets with
            # it - blizzard.py redacts, this keeps the route honest anyway).
            if getattr(exc, "status", None) == 429:
                return 503, {"ok": False,
                             "error": "upstream busy - retry shortly"}
            return 502, {"ok": False, "error": "icon lookup failed"}
    except sqlite3.OperationalError:
        return 503, {"ok": False, "error": "database busy - try again"}
    finally:
        if conn is not None:
            conn.close()
    return {"ok": True, "id": item_id, "icon": name,
            "url": icons.icon_url(eff, name)}


def items(cfg, query):
    """GET /api/items: scored candidates with verdict counts (PLAN 5.6)."""
    query = query or {}
    raw_limit = (query.get("limit") or [str(DEFAULT_LIMIT)])[0]
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 0
    if not 1 <= limit <= MAX_LIMIT:
        return 400, {"ok": False,
                     "error": "limit must be 1..%d" % MAX_LIMIT}
    sort = (query.get("sort") or ["score"])[0]
    if sort not in SORTS:
        return 400, {"ok": False, "error": "unknown sort field"}
    verdict = (query.get("verdict") or [None])[0]
    if verdict is not None and verdict not in pricing.VERDICTS:
        return 400, {"ok": False, "error": "unknown verdict"}

    conn = None
    try:
        conn = db.connect(_db_path(cfg))
        candidates = db.all_candidates(conn)
        th = pricing.thresholds(cfg, conn)
        vmap = db.vendor_source_map(conn)
    except sqlite3.OperationalError:
        return 503, {"ok": False, "error": "database busy - try again"}
    finally:
        if conn is not None:
            conn.close()
    try:
        ctx = dict(pricing.context(cfg, _cache_dir(cfg)))
    except (tsm.TsmError, OSError, UnicodeDecodeError):
        return 503, {"ok": False, "error": "TSM cache unavailable"}
    ctx["vendor_sources"] = vmap

    scored = pricing.score_all(candidates, ctx, th)
    counts = {}
    for row in scored:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    total = len(scored)
    if verdict is not None:
        scored = [row for row in scored if row["verdict"] == verdict]
    scored.sort(key=lambda row: row[SORTS[sort]], reverse=True)
    # The limit must never hide the verdicts users act on: every GOOD and
    # CHECK VENDOR row ships, the rest of the budget is top-score fill.
    keep = [row for row in scored if row["verdict"] in KEEP_VERDICTS]
    rest = [row for row in scored if row["verdict"] not in KEEP_VERDICTS]
    page = keep + rest[:max(0, limit - len(keep))]
    # M12: Undermine Exchange links removed (region-only pricing, no realm).
    for row in page:
        row["wowhead"] = "https://www.wowhead.com/item=%d" % row["id"]
    return {"total": total, "counts": counts, "items": page}


def item_quantity(cfg, query):
    """GET /api/item/quantity: units to post at `price` so all sell within
    24 h at `confidence` (M16), plus an optional verdict block for the
    quantity you own (`qty` param). Demand is TSM's region sold/day as-is
    (a whole-region total - no realm scaling; PLAN.md 2.1). Cache-only
    reads; a missing cache 503s and kicks a background refresh instead of
    blocking on the network."""
    query = query or {}
    item_id = _parse_item_id((query.get("id") or [""])[0])
    if not item_id:
        return 400, {"ok": False, "error": "id must be a positive integer"}
    try:
        price = int((query.get("price") or [""])[0])
    except (TypeError, ValueError):
        price = 0
    if price <= 0:
        return 400, {"ok": False,
                     "error": "price must be a positive integer (copper)"}
    try:
        conf = float((query.get("confidence") or ["0.95"])[0])
    except (TypeError, ValueError):
        conf = -1.0
    if conf not in (0.9, 0.95, 0.99):  # nan misses the tuple on its own
        return 400, {"ok": False,
                     "error": "confidence must be 0.90, 0.95 or 0.99"}
    raw_qty = (query.get("qty") or [None])[0]
    qty = None
    if raw_qty is not None:
        try:
            qty = int(raw_qty)
        except (TypeError, ValueError):
            qty = 0
        if not 0 < qty <= 10 ** 9:
            return 400, {"ok": False,
                         "error": "qty must be a positive integer"}

    conn = None
    try:
        conn = db.connect(_db_path(cfg))
        eff = settings.apply(cfg, conn)  # region override must match the
    except sqlite3.OperationalError:      # ladder the boot refresh fetched
        return 503, {"ok": False, "error": "database busy - try again"}
    finally:
        if conn is not None:
            conn.close()

    status = auctions.ladder_status(eff)
    if status["warming"]:
        auctions.maybe_refresh_async(eff)
        return 503, {"ok": False, "error": "auction_data_warming_up",
                     "refreshing": status["refreshing"]}
    if status["stale"]:
        auctions.maybe_refresh_async(eff)  # serve old data, refresh behind
    loaded = auctions.get_ladder(eff)
    if loaded is None:
        auctions.maybe_refresh_async(eff)
        return 503, {"ok": False, "error": "auction_data_warming_up"}
    ladder, _meta = loaded
    try:
        ctx = pricing.context(eff, _cache_dir(eff))
    except (tsm.TsmError, OSError, UnicodeDecodeError):
        return 503, {"ok": False, "error": "TSM cache unavailable"}
    region_row = ctx["region"].get(item_id)
    name = str((region_row or {}).get("name") or "")

    if item_id not in ladder:
        return {"ok": True, "commodity": False, "id": item_id, "name": name,
                "message": "commodities only - no sold/day data "
                           "for this item"}
    if region_row is None:
        return {"ok": False, "error": "no_sales_data", "id": item_id}

    try:
        sold = float(region_row.get("soldPerDay") or 0)
    except (TypeError, ValueError):
        sold = 0.0
    if not math.isfinite(sold) or sold < 0:
        sold = 0.0
    try:
        market_value = int(region_row.get("marketValue") or 0)
    except (TypeError, ValueError):
        market_value = 0

    levels = ladder.get(item_id) or []
    ahead, _same = auctions.ahead_units(levels, price)
    stretch = quantity.price_stretch(price, market_value)
    lam_eff = sold * stretch
    total = quantity.max_poisson_total(lam_eff, conf)
    max_qty = total - ahead if total > ahead else 0
    achieved = quantity.achieved_confidence(lam_eff, ahead, max_qty)
    days = quantity.expected_days(ahead + max_qty, lam_eff)
    if days is None or not math.isfinite(days):  # JSON has no Infinity
        days = None
    stack = None
    if qty is not None:
        typical = quantity.expected_days(ahead + qty, lam_eff)
        if not math.isfinite(typical):
            typical = None
        days_95 = quantity.days_for_confidence(lam_eff, ahead, qty, conf)
        if not math.isfinite(days_95):
            days_95 = None
        stack = {
            "qty": qty,
            "achieved": quantity.achieved_confidence(lam_eff, ahead, qty),
            "typical_days": typical,
            "days_95": days_95,
        }
    return {
        "ok": True, "commodity": True, "id": item_id, "name": name,
        "price": price, "confidence": conf,
        "market_value": market_value,
        "sold_per_day": sold,
        "stretch": stretch, "lam_eff": lam_eff,
        "ahead_units": ahead,
        "max_total": total, "max_qty": max_qty,
        "achieved": achieved, "expected_days": days,
        "stack": stack,
        "lowest_price": levels[0][0] if levels else 0,
        "tsm_updated_at": str(region_row.get("updatedAt") or ""),
        "auctions_fetched_at": status["fetched_at"],
        "refreshing": status["refreshing"],
    }


def item_verify(cfg, body):
    body = body or {}
    raw_id = body.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int) \
            or not 0 < raw_id <= MAX_ITEM_ID:
        return 400, {"ok": False, "error": "id must be a positive integer"}
    flag = body.get("vendor_verified")
    if not isinstance(flag, bool):
        return 400, {"ok": False,
                     "error": "vendor_verified must be true or false"}
    conn = None
    try:
        conn = db.connect(_db_path(cfg))
        db.init_schema(conn)
        changed = db.set_vendor_verified(conn, raw_id, flag)
    except sqlite3.OperationalError:
        return 503, {"ok": False, "error": "database busy - try again"}
    finally:
        if conn is not None:
            conn.close()
    if not changed:
        return 404, {"ok": False, "error": "unknown item id"}
    return {"ok": True, "id": raw_id, "vendor_verified": 1 if flag else 0}


def item_notes(cfg, body):
    body = body or {}
    raw_id = body.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int) \
            or not 0 < raw_id <= MAX_ITEM_ID:
        return 400, {"ok": False, "error": "id must be a positive integer"}
    notes = body.get("notes")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES:
        return 400, {"ok": False,
                     "error": "notes must be a string of at most %d"
                              " characters" % MAX_NOTES}
    conn = None
    try:
        conn = db.connect(_db_path(cfg))
        db.init_schema(conn)
        changed = db.set_notes(conn, raw_id, notes)
    except sqlite3.OperationalError:
        return 503, {"ok": False, "error": "database busy - try again"}
    finally:
        if conn is not None:
            conn.close()
    if not changed:
        return 404, {"ok": False, "error": "unknown item id"}
    return {"ok": True, "id": raw_id, "notes": notes}


def _settings_payload(cfg, conn):
    overrides = settings.stored(conn)
    eff = settings.apply(cfg, conn)
    return {"ok": True,
            "settings": {key: eff.get(key) for key in settings.SPECS},
            "thresholds": pricing.thresholds(cfg, conn),
            "overrides": sorted(overrides)}


def settings_get(cfg, query=None):
    conn = None
    try:
        conn = db.connect(_db_path(cfg))
        return _settings_payload(cfg, conn)
    except sqlite3.OperationalError:
        return 503, {"ok": False, "error": "database busy - try again"}
    finally:
        if conn is not None:
            conn.close()


def settings_post(cfg, body):
    body = body or {}
    if "settings" in body:
        mapping = body.get("settings")
    elif "key" in body:
        key = body.get("key")
        if not isinstance(key, str):
            return 400, {"ok": False, "error": "unknown setting: %s" % (key,)}
        mapping = {key: body.get("value")}
    else:
        mapping = None
    if not isinstance(mapping, dict) or not mapping:
        return 400, {"ok": False, "error": "settings object required"}
    normalized = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or key not in settings.SPECS:
            return 400, {"ok": False,
                         "error": "unknown setting: %s" % (key,)}
        if value is None:
            normalized[key] = None
            continue
        try:
            normalized[key] = settings.coerce(key, value)
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
    conn = None
    try:
        conn = db.connect(_db_path(cfg))
        db.init_schema(conn)
        settings.write(conn, normalized)
        return _settings_payload(cfg, conn)
    except sqlite3.OperationalError:
        return 503, {"ok": False, "error": "database busy - try again"}
    finally:
        if conn is not None:
            conn.close()


def api_keys_get(cfg, query=None):
    conn = None
    try:
        conn = db.connect(_db_path(cfg))
        db.init_schema(conn)
        return {"ok": True, "keys": api_keys.list_masked(conn)}
    except sqlite3.OperationalError:
        return 503, {"ok": False, "error": "database busy - try again"}
    finally:
        if conn is not None:
            conn.close()


def api_keys_post(cfg, body):
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "object required"}
    action = body.get("action")
    conn = None
    try:
        conn = db.connect(_db_path(cfg))
        db.init_schema(conn)
        if action == "add":
            try:
                key = api_keys.add(conn, body.get("label") or "",
                                   body.get("client_id"),
                                   body.get("client_secret"))
            except ValueError as exc:
                return 400, {"ok": False, "error": str(exc)}
            except sqlite3.IntegrityError:
                return 400, {"ok": False,
                             "error": "that client_id is already stored"}
            conn.commit()
            return {"ok": True, "id": key["id"]}
        if action == "delete":
            key_id = body.get("id")
            if not isinstance(key_id, int):
                return 400, {"ok": False, "error": "id required"}
            api_keys.remove(conn, key_id)
            conn.commit()
            return {"ok": True}
        if action == "test":
            client_id = body.get("client_id")
            client_secret = body.get("client_secret")
            if client_id is None and isinstance(body.get("id"), int):
                row = conn.execute(
                    "SELECT client_id, client_secret FROM api_keys"
                    " WHERE id = ?", (body["id"],)).fetchone()
                if row is None:
                    return 404, {"ok": False, "error": "key not found"}
                client_id, client_secret = row
            if not client_id or not client_secret:
                return 400, {"ok": False,
                             "error": "client_id and client_secret required"}
            ok, message = api_keys.test_credentials(client_id, client_secret)
            return {"ok": ok, "message": message}
        return 400, {"ok": False, "error": "unknown action"}
    except sqlite3.OperationalError:
        return 503, {"ok": False, "error": "database busy - try again"}
    finally:
        if conn is not None:
            conn.close()


ROUTES = {
    "/api/health": health,
    "/api/extract/status": extract_status,
    "/api/icon": item_icon,
    "/api/items": items,
    "/api/item/quantity": item_quantity,
    "/api/settings": settings_get,
    "/api/vendor-check/status": vendor_check_status,
    "/api/api-keys": api_keys_get,
}

POST_ROUTES = {
    "/api/extract/start": extract_start,
    "/api/item/verify": item_verify,
    "/api/item/notes": item_notes,
    "/api/settings": settings_post,
    "/api/vendor-check/start": vendor_check_start,
    "/api/vendor-check/cancel": vendor_check_cancel,
    "/api/api-keys": api_keys_post,
}
