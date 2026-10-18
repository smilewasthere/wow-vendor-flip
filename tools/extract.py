"""M3 extraction CLI: resumable per-item purchase-price fetch.

    python tools\\extract.py --check     # config + universe + progress, no network
    python tools\\extract.py             # run (resumes automatically)
    python tools\\extract.py --limit 25  # smoke: fetch only 25 items
    python tools\\extract.py --restart   # full re-extract from the first item

Ctrl+C pauses safely: committed batches stay on disk and the next run
resumes at the cursor. Credentials come from config.local.json and are
never printed.
"""

import argparse
import os
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import blizzard, config, extract, tsm

SECONDS_PER_ITEM = 0.12   # pace 0.11 + typical latency, pace-bound


def _eta_text(count):
    minutes = int(count * SECONDS_PER_ITEM / 60)
    if minutes < 1:
        return "under a minute"
    return "about %d min" % minutes


def _positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _check(cfg, config_path):
    print("wow-vendor-flip extraction - config check (no network)")
    print("  config file     : %s" % config_path)
    print("  region          : %s" % cfg.get("region"))
    print("  client_id       : %s" % ("***" if cfg.get("client_id") else "(blank)"))
    print("  client_secret   : %s" % ("***" if cfg.get("client_secret") else "(blank)"))

    ready = True
    if not (cfg.get("client_id") and cfg.get("client_secret")):
        print("  credentials     : MISSING - fill config.local.json")
        ready = False
    else:
        print("  credentials     : present (never printed)")

    try:
        tables = tsm.load_tables(cfg, extract.default_cache_dir())
        universe = extract.universe_from_tables(tables)
        print("  universe        : %d ids from TSM cache" % len(universe))
        if not universe:
            ready = False
        eta = _eta_text(len(universe))
    except tsm.TsmError as exc:
        print("  universe        : TSM cache unusable (%s)" % exc)
        print("                    run the app once or check data/tsm/")
        ready = False
        universe = []
        eta = "-"

    st = extract.status(cfg)
    print("  database        : %s" % extract.default_db_path())
    print("  progress        : phase %s | scanned %d | candidates %d%s"
          % (st["phase"], st["scanned"], st["candidates"],
             (" | running" if st["running"] else "")))
    if st["last_error"]:
        print("  last error      : %s" % st["last_error"])
    if universe:
        print("  full run        : ~%.2f s per item -> %s total"
              % (SECONDS_PER_ITEM, eta))
        print("  calls           : 1 token + %d /data/wow/item/{id}" % len(universe))
    if not ready:
        print("  STATUS          : NOT READY - see above")
        return 2
    print("  STATUS          : ready - run: python tools\\extract.py")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="extract")
    parser.add_argument("--config", default=None, help="path to config.local.json")
    parser.add_argument("--check", action="store_true",
                        help="validate config and print the plan, no network")
    parser.add_argument("--limit", type=_positive_int, default=None,
                        help="fetch at most N items this run (smoke runs)")
    parser.add_argument("--restart", action="store_true",
                        help="discard progress and extract every item again")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except config.ConfigError as exc:
        print("config error: %s" % exc)
        return 2

    if args.check:
        return _check(cfg, args.config or config.default_path())

    if not (cfg.get("client_id") and cfg.get("client_secret")):
        print("missing credentials: fill client_id and client_secret in %s"
              % (args.config or config.default_path()))
        print("run  python tools\\extract.py --check  for details")
        return 2

    def log(line):
        print(line, flush=True)

    print("starting extraction%s%s ..." % (
        " (restart)" if args.restart else "",
        " (limit %d)" % args.limit if args.limit else ""), flush=True)
    try:
        status = extract.run_extraction(cfg, limit=args.limit,
                                        restart=args.restart, log=log)
    except KeyboardInterrupt:
        print("", flush=True)
        print("paused - progress saved; re-run the same command to resume")
        st = extract.status(cfg)
        print("  scanned %d, candidates %d, cursor %d"
              % (st["scanned"], st["candidates"], st["cursor"]))
        return 130
    except blizzard.BlizzardError as exc:
        print("error: %s" % exc)
        print("progress saved - re-run the same command to resume")
        st = extract.status(cfg)
        print("  scanned %d, candidates %d, cursor %d"
              % (st["scanned"], st["candidates"], st["cursor"]))
        return 1
    except (sqlite3.OperationalError, OSError) as exc:
        print("error: %s" % exc)
        print("progress preserved - re-run the same command to resume")
        return 1

    print("done: phase %s | scanned %d | candidates %d | not found %d"
          % (status["phase"], status["scanned"], status["candidates"],
             status["not_found"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
