"""TSM public CSV client: 3 files, conditional GET, tolerant name parsing.

https://tradeskillmaster.com/public-data - no API key, no sign-in, no rate limits.
Realm and commodities refresh about every 3 h, region about daily.

Quirk handled here: TSM writes item names containing commas WITHOUT quoting them,
so a row can have more fields than its header. Field 0 is the itemId and the last
N-2 fields are the values after `name`; everything in between is the name.
"""

import csv
import datetime
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request

BASE = "https://public-data.tradeskillmaster.com"
USER_AGENT = "wow-vendor-flip/0.0.1-M9 (public TSM data)"
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = (2, 4)  # seconds before attempt 2 and 3 (429/5xx only)
MAX_RETRY_AFTER = 60    # cap on a hostile/huge Retry-After header
_RETRY_STATUSES = (429, 500, 502, 503, 504)

# One download at a time: concurrent callers wait, then see cheap 304s.
_DOWNLOAD_LOCK = threading.Lock()

SCHEMAS = {
    "commodities": ["itemId", "name", "marketValue", "minBuyout", "recent",
                    "historical", "updatedAt"],
    "region": ["itemId", "name", "marketValue", "historical", "avgSalePrice",
               "saleRate", "soldPerDay", "updatedAt"],
}
INT_FIELDS = {"itemId", "marketValue", "minBuyout", "recent", "historical",
              "avgSalePrice"}
FLOAT_FIELDS = {"saleRate", "soldPerDay"}
# M12: realm CSV dropped - pricing is region + commodities only.
KEYS = ("region", "commodities")


class TsmError(Exception):
    """Any failure talking to or parsing the TSM public data."""


def urls(cfg):
    region = cfg.get("region") or "eu"
    root = "%s/retail/%s" % (BASE, region)
    return {
        "region": "%s/region/items.csv" % root,
        "commodities": "%s/commodities.csv" % root,
    }


def cache_paths(cache_dir):
    return {
        "region": os.path.join(cache_dir, "region-items.csv"),
        "commodities": os.path.join(cache_dir, "commodities.csv"),
    }


def _meta_path(cache_dir, key):
    return os.path.join(cache_dir, "%s.meta.json" % key)


def load_meta(cache_dir, key):
    path = _meta_path(cache_dir, key)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_meta(cache_dir, key, meta):
    path = _meta_path(cache_dir, key)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _meta_int(meta, key, default=0):
    """Corrupt/hostile meta values degrade to the default, never crash."""
    try:
        value = int(meta.get(key) or 0)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _to_int(value):
    value = (value or "").strip()
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return 0


def _to_float(value):
    value = (value or "").strip()
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_table(text, kind, stats=None):
    """Parse one CSV. `stats` (optional dict) receives a "skipped" count of
    malformed rows so gross data loss is visible instead of silent."""
    if kind not in SCHEMAS:
        raise TsmError("unknown TSM table kind: %r" % kind)
    schema = SCHEMAS[kind]
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        if stats is not None:
            stats["skipped"] = 0
        return []

    header = next(csv.reader([lines[0].lstrip("\ufeff")]))
    header = [h.strip() for h in header]
    if header != schema:
        raise TsmError("unexpected %s header: %r" % (kind, header))

    trailing = len(schema) - 2
    rows = []
    skipped = 0
    for line in lines[1:]:
        fields = next(csv.reader([line]))
        if len(fields) < len(schema):
            skipped += 1
            continue
        name = ",".join(fields[1:len(fields) - trailing])
        values = fields[len(fields) - trailing:]
        row = {"itemId": _to_int(fields[0]), "name": name}
        for field, raw in zip(schema[2:], values):
            if field in INT_FIELDS:
                row[field] = _to_int(raw)
            elif field in FLOAT_FIELDS:
                row[field] = _to_float(raw)
            else:
                row[field] = raw
        rows.append(row)
    if stats is not None:
        stats["skipped"] = skipped
    return rows


def _header(resp, name):
    if not resp:
        return None
    wanted = name.lower()
    for key, value in resp.items():
        if key.lower() == wanted:
            return value
    return None


def _cache_ok(cache_dir, key, meta):
    """A 304 is only trustworthy when the cached file is present and intact."""
    path = cache_paths(cache_dir)[key]
    recorded = meta.get("bytes")
    if not isinstance(recorded, int) or recorded <= 0:
        return False
    try:
        return os.path.getsize(path) == recorded
    except OSError:
        return False


def _open_with_retry(opener, url, headers, sleep, log, what):
    """GET with bounded retry on 429/5xx and transient transport errors.

    A 429 Retry-After header (seconds) raises the delay, capped at
    MAX_RETRY_AFTER. Other HTTP statuses pass through untouched.
    """
    delay = 0
    status, body, resp = None, None, None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            status, body, resp = opener(url, headers)
        except TsmError as exc:  # transport failure (DNS, reset, timeout)
            if attempt == RETRY_ATTEMPTS:
                raise
            delay = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
            log("%s: transport error, retry %d/%d in %ss: %s"
                % (what, attempt, RETRY_ATTEMPTS - 1, delay, exc))
            sleep(delay)
            continue
        if status not in _RETRY_STATUSES:
            return status, body, resp
        if attempt == RETRY_ATTEMPTS:
            break
        delay = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
        retry_after = _header(resp, "retry-after")
        if retry_after:
            try:
                delay = max(delay, min(int(float(retry_after)),
                                       MAX_RETRY_AFTER))
            except ValueError:
                pass
        log("%s: HTTP %s, retry %d/%d in %ss"
            % (what, status, attempt, RETRY_ATTEMPTS - 1, delay))
        sleep(delay)
    return status, body, resp


def download(cfg, cache_dir, opener=None, log=None, sleep=None):
    """Fetch all three files with If-None-Match / If-Modified-Since.

    Single-flight (concurrent callers wait, then get cheap 304s); 429/5xx
    are retried with backoff before giving up.
    Returns {key: {"status": 200|304, "rows": int, "changed": bool,
                   "path": str, "skipped": int}}.
    """
    opener = opener or _urlopen_get
    log = log or (lambda line: None)
    sleep = sleep or time.sleep
    with _DOWNLOAD_LOCK:
        return _download_locked(cfg, cache_dir, opener, log, sleep)


# ---- M15: freshness snapshot + boot refresh worker (no caller blocking) ----

_REFRESH_LOCK = threading.Lock()
_REFRESH = {"thread": None, "error": "", "finished_at": "", "stats": None}


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def snapshot(cache_dir):
    """Local meta freshness for the UI. Reads files only - never the network."""
    newest = ""
    rows = 0
    for key in KEYS:
        meta = load_meta(cache_dir, key)
        stamp = str(meta.get("fetched_at") or "")
        if stamp > newest:
            newest = stamp
        if key == "region":
            rows = _meta_int(meta, "rows", 0)
    return {"fetched_at": newest, "rows": rows}


def sweep_tmp(cache_dir):
    """Remove *.tmp leftovers from crashed atomic writes. Live CSVs are
    replaced in place by os.replace, so old data never accumulates."""
    if not os.path.isdir(cache_dir):
        return 0
    removed = 0
    for name in os.listdir(cache_dir):
        if name.endswith(".tmp"):
            try:
                os.remove(os.path.join(cache_dir, name))
                removed += 1
            except OSError:
                pass
    return removed


def refresh_status():
    with _REFRESH_LOCK:
        thread = _REFRESH.get("thread")
        return {"running": bool(thread and thread.is_alive()),
                "error": _REFRESH.get("error", ""),
                "finished_at": _REFRESH.get("finished_at", ""),
                "stats": _REFRESH.get("stats")}


def _resolve_effective(cfg):
    """Best-effort region/settings override from the DB; boot refresh must
    survive a fresh install (no schema yet) by falling back to cfg."""
    try:
        from app import db, extract as _extract, settings as _settings
        path = cfg.get("db_path") or _extract.default_db_path()
        conn = db.connect(path)
        try:
            return _settings.apply(cfg, conn)
        finally:
            conn.close()
    except Exception:
        return cfg


def refresh_async(cfg, cache_dir, runner=None):
    """Boot-time refresh: daemon worker, single-flight, never raises.
    `runner(cfg, cache_dir)` replaces resolve+download for tests."""
    with _REFRESH_LOCK:
        thread = _REFRESH.get("thread")
        if thread and thread.is_alive():
            return False
        _REFRESH.update({"error": "", "finished_at": "", "stats": None})

        def work():
            error = ""
            stats = None
            try:
                sweep_tmp(cache_dir)
                if runner is not None:
                    stats = runner(cfg, cache_dir)
                else:
                    stats = download(_resolve_effective(cfg), cache_dir)
            except Exception as exc:  # boot must survive any TSM failure
                error = str(exc) or exc.__class__.__name__
            with _REFRESH_LOCK:
                _REFRESH["error"] = error
                _REFRESH["stats"] = stats
                _REFRESH["finished_at"] = _now()

        try:
            worker = threading.Thread(target=work, daemon=True,
                                      name="tsm-refresh")
            _REFRESH["thread"] = worker
            worker.start()
        except Exception as exc:  # thread exhaustion must not kill boot
            _REFRESH["thread"] = None
            _REFRESH["error"] = str(exc) or exc.__class__.__name__
            _REFRESH["finished_at"] = _now()
            return False
        return True


def _download_locked(cfg, cache_dir, opener, log, sleep):
    if not os.path.isdir(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    paths = cache_paths(cache_dir)
    stats = {}
    for key, url in urls(cfg).items():
        meta = load_meta(cache_dir, key)
        headers = {"User-Agent": USER_AGENT}
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]

        status, body, resp = _open_with_retry(
            opener, url, headers, sleep, log, key)

        if status == 304 and _cache_ok(cache_dir, key, meta):
            rows = _meta_int(meta, "rows")
            log("%s: 304 not modified (%d rows cached)" % (key, rows))
            stats[key] = {"status": 304, "rows": rows, "changed": False,
                          "path": paths[key],
                          "skipped": _meta_int(meta, "skipped")}
            continue
        if status == 304:
            log("%s: 304 but cached file missing/invalid, refetching" % key)
            status, body, resp = _open_with_retry(
                opener, url, {"User-Agent": USER_AGENT}, sleep, log, key)
        if status != 200:
            raise TsmError("%s: HTTP %s for %s" % (key, status, url))

        try:
            text = body.decode("utf-8-sig", "replace") if isinstance(body, bytes) else body
            parsed_stats = {}
            rows = parse_table(text, key, stats=parsed_stats)
            if not text.strip() or not rows:
                # An empty 200 would clobber a healthy cache and empty the
                # universe; refuse it so the caller can keep the old files.
                raise TsmError("%s: refusing to cache empty response (%d rows)"
                               % (key, len(rows)))
            skipped = int(parsed_stats.get("skipped") or 0)
            tmp_path = paths[key] + ".tmp"
            with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            os.replace(tmp_path, paths[key])
            _save_meta(cache_dir, key, {
                "url": url,
                "etag": _header(resp, "etag"),
                "last_modified": _header(resp, "last-modified"),
                "rows": len(rows),
                "bytes": os.path.getsize(paths[key]),
                "skipped": skipped,
                "fetched_at": datetime.datetime.now().isoformat(
                    timespec="seconds"),
            })
        except (csv.Error, OSError, ValueError) as exc:
            raise TsmError("%s: failed to store: %s" % (key, exc))
        log("%s: 200 %d rows (%d skipped)" % (key, len(rows), skipped))
        stats[key] = {"status": 200, "rows": len(rows), "changed": True,
                      "path": paths[key], "skipped": skipped}
    return stats


def read_cached(cache_dir, key):
    path = cache_paths(cache_dir)[key]
    if not os.path.isfile(path):
        raise TsmError("no cached %s file at %s" % (key, path))
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _urlopen_get(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            try:
                data = resp.read()
            except (OSError, http.client.HTTPException) as exc:
                raise TsmError("%s: read failed: %s" % (url, exc))
            return resp.status, data, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(), dict(exc.headers)
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise TsmError("cannot reach %s: %s" % (url, exc.reason))


def load_tables(cfg, cache_dir):
    """Parse all three cached files. Used by extraction (M3) and pricing (M5)."""
    out = {}
    for key in KEYS:
        out[key] = parse_table(read_cached(cache_dir, key), key)
    return out
