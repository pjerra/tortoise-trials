"""Baseline comparison: k/n metrics get a difference and Fisher's exact p."""
from . import stats


def note_for(p):
    if p < 0.05:
        return "differs (p<0.05)"
    if p < 0.20:
        return "suggestive (p<0.20)"
    return "no evidence"

def fmt_metric(m):
    """k/n carriers its k/n; mean metrics show the per-fight mean (k/n)."""
    k, n = int(m.get("k", 0)), int(m.get("n", 0))
    if "mean" in m:
        return "%.2f/fight (%d/%d)" % (float(m["mean"]), k, n)
    return stats.pct(k, n)


def compare_metrics(walk_metrics, base_metrics):
    """Compare {name: {k, n}} dicts. Returns [{metric, walk, base, diff_pp,
    p, note}] for metrics both walks have (means: values and difference only,
    diff in mean points)."""
    rows = []
    for name in sorted(set(walk_metrics) & set(base_metrics)):
        w, b = walk_metrics[name], base_metrics[name]
        wk, wn = int(w.get("k", 0)), int(w.get("n", 0))
        bk, bn = int(b.get("k", 0)), int(b.get("n", 0))
        if wn == 0 and bn == 0:
            continue
        if "mean" in w or "mean" in b:
            wm = float(w.get("mean", wk / wn if wn else 0.0))
            bm = float(b.get("mean", bk / bn if bn else 0.0))
            rows.append({"metric": name, "walk": fmt_metric(w),
                         "base": fmt_metric(b), "diff_pp": wm - bm,
                         "p": None, "note": "means differ by %+.2f/fight (no test)"
                         % (wm - bm)})
            continue
        wp = 100.0 * wk / wn if wn else 0.0
        bp = 100.0 * bk / bn if bn else 0.0
        p = stats.fisher_two_sided(wk, wn - wk, bk, bn - bk)
        rows.append({"metric": name, "walk": stats.pct(wk, wn),
                     "base": stats.pct(bk, bn), "diff_pp": wp - bp,
                     "p": p, "note": note_for(p)})
    return rows


def render(rows):
    L = ["### Baseline comparison", "",
         "Cycles inside one fight are not independent;",
         "treat cycle-level p-values as optimistic. Fight-level metrics",
         "(kill rate, wipe rate) are the safer test.", ""]
    if not rows:
        L.append("No shared k/n metrics with the baseline.")
        return "\n".join(L) + "\n"
    L.append("| metric | walk | baseline | diff (pp) | p | note |")
    L.append("|---|---|---|---|---|---|")
    for r in rows:
        p = "-" if r["p"] is None else ("%.4f" % r["p"])
        L.append("| %s | %s | %s | %+.1f | %s | %s |"
                 % (r["metric"], r["walk"], r["base"], r["diff_pp"], p, r["note"]))
    return "\n".join(L) + "\n"
