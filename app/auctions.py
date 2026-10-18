import datetime
import json
import os
import threading
import time

from app import blizzard
from app import config

LADDER_TTL = 55 * 60
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = (2, 4)
_RETRY_STATUSES = (429, 500, 502, 503, 504)

COMMODITIES_PATH = "/data/wow/auctions/commodities"


class AuctionsError(Exception):
    """Failure talking to or parsing the Blizzard commodity wall."""


def default_auctions_dir():
    return os.path.join(config.repo_root(), "data", "auctions")


def cache_dir_for(cfg):
    return cfg.get("auctions_dir") or default_auctions_dir()


def region_of(cfg):
    return cfg.get("region") or "eu"


def dynamic_namespace(cfg):
    return blizzard.DYNAMIC % region_of(cfg)


def commodities_url(cfg):
    return blizzard.build_api_url(COMMODITIES_PATH, cfg,
                                  dynamic_namespace(cfg))


def ladder_path(cache_dir, region):
    return os.path.join(cache_dir, "commodities-%s.json" % region)


def meta_path(cache_dir, region):
    return os.path.join(cache_dir, "commodities-%s.meta.json" % region)


def build_ladder(auctions):
    table = {}
    seen = skipped = units = 0
    for row in auctions or []:
        seen += 1
        item_id = qty = price = None
        if isinstance(row, dict):
            item = row.get("item")
            if isinstance(item, dict):
                item_id = item.get("id")
            qty = row.get("quantity")
            price = row.get("unit_price")
        if (not isinstance(item_id, int) or isinstance(item_id, bool)
                or item_id <= 0
                or not isinstance(qty, int) or isinstance(qty, bool)
                or qty <= 0
                or not isinstance(price, int) or isinstance(price, bool)
                or price <= 0):
            skipped += 1
            continue
        levels = table.setdefault(item_id, {})
        levels[price] = levels.get(price, 0) + qty
        units += qty
    ladder = {}
    for item_id, levels in table.items():
        ladder[item_id] = [[p, levels[p]] for p in sorted(levels)]
    stats = {
        "seen": seen,
        "skipped": skipped,
        "items": len(ladder),
        "levels": sum(len(v) for v in ladder.values()),
        "units": units,
    }
    return ladder, stats


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_meta(cache_dir, region):
    path = meta_path(cache_dir, region)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_meta(cache_dir, region, meta):
    path = meta_path(cache_dir, region)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _with_retry(fn, what, log, sleep):
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except blizzard.BlizzardError as exc:
            retryable = exc.status is None or exc.status in _RETRY_STATUSES
            if not retryable or attempt == RETRY_ATTEMPTS:
                raise
            delay = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
            log("%s: HTTP %s, retry %d/%d in %ss"
                % (what, exc.status, attempt, RETRY_ATTEMPTS - 1, delay))
            sleep(delay)


def refresh(cfg, cache_dir=None, *, get=None, post=None, log=None,
            sleep=None):
    """Download the commodity wall and write ladder + meta.

    Any fetch failure leaves the existing files untouched. Returns
    {"commodities": meta}. (No connected-realm index fetch: TSM's
    soldPerDay is already a whole-region total - PLAN.md section 2.1.)
    """
    log = log or (lambda line: None)
    sleep = sleep or time.sleep
    cache_dir = cache_dir or cache_dir_for(cfg)
    os.makedirs(cache_dir, exist_ok=True)
    region = region_of(cfg)
    ns = dynamic_namespace(cfg)
    token, _ = blizzard.get_token(cfg, post=post)

    payload = _with_retry(
        lambda: blizzard.fetch_json(cfg, token, COMMODITIES_PATH, ns, get=get),
        "commodities", log, sleep)
    rows = (payload or {}).get("auctions")
    if not isinstance(rows, list):
        raise AuctionsError("commodities response has no auctions list")
    ladder, stats = build_ladder(rows)
    if not ladder:
        raise AuctionsError("refusing to cache empty commodity ladder "
                            "(%d rows)" % stats["seen"])

    tmp = ladder_path(cache_dir, region) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"region": region, "items": ladder}, fh)
    os.replace(tmp, ladder_path(cache_dir, region))
    meta = {
        "region": region,
        "url": commodities_url(cfg),
        "fetched_at": _now(),
        "bytes": os.path.getsize(ladder_path(cache_dir, region)),
        "items": stats["items"],
        "levels": stats["levels"],
        "units": stats["units"],
        "seen": stats["seen"],
        "skipped": stats["skipped"],
    }
    _save_meta(cache_dir, region, meta)
    return {"commodities": meta}


_LADDER_STATE = {"sig": None, "ladder": None, "meta": None}


def get_ladder(cfg, cache_dir=None):
    """(ladder, meta) loaded from disk and cached by (path, mtime, size);
    None when the file is missing or unreadable. Never touches the network."""
    cache_dir = cache_dir or cache_dir_for(cfg)
    region = region_of(cfg)
    path = ladder_path(cache_dir, region)
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (region, st.st_mtime_ns, st.st_size)
    state = _LADDER_STATE
    if state["sig"] != sig:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        items = data.get("items") if isinstance(data, dict) else None
        ladder = {}
        if isinstance(items, dict):
            for key, levels in items.items():
                try:
                    item_id = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(levels, list):
                    ladder[item_id] = levels
        state["sig"] = sig
        state["ladder"] = ladder
        state["meta"] = load_meta(cache_dir, region)
    return state["ladder"], state["meta"]


def _age_seconds(stamp):
    if not stamp:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    age = time.time() - when.timestamp()
    return age if age >= 0 else 0.0


def ladder_status(cfg, cache_dir=None):
    cache_dir = cache_dir or cache_dir_for(cfg)
    region = region_of(cfg)
    meta = load_meta(cache_dir, region)
    return _status_from(cache_dir, region, meta)


def _status_from(cache_dir, region, meta):
    refresh_state = refresh_status()
    age = _age_seconds(meta.get("fetched_at")) if meta else None
    has_ladder = os.path.isfile(ladder_path(cache_dir, region))
    stale = age is None or age > LADDER_TTL
    return {
        "fetched_at": str(meta.get("fetched_at") or "") if meta else "",
        "items": meta.get("items") if meta else None,
        "levels": meta.get("levels") if meta else None,
        "units": meta.get("units") if meta else None,
        "age_s": age,
        "stale": stale,
        "warming": not has_ladder,
        "refreshing": refresh_state["running"],
        "error": refresh_state["error"],
    }


def ahead_units(levels, price):
    """(units strictly cheaper than price, units at exactly price)."""
    cheaper = same = 0
    for level in levels or []:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        level_price, qty = level[0], level[1]
        if not isinstance(qty, int):
            continue
        if level_price < price:
            cheaper += qty
        elif level_price == price:
            same += qty
    return cheaper, same


def sweep_tmp(cache_dir):
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


_REFRESH_LOCK = threading.Lock()
_REFRESH = {"thread": None, "error": "", "finished_at": "", "stats": None}


def refresh_status():
    with _REFRESH_LOCK:
        thread = _REFRESH.get("thread")
        return {"running": bool(thread and thread.is_alive()),
                "error": _REFRESH.get("error", ""),
                "finished_at": _REFRESH.get("finished_at", ""),
                "stats": _REFRESH.get("stats")}


def _resolve_effective(cfg):
    """Region/settings override from the DB; boot must survive a fresh
    install (no schema yet) by falling back to cfg."""
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


def refresh_async(cfg, cache_dir=None, runner=None):
    """Boot/background refresh: daemon worker, single-flight, never raises.
    `runner(cfg, cache_dir)` replaces resolve+refresh for tests."""
    cache_dir = cache_dir or cache_dir_for(cfg)
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
                    stats = refresh(_resolve_effective(cfg), cache_dir)
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
            with _REFRESH_LOCK:
                _REFRESH["error"] = error
                _REFRESH["stats"] = stats
                _REFRESH["finished_at"] = _now()

        try:
            worker = threading.Thread(target=work, daemon=True,
                                      name="auctions-refresh")
            _REFRESH["thread"] = worker
            worker.start()
        except Exception as exc:
            _REFRESH["thread"] = None
            _REFRESH["error"] = str(exc) or exc.__class__.__name__
            _REFRESH["finished_at"] = _now()
            return False
        return True


def maybe_refresh_async(cfg, cache_dir=None, runner=None, cooldown=30.0):
    """Kick refresh_async unless one runs or the last attempt failed within
    `cooldown` seconds (stops a failed quota/rate limit being hammered by
    every warming request). Returns refresh_async's bool."""
    with _REFRESH_LOCK:
        thread = _REFRESH.get("thread")
        running = bool(thread and thread.is_alive())
        error = _REFRESH.get("error", "")
        finished_at = _REFRESH.get("finished_at", "")
    if running:
        return False
    if error and cooldown > 0:
        age = _age_seconds(finished_at)
        if age is not None and age < cooldown:
            return False
    return refresh_async(cfg, cache_dir, runner)
