"""Resumable extraction (M3): the AH universe x one Blizzard call per item.

Universe = the union of item ids across the three TSM CSVs (31,145 on
2026-10-12). Rationale: anything absent from TSM has no AH price and dies at
verdict rule 2 anyway, so it cannot be a scoreable candidate; the union also
matches the plan's "~31k calls" estimate exactly.

Progress is tracked with a `scanned_ids` set (not a bare cursor) written in the
same transaction as the candidate rows, so a crash can never lose or double-
count a committed item, and ids that appear in the TSM files *below* the old
cursor (TSM refreshes every ~3h) are still picked up on resume. A cross-process
file lock (msvcrt) keeps CLI and UI from running two extractions at once --
the OS releases it if the holder dies.
"""

import datetime
import os
import threading
import time

try:
    import msvcrt
except ImportError:  # pragma: no cover - the plan targets Windows only
    msvcrt = None

from app import api_keys, blizzard, config, db, settings, tsm

DEFAULT_PACE = 0.11       # ~9 req/s: under the 100/s and 36,000/h limits
DEFAULT_BATCH = 100       # state + candidates committed every batch
DEFAULT_MAX_RETRIES = 5

_STATE = {"thread": None, "error": ""}
_LOCK = threading.Lock()


class ItemNotFound(blizzard.BlizzardError):
    """The item id no longer exists upstream (HTTP 404)."""


class RateLimited(blizzard.BlizzardError):
    """Blizzard asked us to slow down (HTTP 429)."""


class TokenExpired(blizzard.BlizzardError):
    """Token rejected (HTTP 401); the loop fetches a fresh one."""


class ExtractionError(blizzard.BlizzardError):
    """Extraction gave up after exhausting retries; state is resumable."""


def default_db_path():
    return os.path.join(config.repo_root(), "data", "vendorflips.sqlite")


def default_cache_dir():
    return os.path.join(config.repo_root(), "data", "tsm")


def fetch_item(cfg, token, item_id, get=None):
    """GET /data/wow/item/{id} with typed status errors for the retry loop."""

    def guarded(url, headers):
        status, body = (get or blizzard._get)(url, headers)
        if status == 404:
            raise ItemNotFound(item_id)
        if status == 429:
            raise RateLimited(item_id)
        if status == 401:
            raise TokenExpired(item_id)
        return status, body

    return blizzard.fetch_json(cfg, token, "/data/wow/item/%d" % item_id,
                               blizzard.static_namespace(cfg), get=guarded)


def universe_from_tables(tables):
    ids = set()
    for rows in (tables or {}).values():
        for row in rows:
            item_id = row.get("itemId")
            if item_id:
                ids.add(int(item_id))
    return sorted(ids)


def candidate_row(parsed, item_id, extracted_at):
    return {
        "id": item_id,
        "name": parsed.get("name") or "",
        "purchase_price": int(parsed.get("purchase_price") or 0),
        "purchase_quantity": int(parsed.get("purchase_quantity") or 0),
        "sell_price": int(parsed.get("sell_price") or 0),
        "quality": parsed.get("quality") or "",
        "is_equippable": 1 if parsed.get("is_equippable") else 0,
        "item_class": parsed.get("item_class") or "",
        "item_subclass": parsed.get("item_subclass") or "",
        "synthetic_multiple": 1 if parsed.get("synthetic_multiple") else 0,
        "extracted_at": extracted_at,
    }


def lock_path(db_path):
    return db_path + ".lock"


def acquire_lock(db_path):
    """OS-level single-run guard. Returns a holder, or None if already held.

    Layout: byte 0 is the locked byte (locking it never blocks pid reads);
    the pid text starts at offset 1. Windows releases the byte lock the
    moment the holder process dies -- no stale locks after a crash.
    """
    path = lock_path(db_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "wb"):
            pass
    handle = open(path, "r+b")
    if msvcrt is None:  # pragma: no cover - Windows-only plan
        _write_pid(handle)
        return handle
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    _write_pid(handle)
    return handle


def _write_pid(handle):
    handle.seek(1)
    handle.truncate()
    handle.write(str(os.getpid()).encode("ascii"))
    handle.flush()


def release_lock(handle):
    if handle is None:
        return
    if msvcrt is not None:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    handle.close()


def _lock_held(db_path):
    """Probe: True while some process holds the lock (self included)."""
    path = lock_path(db_path)
    if msvcrt is None or not os.path.exists(path):
        return False
    try:
        handle = open(path, "r+b")
    except OSError:
        return False
    try:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    finally:
        handle.close()


def owner_pid(db_path):
    """Pid written by the current/last lock holder (0 if none or unreadable)."""
    try:
        with open(lock_path(db_path), "rb") as fh:
            fh.seek(1)
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


_INT_KEYS = ("cursor", "universe_size", "scanned", "candidates", "not_found",
             "retries", "tsm_skipped")


def _state_int(state, key, default=0):
    try:
        return int(state.get(key) or 0)
    except (TypeError, ValueError):
        return default


def _cached_skipped(cache_dir):
    """Sum skipped-row counts recorded in the TSM meta files (M8R: a failed
    refresh must not zero a previously reported count)."""
    total = 0
    for key in tsm.KEYS:
        meta = tsm.load_meta(cache_dir, key)
        try:
            value = int(meta.get("skipped") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            total += value
    return total


def status(cfg=None, db_path=None):
    """Progress snapshot for the UI, the CLI, and tests. Never opens the network."""
    db_path = db_path or default_db_path()
    out = {"phase": "idle", "running": False, "owner_pid": 0, "cursor": 0,
           "universe_size": 0, "scanned": 0, "candidates": 0, "not_found": 0,
           "retries": 0, "tsm_skipped": 0, "last_error": "", "started_at": "",
           "updated_at": ""}
    if os.path.exists(db_path):
        conn = db.connect(db_path)
        try:
            state = db.state_dict(conn)
        finally:
            conn.close()
        out["phase"] = state.get("phase") or "idle"
        for key in _INT_KEYS:
            out[key] = _state_int(state, key)
        out["last_error"] = state.get("last_error") or ""
        out["started_at"] = state.get("started_at") or ""
        out["updated_at"] = state.get("updated_at") or ""
    thread = _STATE.get("thread")
    out["running"] = bool(thread and thread.is_alive()) or _lock_held(db_path)
    out["owner_pid"] = owner_pid(db_path)
    if not out["last_error"] and _STATE.get("error"):
        out["last_error"] = _STATE["error"]
    # M15: local TSM freshness (meta files + boot-worker state only - the
    # status contract is "never opens the network"). The nag fires only
    # after a completed extract when the CSVs were fetched afterwards.
    cache_dir = (cfg or {}).get("cache_dir") or default_cache_dir()
    snap = tsm.snapshot(cache_dir)
    refresh = tsm.refresh_status()
    out["tsm"] = {
        "fetched_at": snap["fetched_at"],
        "rows": snap["rows"],
        "running": refresh["running"],
        "error": refresh["error"],
        "newer_than_extract": (
            out["phase"] == "complete" and bool(snap["fetched_at"]) and
            (not out["updated_at"] or snap["fetched_at"] > out["updated_at"])),
    }
    return out


def _finished_status(cfg, db_path):
    st = status(cfg, db_path)
    st["running"] = False
    return st


def _flush(conn, stamp, cursor, scanned, candidates, not_found, retries, ids):
    if ids:
        db.mark_scanned(conn, ids)
    db.state_bulk(conn, {
        "cursor": str(cursor), "scanned": str(scanned),
        "candidates": str(candidates), "not_found": str(not_found),
        "retries": str(retries), "updated_at": stamp,
    })
    conn.commit()


def _abort(conn, stamp, message, retries=0):
    db.state_bulk(conn, {"last_error": message, "retries": str(retries),
                         "updated_at": stamp})
    conn.commit()
    return ExtractionError(message)


def run_extraction(cfg, db_path=None, cache_dir=None, *, universe=None,
                   fetch_item_fn=None, get_token_fn=None, sleep=None,
                   pace=DEFAULT_PACE, clock=None, batch=DEFAULT_BATCH,
                   max_retries=DEFAULT_MAX_RETRIES, limit=None,
                   restart=False, log=None, now=None):
    """Fetch purchase data for every universe item, resumably.

    `pace` is the minimum seconds between request *starts* (never more than
    ~10 req/s, so the 36,000/h limit holds even if responses are instant).
    Returns the status snapshot. Raises on retry exhaustion or unexpected
    errors with all committed progress intact (phase stays `scanning`).
    """
    db_path = db_path or default_db_path()
    cache_dir = cache_dir or default_cache_dir()
    fetch = fetch_item_fn or fetch_item
    get_token = get_token_fn or blizzard.get_token
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    log = log or (lambda line: None)
    if now is None:
        now = lambda: datetime.datetime.now().isoformat(timespec="seconds")

    lock = acquire_lock(db_path)
    if lock is None:
        raise ExtractionError(
            "extraction already running elsewhere (lock: %s)" % lock_path(db_path))
    conn = None
    tsm_skipped = 0
    try:
        conn = db.connect(db_path)
        db.init_schema(conn)
        cfg = settings.apply(cfg, conn)
        cfg = api_keys.apply(conn, cfg)  # stored keys override config file
        if universe is None:
            # M8: refresh TSM CSVs at extraction start; a failed refresh
            # falls back to the cached files rather than blocking resume.
            try:
                stats = tsm.download(cfg, cache_dir, log=log, sleep=sleep)
                tsm_skipped = sum(int((stats.get(k) or {}).get("skipped")
                                      or 0) for k in tsm.KEYS)
            except tsm.TsmError as exc:
                log("TSM refresh failed (%s); using cached data" % exc)
                tsm_skipped = _cached_skipped(cache_dir)
            universe = universe_from_tables(tsm.load_tables(cfg, cache_dir))
        universe = sorted({int(i) for i in universe if i})

        state = db.state_dict(conn)
        if not restart and state.get("phase") == "complete":
            return _finished_status(cfg, db_path)
        if not universe:
            raise ExtractionError(
                "empty universe - refresh TSM data first (data/tsm)")

        fresh = {"phase": "scanning", "cursor": "0", "scanned": "0",
                 "candidates": "0", "not_found": "0", "retries": "0",
                 "tsm_skipped": str(tsm_skipped),
                 "last_error": "", "started_at": now(), "updated_at": now(),
                 "universe_size": str(len(universe))}
        if restart:
            db.state_bulk(conn, fresh)
            db.truncate_scanned(conn)
            conn.commit()
        elif state.get("phase") != "scanning":
            db.state_bulk(conn, fresh)
            conn.commit()
        else:
            # Accepted resume: clear a sticky error and refresh the size.
            patch = {"last_error": "",
                     "universe_size": str(len(universe)),
                     "updated_at": now()}
            if tsm_skipped:
                # accumulate this run's refresh skips, never lose old ones
                patch["tsm_skipped"] = str(
                    _state_int(state, "tsm_skipped") + tsm_skipped)
            db.state_bulk(conn, patch)
            conn.commit()
        state = db.state_dict(conn)

        cursor = _state_int(state, "cursor")
        scanned = _state_int(state, "scanned")
        candidates = _state_int(state, "candidates")
        not_found = _state_int(state, "not_found")
        retries = _state_int(state, "retries")

        seen = db.scanned_id_set(conn)
        if not seen and cursor > 0:
            # Legacy cursor-only state (pre-scanned_ids): ids <= cursor were
            # processed contiguously, so seed them instead of refetching.
            seed = [i for i in universe if i <= cursor]
            if seed:
                db.mark_scanned(conn, seed)
                conn.commit()
                seen.update(seed)
        pending = [i for i in universe if i not in seen]
        known = db.candidate_ids(conn)
        full_pending = len(pending)
        if limit is not None:
            pending = pending[:limit]
        truncated = limit is not None and full_pending > limit
        log("extract: %d to fetch, %d candidates so far (cursor %d)"
            % (len(pending), candidates, cursor))

        token = None
        refreshes = 0
        in_batch = 0
        batch_ids = []
        last_start = clock()

        for item_id in pending:
            if pace:
                wait = pace - (clock() - last_start)
                if wait > 0:
                    sleep(wait)
                last_start = clock()
            attempts = 0
            while True:
                if token is None:
                    token, _ = get_token(cfg)
                try:
                    payload = fetch(cfg, token, item_id)
                    break
                except ItemNotFound:
                    payload = None
                    break
                except TokenExpired:
                    refreshes += 1
                    if refreshes > 5:
                        raise _abort(
                            conn, now(),
                            "token kept expiring; giving up", retries)
                    token = None
                except RateLimited:
                    attempts += 1
                    retries += 1
                    rotated = False
                    if api_keys.available(conn):
                        # park this key and swap to the next stored one
                        api_keys.mark_cooldown(conn, cfg.get("client_id"))
                        new_cfg = api_keys.apply(conn, cfg)
                        if (new_cfg.get("client_id") !=
                                cfg.get("client_id")):
                            cfg = new_cfg
                            token = None
                            rotated = True
                            log("extract: rate limited - rotated to key %s"
                                % (cfg.get("client_id"),))
                    if rotated:
                        continue
                    if attempts > max_retries:
                        raise _abort(conn, now(),
                                     "rate limited on item %d" % item_id,
                                     retries)
                    sleep(min(60, 2 ** (attempts - 1)))
                except blizzard.BlizzardError as exc:
                    attempts += 1
                    retries += 1
                    if attempts > max_retries:
                        raise _abort(conn, now(), str(exc), retries)
                    sleep(min(60, 2 ** (attempts - 1)))

            if payload is None:
                not_found += 1
                db.expire_candidate(conn, item_id, now())
            else:
                row = candidate_row(blizzard.parse_item(payload), item_id,
                                    now())
                if row["purchase_price"] > 0:
                    db.upsert_candidate(conn, row)
                    candidates += 1
                    known.add(item_id)
                elif item_id in known:
                    # Was a candidate before; its price died upstream.
                    # Refresh to zero (manual flags survive) so M5 cannot
                    # score a stale positive price. New zero-price items
                    # are simply not candidates (universe ∩ price > 0).
                    db.upsert_candidate(conn, row)
            scanned += 1
            cursor = item_id
            batch_ids.append(item_id)
            in_batch += 1
            if in_batch >= batch:
                _flush(conn, now(), cursor, scanned, candidates, not_found,
                       retries, batch_ids)
                in_batch = 0
                batch_ids = []
                log("extract: scanned %d | cursor %d | candidates %d | "
                    "not found %d" % (scanned, cursor, candidates, not_found))

        if in_batch:
            _flush(conn, now(), cursor, scanned, candidates, not_found,
                   retries, batch_ids)
        if not truncated:
            db.state_bulk(conn, {"phase": "complete", "updated_at": now()})
            conn.commit()
            log("extract: complete - %d scanned, %d candidates"
                % (scanned, candidates))
        return _finished_status(cfg, db_path)
    finally:
        if conn is not None:
            conn.close()
        release_lock(lock)


def start_async(cfg, runner=None, db_path=None, cache_dir=None, restart=False,
                log=None):
    """Run extraction in a background thread; second starts are ignored."""
    with _LOCK:
        thread = _STATE.get("thread")
        if thread and thread.is_alive():
            return status(cfg, db_path)
        runner = runner or run_extraction
        _STATE["error"] = ""

        def target():
            try:
                runner(cfg, db_path=db_path, cache_dir=cache_dir,
                       restart=restart, log=log)
            except Exception as exc:
                _STATE["error"] = str(exc)

        worker = threading.Thread(target=target, daemon=True)
        _STATE["thread"] = worker
        worker.start()
    return status(cfg, db_path)


def _join_for_test(timeout=5):
    thread = _STATE.get("thread")
    if thread:
        thread.join(timeout)


def join_worker(timeout=10):
    """Wait for a background extraction worker (M8 graceful shutdown).

    Returns True when no worker is running or it finished within `timeout`;
    False when it is still going (progress is committed, restart resumes).
    Joining under _LOCK cannot race a start (the worker never takes it) and
    never sees a thread that has not been started yet.
    """
    with _LOCK:
        thread = _STATE.get("thread")
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()
