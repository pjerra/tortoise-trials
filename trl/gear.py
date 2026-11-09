"""Gear kits for the trial option `player.gear: "tier"` (data/gear_kits.json, built by build_data.py).

A trial whose player is a GM-levelled character with no items has a threat far below a real tank's, so the
DPS bots sit above 100 percent of it and the threat cap decides every DPS trial. `player.gear: "tier"` has the
runner grant a fixed per-class, per-role, per-level kit with the guarded self-only `.additem` and equip it.
"""

import json
import os

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT_PATH = os.path.join(TOOL_ROOT, "data", "gear_kits.json")
MAX_LEVEL_GAP = 9           # a kit built for level 40 serves a level 40-49 trial if there is no kit of its own


def load_kits(path=None):
    try:
        with open(path or KIT_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def role_of(player):
    """tank for a forced tank role, else dps: a shield would make a DPS player read as a tank."""
    return "tank" if (player or {}).get("forced_role") == "tank" else "dps"


def pick_kit(player, kits=None):
    """(items, kit_level, "") or (None, None, why): the kit of the player's class and role for its level.

    The highest kit level not above the player's level, and at most MAX_LEVEL_GAP below it. A class, role or
    level with no kit is a clear reason, never an empty kit.
    """
    kits = load_kits() if kits is None else kits
    klass = str((player or {}).get("class") or "").lower()
    role = role_of(player)
    try:
        level = int((player or {}).get("level"))
    except (TypeError, ValueError):
        return None, None, "player.level missing: no gear kit"
    by_level = ((kits.get(klass) or {}).get(role)) or {}
    levels = sorted(int(k) for k in by_level if str(k).isdigit() and int(k) <= level)
    if not levels:
        return None, None, "no %s %s gear kit for level %d (data/gear_kits.json; build_data.py makes it)" % (
            klass or "?", role, level)
    kit_level = levels[-1]
    if level - kit_level > MAX_LEVEL_GAP:
        return None, None, "no %s %s gear kit within %d levels of %d (nearest is level %d)" % (
            klass, role, MAX_LEVEL_GAP, level, kit_level)
    items = by_level[str(kit_level)]
    if not items:
        return None, None, "the %s %s kit for level %d is empty" % (klass, role, kit_level)
    return items, kit_level, ""


def kit_entries(player, kits=None):
    items, _lvl, _why = pick_kit(player, kits)
    return [int(i["entry"]) for i in items or []]


def all_entries(kits=None):
    """Every entry of every kit (the strip set: what a gear trial can have left on a slot character)."""
    kits = load_kits() if kits is None else kits
    out = set()
    for roles in kits.values():
        for levels in (roles or {}).values():
            for items in (levels or {}).values():
                out.update(int(i["entry"]) for i in items or [])
    return out
