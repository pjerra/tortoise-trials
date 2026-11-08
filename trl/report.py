"""Report card (card.md + card.json), compare, and the PR block.

card.md: REPORT_TITLE + label + server + tb_version, the run line, a
data-quality block, one verdict table, then FAIL/surprise evidence lines.
compare(run, base): rows matched by (trial id, rule id); latency/ratio/uptime
k/n summed over valid repeats, Fisher's p via wr.stats.fisher_two_sided and
the note via wr.compare.note_for (vendored from the walk-report tool in trl/_wr).
pr_block(before, after, issue): the exact block for PRs.
"""

import glob
import json
import os
import re
import sys

try:
    from .naming import REPORT_TITLE, TOOL_NAME
except ImportError:  # pragma: no cover
    from trl.naming import REPORT_TITLE, TOOL_NAME

_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _surprise(expected, verdict):
    if expected == "FAIL" and verdict == "PASS":
        return "expected FAIL, got PASS"
    if expected == "PASS" and verdict == "FAIL":
        return "expected PASS, got FAIL"
    return ""


def _fmt_value(value, no_match=False):
    if value is None:
        return "no match" if no_match else "-"
    if isinstance(value, float):
        return "%.3g" % value
    return str(value)


POOLED_METRICS = {"ratio", "latency", "uptime"}


def _agg_value(details, metric=None):
    """The card value. k/n metrics (ratio, latency, uptime) show the POOLED
    k/n over the valid repeats (sum k / sum n), never the mean of per-repeat
    ratios; the other metrics keep the plain mean of the repeat values."""
    valid = [d for d in (details or []) if d.get("verdict") in ("PASS", "FAIL")]
    vals = [d.get("value") for d in valid if d.get("value") is not None]
    if metric in POOLED_METRICS:
        k = n = 0
        for d in valid:
            try:
                dk, dn = int(d.get("k", 0) or 0), int(d.get("n", 0) or 0)
            except (TypeError, ValueError):
                continue
            if dn > 0:
                k += dk
                n += dn
        if n > 0:
            return k / n, vals
    nums = []
    for v in vals:
        try:
            if v == float("inf"):
                continue
            nums.append(float(v))
        except (TypeError, ValueError, OverflowError):
            continue
    if not vals:
        return None, []
    if not nums:
        # Every repeat was a no-match (value null): the card shows "no match".
        return None, vals
    return sum(nums) / len(nums), vals


def _repeats_text(run, run_dir, run_trials):
    """The repeats that RAN, not the --repeats flag (0 means "each trial's own count").

    Counted from the rep* directories the run wrote; the flag is shown only when it
    was set. Trials that ran different counts are listed one by one.
    """
    args = run.get("args") if isinstance(run.get("args"), dict) else {}
    counts = []
    for trial in run_trials or []:
        tid = trial.get("trial") or ""
        reps = [d for d in glob.glob(os.path.join(run_dir, tid, "rep*")) if os.path.isdir(d)]
        counts.append((tid, len(reps)))
    ran = sorted({n for _, n in counts})
    if not counts or ran == [0]:
        flag = args.get("repeats")
        return "%s" % flag if flag else "?"
    if len(ran) == 1:
        return "%d" % ran[0]
    return ", ".join("%s %d" % (tid, n) for tid, n in counts)


def _rep_number(path):
    m = re.search(r"rep(\d+)", path)
    return int(m.group(1)) if m else 0


def _rep_tags(setup):
    """'; slot N; difficulty step S' for a rep whose setup.json carries them (parallel and ladder runs)."""
    tags = ""
    if setup.get("slot") not in (None, ""):
        tags += "; slot %s" % setup.get("slot")
    dif = setup.get("difficulty")
    if isinstance(dif, dict) and dif.get("step") is not None:
        tags += "; difficulty step %s" % dif.get("step")
    return tags


def _ladder_line(trial_id, steps):
    """One line per trial when any rep ran easier than written (step > 0), else ''."""
    if not any((dif.get("step") or 0) > 0 for _rep, dif in steps):
        return ""
    parts = []
    for rep, dif in steps:
        text = "%s step %s" % (rep, dif.get("step"))
        if (dif.get("step") or 0) > 0:
            text += " (level_delta %s, count %s, refill %s)" % (
                dif.get("level_delta"), dif.get("count"), "on" if dif.get("refill") else "off")
        parts.append(text)
    return "- %s: ran easier than written after rescue-cap reps: %s" % (trial_id, "; ".join(parts))


def write_card(run_dir, run_trials):
    run = _read_json(os.path.join(run_dir, "RUN.json"), {}) or {}
    label = run.get("label") or os.path.basename(run_dir.rstrip("/"))
    server = run.get("server") or "?"
    tb = run.get("tb_version") or "?"
    lines = ["# %s — %s — %s — %s" % (REPORT_TITLE, label, server, tb), "",
             "Run: started %s, slots %s, repeats %s." % (
                 run.get("started") or "?",
                 ", ".join(str(s) for s in (run.get("slots") or [])) or "?",
                 _repeats_text(run, run_dir, run_trials)),
             ""]
    lines.append("## Data quality")
    for trial in run_trials or []:
        tdir = os.path.join(run_dir, trial.get("trial") or "")
        ladder_steps = []
        for det in sorted(glob.glob(os.path.join(tdir, "rep*", "setup.json")), key=_rep_number):
            repdir = os.path.dirname(det)
            setup = _read_json(det, {}) or {}
            rep = os.path.basename(repdir)
            failed = [c.get("id") for c in (setup.get("checks") or []) if not c.get("ok")]
            verdicts = _read_json(os.path.join(repdir, "verdicts.json"), {}) or {}
            ev = (os.path.exists(os.path.join(repdir, "events.jsonl"))
                  and "events ok" or "events missing")
            summary = _read_json(os.path.join(repdir, "events_summary.json"), {}) or {}
            dropped = summary.get("dropped_kinds") or {}
            xspans = summary.get("excluded_spans") or []
            drop_s = ",".join("%s=%d" % (k, v) for (k, v) in sorted(dropped.items())) or "none"
            excl_s = "%.0f s" % sum(float(x.get("to", 0)) - float(x.get("from", 0)) for x in xspans)
            reason = verdicts.get("invalid_reason") or setup.get("invalid_reason") or ""
            cut_s = ("cut-short: %s" % reason) if "cut short" in reason else "cut-short: none"
            if "thresholds_assumed" in verdicts:
                thr_s = _thr_text(verdicts)
            else:
                thr_s = "assumed 20/50/70" if _thresholds_assumed(run) else "live"
            lines.append("- %s %s: setup %s; failed=[%s]; scored=%s s; excluded=%s; %s; %s; events_dropped=[%s]; thresholds=%s%s" % (
                trial.get("trial"), rep,
                "ok" if setup.get("ok") else "INVALID (%s)" % (setup.get("invalid_reason") or "?"),
                ",".join(failed), verdicts.get("scored_s", "?"), excl_s, cut_s, ev, drop_s,
                thr_s, "; fight_end missing" if verdicts.get("fight_end_missing") else "")
                + _rep_tags(setup))
            if isinstance(setup.get("difficulty"), dict):
                ladder_steps.append((rep, setup["difficulty"]))
        ladder_line = _ladder_line(trial.get("trial"), ladder_steps)
        if ladder_line:
            lines.append(ladder_line)
    lines += ["", "| trial | rule | expected | verdict | metric k/n (summed over repeats) | value | pass_if | surprise |",
              "|---|---|---|---|---|---|---|---|"]
    card_rows = []
    for trial in run_trials or []:
        for row in trial.get("verdicts") or []:
            sur = _surprise(row.get("expected"), row.get("verdict"))
            agg, vals = _agg_value(row.get("detail"), row.get("metric"))
            nm = sum(1 for d in (row.get("detail") or []) if d.get("no_match"))
            val_s = _fmt_value(agg, no_match=nm > 0)
            if len(vals) > 1:
                val_s += " (%s)" % ", ".join(_fmt_value(v) for v in vals)
            if nm and agg is not None:
                val_s += " [no match x%d]" % nm
            lines.append("| %s | %s | %s | %s | %d/%d | %s | %s | %s |" % (
                trial.get("trial"), row.get("id"), row.get("expected") or "?",
                row.get("verdict"), row.get("k"), row.get("n"),
                val_s, row.get("pass_if") or "", sur))
            card_rows.append({"trial": trial.get("trial"), "rule": row.get("id"),
                              "expected": row.get("expected"), "verdict": row.get("verdict"),
                              "k": row.get("k"), "n": row.get("n"),
                              "value": agg, "values": vals, "no_match": nm > 0,
                              "metric": row.get("metric"),
                              "pass_if": row.get("pass_if"), "surprise": sur,
                              "source": row.get("source"), "detail": row.get("detail")})
    lines.append("")
    for trial in run_trials or []:
        for row in trial.get("verdicts") or []:
            if row.get("verdict") in ("FAIL",) or _surprise(row.get("expected"), row.get("verdict")):
                lines.append("**%s** (%s)" % (row.get("id"), row.get("source") or "no source"))
                for det in (row.get("detail") or [])[:3]:
                    for ev in (det.get("evidence") or [])[:3]:
                        lines.append("- %s %s: %s" % (trial.get("trial"), det.get("rep"), ev))
                lines.append("")
    with open(os.path.join(run_dir, "card.md"), "w") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    with open(os.path.join(run_dir, "card.json"), "w") as f:
        json.dump({"title": "%s — %s — %s — %s" % (REPORT_TITLE, label, server, tb),
                   "run": run, "rows": card_rows}, f, indent=2, sort_keys=True,
                  allow_nan=False)
    return card_rows


def _thr_text(verdicts):
    """The thresholds the rep was scored with, following score's config logic."""
    thr = verdicts.get("thresholds") or {}
    vals = [thr.get(k) for k in ("critical", "low", "medium")]
    if verdicts.get("thresholds_assumed"):
        return "assumed %s" % "/".join(_fmt_thr(v) for v in vals)
    if any(v is None for v in vals):
        return "unknown"
    return "live"


def _fmt_thr(v):
    try:
        return "%g" % float(v)
    except (TypeError, ValueError):
        return "?"


def _thresholds_assumed(run):
    return run.get("critical_health") is None


def _wr():
    from trl._wr import compare as _compare
    from trl._wr import stats as _stats
    return _compare, _stats


def _rule_kn(trial_dir, rule_id):
    """k/n summed over valid repeats: answered/triggers, num/den, up/samples."""
    k = n = 0
    vals = []
    for path in sorted(glob.glob(os.path.join(trial_dir, "rep*", "verdicts.json"))):
        doc = _read_json(path, {}) or {}
        for row in doc.get("rules") or []:
            if row.get("id") != rule_id:
                continue
            if row.get("verdict") in ("PASS", "FAIL"):
                try:
                    rk, rn = int(row.get("k", 0) or 0), int(row.get("n", 0) or 0)
                except (TypeError, ValueError):
                    rk, rn = 0, 0
                k += rk
                n += rn
                vals.append(row.get("value"))
    return k, n, vals


# Metrics whose value is a k/n rate: Fisher's exact test applies. Count,
# rate and first show values and the difference with no test.
RATEish_KINDS = {"ratio", "latency", "latency-share", "uptime", "uptime-share"}


def _metric_kind(run, base, trial_id, rule_id):
    for root in (run, base):
        doc = _read_json(os.path.join(root, trial_id, "trial.json"), {}) or {}
        for row in doc.get("verdicts") or []:
            if row.get("id") == rule_id:
                return None
    return None


def compare(run, base):
    """Match rows by (trial id, rule id).

    The card row carries the aggregate across repeats (k/n summed over
    valid repeats, and the per-repeat values in detail) for ratio,
    latency-share and uptime-share kinds; Fisher's p applies to those
    only. Count/rate/first rows show values and the difference, no test.
    """
    _compare, _stats = _wr()
    run_card = _read_json(os.path.join(run, "card.json"), {}) or {}
    base_card = _read_json(os.path.join(base, "card.json"), {}) or {}
    run_rows = {(r.get("trial"), r.get("rule")): r for r in (run_card.get("rows") or [])}
    base_rows = {(r.get("trial"), r.get("rule")): r for r in (base_card.get("rows") or [])}
    out = []
    for key in sorted(set(run_rows) | set(base_rows)):
        r, b = run_rows.get(key), base_rows.get(key)
        if r is None or b is None:
            out.append({"trial": key[0], "rule": key[1], "before": b and b.get("verdict"),
                        "after": r and r.get("verdict"), "note": "not run"})
            continue
        kind = (r.get("metric") or b.get("metric") or "")
        entry = {"trial": key[0], "rule": key[1], "before": b.get("verdict"),
                 "after": r.get("verdict"), "value_before": b.get("value"),
                 "value_after": r.get("value"),
                 "no_match_before": bool(b.get("no_match")),
                 "no_match_after": bool(r.get("no_match")),
                 "values_before": b.get("values") or [],
                 "values_after": r.get("values") or []}
        rk, rn, _ = _rule_kn(os.path.join(run, key[0]), key[1])
        bk, bn, _ = _rule_kn(os.path.join(base, key[0]), key[1])
        entry["k_n_before"] = "%d/%d" % (bk, bn)
        entry["k_n_after"] = "%d/%d" % (rk, rn)
        entry["k_before"], entry["n_before"] = bk, bn
        entry["k_after"], entry["n_after"] = rk, rn
        if kind in RATEish_KINDS:
            entry["p"] = _stats.fisher_two_sided(rk, rn - rk, bk, bn - bk) if (rn or bn) else 1.0
            entry["note"] = _compare.note_for(entry["p"])
        else:
            entry["p"] = None
            vb, va = b.get("value"), r.get("value")
            try:
                entry["note"] = "diff %+.3g" % (float(va) - float(vb))
            except (TypeError, ValueError):
                entry["note"] = "values differ" if vb != va else "same"
        out.append(entry)
    return out


def _fmt_p(p):
    if p is None:
        return "-"
    try:
        return "%.4f" % float(p)
    except (TypeError, ValueError):
        return "-"


def render_compare(rows):
    lines = ["| trial | rule | before | after | value before → after | p | note |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("note") == "not run":
            lines.append("| %s | %s | %s | %s | - | - | not run |" % (
                r["trial"], r["rule"], r.get("before") or "-", r.get("after") or "-"))
            continue
        lines.append("| %s | %s | %s | %s | %s → %s | %s | %s |" % (
            r["trial"], r["rule"], r.get("before"), r.get("after"),
            _fmt_value(r.get("value_before")), _fmt_value(r.get("value_after")),
            _fmt_p(r.get("p")), r.get("note")))
    return "\n".join(lines) + "\n"


def _reason_of(det):
    """Why a repeat is INCONCLUSIVE / INVALID: the recorded reason, else the
    guard text, else the first evidence line."""
    return (det.get("invalid_reason") or det.get("reason") or det.get("guard")
            or (det.get("evidence") or [""])[0] or "")


def pr_block(before, after, issue=None):
    """The exact block for issue-fix PRs."""
    b = _read_json(os.path.join(before, "card.json"), {}) or {}
    a = _read_json(os.path.join(after, "card.json"), {}) or {}
    Brow, Arow = (b.get("run") or {}), (a.get("run") or {})
    title = "### %s: before/after" % TOOL_NAME
    if issue is not None:
        title += ", issue #%s" % issue
    lines = [title,
             "Before: %s %s (%s) · After: %s %s (%s)" % (
                 Brow.get("label") or "?", Brow.get("tb_version") or "?",
                 os.path.basename(before.rstrip("/")),
                 Arow.get("label") or "?", Arow.get("tb_version") or "?",
                 os.path.basename(after.rstrip("/"))),
             "",
             "| trial | rule | before | after | value before → after |",
             "|---|---|---|---|---|"]
    for row in compare(after, before):
        lines.append("| %s | %s | %s | %s | %s → %s |" % (
            row["trial"], row["rule"], row.get("before") or "-",
            row.get("after") or "-",
            _fmt_value(row.get("value_before"), row.get("no_match_before", False)),
            _fmt_value(row.get("value_after"), row.get("no_match_after", False))))
    lines += ["<details><summary>Evidence</summary>", ""]
    arows = {(r.get("trial"), r.get("rule")): r for r in (a.get("rows") or [])}
    brows = {(r.get("trial"), r.get("rule")): r for r in (b.get("rows") or [])}
    for key in sorted(set(arows) | set(brows)):
        # "before:" shows the BEFORE run's evidence, "after:" the AFTER run's.
        bdet = (brows.get(key) or {}).get("detail") or []
        adet = (arows.get(key) or {}).get("detail") or []
        src = (arows.get(key) or brows.get(key) or {}).get("source") or "no source"
        lines.append("**%s** (%s)" % (key[1], src))
        for d in bdet[:2]:
            for ev in (d.get("evidence") or [])[:3]:
                lines.append("- before: %s" % ev)
        for d in adet[:2]:
            for ev in (d.get("evidence") or [])[:3]:
                lines.append("- after: %s" % ev)
        # Every INCONCLUSIVE and every INVALID prints its real reason (the
        # guard text such as "n 3 < min_n 5" / "threshold unknown").
        for tag, det in (("before", bdet), ("after", adet)):
            for d in det:
                if d.get("verdict") in ("INCONCLUSIVE", "INVALID"):
                    reason = _reason_of(d)
                    if reason:
                        lines.append("- %s %s: %s (%s)" % (
                            tag, d.get("verdict"), reason, d.get("rep") or "?"))
    setup_bits = []
    for path in sorted(glob.glob(os.path.join(after, "*", "rep*", "setup.json"))):
        doc = _read_json(path, {}) or {}
        for check in doc.get("checks") or []:
            if check.get("ok") and check.get("id") in ("player_stance", "player_talents", "bot_spec"):
                setup_bits.append("%s=%s" % (check.get("id"), check.get("value")))
                break
    lines += ["Setup proof (after): %s" % (" · ".join(setup_bits[:6]) or "n/a"),
              "</details>"]
    return "\n".join(lines) + "\n"


def card_md(run_dir):
    return open(os.path.join(run_dir, "card.md")).read()


def card_json(run_dir):
    return _read_json(os.path.join(run_dir, "card.json"), {}) or {}
