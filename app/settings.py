"""Runtime settings (M6): settings-table overrides layered over config.

The UI writes here; `config.local.json` is never modified by the app.
Values are validated on write and coerced on read; `apply(cfg, conn)`
returns the effective config used by extraction, icons, and pricing.

M12: only Region and Icon host remain user-facing. Pricing thresholds are
hard-set in pricing.DEFAULTS; realm and refresh were removed (region-only
pricing; no auto-refresh loop exists).
"""

import math
import re

SPECS = {
    "region": {"kind": "choice", "choices": ("eu", "us")},
    "icon_host": {"kind": "choice", "choices": ("blizzard", "wowhead")},
}

# Keys that used to be settings; purged at boot so stale rows can never
# override the hard-set defaults.
REMOVED_KEYS = (
    "realm_slug", "refresh_minutes",
    "min_profit", "min_roi", "min_sold_per_day", "min_sale_rate",
    "commodity_share", "daily_budget", "ah_count",
)


def purge_removed(conn):
    """Delete settings rows for keys no longer in SPECS (best-effort)."""
    if not REMOVED_KEYS:
        return
    marks = ",".join("?" for _ in REMOVED_KEYS)
    conn.execute("DELETE FROM settings WHERE key IN (%s)" % marks,
                 list(REMOVED_KEYS))
    conn.commit()


def coerce(key, raw):
    """JSON value -> normalized python value. Raises ValueError."""
    spec = SPECS.get(key)
    if spec is None:
        raise ValueError("unknown setting: %s" % key)
    if isinstance(raw, bool):
        raise ValueError("%s must not be a boolean" % key)
    kind = spec["kind"]
    if kind == "text":
        if not isinstance(raw, str):
            raise ValueError("%s must be a string" % key)
        text = raw.strip()
        if len(text) > spec["max_len"]:
            raise ValueError("%s too long (max %d)"
                             % (key, spec["max_len"]))
        pattern = spec.get("pattern")
        if pattern and not re.fullmatch(pattern, text):
            raise ValueError("%s must match %s" % (key, pattern))
        return text
    if kind == "choice":
        if raw not in spec["choices"]:
            raise ValueError("%s must be one of %s"
                             % (key, ", ".join(spec["choices"])))
        return str(raw)
    if kind == "integer":
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError("%s must be an integer" % key)
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("%s must be an integer" % key)
    else:  # number
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("%s must be a number" % key)
        if not math.isfinite(value):
            raise ValueError("%s must be a finite number" % key)
    if value < spec["min"] or value > spec["max"]:
        raise ValueError("%s must be between %s and %s"
                         % (key, spec["min"], spec["max"]))
    return value


def _store_string(key, value):
    """Normalized python value -> the string persisted in the table.

    repr/str round-trip exactly (float shortest-repr); %g would corrupt
    large or high-precision values on read-back.
    """
    if isinstance(value, float):
        return repr(value)
    return str(value)


def stored(conn):
    """All persisted overrides as normalized values (corrupt rows skipped)."""
    out = {}
    for key, raw in conn.execute("SELECT key, value FROM settings"):
        try:
            out[key] = coerce(key, raw)
        except ValueError:
            continue
    return out


def apply(cfg, conn):
    """Config with settings-table overrides layered on top."""
    merged = dict(cfg or {})
    merged.update(stored(conn))
    return merged


def write(conn, mapping):
    """Validate the whole batch first, then persist (atomic per batch)."""
    normalized = {}
    for key, value in mapping.items():
        normalized[key] = None if value is None else coerce(key, value)
    for key, value in normalized.items():
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, _store_string(key, value)))
    conn.commit()
