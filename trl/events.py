"""Raw turtle-agent rows -> normalised events.jsonl, one row per event.

Inputs per repeat dir: raw-ta.jsonl (agent `events` rows: seq, time, kind,
source, target, spell_id, amount, crit, data; source/target hold name+guid),
samples.jsonl, marks.jsonl, setup.json (guids: player, bot, pet, mobs, marked).
"""

import json
import os
from datetime import datetime

KIND_MAP = {
    "spell_go": "cast",
    "spell_start": "cast_start",
    "spell_fail": "cast_fail",
    "aura_applied": "aura_on",
    "aura_removed": "aura_off",
    "melee": "damage",
    "spell_damage": "damage",
    "periodic": "damage",
    "heal": "heal",
    "death": "death",
}


def _wall_secs(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.timestamp()


def _norm_guid(value):
    if not value:
        return ""
    text = str(value).strip().lower()
    if not text.startswith("0x"):
        try:
            text = "0x%016x" % int(text)
        except ValueError:
            return text
    try:
        return "0x%016x" % int(text, 16)
    except ValueError:
        return text


def _side_guid(row, side):
    flat = row.get(side + "_guid") or row.get(side + "Guid")
    if flat:
        return _norm_guid(flat)
    value = row.get(side)
    if isinstance(value, dict):
        return _norm_guid(value.get("guid") or "")
    return ""


def _side_name(row, side):
    value = row.get(side)
    if isinstance(value, dict):
        return str(value.get("name") or "")
    if isinstance(value, str):
        return value
    return ""


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


def _unit_key(unit):
    if unit in ("$player", "$bot", "$pet", "$marked", "$extra1", "$extra2", "$extra3"):
        return unit
    if isinstance(unit, str) and unit.startswith("mob:"):
        return unit
    return unit

def _rewall(value, base_wall):
    """A wall clock (ISO) or rebased seconds relative to base_wall.

    Marks carry ISO walls; samples/events carry rebased seconds (t seconds
    since fight_start, or setup_ok when no pull). score/rules never read the
    raw marks: they read the normaliser's `kind: excluded` rows here.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    secs = _wall_secs(value)
    if secs is None or base_wall is None:
        return None
    return secs - base_wall


def _merge_spans(spans):
    """Merge overlapping/touching (a, b, why) spans; keep the first why."""
    ordered = sorted([(float(a), float(b), w) for (a, b, w) in spans
                      if b is not None and a is not None and float(b) > float(a)])
    out = []
    for (a, b, w) in ordered:
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b, out[-1][2])
        else:
            out.append((a, b, w))
    return out


def normalise(rep_dir, trial):
    """Write events.jsonl in rep_dir; return the rows list.

    One time base: samples AND raws are re-based from their ISO `wall` onto
    the fight_start wall (or setup_ok when no pull). Events and the excluded
    rows below carry re-based seconds; score/rules read ONLY this output
    (plus the untouched samples), never the runner's raw marks.
    """
    raw = _read_jsonl(os.path.join(rep_dir, "raw-ta.jsonl"))
    samples = _read_jsonl(os.path.join(rep_dir, "samples.jsonl"))
    marks = _read_jsonl(os.path.join(rep_dir, "marks.jsonl"))
    try:
        with open(os.path.join(rep_dir, "setup.json")) as f:
            setup = json.load(f)
    except (OSError, ValueError):
        setup = {}

    guids = setup.get("guids") or {}
    player = _norm_guid(guids.get("player"))
    bot = _norm_guid(guids.get("bot"))
    pet = _norm_guid(guids.get("pet"))
    mobs = {_norm_guid(g) for g in (guids.get("mobs") or []) if g}
    marked = _norm_guid(guids.get("marked"))
    extras_raw = guids.get("extras")
    extras = [_norm_guid(g) for g in (extras_raw if isinstance(extras_raw, (list, tuple)) else [])][:3]

    # t = wall - fight_start wall (or setup_ok when no pull): ONE base for
    # samples and raws alike. Re-base rows carrying an ISO wall; rows with
    # no usable wall keep their (already re-based) t.
    base_wall = None
    for mark in marks:
        if mark.get("kind") == "fight_start":
            base_wall = _wall_secs(mark.get("wall"))
            break
    if base_wall is None:
        for mark in marks:
            if mark.get("kind") == "setup_ok":
                base_wall = _wall_secs(mark.get("wall"))
                break
    if base_wall is None:
        base_wall = _wall_secs((samples[0].get("wall") if samples else None)) or 0.0

    # Excluded spans in re-based seconds (ONE source for score/rules: the
    # kind: excluded rows below). ISO from/to are re-based; numeric from/to
    # are kept as-is (already re-based seconds, e.g. live-collect writes).
    excluded = []
    for mark in marks:
        kind = mark.get("kind")
        if kind == "excluded":
            a = mark.get("from")
            b = mark.get("to")
            ra, rb = _rewall(a, base_wall), _rewall(b, base_wall)
            if ra is not None and rb is not None and rb > ra:
                excluded.append((ra, rb, str(mark.get("why") or "")))
        elif kind == "gm_heal":
            w = _wall_secs(mark.get("wall"))
            if w is not None:
                excluded.append((w - base_wall, w - base_wall + 3.0, "floor"))
    excluded = _merge_spans(excluded)

    # live3: the host wall clock (WSL2) steps back ~2-3 s every ~32 s, and the agent stamps its
    # events with that clock too (`time`), so events ran backwards in blocks. Events also carry
    # `mono_ms` (the agent's monotonic axis, the same one `status.mono_ms` reports): when the
    # samples carry the status mono_ms next to the runner's steady wall, an event's time is that
    # offset plus its mono_ms. Old runs (no mono_ms in the samples) keep the `time` rebase.
    offsets = sorted(
        w - float(smp["mono_ms"]) / 1000.0
        for smp in samples
        for w in [_wall_secs(smp.get("wall"))]
        if w is not None and smp.get("mono_ms") is not None)
    mono_offset = offsets[len(offsets) // 2] if offsets else None

    def rebase(value, fallback):
        secs = _wall_secs(value)
        if secs is None:
            return fallback
        if base_wall and abs(base_wall) > 1e-9:
            return secs - base_wall
        return fallback

    def role_of(guid):
        if guid and guid == player:
            return "player"
        if guid and guid == bot:
            return "bot"
        if guid and guid == pet:
            return "pet"
        if guid and guid in extras:
            return "extra"
        if guid and (guid in mobs or guid == marked):
            return "mob"
        return "other"

    def unit_of(guid):
        if guid and guid == player:
            return "$player"
        if guid and guid == bot:
            return "$bot"
        if guid and guid == pet:
            return "$pet"
        if guid and guid in extras:
            return "$extra%d" % (extras.index(guid) + 1)
        if guid and guid == marked:
            return "$marked"
        if guid and guid in mobs:
            return "mob:%s" % guid
        return guid or ""

    # Samples: re-base ISO walls onto the fight_start wall, same base as raws.
    rebased_samples = []
    for sample in samples:
        sample = dict(sample)
        wall = sample.get("wall")
        if wall is not None and _wall_secs(wall) is not None and base_wall:
            try:
                sample["t"] = round(_wall_secs(wall) - base_wall, 2)
            except (TypeError, ValueError):
                pass
        rebased_samples.append(sample)
    with open(os.path.join(rep_dir, "samples.jsonl"), "w") as f:
        for sample in rebased_samples:
            f.write(json.dumps(sample, sort_keys=True, allow_nan=False) + "\n")
    samples = rebased_samples

    # Samples by guid, sorted by t.
    by_guid = {}
    for sample in samples:
        guid = _norm_guid(sample.get("guid"))
        try:
            t = float(sample.get("t", 0.0))
        except (TypeError, ValueError):
            continue
        by_guid.setdefault(guid, []).append((t, sample))
    for rows in by_guid.values():
        rows.sort(key=lambda pair: pair[0])

    try:
        import trl.spells as _spells
    except ImportError:  # pragma: no cover - direct path fallback
        from trl import spells as _spells

    def hp_at(guid, t):
        rows = by_guid.get(guid) or []
        best = None
        for st, sample in rows:
            if st <= t and t - st <= 1.0:
                best = (st, sample)
            elif st > t:
                break
        if best is None:
            return None
        sample = best[1]
        try:
            hp = float(sample.get("hp"))
            max_hp = float(sample.get("max_hp"))
        except (TypeError, ValueError):
            return None
        if max_hp <= 0:
            return None
        return 100.0 * hp / max_hp

    def aura_set(guid):
        rows = by_guid.get(guid) or []
        if not rows:
            return set()
        last = rows[-1][1].get("auras") or []
        try:
            return {int(a) for a in last}
        except (TypeError, ValueError):
            return set()

    # Dedupe on (seq, content): null seq rows dedupe on content alone.
    seen = set()
    out = []
    dropped_kinds = {}
    for row in raw:
        seq = row.get("seq")
        content = json.dumps(row, sort_keys=True, default=str, allow_nan=False)
        key = (seq, content) if seq is not None else (None, content)
        if key in seen:
            continue
        seen.add(key)
        kind = KIND_MAP.get(row.get("kind"))
        if kind is None:
            dropped_kinds[row.get("kind")] = dropped_kinds.get(row.get("kind"), 0) + 1
            continue
        try:
            fallback = float(row.get("t", 0.0))
        except (TypeError, ValueError):
            fallback = 0.0
        t = rebase(row.get("time"), fallback)
        if mono_offset is not None and row.get("mono_ms") is not None and base_wall:
            try:
                t = mono_offset + float(row["mono_ms"]) / 1000.0 - base_wall
            except (TypeError, ValueError):
                pass
        try:
            t = float(t)
        except (TypeError, ValueError):
            t = fallback
        src_guid = _side_guid(row, "source")
        dst_guid = _side_guid(row, "target")
        spell_id = row.get("spell_id")
        try:
            spell_id = int(spell_id) if spell_id is not None else None
        except (TypeError, ValueError):
            spell_id = None
        # A missing time (no ISO wall, no usable t) is dropped and counted.
        if row.get("time") is None and row.get("t") is None:
            dropped_kinds["<missing time>"] = dropped_kinds.get("<missing time>", 0) + 1
            continue
        out.append({
            "t": round(t, 2),
            "wall": row.get("time"),
            "kind": kind,
            "src": _side_name(row, "source"),
            "src_guid": src_guid,
            "dst": _side_name(row, "target"),
            "dst_guid": dst_guid,
            "src_unit": unit_of(src_guid),
            "dst_unit": unit_of(dst_guid),
            "spell_id": spell_id,
            "spell": _spells.name_for(spell_id) if spell_id is not None else None,
            "amount": row.get("amount"),
            "dst_hp_pct": hp_at(dst_guid, t),
            "src_role": role_of(src_guid),
            "dst_role": role_of(dst_guid),
            "origin": "ta",
        })

    # Excluded rows from marks (gm_heal, excluded) become kind: excluded rows.
    # This is the ONE source score/rules read (merged above).
    for (a, b, why) in excluded:
        out.append({
            "t": round(a, 2),
            "wall": None,
            "kind": "excluded",
            "src": "",
            "src_guid": "",
            "dst": "",
            "dst_guid": "",
            "src_unit": "",
            "dst_unit": "",
            "spell_id": None,
            "spell": None,
            "amount": None,
            "dst_hp_pct": None,
            "src_role": "other",
            "dst_role": "other",
            "origin": "marks",
            "from": round(a, 2),
            "to": round(b, 2),
            "why": why,
        })

    out.sort(key=lambda r: (r.get("t", 0.0) if isinstance(r.get("t"), (int, float)) else 0.0,))
    with open(os.path.join(rep_dir, "events.jsonl"), "w") as f:
        for row in out:
            f.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    # Kept in the output, not thrown away: agent-ring drops and excluded spans.
    summary = {"dropped_kinds": dropped_kinds,
               "excluded_spans": [{"from": round(a, 2), "to": round(b, 2), "why": w}
                                  for (a, b, w) in excluded]}
    with open(os.path.join(rep_dir, "events_summary.json"), "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True, allow_nan=False)
    return out
