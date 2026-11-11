"""Offline builder: data/spells.json + data/mobs.json from public core SQL.

Reads <core>/sql/base/tw_world_spell_template.sql (core checkout: TRIALS_CORE_DIR),
tw_world_creature_template.sql (+ tw_world_creature_spells.sql). Parses the
INSERT INTO ... VALUES (...),(...) rows with a small tokenizer (quoted
strings with \\' escapes; reads the CREATE TABLE column list for the column
order — never hard-codes positions). Never contacts a server.

Usage: python3 -I build_data.py
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trl import spells as _spells  # noqa: E402  (the shared interruptible definition)

CORE_DIR = os.path.expanduser(os.environ.get("TRIALS_CORE_DIR", ""))
CORE_SQL = os.path.join(CORE_DIR, "sql", "base")
TOOL_ROOT = os.path.dirname(os.path.abspath(__file__))

HOSTILE_FACTIONS = {14, 16}

# Non-selectable / non-attackable bits of creature_template.unit_flags.
UNIT_FLAG_NOT_SELECTABLE = 0x02000000
UNIT_FLAG_NON_ATTACKABLE = 0x00000080


def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _create_columns(sql, table):
    i = sql.find("CREATE TABLE `%s`" % table)
    if i < 0:
        raise ValueError("no CREATE TABLE for %s" % table)
    j = sql.find("\n;", i)
    body = sql[i:j]
    return re.findall(r"^\s*`([a-zA-Z0-9_]+)`", body, re.M)


def _split_inserts(sql):
    """Yield one VALUES row (raw text incl. parens) per INSERT row."""
    rows = []
    for line in sql.splitlines():
        line = line.strip()
        if not line.startswith("INSERT INTO"):
            continue
        k = line.find("VALUES")
        if k < 0:
            continue
        rows.extend(_split_rows(line[k + len("VALUES"):]))
    return rows


def _split_rows(text):
    rows = []
    depth = 0
    quote = None
    cur = ""
    for pos, ch in enumerate(text):
        if quote:
            cur += ch
            if ch == "\\":
                continue
            if ch == quote and not cur.endswith("\\\\"):
                # count preceding backslashes: odd count = escaped quote
                back = 0
                p = len(cur) - 2
                while p >= 0 and cur[p] == "\\":
                    back += 1
                    p -= 1
                if back % 2 == 0:
                    quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur += ch
            continue
        if ch == "(":
            if depth == 0:
                cur = ""
            else:
                cur += ch
            depth += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                rows.append(cur)
                cur = ""
            else:
                cur += ch
            continue
        if depth > 0:
            cur += ch
    return rows


def _split_values(row):
    vals = []
    quote = None
    cur = ""
    i = 0
    while i < len(row):
        ch = row[i]
        if quote:
            cur += ch
            if ch == "\\" and i + 1 < len(row):
                cur += row[i + 1]
                i += 1
            elif ch == quote:
                back = 0
                p = len(cur) - 2
                while p >= 0 and cur[p] == "\\":
                    back += 1
                    p -= 1
                if back % 2 == 0:
                    quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            cur += ch
        elif ch == ",":
            vals.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    vals.append(cur.strip())
    return vals


def _unquote(token):
    token = token.strip()
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        inner = token[1:-1].replace("\\'", "'").replace("\\\\", "\\")
        return inner
    if token.upper() == "NULL":
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _parse_table(path, table):
    sql = _read(path)
    create_cols = _create_columns(sql, table)
    # Layout: one INSERT header line (carries the column list), then one row
    # per line. Honour the header list; fall back to CREATE TABLE order.
    out = []
    skipped = 0
    collist = create_cols
    for line in sql.splitlines():
        line = line.strip()
        if line.startswith("INSERT INTO"):
            k = line.find("VALUES")
            head = line[:k] if k >= 0 else line
            m = re.search(r"\(([^()]*)\)\s*$", head)
            if m:
                collist = [c.strip(" `") for c in m.group(1).split(",")]
            body = line[k + len("VALUES"):] if k >= 0 else ""
            if body.strip():
                for raw in _split_rows(body):
                    vals = [_unquote(t) for t in _split_values(raw)]
                    if len(vals) != len(collist):
                        skipped += 1
                        continue
                    out.append(dict(zip(collist, vals)))
            continue
        if not line.startswith("("):
            continue
        for raw in _split_rows(line):
            vals = [_unquote(t) for t in _split_values(raw)]
            if len(vals) != len(collist):
                skipped += 1
                continue
            out.append(dict(zip(collist, vals)))
    if skipped:
        print("WARN: %s: skipped %d rows (shape mismatch)" % (table, skipped))
    return out


def _cast_table():
    """SpellCastTimes.dbc base cast times (ms) by castingTimeIndex.

    From the client DBC (not in core SQL): ID -> base CastTime. Only the
    base is stored; per-level scaling needs the caster's rank, so
    build_data keeps the unscaled base in cast_ms.

    Index 18 is -1000000 on purpose: it is the DBC's marker for the hunter
    ranged shots (Serpent/Viper Sting, Arcane Shot, Distracting Shot, ...) whose
    time is not a fixed cast bar. SpellEntry::GetCastTime clamps a negative
    CastTime with std::max(castTime, MinCastTime) and returns 0 for anything
    not > 0 (core src/game/Spells/SpellEntry.cpp ~582-610), so these never
    count as cast-time spells; the negative value keeps "cast_ms > 0" false in
    is_interruptible. The DBC itself is client data and not in the core repo,
    so the value is kept as read from it, not re-derived.
    """
    return {1: 0, 2: 250, 3: 500, 4: 1000, 5: 2000, 6: 5000, 7: 10000,
            8: 20000, 9: 30000, 10: 1000, 11: 2000, 12: 5000, 13: 30000,
            14: 3000, 15: 4000, 16: 1500, 18: -1000000, 19: 2500, 20: 2500,
            21: 2600, 22: 3500, 23: 1800, 24: 2200, 25: 2900, 26: 3700,
            27: 4100, 28: 3200, 29: 4700, 30: 4500, 31: 2300, 32: 7000,
            33: 5125, 34: 8000, 35: 12500, 36: 600, 37: 25000, 38: 45000,
            39: 60000, 50: 1300, 70: 300000, 90: 1700, 91: 2800, 110: 750,
            130: 1600, 150: 3800, 151: 2700, 152: 3100, 153: 3400,
            170: 8000, 171: 6000, 190: 100, 191: 0}


def _core_commit():
    import subprocess
    try:
        return subprocess.run(
            ["git", "-C", CORE_DIR,
             "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001 - offline builder still works
        return "unknown"


def build_spells(path=None):
    path = path or os.path.join(CORE_SQL, "tw_world_spell_template.sql")
    rows = _parse_table(path, "spell_template")
    cast_table = _cast_table()
    by_name = {}
    by_id = {}
    for row in rows:
        name = row.get("name")
        entry = row.get("entry")
        if not name or entry is None:
            continue
        try:
            entry = int(entry)
        except (TypeError, ValueError):
            continue
        rank = row.get("nameSubtext") or ""
        try:
            cidx = int(row.get("castingTimeIndex") or 0)
        except (TypeError, ValueError):
            cidx = 0
        try:
            prevention = int(row.get("preventionType") or 0)
        except (TypeError, ValueError):
            prevention = 0
        try:
            flags = int(row.get("interruptFlags") or 0)
        except (TypeError, ValueError):
            flags = 0
        by_name.setdefault(str(name), []).append(entry)
        by_id[str(entry)] = {
            "name": str(name),
            "rank": str(rank) if isinstance(rank, str) else rank,
            "dispel": row.get("dispel"),
            "cast_ms": cast_table.get(cidx, 0),
            "casting_time_index": cidx,
            "school": row.get("school"),
            "interrupt_flags": flags,
            "prevention_type": prevention,
        }
    for ids in by_name.values():
        ids.sort()
    return {"by_name": by_name, "by_id": by_id,
            "core_commit": _core_commit(),
            "cast_table": "SpellCastTimes.dbc base CastTime by castingTimeIndex"}


def build_mobs(spells_path=None, template_path=None, cspells_path=None):
    template_path = template_path or os.path.join(CORE_SQL, "tw_world_creature_template.sql")
    cspells_path = cspells_path or os.path.join(CORE_SQL, "tw_world_creature_spells.sql")
    spells_path = spells_path or os.path.join(CORE_SQL, "tw_world_spell_template.sql")
    mobs = _parse_table(template_path, "creature_template")
    links = _parse_table(cspells_path, "creature_spells")
    spell_rows = _parse_table(spells_path, "spell_template")
    spell_by_id = {}
    for row in spell_rows:
        try:
            spell_by_id[int(row.get("entry"))] = row
        except (TypeError, ValueError):
            continue
    # A caster mob needs an INTERRUPTIBLE spell, by the same definition the
    # rules use (trl.spells.is_interruptible: cast time > 0, silence prevention,
    # damage interrupt flag). Cast times come from the cast-time table below.
    cast_table = _cast_table()
    interrupt_caster_lists = set()
    for link in links:
        for slot in range(1, 9):
            sid = link.get("spellId_%d" % slot)
            if not sid:
                continue
            try:
                spell = spell_by_id.get(int(sid))
            except (TypeError, ValueError):
                spell = None
            if spell is not None and _spells.is_interruptible(
                    cast_table.get(_as_int(spell.get("castingTimeIndex")), 0),
                    _as_int(spell.get("preventionType")),
                    _as_int(spell.get("interruptFlags"))):
                interrupt_caster_lists.add(link.get("entry"))
                break
    melee, caster, humanoid = [], [], []
    for mob in mobs:
        try:
            entry = int(mob.get("entry"))
        except (TypeError, ValueError):
            continue
        rank = mob.get("rank") or 0
        if int(rank or 0) != 0:
            continue
        if mob.get("script_name") or mob.get("ai_name"):
            continue
        try:
            if int(mob.get("spell_list_id") or 0) != 0:
                continue
            if int(mob.get("npc_flags") or 0) != 0:
                continue
            flags = int(mob.get("unit_flags") or 0)
            if flags & (UNIT_FLAG_NOT_SELECTABLE | UNIT_FLAG_NON_ATTACKABLE):
                continue
            ctype = int(mob.get("type") or 0)
            faction = int(mob.get("faction") or 0)
        except (TypeError, ValueError):
            continue
        if ctype not in (1, 7) or faction not in HOSTILE_FACTIONS:
            continue
        try:
            lo = int(mob.get("level_min") or 0)
            hi = int(mob.get("level_max") or 0)
        except (TypeError, ValueError):
            continue
        row = {"entry": entry, "name": mob.get("name"), "level_min": lo,
               "level_max": hi, "faction": faction}
        melee.append(row)
        if ctype == 7:
            humanoid.append(row)
    for mob in mobs:
        try:
            entry = int(mob.get("entry"))
            list_id = int(mob.get("spell_list_id") or 0)
        except (TypeError, ValueError):
            continue
        if list_id not in interrupt_caster_lists:
            continue
        if int(mob.get("rank") or 0) != 0:
            continue
        if mob.get("script_name") or mob.get("ai_name"):
            continue
        try:
            if int(mob.get("npc_flags") or 0) != 0:
                continue
            flags = int(mob.get("unit_flags") or 0)
            if flags & (UNIT_FLAG_NOT_SELECTABLE | UNIT_FLAG_NON_ATTACKABLE):
                continue
            ctype = int(mob.get("type") or 0)
            faction = int(mob.get("faction") or 0)
        except (TypeError, ValueError):
            continue
        if ctype not in (1, 7) or faction not in HOSTILE_FACTIONS:
            continue
        try:
            lo = int(mob.get("level_min") or 0)
            hi = int(mob.get("level_max") or 0)
        except (TypeError, ValueError):
            continue
        caster.append({"entry": entry, "name": mob.get("name"), "level_min": lo,
                       "level_max": hi, "faction": faction})
    doc = {"melee": melee, "caster": caster, "humanoid": humanoid}
    for band_lo in range(10, 60, 5):
        band_hi = band_lo + 4
        for role in ("melee", "caster", "humanoid"):
            n = sum(1 for m in doc[role]
                    if m["level_min"] <= band_hi and m["level_max"] >= band_lo)
            if n < 3:
                print("WARN: %s levels %d-%d has %d entries" % (role, band_lo, band_hi, n))
    return doc


# -- tank / DPS gear kits (trial option player.gear: "tier") -----------------------------------------

GEAR_KIT_LEVELS = (40, 45, 50)
GEAR_KIT_CLASSES = {"warrior": 1}                  # class name -> ChrClasses id (allowable_class bit = 1 << (id - 1))
GEAR_ARMOR_SLOTS = (            # slot name -> (inventory_type values, item class, item subclass)
    ("head", (1,), 4, 4), ("shoulder", (3,), 4, 4), ("chest", (5, 20), 4, 4), ("waist", (6,), 4, 4),
    ("legs", (7,), 4, 4), ("feet", (8,), 4, 4), ("wrist", (9,), 4, 4), ("hands", (10,), 4, 4),
    ("back", (16,), 4, 1),
)
GEAR_MAX_ENTRY = 20000
GEAR_BAD_NAME = re.compile(r"arena|marshal|sergeant|warlord|general|champion|zzz|test|deprecated|\bold\b|\(|monster|dnd|\bdep\b", re.I)
WARRIOR_1H_SUBCLASSES = (0, 4, 7)                  # axe, mace, sword: warriors know these from level 1
WARRIOR_2H_SUBCLASSES = (1, 5, 8)                  # 2H axe, mace, sword


def _opt_int(value):
    """An int, or None when the column is missing or empty (the kit data must not read a missing item class as 0)."""
    if value is None or str(value).strip() == "":
        return None
    return _as_int(value)


def _has_spell(row):
    """A proc or on-use effect would change the fight the trial measures: weapons without one."""
    return any(_as_int(row.get("spellid_%d" % i)) for i in range(1, 6))


def _weapon_dps(row):
    delay = _as_int(row.get("delay"))
    if delay <= 0:
        return 0.0
    return (float(row.get("dmg_min1") or 0) + float(row.get("dmg_max1") or 0)) / 2.0 / (delay / 1000.0)


def build_gear_kits(path=None):
    """Per class, role (tank | dps) and kit level: one plain, non-random, unrestricted item per slot.

    Armor: plate (the back slot: cloak), highest armor then item level, required level within 8 levels below the
    kit level, quality uncommon or rare, no reputation / honor / profession / spell requirement, no random suffix,
    usable by the class. Tank: a one-handed axe, mace or sword (highest dps) and a shield (highest armor). DPS: a
    two-handed axe, mace or sword (highest dps), no off-hand. Read from the public core's item_template.
    """
    path = path or os.path.join(CORE_SQL, "tw_world_item_template.sql")
    rows = _parse_table(path, "item_template")

    def usable(row, level, class_bit):
        if _as_int(row.get("quality")) not in (2, 3):
            return False
        req = _as_int(row.get("required_level"))
        if not level - 8 <= req <= level:
            return False
        if _as_int(row.get("allowable_race")) not in (-1, 0, 32767, 1791):
            return False
        allowed = _as_int(row.get("allowable_class"))
        if allowed not in (-1, 0, 32767) and not allowed & class_bit:
            return False
        for key in ("required_reputation_faction", "required_honor_rank", "required_city_rank",
                    "required_spell", "required_skill", "random_property", "start_quest", "set_id"):
            if _as_int(row.get(key)):
                return False
        # a warrior kit has no caster stats (intellect 5, spirit 6): the cloak pick took a healer's cloak
        if any(_as_int(row.get("stat_type%d" % i)) in (5, 6) and _as_int(row.get("stat_value%d" % i)) > 0
               for i in range(1, 11)):
            return False
        # vanilla 1.12 entries only (the server's own additions sit above 20000 and are unproven: a level-60
        # kit from this table would pull in 173 such entries at 20000 and up, a 26-item "90 Green Warrior"
        # batch among them; ilvl 90 at req 60 breaks the kit's vanilla rule), and no test, PvP or
        # placeholder names
        name = str(row.get("name") or "")
        if _as_int(row.get("entry")) >= GEAR_MAX_ENTRY or GEAR_BAD_NAME.search(name):
            return False
        return True

    kits = {}
    for klass, class_id in GEAR_KIT_CLASSES.items():
        bit = 1 << (class_id - 1)
        for role in ("tank", "dps"):
            for level in GEAR_KIT_LEVELS:
                items = []
                pool = [r for r in rows if usable(r, level, bit)]
                for slot, invs, icls, isub in GEAR_ARMOR_SLOTS:
                    cands = [r for r in pool if _as_int(r.get("class")) == icls and _as_int(r.get("subclass")) == isub
                             and _as_int(r.get("inventory_type")) in invs]
                    if not cands:
                        continue
                    best = max(cands, key=lambda r: (_as_int(r.get("armor")), _as_int(r.get("item_level")), -_as_int(r.get("entry"))))
                    items.append((slot, best))
                if role == "tank":
                    weapons = [r for r in pool if _as_int(r.get("class")) == 2 and not _has_spell(r)
                               and _as_int(r.get("subclass")) in WARRIOR_1H_SUBCLASSES
                               and _as_int(r.get("inventory_type")) in (13, 21)]
                    shields = [r for r in pool if _as_int(r.get("class")) == 4 and _as_int(r.get("subclass")) == 6
                               and _as_int(r.get("inventory_type")) == 14]
                    if weapons:
                        items.append(("mainhand", max(weapons, key=lambda r: (_weapon_dps(r), -_as_int(r.get("entry"))))))
                    if shields:
                        items.append(("offhand", max(shields, key=lambda r: (_as_int(r.get("armor")), _as_int(r.get("item_level")), -_as_int(r.get("entry"))))))
                else:
                    weapons = [r for r in pool if _as_int(r.get("class")) == 2 and not _has_spell(r)
                               and _as_int(r.get("subclass")) in WARRIOR_2H_SUBCLASSES
                               and _as_int(r.get("inventory_type")) == 17]
                    if weapons:
                        items.append(("twohand", max(weapons, key=lambda r: (_weapon_dps(r), -_as_int(r.get("entry"))))))
                kits.setdefault(klass, {}).setdefault(role, {})[str(level)] = [
                    {"slot": slot, "entry": _as_int(r["entry"]), "name": r["name"],
                     "armor": _as_int(r.get("armor")), "required_level": _as_int(r.get("required_level")),
                     "item_class": _opt_int(r.get("class")), "subclass": _opt_int(r.get("subclass"))}
                    for slot, r in items]
    return kits


def main():
    spells = build_spells()
    data_dir = os.path.join(TOOL_ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "spells.json"), "w") as f:
        json.dump(spells, f, indent=1, sort_keys=True)
    mobs = build_mobs()
    with open(os.path.join(data_dir, "mobs.json"), "w") as f:
        json.dump(mobs, f, indent=1, sort_keys=True)
    kits = build_gear_kits()
    with open(os.path.join(data_dir, "gear_kits.json"), "w") as f:
        json.dump(kits, f, indent=1, sort_keys=True)
    print("gear kits: %s" % ", ".join("%s/%s/%s=%d" % (k, r, lv, len(v)) for k, roles in sorted(kits.items())
                                      for r, lvls in sorted(roles.items()) for lv, v in sorted(lvls.items())))
    print("spells: %d names, %d ids" % (len(spells["by_name"]), len(spells["by_id"])))
    print("mobs: melee=%d caster=%d humanoid=%d" % (
        len(mobs["melee"]), len(mobs["caster"]), len(mobs["humanoid"])))


if __name__ == "__main__":
    main()
