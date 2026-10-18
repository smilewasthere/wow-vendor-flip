"""M9 vendor-check: verify attractive candidates against live Wowhead.

The job scores every candidate with the same engine the UI uses, then keeps
the rows a user would actually act on (verdict != NOT WORTH IT) and checks
each one on www.wowhead.com/item=<id> through a single headed Edge tab
(CDP, app/browser.py). Politeness delay 4-8s random between items; 5
consecutive Cloudflare blocks pause the run and raise needs_attention so the
user can finish the challenge in the visible window and press start again.

Existing ok rows are kept (resumable); blocked/error rows are rechecked.
"""

import datetime
import os
import random
import threading
import time

from app import browser as browser_mod
from app import config, db, extract, pricing

DEFAULT_MAX_BLOCKED = 5
DEFAULT_DELAY_RANGE = (4.0, 8.0)

_STATE = {
    "thread": None, "error": "", "running": False, "phase": "idle",
    "done": 0, "total": 0, "current": 0, "sold": 0, "not_sold": 0,
    "blocked": 0, "needs_attention": False,
}
_LOCK = threading.Lock()
_CANCEL = threading.Event()

_FIELDS = ("phase", "error", "running", "done", "total", "current",
           "sold", "not_sold", "blocked", "needs_attention")


def default_profile_dir():
    return os.path.join(config.repo_root(), "data", "vf-edge-profile")


def wowhead_cost(npcs):
    """First gold vendor price from Wowhead's cost arrays.

    Gold arrives as nested ``[[copper]]`` or flat ``[copper]``; a two-element
    inner list ``[[points, currencyId]]`` is a point/currency price, not
    copper, and is skipped (never compare it to purchase_price).
    """
    for npc in npcs or []:
        cost = npc.get("cost")
        if not isinstance(cost, list) or not cost:
            continue
        first = cost[0]
        if isinstance(first, list):
            if len(first) == 1 and isinstance(first[0], int):
                return first[0] or None
            continue
        if isinstance(first, int):
            return first or None
    return None


def build_scope(candidates, ctx, th):
    """Item ids the user would act on: every verdict except NOT WORTH IT."""
    scored = pricing.score_all(candidates, ctx, th)
    return [row["id"] for row in scored if row["verdict"] != "NOT WORTH IT"]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _reset_counters():
    _STATE.update(phase="starting", error="", done=0, total=0, current=0,
                  sold=0, not_sold=0, blocked=0, needs_attention=False)


def run_check(cfg, *, db_path=None, cache_dir=None, browser=None, delay=None,
              keep_existing=True, max_blocked=DEFAULT_MAX_BLOCKED, log=None):
    """Verify the attractive scope on live Wowhead; returns the final status."""
    db_path = db_path or (cfg.get("db_path") or extract.default_db_path())
    cache_dir = cache_dir or (cfg.get("cache_dir") or extract.default_cache_dir())
    _log = log or (lambda msg: None)
    delay_range = DEFAULT_DELAY_RANGE if delay is None else (delay, delay)
    conn = None
    br = None
    owns_browser = browser is None
    with _LOCK:
        _reset_counters()
        # A cancel clicked before the worker reached run_check must stick;
        # only a fresh top-level call (not the start_async worker) clears.
        if _STATE.get("thread") is not threading.current_thread():
            _CANCEL.clear()
        _STATE["running"] = True
    try:
        conn = db.connect(db_path)
        candidates = db.all_candidates(conn)
        th = pricing.thresholds(cfg, conn)
        ctx = pricing.context(cfg, cache_dir)
        scope = build_scope(candidates, ctx, th)
        existing = db.vendor_source_map(conn) if keep_existing else {}
        todo = [
            item_id for item_id in scope
            if not (keep_existing
                    and existing.get(item_id, {}).get("status") == "ok")
        ]
        with _LOCK:
            _STATE.update(phase="running", total=len(todo))
        _log("vendor-check: %d to verify (%d already checked)"
             % (len(todo), len(scope) - len(todo)))

        br = browser if browser is not None else browser_mod.Browser(
            default_profile_dir())
        if hasattr(br, "launch"):
            br.launch()
        consecutive_blocked = 0
        cancelled = False
        for item_id in todo:
            if _CANCEL.is_set():
                cancelled = True
                _log("vendor-check: cancelled by user")
                break
            with _LOCK:
                _STATE["current"] = item_id
            try:
                result = br.extract_sold_by(item_id)
            except Exception as exc:  # browser crash must not kill the job
                result = {"status": "error", "detail": str(exc)}
                if hasattr(br, "relaunch_if_dead"):
                    try:
                        br.relaunch_if_dead()
                    except Exception:
                        pass
            status = result.get("status") or "error"
            if status == "ok":
                consecutive_blocked = 0
                sold = 1 if result.get("sold") else 0
                npcs = result.get("npcs") or []
                db.upsert_vendor_source(conn, item_id, sold, npcs,
                                        wowhead_cost(npcs), _now(), "ok")
                with _LOCK:
                    _STATE["sold" if sold else "not_sold"] += 1
            elif status == "blocked":
                consecutive_blocked += 1
                db.upsert_vendor_source(conn, item_id, 0, [], None, _now(),
                                        "blocked")
                with _LOCK:
                    _STATE["blocked"] += 1
                _log("vendor-check: Cloudflare challenge at item %d"
                     % item_id)
            else:
                db.upsert_vendor_source(conn, item_id, 0, [], None, _now(),
                                        "error")
                if hasattr(br, "relaunch_if_dead"):
                    try:
                        br.relaunch_if_dead()
                    except Exception:
                        pass
            with _LOCK:
                _STATE["done"] += 1
            if status == "blocked" and consecutive_blocked >= max_blocked:
                with _LOCK:
                    _STATE["needs_attention"] = True
                _log("vendor-check: paused - finish the check in the "
                     "Edge window, then start again to resume")
                break
            pause = random.uniform(delay_range[0], delay_range[1])
            if pause > 0:
                time.sleep(pause)
        with _LOCK:
            if cancelled:
                _STATE["phase"] = "cancelled"
            elif _STATE["needs_attention"]:
                _STATE["phase"] = "paused"
            else:
                _STATE["phase"] = "complete"
            _STATE["current"] = 0
            _STATE["running"] = False
        _log("vendor-check: %s - %d sold, %d not sold, %d blocked"
             % (_STATE["phase"], _STATE["sold"], _STATE["not_sold"],
                _STATE["blocked"]))
        return status_snapshot()
    except Exception as exc:
        with _LOCK:
            _STATE["error"] = str(exc)
            _STATE["phase"] = "error"
            _STATE["running"] = False
        _log("vendor-check: failed: %s" % exc)
        return status_snapshot()
    finally:
        if conn is not None:
            conn.close()
        if owns_browser and br is not None and hasattr(br, "close"):
            br.close()


def status(cfg, db_path=None):
    with _LOCK:
        out = {key: _STATE[key] for key in _FIELDS}
        thread = _STATE.get("thread")
        if thread is not None:
            out["running"] = thread.is_alive()
        return out


def status_snapshot():
    return status({})


def start_async(cfg, runner=None, db_path=None, cache_dir=None, browser=None,
                delay=None, keep_existing=True, log=None):
    """Run vendor-check in a background thread; second starts are ignored."""
    with _LOCK:
        thread = _STATE.get("thread")
        if thread and thread.is_alive():
            pass  # a run is already in flight
        else:
            runner = runner or run_check
            _STATE["error"] = ""
            _CANCEL.clear()  # a new run starts un-cancelled; later clicks stick

            def target():
                try:
                    runner(cfg, db_path=db_path, cache_dir=cache_dir,
                           browser=browser, delay=delay,
                           keep_existing=keep_existing, log=log)
                except Exception as exc:
                    with _LOCK:
                        _STATE["error"] = str(exc)
                        _STATE["running"] = False

            worker = threading.Thread(target=target, daemon=True)
            _STATE["thread"] = worker
            worker.start()
    return status(cfg, db_path)


def cancel():
    """Request a stop at the next item boundary; True when a run was active."""
    with _LOCK:
        thread = _STATE.get("thread")
        active = bool(thread and thread.is_alive())
    if active:
        _CANCEL.set()
    return active


def join_worker(timeout=10):
    """Wait for a background vendor-check worker (graceful shutdown).

    The worker takes `_LOCK` for every counter update, so the lock must NOT be
    held here or the join could never return.
    """
    with _LOCK:
        thread = _STATE.get("thread")
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()
