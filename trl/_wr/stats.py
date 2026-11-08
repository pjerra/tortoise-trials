"""Wilson interval, Fisher's exact test, pct formatting, median helpers. Stdlib only."""
import math
import statistics


def wilson(k, n, z=1.96):
    """Wilson score interval for k/n at ~95% (z=1.96). Returns (low, high).

    n == 0 is undefined: returns (0.0, 1.0) (total uncertainty), never crash.
    """
    k = int(k)
    n = int(n)
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _hypergeom_prob(x, n1, n2, k1):
    """P(X == x) for a 2x2 table with row sums n1, n2 and col-1 sum k1."""
    return math.comb(n1, x) * math.comb(n2, k1 - x) / math.comb(n1 + n2, k1)


def fisher_two_sided(a, b, c, d, rel_tol=1e-7):
    """Two-sided Fisher exact p for [[a, b], [c, d]].

    Hypergeometric sum over all tables with the same margins whose probability
    is at most the observed table's (relative tolerance rel_tol). With
    math.comb this is exact integer arithmetic, no scipy needed.
    """
    n1 = a + b
    n2 = c + d
    k1 = a + c
    n = n1 + n2
    if n == 0:
        return 1.0
    lo = max(0, k1 - n2)
    hi = min(n1, k1)
    obs = _hypergeom_prob(a, n1, n2, k1)
    total = 0.0
    for x in range(lo, hi + 1):
        if _hypergeom_prob(x, n1, n2, k1) <= obs * (1.0 + rel_tol):
            total += _hypergeom_prob(x, n1, n2, k1)
    return min(1.0, max(0.0, total))


def pct(k, n):
    """'k/n (x.y%)' with a 0/0-safe 0.0%."""
    k = int(k)
    n = int(n)
    return "%d/%d (%.1f%%)" % (k, n, (100.0 * k / n) if n else 0.0)


def median(vals):
    """Median of vals ignoring None; None when empty."""
    v = [float(x) for x in vals if x is not None]
    if not v:
        return None
    return float(statistics.median(v))


def p90(vals):
    """90th percentile (nearest-rank) ignoring None; None when empty."""
    v = sorted(float(x) for x in vals if x is not None)
    if not v:
        return None
    idx = min(len(v) - 1, max(0, math.ceil(0.9 * len(v)) - 1))
    return float(v[idx])
