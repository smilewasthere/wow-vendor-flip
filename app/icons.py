"""Lazy icon filenames (M4): fetched only when a row actually asks for one.

Cache = `icon_cache` table, bounded by `icon_cache_max` (default 1000): every
newly loaded icon evicts the least recently used one, so scrolling never grows
storage beyond the cap. Empty filenames (upstream 404s) are cached too so a
missing icon is fetched at most once. The image bytes themselves are loaded by
the browser straight from the CDN - only the filename lives here.
"""

import datetime
import re
import sqlite3
import time
import urllib.error
import urllib.request

from app import blizzard

DEFAULT_CAP = 1000
MAX_CAP = 100000  # ~5 MB even at this extreme; anything larger is nonsense
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
_RENDER_PREFIX = "https://render.worldofwarcraft.com/"
_TOKEN_CACHE = {}  # (client_id, region) -> (token, expiry epoch)
# Wowhead answers 403 to a bare/stdlib user agent; this browser set was
# verified live to return 200.
WOWHEAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-site",
}
_OG_IMAGE_RE = re.compile(
    r"wow\.zamimg\.com/images/wow/icons/large/([A-Za-z0-9_]+)\.jpg")
_COOLDOWN_S = 300.0
_RESOLVE_COOLDOWN = {}  # numeric render URL -> epoch until re-resolution


def cache_cap(cfg):
    try:
        cap = int((cfg or {}).get("icon_cache_max") or 0)
    except (TypeError, ValueError):
        return DEFAULT_CAP
    if 0 < cap <= MAX_CAP:
        return cap
    return DEFAULT_CAP


def default_now():
    return datetime.datetime.now().isoformat(timespec="microseconds")


def _resolve_render_value(cfg, conn, item_id, value, resolve_fn):
    """Wowhead mode: trade a numeric render URL for its real icon filename.

    Numeric basenames do not exist on the Wowhead CDN, so the icon page is
    asked for the filename and the cache row is rewritten. A failed lookup
    cools the URL off so it is not re-fetched for every row asking for it.
    """
    if not value or (cfg or {}).get("icon_host") != "wowhead":
        return value
    if not value.startswith(_RENDER_PREFIX):
        return value
    fileid = value.rsplit("/", 1)[-1].split(".", 1)[0]
    if not fileid.isdigit():
        return value
    now = time.time()
    if now < _RESOLVE_COOLDOWN.get(value, 0.0):
        return value
    name = (resolve_fn or resolve_icon_name)(fileid)
    if not name:
        _RESOLVE_COOLDOWN[value] = now + _COOLDOWN_S
        return value
    try:
        conn.execute("UPDATE icon_cache SET icon = ? WHERE id = ?",
                     (name, item_id))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return name


def get_icon(cfg, conn, item_id, *, fetch_fn=None, now_fn=None, cap=None,
             resolve_fn=None):
    """Icon filename for item_id, fetching it only on a cache miss."""
    now = (now_fn or default_now)()
    if cap is None:
        cap = cache_cap(cfg)
    elif not 0 < cap <= MAX_CAP:
        cap = DEFAULT_CAP
    row = conn.execute(
        "SELECT icon FROM icon_cache WHERE id = ?", (item_id,)).fetchone()
    if row is not None:
        # LRU touch is best-effort: during extraction the write lock may be
        # held; a skipped touch just ranks the row slightly lower next time.
        try:
            conn.execute(
                "UPDATE icon_cache SET updated_at = ? WHERE id = ?",
                (now, item_id))
            conn.commit()
        except sqlite3.OperationalError:
            pass
        return _resolve_render_value(cfg, conn, item_id, row[0], resolve_fn)
    try:
        name = (fetch_fn or fetch_icon_name)(cfg, item_id)
    except blizzard.BlizzardError as exc:
        if getattr(exc, "status", None) != 404:
            raise
        name = ""
    conn.execute(
        "INSERT OR REPLACE INTO icon_cache (id, icon, updated_at)"
        " VALUES (?, ?, ?)", (item_id, name, now))
    conn.execute(
        "DELETE FROM icon_cache WHERE id NOT IN"
        " (SELECT id FROM icon_cache ORDER BY updated_at DESC LIMIT ?)",
        (cap,))
    conn.commit()
    return _resolve_render_value(cfg, conn, item_id, name, resolve_fn)


def icon_url(cfg, icon):
    """URL the browser loads the icon from.

    Only the render CDN may be linked to (https, pinned prefix): the stored
    value comes from Blizzard, but a poisoned cache row or hostile response
    must not aim the UI at an arbitrary host or a non-https scheme. Name-style
    values are embedded in a fixed-host template after a strict character
    check; anything else renders nothing.

    M12: Blizzard's media API returns full render CDN URLs (numeric file
    ids), which wowhead mode used to rewrite to the Wowhead CDN.

    M13: zamimg hosts no numeric filenames (404), so in wowhead mode a
    numeric basename now passes through to the render CDN, which serves
    every file id 200; name-style values still rewrite to the Wowhead CDN.
    get_icon resolves numeric ids to filenames so the rewrite happens
    whenever the icon page knows the name.
    """
    if not icon:
        return ""
    wowhead = (cfg or {}).get("icon_host") == "wowhead"
    if icon.startswith("https://"):
        if not icon.startswith(_RENDER_PREFIX):
            return ""
        if not wowhead:
            return icon
        name = icon.rsplit("/", 1)[-1]
        if not _SAFE_NAME.fullmatch(name):
            return ""
        if name.split(".", 1)[0].isdigit():
            return icon
        return "https://wow.zamimg.com/images/wow/icons/large/%s" % name
    if not _SAFE_NAME.fullmatch(icon):
        return ""
    if wowhead:
        return "https://wow.zamimg.com/images/wow/icons/large/%s.jpg" % icon
    region = (cfg or {}).get("region") or "eu"
    return "https://render.worldofwarcraft.com/%s/icons/56/%s.jpg" % (
        region, icon)


def _wowhead_get(url, headers, timeout):
    req = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.geturl()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(), exc.geturl()
        finally:
            exc.close()


def resolve_icon_name(fileid, get=None, timeout=8.0):
    """Filename behind a numeric wowhead icon id ('' when unknown).

    The icon page 302-redirects to /icon={id}/{slug} and embeds the real
    filename in og:image; zamimg hosts no numeric names, so the id has to
    be translated before the UI can load the icon from the Wowhead CDN.
    """
    url = "https://www.wowhead.com/icon=%s" % fileid
    try:
        if get is None:
            status, body, final_url = _wowhead_get(url, WOWHEAD_HEADERS,
                                                   timeout)
        else:
            status, body, final_url = get(url, WOWHEAD_HEADERS)
        if status != 200:
            return ""
        text = (body.decode("utf-8", "replace")
                if isinstance(body, bytes) else (body or ""))
        match = _OG_IMAGE_RE.search(text)
        if match:
            name = match.group(1)
        else:
            name = (final_url or "").rsplit("/", 1)[-1].replace("-", "_")
        if not _SAFE_NAME.fullmatch(name) or name.isdigit():
            return ""
        return name
    except Exception:
        return ""


def _cached_token(cfg, get_token_fn, time_fn):
    key = (cfg.get("client_id") or "", cfg.get("region") or "")
    token, expiry = _TOKEN_CACHE.get(key, ("", 0.0))
    if time_fn() < expiry - 60:
        return token
    token, expires_in = get_token_fn(cfg)
    _TOKEN_CACHE[key] = (token, time_fn() + (expires_in or 0))
    return token


def fetch_icon_name(cfg, item_id, get=None, get_token_fn=None, time_fn=None):
    """Blizzard media call -> CDN URL (or icon name on older payloads);
    404 becomes '' (cached upstream so it is fetched at most once).

    Measured live 2026-10-13: /data/wow/media/item/{id} lives on the
    static-{region} namespace (dynamic-* 404s) and assets[].value is a full
    render CDN URL with a numeric file id, not an icon filename.
    """
    get_token_fn = get_token_fn or blizzard.get_token
    time_fn = time_fn or time.time
    token = _cached_token(cfg, get_token_fn, time_fn)
    namespace = blizzard.STATIC % (cfg.get("region") or "eu")
    try:
        payload = blizzard.fetch_json(
            cfg, token, "/data/wow/media/item/%d" % item_id,
            namespace, get=get)
    except blizzard.BlizzardError as exc:
        if getattr(exc, "status", None) == 404:
            return ""
        raise
    return blizzard.parse_media_icon(payload)
