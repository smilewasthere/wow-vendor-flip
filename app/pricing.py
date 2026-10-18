"""Pricing engine (M5): PLAN section 5 - derived values, suspicion, verdicts.

Pure functions over extracted candidates + the three TSM tables. No DB
writes, no network: scoring is recomputed per request. Money is copper
throughout (TSM and Blizzard both report copper); thresholds are gold and
converted at comparison time.
"""

import math
import os

from app import tsm, tiering

DEFAULTS = {
    "min_profit": 5.0,
    "min_roi": 0.30,
    "min_sold_per_day": 0.5,
    "min_sale_rate": 0.02,
    "commodity_share": 0.02,
    "daily_budget": 50000,
    "ah_count": 1,
}

COPPER = 10000  # 1 gold in copper

VERDICTS = ("NOT WORTH IT", "CHECK IN GAME", "RISKY", "CHECK VENDOR",
            "GOOD", "NOT VENDOR-SOLD")

# M10: event/DEV vendors Wowhead lists under "Sold by" that no live player
# can buy from - alpha/Tournament-Realm shops, PTR test vendors, Timewalking
# and Remix event vendors, removed-affix dungeon shops. Items ONLY sold by
# these are treated as not vendor-sold; items also sold by a real vendor
# keep the real vendor(s) alone.
EVENT_VENDOR_IDS = frozenset({
    224342,   # Herbataur - alpha/beta + Tournament Realms reagent shop
    60977,    # Flaskataur, Esq. - MoP PTR test vendor (stored as Glyphataur)
    223884,   # Bobadormu - Classic Timewalking event weeks only
    234555,   # Bobadormu (second spawn id)
    219033,   # Nostwin - MoP: Remix 2024 vendor (event ended)
    220359,   # Nostwin (Past) - MoP: Remix
    220360,   # Nostwin (Future) - MoP: Remix
    220438,   # Nostwin - MoP: Remix
    222186,   # Nostwin - MoP: Remix
    185861,   # Xy Relic Dealer - SL S4 Encrypted-affix dungeon shop
})

# Single slot (key, ctx); one assignment per swap so a concurrent reader
# never sees a new key paired with an old ctx.
_CTX = {"slot": (None, None)}


def default_thresholds():
    return dict(DEFAULTS)


def _num(value, fallback):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return out if math.isfinite(out) else float(fallback)


def thresholds(cfg=None, conn=None):
    """config > built-in defaults (M12: settings-table overrides removed)."""
    out = default_thresholds()
    cfg = cfg or {}
    for key in DEFAULTS:
        if key in cfg and cfg[key] is not None:
            out[key] = _num(cfg[key], DEFAULTS[key])
    out["ah_count"] = max(1, int(round(out["ah_count"])))
    return out


def context(cfg, cache_dir):
    """itemId indexes for the two TSM tables, cached until files change."""
    paths = tsm.cache_paths(cache_dir)
    key = tuple((p, os.path.getmtime(p), os.path.getsize(p))
                for p in (paths["region"], paths["commodities"]))
    cached_key, cached_ctx = _CTX["slot"]
    if cached_key == key:
        return cached_ctx
    tables = tsm.load_tables(cfg, cache_dir)
    ctx = {
        "region": {row["itemId"]: row for row in tables["region"]},
        "commodities": {row["itemId"]: row for row in tables["commodities"]},
        "commodity_ids": {row["itemId"] for row in tables["commodities"]},
    }
    _CTX["slot"] = (key, ctx)
    return ctx


def _positive(values):
    return [v for v in values
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and v > 0]


def _suspicious(commodity, region, comm):
    """PLAN section 5.4. Returns (flagged, reason). M12: realm dropped."""
    if commodity and comm is not None:
        mv = _num(comm.get("marketValue"), 0)
        hist = _num(comm.get("historical"), 0)
        recent = _num(comm.get("recent"), 0)
        if hist and mv > 1.75 * hist:
            return True, "market value far above 60-day history"
        if region:
            avg = _num(region.get("avgSalePrice"), 0)
            if avg > 0 and mv > 1.5 * avg:
                return True, "listed well above real sale prices"
        if mv and recent and abs(mv - recent) / mv > 0.5:
            return True, "price just moved sharply"
        if region:
            sold = _num(region.get("soldPerDay"), 0)
            rate = _num(region.get("saleRate"), 0)
            if sold == 0 or rate == 0:
                return True, "no sales at this price"
            if rate < 0.02 and sold > 20:
                return True, "undercut market (low sale rate with volume)"
        return False, ""

    if region:
        sold = _num(region.get("soldPerDay"), 0)
        rate = _num(region.get("saleRate"), 0)
        if sold == 0 or rate == 0:
            return True, "no sales at this price"
        if rate < 0.02 and sold > 20:
            return True, "undercut market (low sale rate with volume)"
    return False, ""


def score_item(cand, ctx, th):
    """Score one candidate row: values, suspicion, and the 13-rule verdict."""
    item_id = cand["id"]
    region = ctx["region"].get(item_id)
    commodity = item_id in ctx["commodity_ids"]
    comm = ctx["commodities"].get(item_id) if commodity else None

    pp = int(cand.get("purchase_price") or 0)
    qty = int(cand.get("purchase_quantity") or 0)
    if qty <= 0:
        qty = 1
    cost = pp / qty if pp else 0.0

    suspicious, sus_reason = _suspicious(commodity, region, comm)

    vsrc = (ctx.get("vendor_sources") or {}).get(item_id)
    event_only = False
    if vsrc and vsrc.get("status") == "ok":
        vendor_sold = 1 if vsrc.get("sold") else 0
        vendor_npcs = [n for n in (vsrc.get("npcs") or [])
                       if (n or {}).get("id") not in EVENT_VENDOR_IDS]
        if vendor_sold == 1 and not vendor_npcs:
            # every "Sold by" entry is an event/DEV shop -> no live vendor
            vendor_sold = 0
            event_only = True
        vendor_checked_at = vsrc.get("checked_at")
    else:
        vendor_sold = None
        vendor_npcs = []
        vendor_checked_at = None
    vendor_npc = ""
    if vendor_sold == 1:
        for npc in vendor_npcs:
            name = (npc or {}).get("name") or ""
            if name:
                vendor_npc = name
                break

    pool = []
    if region:
        pool += [region.get("marketValue"), region.get("historical")]
    if comm:
        pool += [comm.get("marketValue"), comm.get("historical")]
    if suspicious:
        # PLAN 5.4: flagged rows fall back to the region pair only. No region
        # data means no trustworthy sell price -> rule 2 ("no AH price data").
        pool = _positive([region.get("marketValue"),
                          region.get("historical")] if region else [])
    pos = _positive(pool)
    sell = int(min(pos)) if pos else 0

    sold_raw = _num(region.get("soldPerDay"), 0) if region else 0.0
    sale_rate = _num(region.get("saleRate"), 0) if region else 0.0

    volume = sold_raw if commodity else sold_raw / th["ah_count"]
    # daily_budget is gold, cost is copper -> convert the budget to copper.
    units_cap = (th["daily_budget"] * COPPER / cost) if cost > 0 else 0.0
    expected = min(max(volume, 0.0) * th["commodity_share"], units_cap)

    confidence = min(max(sale_rate / 0.1, 0.2), 1.0)
    net = sell * 0.95 - cost
    roi = (net / cost) if cost > 0 else 0.0
    score = net * expected * confidence

    tier = tiering.vendor_tier(cand)
    chips = tier["chips"]
    wowhead_cost = vsrc.get("wowhead_cost") if vsrc else None
    if wowhead_cost and pp > 0 and abs(wowhead_cost - pp) / pp > 0.10:
        chips.append("cost mismatch")
    reason = _verdict(cand, pp, sell, net, roi, expected, sale_rate,
                      suspicious, sus_reason, th, chips,
                      vendor_sold, vendor_npc, event_only)
    if vendor_sold == 0:
        score = 0

    return {
        "id": item_id,
        "name": cand.get("name") or "",
        "verdict": reason[0],
        "reason": reason[1],
        "tier": tier["tier"],
        "chips": chips,
        "purchase_price": pp,
        "purchase_quantity": qty,
        "vendor_cost_unit": cost,
        "sell_price": sell,
        "net_profit_unit": net,
        "roi": roi,
        "confidence": confidence,
        "score": score,
        "sold_per_day": sold_raw,
        "sale_rate": sale_rate,
        "expected_sold_per_day": expected,
        "commodity": commodity,
        "suspicious": suspicious,
        "suspicious_reason": sus_reason,
        "vendor_verified": int(cand.get("vendor_verified") or 0),
        "vendor_sold": vendor_sold,
        "vendor_npcs": vendor_npcs,
        "vendor_checked_at": vendor_checked_at,
        "excluded": int(cand.get("excluded") or 0),
        "flagged_risky": int(cand.get("flagged_risky") or 0),
        "notes": cand.get("notes") or "",
    }


def _verdict(cand, pp, sell, net, roi, expected, sale_rate,
             suspicious, sus_reason, th, chips,
             vendor_sold=None, vendor_npc="", event_only=False):
    """PLAN section 5.6: first match wins."""
    if cand.get("excluded"):
        return "NOT WORTH IT", "excluded by you"
    if vendor_sold == 0:
        if event_only:
            return ("NOT VENDOR-SOLD",
                    "only event/DEV vendors sell it (not in live game)")
        return "NOT VENDOR-SOLD", "no vendor sells this item (Wowhead)"
    if sell <= 0:
        return "NOT WORTH IT", "no AH price data"
    if pp <= 0:
        return "NOT WORTH IT", "no buy price in item data"
    if pp == 1:
        return ("CHECK IN GAME",
                "vendor price unknown (1 copper) - could be a real vendor"
                " item")
    if net <= 0:
        return "NOT WORTH IT", "sells for less than the vendor price"
    if net / COPPER < th["min_profit"]:
        return ("NOT WORTH IT",
                "profit too low after 5%% AH cut (%.2f g/unit)"
                % (net / COPPER))
    if roi < th["min_roi"]:
        return ("NOT WORTH IT",
                "return too low for the capital (%.0f%%)" % (roi * 100))
    if expected < th["min_sold_per_day"]:
        return ("NOT WORTH IT",
                "sells too slowly (~%.2f/day)" % expected)
    if suspicious:
        return "RISKY", sus_reason
    if sale_rate < th["min_sale_rate"]:
        return ("RISKY",
                "very low sale rate (%.3f)" % sale_rate)
    if cand.get("flagged_risky") or cand.get("notes"):
        note = cand.get("notes") or ""
        if note:
            return "RISKY", "your note: %s" % note
        return "RISKY", "flagged risky by you"
    if not cand.get("vendor_verified") and vendor_sold != 1:
        reason = "looks profitable - confirm a vendor sells it"
        if chips:
            reason += " (" + "; ".join(chips) + ")"
        return "CHECK VENDOR", reason
    reason = ("profit %.2f g/unit, ~%.2f sold/day"
              % (net / COPPER, expected))
    if vendor_sold == 1 and not cand.get("vendor_verified"):
        reason += " - sold by %s (Wowhead)" % (vendor_npc or "a vendor")
    return "GOOD", reason


def score_all(candidates, ctx, th):
    return [score_item(cand, ctx, th) for cand in candidates]
