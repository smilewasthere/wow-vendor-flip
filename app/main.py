"""Entry point: load config, start the local server, open the browser."""

import argparse
import os
import sys
import webbrowser

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auctions, config, extract, httpd, tsm, vendor_check


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wow-vendor-flip")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--config", default=None, help="path to config.local.json")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except config.ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2

    try:
        server = httpd.build_server(cfg)
    except OSError as exc:
        print("cannot start server on %s:%d: %s"
              % (cfg["host"], cfg["port"], exc), file=sys.stderr)
        return 2
    # M15: refresh TSM CSVs in the background while the app comes up.
    # Daemon thread, single-flight, errors recorded - boot never blocks.
    if cfg.get("tsm_boot_refresh", True):
        tsm.refresh_async(cfg, cfg.get("cache_dir")
                          or extract.default_cache_dir())
    # M16: same pattern for the commodity wall + connected-realm count.
    if cfg.get("auctions_boot_refresh", True):
        auctions.refresh_async(cfg)
    url = "http://%s:%d/" % (cfg["host"], server.server_address[1])
    print("wow-vendor-flip listening on %s" % url, flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.server_close()
        # Stop the vendor-check worker at an item boundary so its finally
        # closes the headed Edge (otherwise the profile keeps a live browser).
        vendor_check.cancel()
        if not vendor_check.join_worker(30):
            print("vendor check still winding down - Edge will close shortly")
        if not extract.join_worker(10):
            print("extraction still in progress - progress is saved;"
                  " restart to resume")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
