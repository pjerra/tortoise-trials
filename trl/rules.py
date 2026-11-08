"""The check DSL engine: evaluate(rule, rows, samples, marks, ctx).

No eval, closed set. Each metric returns a number plus the matched events as
evidence. ctx selects `min_*` threshold aliases and carries `walk_report`
(a {metric: {k, n}} dict mapped by brief 4 into ctx for walk_metric rules).
"""

try:
    from . import spells as _spells
except ImportError:  # pragma: no cover - direct execution
    from trl import spells as _spells

DEFAULT_CC_AURAS = {118, 6770, 14325, 28272, 28271, 9484, 9035, 2094, 2108,
                    17826, 18651, 10955, 10956}


def _num(value, ctx, default=None):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        v = value.strip()
        if v in ("$critical", "$low", "$medium") and isinstance(ctx, dict):
            got = (ctx.get("thresholds") or {}).get(v[1:])
            if got is not None:
                try:
                    return float(got)
                except (TypeError, ValueError):
                    pass
            return default
        try:
            return float(v)
        except ValueError:
            return default
    return default
_EXTRA_NAMES = ("$extra1", "$extra2", "$extra3")


def _extras(guids):
    got = (guids or {}).get("extras")
    return list(got) if isinstance(got, (list, tuple)) else []


def _alias_map(guids):
    """Single-guid unit aliases -> guid ($extraN follow guids["extras"] order)."""
    guids = guids or {}
    out = {"$player": guids.get("player"), "$bot": guids.get("bot"),
           "$pet": guids.get("pet"), "$marked": guids.get("marked")}
    extras = _extras(guids)
    for i, name in enumerate(_EXTRA_NAMES):
        out[name] = extras[i] if i < len(extras) else None
    return out


def _unit_ids(unit, ctx):
    """Resolve a where unit alias to a set of guids ('*' = any)."""
    guids = (ctx.get("guids") or {}) if isinstance(ctx, dict) else {}
    if unit == "$player":
        return {str(guids.get("player") or "").lower()}
    if unit == "$bot":
        return {str(guids.get("bot") or "").lower()}
    if unit == "$pet":
        return {str(guids.get("pet") or "").lower()}
    if unit == "$marked":
        return {str(guids.get("marked") or "").lower()}
    if unit == "$extras":
        return {str(g or "").lower() for g in _extras(guids) if g}
    if unit in _EXTRA_NAMES:
        return {str(_alias_map(guids).get(unit) or "").lower()}
    if unit == "$mob":
        return {"$mob"}
    if isinstance(unit, str) and unit.startswith("mob:"):
        return {unit[4:].lower()}
    if isinstance(unit, str):
        return {unit.lower()}
    return set()

def _spell_ids(where):
    ids = set()
    if "spell" in where and where["spell"] is not None:
        names = where["spell"] if isinstance(where["spell"], list) else [where["spell"]]
        for name in names:
            ids |= _spells.ids_for(name)
    if "spell_id" in where and where["spell_id"] is not None:
        raw = where["spell_id"] if isinstance(where["spell_id"], list) else [where["spell_id"]]
        for i in raw:
            try:
                ids.add(int(i))
            except (TypeError, ValueError):
                continue
    return ids


def _sample_rows(samples):
    by_guid = {}
    for sample in samples or []:
        guid = str(sample.get("guid") or "").lower()
        try:
            t = float(sample.get("t", 0.0))
        except (TypeError, ValueError):
            continue
        by_guid.setdefault(guid, []).append((t, sample))
    for rows in by_guid.values():
        rows.sort(key=lambda pair: pair[0])
    return by_guid


def _at(samples_by_guid, guid, t):
    rows = samples_by_guid.get(str(guid or "").lower()) or []
    best = None
    for st, sample in rows:
        if st <= t and t - st <= 1.0:
            best = sample
        elif st > t:
            break
    return best


def _unit_hp(samples_by_guid, guid, t):
    sample = _at(samples_by_guid, guid, t)
    if not sample:
        return None
    try:
        hp = float(sample.get("hp"))
        max_hp = float(sample.get("max_hp"))
    except (TypeError, ValueError):
        return None
    if max_hp <= 0:
        return None
    return 100.0 * hp / max_hp


def _in_excluded(excluded, t):
    for (a, b) in excluded:
        if a <= t <= b:
            return True
    return False


def _matches(row, where, samples_by_guid, ctx, trial):
    for key in where:
        if key not in ("kind", "src", "dst", "spell", "spell_id",
                       "dst_hp_gt", "dst_hp_lt", "dst_hp_le", "in_combat",
                       "t_from", "t_to", "dst_has_aura", "dst_lacks_aura",
                       "src_power_lt", "response_dst", "same_dst",
                       "trigger_debounce_s", "src_target_of", "dst_target_of",
                       "src_within_of_marked", "marked_has_cc",
                       "spell_interruptible", "src_hp_le",
                       "num", "den", "trigger", "response", "within_s",
                       "aura", "unit", "phase", "name", "alive", "min_n",
                       "triggers"):
            return False
    kind = where.get("kind")
    if kind is not None and row.get("kind") != kind:
        return False
    for side, key in (("src", "src"), ("dst", "dst")):
        want = where.get(key)
        if want is None:
            continue
        wants = want if isinstance(want, list) else [want]
        # response_dst/same_dst match by GUID, never by display name: the
        # trigger plants a guid here, so resolve it directly.
        guid = str(row.get(side + "_guid") or "").lower()
        hit = False
        for w in wants:
            ids = _unit_ids(w, ctx)
            if "$mob" in ids and row.get(side + "_role") == "mob":
                hit = True
            elif guid and guid in ids:
                hit = True
        if not hit:
            return False
    spell_ids = _spell_ids(where)
    if spell_ids and (row.get("spell_id") not in spell_ids):
        return False
    if where.get("spell_interruptible"):
        # One shared definition (spells.is_interruptible): cast time > 0 AND
        # PreventionType == SILENCE AND the damage interrupt flag; build_data
        # filters its caster mobs with the same helper.
        if not _spells.spell_is_interruptible(row.get("spell_id")):
            return False
    for key, cmp in (("dst_hp_gt", lambda a, b: a > b),
                     ("dst_hp_lt", lambda a, b: a < b),
                     ("dst_hp_le", lambda a, b: a <= b)):
        if key in where and where[key] is not None:
            # Missing hp is treated as above critical (+inf): it passes gt
            # gates and fails lt/le gates — never silently "below".
            hp = row.get("dst_hp_pct")
            if hp is None:
                if key != "dst_hp_gt":
                    return False
            else:
                limit = _num(where[key], ctx)
                if limit is None or not cmp(hp, limit):
                    return False
    if "src_hp_le" in where and where["src_hp_le"] is not None:
        limit = _num(where["src_hp_le"], ctx)
        if limit is None:
            return False
        hp = _unit_hp(samples_by_guid, row.get("src_guid"), row.get("t", 0.0))
        if hp is None or not (hp <= limit):
            return False
    if "in_combat" in where and where["in_combat"] is not None:
        sample = _at(samples_by_guid, row.get("src_guid"), row.get("t", 0.0))
        want = bool(where["in_combat"])
        if sample is None or bool(sample.get("in_combat")) != want:
            return False
    if "t_from" in where and where["t_from"] is not None:
        if row.get("t", 0.0) < float(where["t_from"]):
            return False
    if "t_to" in where and where["t_to"] is not None:
        if row.get("t", 0.0) > float(where["t_to"]):
            return False
    for key, present in (("dst_has_aura", True), ("dst_lacks_aura", False)):
        if key in where and where[key] is not None:
            ids = where[key] if isinstance(where[key], list) else [where[key]]
            try:
                ids = {int(i) for i in ids}
            except (TypeError, ValueError):
                return False
            sample = _at(samples_by_guid, row.get("dst_guid"), row.get("t", 0.0))
            have = set()
            if sample:
                try:
                    have = {int(a) for a in (sample.get("auras") or [])}
                except (TypeError, ValueError):
                    have = set()
            if present and not (ids & have):
                return False
            if not present and (ids & have):
                return False
    if "src_power_lt" in where and where["src_power_lt"] is not None:
        sample = _at(samples_by_guid, row.get("src_guid"), row.get("t", 0.0))
        if sample is None:
            return False
        try:
            pct = 100.0 * float(sample.get("power")) / float(sample.get("max_power"))
        except (TypeError, ValueError, ZeroDivisionError):
            return False
        limit = _num(where["src_power_lt"], ctx)
        if limit is None or not pct < limit:
            return False
    for key in ("src_target_of", "dst_target_of"):
        if key in where and where[key] is not None:
            side = key.split("_")[0] + "_guid"
            sample = _at(samples_by_guid, row.get(side), row.get("t", 0.0))
            if sample is None:
                return False
            want = str(where[key]).lower()
            guids = (ctx.get("guids") or {}) if isinstance(ctx, dict) else {}
            alias = _alias_map(guids).get(want, want)
            if str(sample.get("target") or "").lower() != str(alias or "").lower():
                return False
    if "src_within_of_marked" in where and where["src_within_of_marked"] is not None:
        try:
            limit = float(where["src_within_of_marked"])
        except (TypeError, ValueError):
            return False
        guids = (ctx.get("guids") or {}) if isinstance(ctx, dict) else {}
        a = _at(samples_by_guid, row.get("src_guid"), row.get("t", 0.0))
        b = _at(samples_by_guid, guids.get("marked"), row.get("t", 0.0))
        if not a or not b:
            return False
        try:
            import math
            dist = math.sqrt((float(a.get("x")) - float(b.get("x"))) ** 2
                             + (float(a.get("y")) - float(b.get("y"))) ** 2
                             + (float(a.get("z")) - float(b.get("z"))) ** 2)
        except (TypeError, ValueError):
            return False
        if dist > limit:
            return False
    if where.get("marked_has_cc"):
        guids = (ctx.get("guids") or {}) if isinstance(ctx, dict) else {}
        sample = _at(samples_by_guid, guids.get("marked"), row.get("t", 0.0))
        if sample is None:
            return False
        try:
            have = {int(a) for a in (sample.get("auras") or [])}
        except (TypeError, ValueError):
            have = set()
        if not (have & DEFAULT_CC_AURAS):
            return False
    return True


def evidence_line(row):
    spell = row.get("spell") or row.get("spell_id") or "?"
    hp = row.get("dst_hp_pct")
    hp_s = "hp=?" if hp is None else "hp=%d%%" % round(hp)
    return "t=%.1f %s %s (%s) %s -> %s %s" % (
        row.get("t", 0.0), row.get("kind"), spell, row.get("spell_id"),
        row.get("src") or "?", row.get("dst") or "?", hp_s)


THRESHOLD_NAMES = ("critical", "low", "medium")


def _alias_names(rule, trial=None):
    """Threshold aliases ($critical/$low/$medium, as names) a rule reads.

    Looks at where/num/den/trigger/response and, when the rule has a
    min_opportunity_s, at the trial's opportunity block.
    """
    check = rule.get("check") or {}
    blobs = [check.get("where") or {}, check.get("num") or {},
             check.get("den") or {}, check.get("trigger") or {},
             check.get("response") or {}]
    if rule.get("min_opportunity_s") and isinstance(trial, dict):
        blobs.append(trial.get("opportunity") or {})
    found = []
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        for val in blob.values():
            vals = val if isinstance(val, list) else [val]
            for v in vals:
                if isinstance(v, str) and v.strip() in ("$critical", "$low", "$medium"):
                    name = v.strip()[1:]
                    if name not in found:
                        found.append(name)
    return found


def _uses_threshold_alias(rule, trial=None):
    """Only rules using $critical/$low/$medium get the 'thresholds assumed' guard."""
    return bool(_alias_names(rule, trial))


def unknown_thresholds(rule, ctx):
    """Alias names the rule reads whose threshold is unknown (None / not a number)."""
    thresholds = (ctx.get("thresholds") or {}) if isinstance(ctx, dict) else {}
    out = []
    for name in _alias_names(rule, (ctx or {}).get("trial")):
        try:
            float(thresholds.get(name))
        except (TypeError, ValueError):
            out.append(name)
    return out


def _pass(pass_if, value):
    import re
    m = re.match(r"^(==|!=|<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)$", str(pass_if).strip())
    if not m or value is None:
        return False
    if isinstance(value, float) and value == float("inf"):
        return m.group(1) in ("!=", ">", ">=")
    if isinstance(value, float) and value == float("-inf"):
        return m.group(1) in ("!=", "<", "<=")
    op, num = m.group(1), float(m.group(2))
    ops = {"==": value == num, "!=": value != num, "<=": value <= num,
           ">=": value >= num, "<": value < num, ">": value > num}
    return bool(ops[op])


def _excluded_from(rows):
    """The normaliser's kind: excluded rows are the ONE source of spans.

    Merged here too (overlapping spans subtracted once); score.py merges
    the same way so the cut-short check and the rate denominator agree.
    """
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
    spans.sort()
    out = []
    for (a, b) in spans:
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _triggers(check, samples, ctx):
    trig = check.get("trigger") or {}
    if trig.get("kind") == "cross":
        unit = trig.get("unit")
        below = _num(trig.get("below"), ctx)
        if below is None:
            # A bad threshold alias never crashes evaluation: no triggers.
            return []
        guids = (ctx.get("guids") or {}) if isinstance(ctx, dict) else {}
        alias = _alias_map(guids).get(unit, unit)
        rows = sorted([s for s in (samples or [])
                       if str(s.get("guid") or "").lower() == str(alias or "").lower()],
                      key=lambda s: float(s.get("t", 0.0)))
        out = []
        prev = None
        for sample in rows:
            try:
                hp = float(sample.get("hp"))
                max_hp = float(sample.get("max_hp"))
                pct = 100.0 * hp / max_hp if max_hp > 0 else None
            except (TypeError, ValueError, ZeroDivisionError):
                pct = None
            if pct is None:
                prev = None
                continue
            try:
                t = float(sample.get("t", 0.0))
            except (TypeError, ValueError):
                prev = pct
                continue
            if prev is not None and pct < below <= prev:
                out.append({"t": t, "guid": alias, "unit": unit,
                            "src_guid": str(alias or "").lower(),
                            "dst_guid": str(alias or "").lower()})
            prev = pct
        return out
    # Event trigger: its kind FILTERS the rows (never dropped), together with
    # every where-style field (src, dst, spell, spell_interruptible, ...).
    where = {k: v for k, v in trig.items() if k not in ("unit", "below")}
    samples_by_guid = _sample_rows(ctx.get("samples_all", samples))
    return [r for r in (ctx.get("rows") or []) if _matches(r, where, samples_by_guid, ctx, ctx.get("trial") or {})]


def evaluate(rule, rows, samples, marks, ctx):
    """Return {value, verdict(PASS/FAIL/INCONCLUSIVE), evidence, guard}."""
    ctx = dict(ctx or {})
    trial = ctx.get("trial") or {}
    # The scored window is fight_start..fight_end: only rows and samples
    # inside it count (excluded rows stay: they carry the spans). Hp / aura /
    # combat lookups still read ALL samples so the window's first events have
    # their data.
    window = ctx.get("window")
    all_samples = samples
    if window:
        rows = window_rows(rows, window)
        samples = window_samples(samples, window)
    ctx["rows"] = rows
    ctx["samples_all"] = all_samples
    samples_by_guid = _sample_rows(all_samples)
    excluded = _excluded_from(rows)
    check = rule.get("check") or {}
    metric = check.get("metric")
    where = check.get("where") or {}
    guard_note = ""
    uses_threshold_alias = _uses_threshold_alias(rule, trial)
    if uses_threshold_alias and ctx.get("thresholds_assumed"):
        guard_note = "thresholds assumed"
    if unknown_thresholds(rule, ctx):
        # A None threshold never reaches a comparison.
        return {"value": None, "k": 0, "n": 0, "verdict": "INCONCLUSIVE",
                "evidence": ["threshold unknown"], "reason": "threshold unknown",
                "guard": "threshold unknown"}

    def scored(evts):
        return [r for r in evts if not _in_excluded(excluded, r.get("t", 0.0))]

    if metric == "walk_metric":
        name = check.get("name")
        report = ctx.get("walk_report") or {}
        found = report.get(name)
        if found is None and isinstance(report.get("metrics"), dict):
            found = report["metrics"].get(name)
        n = 0
        if isinstance(found, dict):
            try:
                n = int(found.get("n", 0))
            except (TypeError, ValueError):
                n = 0
        if found is None or n == 0:
            return {"value": None, "n": 0, "verdict": "INCONCLUSIVE",
                    "evidence": ["metric not in report" if found is None else "walk n == 0"],
                    "guard": guard_note}
        if isinstance(found, dict) and "mean" in found:
            value = float(found["mean"])
        else:
            k = float(found.get("k", 0)) if isinstance(found, dict) else 0.0
            value = k / n
        ok = _pass(rule.get("pass_if"), value)
        return {"value": value, "n": n, "verdict": "PASS" if ok else "FAIL",
                "evidence": ["%s = %s" % (name, value)], "guard": guard_note}
    if metric == "count":
        hits = scored([r for r in rows if _matches(r, where, samples_by_guid, ctx, trial)])
        if str(rule.get("pass_if", "")).strip() == "== 0" and not hits:
            nowhere = {k: v for k, v in where.items() if k != "dst"}
            same = scored([r for r in rows if _matches(r, nowhere, samples_by_guid, ctx, trial)])
            opp = opportunity_seconds(trial, samples, excluded, ctx)
            ev = ["opportunity %.0f s, %d same-spell casts on other targets" % (opp, len(same))]
        else:
            ev = [evidence_line(r) for r in hits[:5]]
        return {"value": len(hits), "k": len(hits), "n": 1,
                "verdict": "PASS" if _pass(rule.get("pass_if"), len(hits)) else "FAIL",
                "evidence": ev, "guard": guard_note}

    if metric == "ratio":
        num = scored([r for r in rows if _matches(r, check.get("num") or {}, samples_by_guid, ctx, trial)])
        den = scored([r for r in rows if _matches(r, check.get("den") or {}, samples_by_guid, ctx, trial)])
        n = len(den)
        k = len(num)
        if not den:
            return {"value": None, "k": 0, "n": 0, "verdict": "INCONCLUSIVE",
                    "evidence": ["den 0"], "guard": guard_note}
        value = k / n
        return {"value": value, "k": k, "n": n,
                "verdict": "PASS" if _pass(rule.get("pass_if"), value) else "FAIL",
                "evidence": ["%d/%d" % (k, n)] + [evidence_line(r) for r in num[:5]],
                "guard": guard_note}

    if metric == "latency":
        triggers = _triggers(check, samples, ctx)
        resp_where = dict(check.get("response") or {})
        dst_mode = where.get("response_dst") or check.get("response_dst")
        # trigger_debounce_s is per src guid, accepted at check level too.
        debounce = check.get("trigger_debounce_s")
        if debounce is None:
            debounce = where.get("trigger_debounce_s")
        try:
            debounce = float(debounce) if debounce is not None else None
        except (TypeError, ValueError):
            debounce = None
        if debounce:
            kept, last_by_src = [], {}
            for trig in sorted(triggers, key=lambda r: r.get("t", 0.0)):
                src = (trig.get("src_guid") or trig.get("src")
                       or trig.get("guid") or trig.get("unit"))
                last = last_by_src.get(src)
                if last is None or trig.get("t", 0.0) - last >= debounce:
                    kept.append(trig)
                    last_by_src[src] = trig.get("t", 0.0)
            triggers = kept
        within = float(check.get("within_s") or 0.0)
        answered = 0
        counted = 0
        ev = []
        for trig in sorted(triggers, key=lambda r: r.get("t", 0.0)):
            t0 = trig.get("t", 0.0)
            # A trigger inside an excluded span, or whose response window
            # crosses one, carries no opportunity: skip it entirely.
            if _in_excluded(excluded, t0) or any(a <= t0 + within and t0 <= b for (a, b) in excluded):
                continue
            counted += 1
            rw = dict(resp_where)
            trig_guid = trig.get("src_guid") or trig.get("dst_guid") or trig.get("guid")
            if dst_mode == "$trigger.src":
                rw["dst"] = trig.get("src_guid") or trig.get("src") or trig.get("guid") or trig.get("unit")
            elif dst_mode == "$trigger.dst":
                rw["dst"] = trig.get("dst_guid") or trig.get("dst") or trig.get("guid")
            if check.get("same_dst"):
                rw["dst"] = trig.get("dst_guid") or trig.get("dst") or trig.get("guid") or trig.get("unit")
            hit = None
            for cand in sorted(rows, key=lambda r: r.get("t", 0.0)):
                if not (t0 <= cand.get("t", 0.0) <= t0 + within):
                    continue
                if _in_excluded(excluded, cand.get("t", 0.0)):
                    continue
                if _matches(cand, rw, samples_by_guid, ctx, trial):
                    hit = cand
                    break
            if hit is not None:
                answered += 1
                if len(ev) < 5:
                    ev.append("t=%.1f answered t=%.1f" % (t0, hit.get("t", 0.0)))
        if not counted:
            return {"value": None, "k": 0, "n": 0, "verdict": "INCONCLUSIVE",
                    "evidence": ["no triggers"], "guard": guard_note}
        value = answered / counted
        return {"value": value, "k": answered, "n": counted,
                "verdict": "PASS" if _pass(rule.get("pass_if"), value) else "FAIL",
                "evidence": ["%d/%d" % (answered, counted)] + ev, "guard": guard_note}

    if metric == "uptime":
        unit = check.get("unit")
        guids = ctx.get("guids") or {}
        alias = _alias_map(guids).get(unit, unit)
        phase = check.get("phase", "in_combat")
        aura = check.get("aura")
        if aura is not None and not isinstance(aura, list):
            aura = [aura]
        try:
            aura = {int(a) for a in (aura or [])}
        except (TypeError, ValueError):
            aura = set()
        alive_only = check.get("alive")
        if alive_only:
            # Pet "alive" uptime: missing samples (pet gone) count as NOT
            # alive, and the denominator is ALL fight samples (not only
            # in-combat samples where the pet is present).
            times = set()
            for s in samples or []:
                try:
                    t = round(float(s.get("t", 0.0)), 2)
                except (TypeError, ValueError):
                    continue
                try:
                    if _in_excluded(excluded, float(s.get("t", 0.0))):
                        continue
                except (TypeError, ValueError):
                    continue
                times.add(t)
            if not times:
                return {"value": None, "k": 0, "n": 0, "verdict": "INCONCLUSIVE",
                        "evidence": ["no samples"], "guard": guard_note}
            by_t = {}
            for s in samples or []:
                try:
                    by_t.setdefault(round(float(s.get("t", 0.0)), 2), []).append(s)
                except (TypeError, ValueError):
                    continue
            up = 0
            for t in sorted(times):
                cands = [c for c in (by_t.get(t) or [])
                         if str(c.get("guid") or "").lower() == str(alias or "").lower()]
                if not cands:
                    continue  # missing sample: NOT alive, still in the denominator
                try:
                    if float(cands[0].get("hp") or 0) > 0:
                        up += 1
                except (TypeError, ValueError):
                    continue
            n = len(times)
            value = up / n if n else 0.0
            return {"value": value, "k": up, "n": n,
                    "verdict": "PASS" if _pass(rule.get("pass_if"), value) else "FAIL",
                    "evidence": ["%d/%d samples" % (up, n)], "guard": guard_note}
        rows_s = sorted([s for s in (samples or [])
                         if str(s.get("guid") or "").lower() == str(alias or "").lower()
                         and (phase not in ("in_combat", "out_of_combat")
                              or bool(s.get("in_combat")) == (phase == "in_combat"))],
                        key=lambda s: float(s.get("t", 0.0)))
        rows_s = [s for s in rows_s if not _in_excluded(excluded, float(s.get("t", 0.0)))]
        if not rows_s:
            return {"value": None, "k": 0, "n": 0, "verdict": "INCONCLUSIVE",
                    "evidence": ["no samples"], "guard": guard_note}
        up = 0
        for sample in rows_s:
            try:
                have = {int(a) for a in (sample.get("auras") or [])}
            except (TypeError, ValueError):
                have = set()
            up += 1 if (have & aura) else 0
        value = up / len(rows_s)
        return {"value": value, "k": up, "n": len(rows_s),
                "verdict": "PASS" if _pass(rule.get("pass_if"), value) else "FAIL",
                "evidence": ["%d/%d samples" % (up, len(rows_s))], "guard": guard_note}

    if metric == "first":
        hits = sorted(scored([r for r in rows if _matches(r, where, samples_by_guid, ctx, trial)]),
                      key=lambda r: r.get("t", 0.0))
        if not hits:
            # No match: pass_if still decides, as +inf (e.g. "< 30" FAILs, "> 30"
            # PASSes). The +inf stays internal; the stored value is null (JSON-safe).
            return {"value": None, "no_match": True, "k": 0, "n": 1,
                    "verdict": "PASS" if _pass(rule.get("pass_if"), float("inf")) else "FAIL",
                    "evidence": ["no match"], "guard": guard_note}
        value = hits[0].get("t", 0.0)
        return {"value": value, "k": 1, "n": 1,
                "verdict": "PASS" if _pass(rule.get("pass_if"), value) else "FAIL",
                "evidence": [evidence_line(hits[0])], "guard": guard_note}

    if metric == "rate":
        hits = scored([r for r in rows if _matches(r, where, samples_by_guid, ctx, trial)])
        span = _scored_span(rows, samples, excluded, window)
        value = (len(hits) / span * 60.0) if span > 0 else 0.0
        return {"value": value, "k": len(hits), "n": 1,
                "verdict": "PASS" if _pass(rule.get("pass_if"), value) else "FAIL",
                "evidence": ["%d in %.0f s scored" % (len(hits), span)], "guard": guard_note}

    return {"value": None, "verdict": "INCONCLUSIVE",
            "evidence": ["unknown metric %r" % metric], "guard": guard_note}


def window_rows(rows, window):
    lo, hi = window
    out = []
    for r in rows or []:
        if r.get("kind") == "excluded":
            out.append(r)
            continue
        try:
            t = float(r.get("t", 0.0))
        except (TypeError, ValueError):
            continue
        if lo <= t <= hi:
            out.append(r)
    return out


def window_samples(samples, window):
    lo, hi = window
    out = []
    for s in samples or []:
        try:
            t = float(s.get("t", 0.0))
        except (TypeError, ValueError):
            continue
        if lo <= t <= hi:
            out.append(s)
    return out


def _scored_span(rows, samples, excluded, window=None):
    if window:
        lo, hi = window
        cut = 0.0
        for (a, b) in excluded:
            cut += max(0.0, min(hi, b) - max(lo, a))
        return max(0.0, (hi - lo) - cut)
    ts = [float(r.get("t", 0.0)) for r in (rows or []) if r.get("kind") != "excluded"]
    for s in samples or []:
        try:
            ts.append(float(s.get("t", 0.0)))
        except (TypeError, ValueError):
            continue
    if not ts:
        return 0.0
    lo, hi = min(ts), max(ts)
    cut = 0.0
    for (a, b) in excluded:
        cut += max(0.0, min(hi, b) - max(lo, a))
    return max(0.0, (hi - lo) - cut)


def opportunity_seconds(trial, samples, excluded, ctx):
    opp = (trial.get("opportunity") or {}) if isinstance(trial, dict) else {}
    if not opp:
        return 0.0
    guids = (ctx.get("guids") or {}) if isinstance(ctx, dict) else {}
    alias = _alias_map(guids).get(
        opp.get("unit"), opp.get("unit"))
    total = 0.0
    prev = None
    for sample in sorted(samples or [], key=lambda s: float(s.get("t", 0.0) or 0.0)):
        try:
            t = float(sample.get("t", 0.0))
        except (TypeError, ValueError):
            continue
        if str(sample.get("guid") or "").lower() != str(alias or "").lower():
            continue
        if prev is not None:
            try:
                hp = float(sample.get("hp"))
                max_hp = float(sample.get("max_hp"))
                pct = 100.0 * hp / max_hp if max_hp > 0 else None
            except (TypeError, ValueError, ZeroDivisionError):
                pct = None
            ok = True
            if "hp_lt" in opp and opp["hp_lt"] is not None and not (pct is not None and pct < _num(opp["hp_lt"], ctx, float("-inf"))):
                ok = False
            if "hp_gt" in opp and opp["hp_gt"] is not None and not (pct is not None and pct > _num(opp["hp_gt"], ctx, 0)):
                ok = False
            if "in_combat" in opp and opp["in_combat"] is not None and bool(sample.get("in_combat")) != bool(opp["in_combat"]):
                ok = False
            if ok:
                # The interval (prev, t] contributes its length minus any
                # merged excluded overlap — never double-counted.
                seg = t - prev
                for (a, b) in excluded:
                    seg -= max(0.0, min(t, b) - max(prev, a))
                total += max(0.0, seg)
        prev = t
    return total
