"""Configuration: defaults overlaid by config.local.json (gitignored, never logged)."""

import json
import os

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8008,
    "region": "eu",
    "client_id": "",
    "client_secret": "",
    "daily_budget": 50000,
    "commodity_share": 0.02,
    "min_profit": 5.0,
    "min_roi": 0.30,
    "min_sold_per_day": 0.5,
    "min_sale_rate": 0.02,
    "ah_count": 1,
    "icon_host": "blizzard",
    "tsm_boot_refresh": True,
    "auctions_boot_refresh": True,
}

SECRET_KEYS = ("client_secret",)


class ConfigError(Exception):
    """Raised when config.local.json cannot be used."""


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_path():
    return os.path.join(repo_root(), "config.local.json")


def defaults():
    return dict(DEFAULTS)


def load(path=None):
    cfg = defaults()
    if path is None:
        path = default_path()
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise ConfigError("config file is not valid JSON: %s (%s)" % (path, exc))
    except OSError as exc:
        raise ConfigError("cannot read config file: %s (%s)" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("config file must contain a JSON object: %s" % path)
    cfg.update(data)
    return cfg


def redacted(cfg):
    out = dict(cfg)
    for key in SECRET_KEYS:
        if out.get(key):
            out[key] = "***"
    return out
