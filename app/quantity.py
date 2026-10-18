import math


def poisson_cdf(k, lam):
    if lam < 0:
        raise ValueError("lam must be >= 0")
    if k < 0:
        return 0.0
    if lam == 0.0:
        return 1.0
    if lam <= 700.0:
        p = math.exp(-lam)
        cum = p
        for i in range(1, k + 1):
            p *= lam / i
            cum += p
        return min(cum, 1.0)
    return 0.5 * (1.0 + math.erf((k + 0.5 - lam) / math.sqrt(2.0 * lam)))


def poisson_sf(k, lam):
    return 1.0 - poisson_cdf(k - 1, lam)


def max_poisson_total(lam, conf):
    if lam < 0:
        raise ValueError("lam must be >= 0")
    if not 0.0 < conf < 1.0:
        raise ValueError("conf must be in (0, 1)")
    thr = 1.0 - conf
    if lam <= 700.0:
        p = math.exp(-lam)
        cum = p
        if cum > thr:
            return 0
        last = 0
        k = 1
        while True:
            p *= lam / k
            cum += p
            if cum > thr:
                return last + 1
            last = k
            k += 1
    sigma = math.sqrt(lam)
    start = int(lam - 6.0 * sigma)
    if start < 0 or poisson_cdf(start, lam) > thr:
        start = 0
        if poisson_cdf(0, lam) > thr:
            return 0
    last = start
    k = start + 1
    while poisson_cdf(k, lam) <= thr:
        last = k
        k += 1
    return last + 1


def max_post_quantity(lam_eff, ahead, conf):
    if ahead < 0:
        ahead = 0
    return max(0, max_poisson_total(lam_eff, conf) - ahead)


def achieved_confidence(lam_eff, ahead, qty):
    if ahead < 0:
        ahead = 0
    if qty < 0:
        qty = 0
    return poisson_sf(ahead + qty, lam_eff)


def price_stretch(price, market_value):
    if market_value is None or market_value <= 0:
        return 1.0
    if price <= market_value:
        return 1.0
    return max(0.1, min(1.0, market_value / price))


def expected_days(units, lam_eff):
    if lam_eff <= 0:
        return float("inf")
    return units / lam_eff


def days_for_confidence(lam_eff, ahead, qty, conf):
    """Smallest d >= 0 such that selling (ahead + qty) units within d days
    is at least `conf` likely: achieved_confidence(lam_eff * d, ...) >= conf.
    Doubles a hi bound then bisects (sf is monotone in d)."""
    if lam_eff < 0:
        raise ValueError("lam_eff must be >= 0")
    if not 0.0 < conf < 1.0:
        raise ValueError("conf must be in (0, 1)")
    if qty <= 0:
        return 0.0
    if lam_eff == 0.0:
        return float("inf")

    def ok(d):
        return achieved_confidence(lam_eff * d, ahead, qty) >= conf

    lo, hi = 0.0, 1.0
    while not ok(hi):
        hi *= 2.0
        if hi > 1e12:
            return float("inf")
    while hi - lo > 1e-3:
        mid = (lo + hi) * 0.5
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi
