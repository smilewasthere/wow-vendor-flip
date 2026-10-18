"""Blizzard OAuth + Game Data API client. Stdlib only; secrets are never logged."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth.battle.net/token"
USER_AGENT = "wow-vendor-flip/0.0.1-M9"
LOCALES = {"eu": "en_GB", "us": "en_US", "kr": "ko_KR", "tw": "zh_TW"}
STATIC = "static-%s"
DYNAMIC = "dynamic-%s"


class BlizzardError(Exception):
    """Any Blizzard API failure. Messages never contain credentials."""

    status = None  # HTTP status when the failure came from an API response


class CredentialError(BlizzardError):
    """Missing or unusable client id/secret."""


def _redact(text, secret):
    if secret:
        text = text.replace(secret, "***")
    return text


def _decode(body):
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body or ""


def token_request_parts(cfg):
    client_id = cfg.get("client_id") or ""
    client_secret = cfg.get("client_secret") or ""
    if not client_id or not client_secret:
        raise CredentialError(
            "blizzard client_id and client_secret are required in config.local.json"
        )
    # Battle.net rejects a token request that repeats client_id in the body
    # alongside Basic auth (HTTP 401 invalid_client); send grant_type only.
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii")
    cred = base64.b64encode(("%s:%s" % (client_id, client_secret)).encode("utf-8"))
    headers = {
        "Authorization": "Basic " + cred.decode("ascii"),
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    }
    return TOKEN_URL, body, headers


def build_token_request(cfg):
    url, body, headers = token_request_parts(cfg)
    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    return req


def parse_token_response(status, body, secret=None):
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    if status != 200:
        err = BlizzardError("token request failed: HTTP %s: %s" % (status, text[:500]))
        # keep the HTTP status so routes can map 429 -> retryable 503
        err.status = status
        raise err
    try:
        data = json.loads(text)
    except ValueError:
        raise BlizzardError("token response was not JSON: %s" % text[:300])
    token = data.get("access_token")
    if not token:
        raise BlizzardError("token response has no access_token: %s" % text[:300])
    return token, int(data.get("expires_in", 0))


def get_token(cfg, post=None):
    url, body, headers = token_request_parts(cfg)
    status, payload = (post or _post)(url, body, headers)
    return parse_token_response(status, payload, secret=cfg.get("client_secret"))


def build_api_url(path, cfg, namespace, params=None):
    region = cfg.get("region") or "eu"
    locale = LOCALES.get(region, "en_GB")
    query = {"namespace": namespace, "locale": locale}
    if params:
        query.update(params)
    return "https://%s.api.blizzard.com%s?%s" % (
        region,
        path,
        urllib.parse.urlencode(query),
    )


def parse_api_response(status, body, secret=None):
    text = _redact(_decode(body), secret)
    if status != 200:
        err = BlizzardError("api request failed: HTTP %s: %s" % (status, text[:500]))
        err.status = status
        raise err
    try:
        return json.loads(text)
    except ValueError:
        raise BlizzardError("api response was not JSON: %s" % text[:300])


def fetch_json(cfg, token, path, namespace, params=None, get=None):
    url = build_api_url(path, cfg, namespace, params)
    headers = {"Authorization": "Bearer " + token, "User-Agent": USER_AGENT}
    status, body = (get or _get)(url, headers)
    return parse_api_response(status, body, secret=cfg.get("client_secret"))


def static_namespace(cfg):
    return STATIC % (cfg.get("region") or "eu")


def parse_media_icon(payload):
    """Icon filename from a /data/wow/media/item payload ('' if absent)."""
    for asset in (payload or {}).get("assets") or []:
        if asset.get("key") == "icon":
            return asset.get("value") or ""
    return ""


# Item payload parsing shared by the M1 probe and extraction (M3).

MULTIPLE_LO = 3.9
MULTIPLE_HI = 5.1


def is_synthetic_multiple(purchase_price, sell_price):
    """Hypothesis only: buy price sitting at 4-5x the sell price."""
    if not sell_price:
        return False
    ratio = purchase_price / sell_price
    return MULTIPLE_LO <= ratio <= MULTIPLE_HI


def parse_item(payload):
    payload = payload or {}
    preview = payload.get("preview_item") or {}
    requirements = preview.get("requirements") or {}
    reputation = requirements.get("reputation") or {}
    faction = reputation.get("faction") or {}

    purchase = int(payload.get("purchase_price") or 0)
    sell = int(payload.get("sell_price") or 0)
    quantity = int(payload.get("purchase_quantity") or 0)
    ratio = round(purchase / sell, 4) if sell else None
    quality = payload.get("quality")
    if isinstance(quality, dict):
        quality = quality.get("type") or quality.get("name") or ""
    elif quality is None:
        quality = ""
    return {
        "id": int(payload.get("id") or 0),
        "name": payload.get("name") or "",
        "purchase_price": purchase,
        "sell_price": sell,
        "purchase_quantity": quantity,
        "ratio": ratio,
        "synthetic_multiple": is_synthetic_multiple(purchase, sell),
        "quality": quality,
        "is_equippable": bool(payload.get("is_equippable")),
        "item_class": (payload.get("item_class") or {}).get("name", ""),
        "item_subclass": (payload.get("item_subclass") or {}).get("name", ""),
        "reputation": (faction.get("name") or ""),
        "error": "",
        "expected": "",
    }


def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    return _open(req)


def _get(url, headers):
    req = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        req.add_header(key, value)
    return _open(req)


def _open(req, timeout=60):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise BlizzardError("cannot reach %s: %s" % (req.full_url.split("?")[0], exc.reason))
