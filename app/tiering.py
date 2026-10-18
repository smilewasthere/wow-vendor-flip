"""Vendor tiering (M4), PLAN section 5.3.

Soft hints only: they move `likely` into `unverified` and show UI chips.
They never exclude an item from scoring (user rule, revision 3).
"""

VENDOR_GOODS_CLASSES = frozenset({
    "Tradeskill", "Consumable", "Miscellaneous", "Recipe", "Profession",
    "Glyph", "Container", "Gem", "Item Enhancement", "Housing",
})


def _as_dict(row):
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    raise TypeError("vendor_tier expects a mapping or sqlite3.Row")


def vendor_tier(row):
    """Return {"tier": str, "chips": [str, ...]} for one candidate row.

    Tiers: likely / unverified / unknown-price / no-price.
    """
    data = _as_dict(row)
    try:
        purchase = int(data.get("purchase_price") or 0)
    except (TypeError, ValueError):
        purchase = 0

    chips = []
    if data.get("synthetic_multiple"):
        chips.append("buy ~4-5x sell (soft hint)")
    if data.get("is_equippable"):
        chips.append("equippable (soft hint)")
    item_class = data.get("item_class") or ""
    if item_class and item_class not in VENDOR_GOODS_CLASSES:
        chips.append("class %s outside vendor goods (soft hint)"
                     % item_class)

    if purchase <= 0:
        tier = "no-price"
    elif purchase == 1:
        tier = "unknown-price"
    elif chips:
        tier = "unverified"
    else:
        tier = "likely"
    return {"tier": tier, "chips": chips}
