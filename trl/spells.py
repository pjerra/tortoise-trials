"""Spell name <-> ids from data/spells.json (all ranks)."""

import json
import os

_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_cache = {}


def _load(path=None):
    global _cache
    if path is None:
        path = os.path.join(_TOOL_ROOT, "data", "spells.json")
    if path in _cache:
        return _cache[path]
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {"by_name": {}, "by_id": {}}
    if not isinstance(doc, dict):
        doc = {"by_name": {}, "by_id": {}}
    _cache[path] = doc
    return doc


def ids_for(name, path=None):
    """All spell ids for a name (all ranks). Unknown name -> empty set."""
    doc = _load(path)
    by_name = doc.get("by_name") or {}
    if not isinstance(by_name, dict):
        return set()
    ids = by_name.get(name) or by_name.get(str(name).lower()) or []
    out = set()
    for i in ids:
        try:
            out.add(int(i))
        except (TypeError, ValueError):
            continue
    return out


def has_id(spell_id, path=None):
    """True when the id is a known spell id in data/spells.json."""
    doc = _load(path)
    by_id = doc.get("by_id") or {}
    return isinstance(by_id, dict) and str(spell_id) in by_id


def name_for(spell_id, path=None):
    """Spell name for an id. Unknown id -> str(id)."""
    doc = _load(path)
    by_id = doc.get("by_id") or {}
    if not isinstance(by_id, dict):
        return str(spell_id)
    row = by_id.get(str(spell_id))
    if isinstance(row, dict) and row.get("name"):
        return str(row["name"])
    if isinstance(row, str):
        return row
    return str(spell_id)


def clear_cache():
    _cache.clear()


# SPELL_INTERRUPT_FLAG_DAMAGE: the flag bit the core tests in SpellEffects.cpp ~3433-3442 (SpellDefines.h ~438-442).
INTERRUPT_FLAG_DAMAGE = 0x02
PREVENTION_SILENCE = 1


def is_interruptible(cast_ms, prevention_type, interrupt_flags):
    """THE definition of an interruptible spell (rules.py and build_data.py share it).

    cast time > 0 AND PreventionType == SILENCE (1) AND
    (InterruptFlags & SPELL_INTERRUPT_FLAG_DAMAGE). Channeled spells are left
    out on purpose: they carry no cast time (cast_ms 0), so the first
    condition drops them; a channel is stopped by other means than a kick on
    a cast bar, and the trials do not score those. Cast-time index 18 (hunter
    ranged shots) is stored as a negative base and is never > 0 either.
    """
    def _n(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
    return bool(_n(cast_ms) > 0 and int(_n(prevention_type)) == PREVENTION_SILENCE
                and (int(_n(interrupt_flags)) & INTERRUPT_FLAG_DAMAGE))


def spell_is_interruptible(spell_id, path=None):
    """is_interruptible() for a spell id from data/spells.json (unknown id -> False)."""
    info = (_load(path).get("by_id") or {}).get(str(spell_id))
    if not isinstance(info, dict):
        return False
    return is_interruptible(info.get("cast_ms"), info.get("prevention_type"),
                            info.get("interrupt_flags"))
