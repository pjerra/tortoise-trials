"""Verdicts per rule/repeat/trial, and per-run scoring.

score_rep(rep_dir, trial): setup.json ok false -> every rule INVALID with
invalid_reason; scored time < min_valid_s -> INVALID "cut short (<n> s,
ended: <reason>)"; else each rule -> PASS/FAIL/INCONCLUSIVE.
score_trial(trial_dir, trial): majority of valid non-INCONCLUSIVE repeats,
tie -> FAIL; zero -> INCONCLUSIVE or INVALID (INVALID wins when any repeat
was INVALID and none scored). score_run(run_dir) scores all trials, writes
card.md and card.json in the run dir.
"""

import glob
import json
import os
import sys

try:
    from . import events as _events
except ImportError:  # pragma: no cover
    from trl import events as _events

try:
    from . import rules as _rules
except ImportError:  # pragma: no cover
    from trl import rules as _rules

try:
    from . import scenario as _scenario
except ImportError:  # pragma: no cover
    from trl import scenario as _scenario


def _read_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


DEFAULT_THRESHOLDS = {"critical": 20, "low": 50, "medium": 70}


def _num_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _config_read_failed(setup):
    """True only when the run proves config_read FAILED (a check with ok false)."""
    for check in (setup or {}).get("checks") or []:
        if check.get("id") == "config_read" and not check.get("ok"):
            return True
    return False


def _thresholds(run_dir, setup=None):
    """(thresholds, assumed): setup.json config first, then RUN.json, then the
    defaults 20/50/70 only when config_read FAILED; otherwise unknown (None)."""
    setup = setup or {}
    cfg = setup.get("config") if isinstance(setup.get("config"), dict) else {}
    run = _read_json(os.path.join(run_dir, "RUN.json"), {}) or {}
    failed = _config_read_failed(setup)
    out = {}
    assumed = False
    for name in ("critical", "low", "medium"):
        value = _num_or_none(cfg.get(name))
        if value is None:
            value = _num_or_none(run.get(name + "_health"))
        if value is None and failed:
            value = float(DEFAULT_THRESHOLDS[name])
            assumed = True
        out[name] = value
    return out, assumed


def _merge(excluded):
    spans = sorted((float(a), float(b)) for (a, b) in (excluded or [])
                   if b is not None and a is not None and float(b) > float(a))
    out = []
    for (a, b) in spans:
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _scored_seconds(rows, samples, excluded, window=None):
    # The scored time is the fight_start..fight_end window minus the merged
    # excluded spans; it drives the cut-short check and the rate denominator.
    if window:
        lo, hi = window
        cut = 0.0
        for (a, b) in _merge(excluded):
            cut += max(0.0, min(hi, b) - max(lo, a))
        return max(0.0, (hi - lo) - cut)
    lo, hi = None, None
    for r in rows or []:
        if r.get("kind") == "excluded":
            continue
        try:
            t = float(r.get("t", 0.0))
        except (TypeError, ValueError):
            continue
        lo = t if lo is None else min(lo, t)
        hi = t if hi is None else max(hi, t)
    for s in samples or []:
        try:
            t = float(s.get("t", 0.0))
        except (TypeError, ValueError):
            continue
        lo = t if lo is None else min(lo, t)
        hi = t if hi is None else max(hi, t)
    if lo is None or hi is None:
        return 0.0
    cut = 0.0
    for (a, b) in _merge(excluded):
        cut += max(0.0, min(hi, b) - max(lo, a))
    return max(0.0, (hi - lo) - cut)


def _mark_time(mark, base_wall):
    """A mark's time on the normaliser's base (seconds since fight_start, or
    setup_ok when there was no pull): its ISO wall minus the base wall, else
    its plain numeric `t`."""
    if not mark:
        return None
    wall = _events._wall_secs(mark.get("wall"))
    if wall is not None and base_wall is not None:
        return wall - base_wall
    try:
        return float(mark.get("t"))
    except (TypeError, ValueError):
        return None


def _fight_window(marks, samples):
    """(lo, hi, fight_end_missing) of the scored window, or (None, None, ...).

    lo = fight_start (the base itself, so 0), hi = fight_end from the marks.
    With no usable fight_end the last in-fight sample ends the window and the
    caller flags "fight_end missing" in the data quality.
    """
    fs = so = fe = None
    for mark in marks or []:
        kind = mark.get("kind")
        if kind == "fight_start" and fs is None:
            fs = mark
        elif kind == "setup_ok" and so is None:
            so = mark
        elif kind == "fight_end":
            fe = mark
    base = _events._wall_secs((fs or {}).get("wall"))
    if base is None:
        base = _events._wall_secs((so or {}).get("wall"))
    times = []
    for sample in samples or []:
        try:
            times.append((float(sample.get("t", 0.0)), sample))
        except (TypeError, ValueError):
            continue
    lo = _mark_time(fs, base) if fs else None
    if lo is None:
        lo = 0.0 if (fs or so) else (min(t for t, _ in times) if times else None)
    hi = _mark_time(fe, base) if fe else None
    missing = hi is None
    if missing:
        fight = [t for t, sample in times if sample.get("in_combat") and (lo is None or t >= lo)]
        later = [t for t, _ in times if lo is None or t >= lo]
        hi = max(fight) if fight else (max(later) if later else None)
    if lo is None or hi is None:
        return None, None, missing
    return lo, max(lo, hi), missing


def _excluded_spans(rows, marks=None):
    """ONE source: the normaliser's kind: excluded rows (merged)."""
    spans = []
    for row in rows or []:
        if row.get("kind") != "excluded":
            continue
        try:
            a, b = float(row.get("from")), float(row.get("to"))
        except (TypeError, ValueError):
            continue
        if b > a:
            spans.append((a, b))
    return _merge(spans)


def _ctx(run_dir, trial, setup, window=None):
    thresholds, assumed = _thresholds(run_dir, setup)
    ctx = {"guids": setup.get("guids") or {}, "thresholds": thresholds,
           "thresholds_assumed": assumed, "trial": trial}
    if window:
        ctx["window"] = window
    return ctx


def _load_rep_rows(rep_dir, trial):
    setup = _read_json(os.path.join(rep_dir, "setup.json"), {}) or {}
    if not os.path.exists(os.path.join(rep_dir, "setup.json")):
        return None, None, None, None, None, True
    samples = _read_jsonl(os.path.join(rep_dir, "samples.jsonl"))
    marks = _read_jsonl(os.path.join(rep_dir, "marks.jsonl"))
    rows = _read_jsonl(os.path.join(rep_dir, "events.jsonl"))
    raw_path = os.path.join(rep_dir, "raw-ta.jsonl")
    if ((not rows or not any(isinstance(r, dict) and r.get("seq") is not None for r in rows))
            and os.path.exists(raw_path)):
        rows = _events.normalise(rep_dir, trial)
        samples = _read_jsonl(os.path.join(rep_dir, "samples.jsonl"))
        marks = _read_jsonl(os.path.join(rep_dir, "marks.jsonl"))
    # The victim table needs the raw agent packets (attack_start/stop), which the
    # normaliser drops; build it here once per rep the same way samples_by_guid is built.
    raw = _read_jsonl(os.path.join(rep_dir, "raw-ta.jsonl"))
    victims = _events.attack_victims(raw, marks, samples) if raw else {}
    return setup, samples, marks, rows, victims, False


def cut_short_reason(marks, scored):
    """'cut short (<n> s, ended: <reason>)'; a floor end says how many rescues it took.

    The governor ends a repeat as "floor" after max_rescues_per_min GM rescues (the `gm_heal`
    marks) within 60 s.
    """
    ended = ""
    for mark in marks:
        if mark.get("kind") == "fight_end" and mark.get("ended"):
            ended = str(mark.get("ended"))
    if ended == "floor":
        return "cut short (%.0f s, ended: floor after %d rescues)" % (
            scored, sum(1 for mark in marks if mark.get("kind") == "gm_heal"))
    return "cut short (%.0f s, ended: %s)" % (scored, ended or "unknown")


def score_rep(rep_dir, trial, prior_events=None):
    setup, samples, marks, rows, victims, missing = _load_rep_rows(rep_dir, trial)
    if missing:
        # A rep dir without setup.json is INVALID with a reason (not dropped).
        reason = "rep without setup.json"
        out = {"trial": trial.get("id"), "rep": os.path.basename(rep_dir.rstrip("/")),
               "ok_setup": False, "invalid_reason": reason, "rules": []}
        for rule in trial.get("rules") or []:
            out["rules"].append({"id": rule.get("id"), "verdict": "INVALID",
                                 "invalid_reason": reason, "value": None,
                                 "evidence": [], "guard": ""})
        _write(os.path.join(rep_dir, "verdicts.json"), out)
        return out
    # run dir is two levels up from rep dir (<run>/<trial>/repN)
    run_dir = os.path.dirname(os.path.dirname(os.path.abspath(rep_dir)))
    lo, hi, end_missing = _fight_window(marks, samples)
    window = (lo, hi) if lo is not None else None
    ctx = _ctx(run_dir, trial, setup, window)
    ctx["victims_by_guid"] = victims or {}
    base_wall = _events._time_base(marks, samples)
    excluded = _excluded_spans(rows, marks)
    out = {"trial": trial.get("id"), "rep": os.path.basename(rep_dir.rstrip("/")),
           "ok_setup": bool(setup.get("ok")), "rules": [],
           "thresholds": ctx["thresholds"],
           "thresholds_assumed": bool(ctx["thresholds_assumed"]),
           "fight_end_missing": bool(end_missing)}
    if not setup.get("ok"):
        reason = setup.get("invalid_reason") or "setup not proven"
        for rule in trial.get("rules") or []:
            out["rules"].append({"id": rule.get("id"), "verdict": "INVALID",
                                 "invalid_reason": reason, "value": None,
                                 "evidence": [], "guard": ""})
        out["invalid_reason"] = reason
        _write(os.path.join(rep_dir, "verdicts.json"), out)
        return out
    scored = _scored_seconds(rows, samples, excluded, window)
    out["scored_s"] = round(scored, 1)
    if window:
        out["window"] = [round(window[0], 2), round(window[1], 2)]
        out["excluded_s"] = round(sum(max(0.0, min(window[1], b) - max(window[0], a))
                                      for (a, b) in excluded), 1)
    else:
        out["excluded_s"] = round(sum(b - a for (a, b) in excluded), 1)
    win_samples = _rules.window_samples(samples, window) if window else samples
    min_valid = float(trial.get("min_valid_s") or 0)
    if scored < min_valid:
        reason = cut_short_reason(marks, scored)
        for rule in trial.get("rules") or []:
            out["rules"].append({"id": rule.get("id"), "verdict": "INVALID",
                                 "invalid_reason": reason, "value": None,
                                 "evidence": [], "guard": ""})
        out["invalid_reason"] = reason
        _write(os.path.join(rep_dir, "verdicts.json"), out)
        return out
    for rule in trial.get("rules") or []:
        # a rule that carries the responder's cooldown across reps gets the earlier responses as excluded
        # spans on this rep's base: a trigger inside one is no chance, exactly like an in-rep cooldown
        rule_rows = rows
        spans = _rules.prior_response_spans(rule, prior_events, base_wall, ctx)
        if spans:
            rule_rows = list(rows) + [{"kind": "excluded", "from": round(a, 3), "to": round(b, 3),
                                       "why": "prior rep responder cooldown", "t": round(a, 3)}
                                      for (a, b) in spans]
        res = _rules.evaluate(rule, rule_rows, samples, marks, ctx)
        min_opp = rule.get("min_opportunity_s") or 0
        min_n = rule.get("min_n") or rule.get("check", {}).get("min_n", 0) or 0
        guard_hold = True
        guard_why = res.get("verdict")
        if min_opp and res.get("verdict") in ("PASS", "FAIL"):
            # the rule's own not_within_s_after_mark windows carry no chance either: the events inside them
            # are dropped from the count, so the opportunity the guard reads must drop the same seconds
            opp = _rules.opportunity_seconds(trial, _rules.after_mark_samples(win_samples, rule, marks),
                                             excluded, ctx)
            if opp < float(min_opp):
                guard_hold = False
                guard_why = "opportunity %.0f s < %.0f s" % (opp, float(min_opp))
        # Fewer opportunities than min_n -> INCONCLUSIVE (k/n metrics).
        try:
            n_have = int(res.get("n", 0) or 0)
        except (TypeError, ValueError):
            n_have = 0
        if min_n and res.get("verdict") in ("PASS", "FAIL") and n_have < int(min_n):
            guard_hold = False
            guard_why = "n %d < min_n %d" % (n_have, int(min_n))
        entry = {"id": rule.get("id"), "verdict": res.get("verdict"),
                 "value": res.get("value"), "k": res.get("k", 0), "n": res.get("n", 0),
                 "evidence": res.get("evidence") or [],
                 "guard": res.get("guard") or "",
                 "no_match": bool(res.get("no_match"))}
        if res.get("verdict") == "INCONCLUSIVE":
            # The real reason an INCONCLUSIVE is printed in the PR block.
            entry["reason"] = res.get("reason") or (res.get("evidence") or [""])[0]
        if guard_hold is False:
            entry["verdict"] = "INCONCLUSIVE"
            entry["reason"] = guard_why
            entry["guard"] = (entry["guard"] + "; " if entry["guard"] else "") + guard_why
        out["rules"].append(entry)
    _write(os.path.join(rep_dir, "verdicts.json"), out)
    return out


def _write(path, doc):
    # allow_nan=False: a NaN/inf anywhere fails loudly instead of writing
    # the non-standard Infinity/NaN tokens that strict JSON parsers reject.
    with open(path, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True, allow_nan=False)


def _prior_events(rep_dir):
    """[(absolute wall, row)] of one scored repeat, for the next repeat's cross-rep debounce.

    The wall is the repeat's own fight_start (or setup_ok) wall plus the row's re-based `t`: both come
    from the runner's steady clock, so the difference between two repeats is real seconds.
    """
    marks = _read_jsonl(os.path.join(rep_dir, "marks.jsonl"))
    base = _events._time_base(marks, [])
    if not base:
        return []
    out = []
    for row in _read_jsonl(os.path.join(rep_dir, "events.jsonl")):
        try:
            out.append((base + float(row.get("t") or 0.0), row))
        except (TypeError, ValueError):
            continue
    return out


def score_trial(trial_dir, trial):
    reps = sorted(glob.glob(os.path.join(trial_dir, "rep*")))
    carries = any(((r.get("check") or {}).get("debounce_responder_across_reps")
                   for r in trial.get("rules") or []))
    prior = []
    scored = []
    for rep in reps:
        if not os.path.isdir(rep):
            continue
        if os.path.exists(os.path.join(rep, "setup.json")):
            scored.append(score_rep(rep, trial, prior_events=prior))
        elif os.path.exists(os.path.join(rep, "verdicts.json")):
            # Pre-scored (hand-written test reps): read as-is.
            scored.append(_read_json(os.path.join(rep, "verdicts.json"), {}) or {})
        else:
            scored.append(score_rep(rep, trial, prior_events=prior))
        if carries:
            prior = prior + _prior_events(rep)
    rules = {}
    for rep in scored:
        for row in rep.get("rules") or []:
            rules.setdefault(row.get("id"), []).append((rep.get("rep"), row))
    out_rules = []
    for rule in trial.get("rules") or []:
        rid = rule.get("id")
        votes = rules.get(rid, [])
        valid = [(rep, row) for (rep, row) in votes if row.get("verdict") in ("PASS", "FAIL")]
        if not valid:
            # Zero valid repeats: INCONCLUSIVE unless a repeat was INVALID
            # and none scored (all-invalid -> INVALID).
            any_invalid = any(row.get("verdict") == "INVALID" for (_, row) in votes)
            any_scored = any(row.get("verdict") in ("PASS", "FAIL", "INCONCLUSIVE")
                             for (_, row) in votes)
            verdict = "INVALID" if (any_invalid and not any_scored) else "INCONCLUSIVE"
            k_sum, n_sum = 0, 0
            reason = ""
        else:
            npass = sum(1 for (_, row) in valid if row.get("verdict") == "PASS")
            nfail = len(valid) - npass
            verdict = "PASS" if npass > nfail else "FAIL"
            # Compare aggregates k/n summed over valid repeats (not rep1 only).
            k_sum = sum(int(row.get("k", 0) or 0) for (_, row) in valid)
            n_sum = sum(int(row.get("n", 0) or 0) for (_, row) in valid)
            min_reps = rule.get("min_reps") or 0
            try:
                min_reps = int(min_reps)
            except (TypeError, ValueError):
                min_reps = 0
            reason = ""
            if min_reps > 1 and len(valid) < min_reps:
                verdict = "INCONCLUSIVE"
                reason = "valid reps %d < min_reps %d" % (len(valid), min_reps)
        detail = [{"rep": rep, "verdict": row.get("verdict"), "value": row.get("value"),
                   "k": row.get("k", 0), "n": row.get("n", 0),
                   "evidence": row.get("evidence") or [], "guard": row.get("guard") or "",
                   "reason": row.get("reason") or "",
                   "no_match": bool(row.get("no_match")),
                   "invalid_reason": row.get("invalid_reason") or ""} for (rep, row) in votes]
        out_rules.append({"id": rid, "verdict": verdict,
                          "k": k_sum if valid else sum(1 for (_, row) in valid if row.get("verdict") == verdict),
                          "n": n_sum if valid else len(valid), "detail": detail,
                          "expected": rule.get("expect_today"), "metric": (rule.get("check") or {}).get("metric"),
                          "pass_if": rule.get("pass_if"), "source": rule.get("source"),
                          "desc": rule.get("desc"),
                          "reason": reason if valid and reason else ""})
    out = {"trial": trial.get("id"), "verdicts": out_rules}
    _write(os.path.join(trial_dir, "trial.json"), out)
    return out


def score_run(run_dir):
    try:
        from . import report as _report
    except ImportError:  # pragma: no cover
        from trl import report as _report
    run_trials = []
    run_doc = _read_json(os.path.join(run_dir, "RUN.json"), {}) or {}
    for tid in run_doc.get("trials") or []:
        tdir = os.path.join(run_dir, tid)
        # The run froze the effective trial (merged for a `<trial>@<variant>`) before it ran: score that, never
        # today's definition. Only a run made before snapshots existed (RUN.json has no `trial_snapshots`) falls back
        # to the lookup, loudly; a snapshot-era run with a missing or unreadable snapshot is an error.
        snap = os.path.join(tdir, _scenario.SNAPSHOT_NAME)
        trial = None
        if os.path.isfile(snap):
            try:
                with open(snap) as f:
                    trial = json.load(f)
            except (OSError, ValueError) as exc:
                raise ValueError("%s: unreadable trial snapshot %s: %s" % (run_dir, snap, exc))
            if not isinstance(trial, dict) or trial.get("id") != tid:
                raise ValueError("%s: trial snapshot %s does not hold trial %r" % (run_dir, snap, tid))
        elif run_doc.get("trial_snapshots"):
            if os.path.isdir(tdir):
                raise ValueError("%s: no %s for trial %r although the run recorded snapshots; refusing to score it "
                                 "against today's trial files" % (run_dir, _scenario.SNAPSHOT_NAME, tid))
            continue
        else:
            print("score: %s has no trial snapshots (a run from before they existed): scoring trial %r against the "
                  "CURRENT trial files" % (run_dir, tid), file=sys.stderr)
            trial = _scenario.find_trial_by_id(tid)
        if trial is None:
            for path in glob.glob(os.path.join(_scenario._tool_dir, "trials", "_fixtures", "*.json")):
                try:
                    cand = _scenario.load_trial(path)
                except (OSError, ValueError):
                    continue
                if cand.get("id") == tid:
                    trial = cand
                    break
        if trial is None or not os.path.isdir(tdir):
            continue
        run_trials.append(score_trial(tdir, trial))
    _report.write_card(run_dir, run_trials)
    return run_trials
