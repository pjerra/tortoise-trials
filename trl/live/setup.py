"""Setup + proof: every check writes {id, ok, value, evidence}.

Order (brief 1 section 3): `.gm off`; level up; talents learned and proven by
the spells RPC; stance aura; forced role; the hire, its level, spec and role;
its items and mana; grouped; config thresholds. Any failed check except
config_read is retried once, then the repeat is INVALID.
"""

import json
import os
import re
import time

from .. import gear as gear_mod
from .. import talents as talents_mod
from .deadline import Deadline
from .agent import event_guid, event_name, event_text, norm_guid, wait_until
from .gm_guard import GmRefused, GmServerError
from .situations import THREAT_CAST, bring_hire

HIRED = re.compile(r"\bhired\s+([A-Za-z]{2,12})\s+for\b", re.I)
HIRE_REFUSAL = re.compile(
    r"maximum number of companions|raid already holds the maximum|cannot afford|"
    r"cannot fill that role|no mercenary of that kind|hiring is disabled|may not hire|"
    r"not unlocked yet|may not hire|bad level|unclear number|conversion to a raid|"
    r"no race of your faction can be that class",
    re.I,
)
LEVEL_RE = re.compile(r"\bLevel (\d{1,2})\.")
ITEM_ID = re.compile(r"\|Hitem:(\d+)")
ITEM_WORD = re.compile(r"^\s*(\d{1,6})\s+-\s+\[")
# exact-name forms: the link text and the `123 - [Name]` row
ITEM_LINK_NAME = re.compile(r"\|Hitem:(\d+)[^|]*\|h\[([^\]]+)\]\|h")
ITEM_WORD_NAME = re.compile(r"^\s*(\d{1,6})\s+-\s+\[([^\]]+)\]")
ROLE_SET = re.compile(r"(Your role set to \w+|Bot \S+ role set to \w+)", re.I)

# spec id (commands/Party.cpp kSpecRows) -> the strategy word `co ?` lists
SPEC_STRATEGY = {
    "arms": "arms", "fury": "fury", "protection": "protection",
    "holy": "holy", "retribution": "retribution",
    "bm": "beast mastery", "mm": "marksmanship", "survival": "survival",
    "assa": "assassination", "combat": "combat", "subtlety": "subtlety",
    "discipline": "discipline", "shadow": "shadow", "hybrid": "holy",
    "ele": "elemental", "enh": "enhancement", "restoration": "restoration",
    "arcane": "arcane", "fire": "fire", "frost": "frost",
    "affliction": "affliction", "demonology": "demonology", "destruction": "destruction",
    "balance": "balance", "boomkin": "balance", "cat": "dps feral", "bear": "tank feral",
}

PET_CLASSES = {"hunter", "warlock"}

# live3: the player was logged in as a hidden game master (VISIBILITY_OFF): the bots skip it
# (PartyMemberToHeal::Check :358, PartyMemberValue::Check :171-179), so the priest never healed,
# shielded or buffed it. The core's hide is the aura "Greater Invisibility" 16380 plus the
# visibility flag; `.gm visible on` removes both, `.unaura all` removes only the aura.
GM_INVISIBLE_AURA = 16380
VISIBLE_REPLY = re.compile(r"\bvisible\b", re.I)
VISIBLE_FAILED = re.compile(r"\binvisib|no such command|unknown command|incorrect syntax|"
                            r"\bsyntax\b|\busage\b|not allowed|no access|you can.?t", re.I)
VISIBLE_WINDOW_S = 30.0
EQUIPMENT_OFFHAND_SLOT = 16        # EQUIPMENT_SLOT_OFFHAND (1.12): HEAD 0 .. MAINHAND 15, OFFHAND 16, RANGED 17
REASON_NOT_VISIBLE = "player not visible to bots"


def row(cid, ok, value=None, evidence=""):
    return {"id": cid, "ok": bool(ok), "value": value, "evidence": evidence}


def _retry(fn, attempts=2, deadline=None):
    """Run a check up to `attempts` times; with a `deadline` a spent one ends the retries (the last result,
    whose evidence names the phase that ran out, stands)."""
    out = None
    for _ in range(attempts):
        out = fn()
        if out is None or out.get("ok"):
            return out
        if deadline is not None and deadline.expired():
            break
    return out


def _aura_ids(unit_row):
    return {int(a.get("spell_id", 0)) for a in (unit_row or {}).get("auras") or []}


def check_gm_visible(agent, guard, ctx=None, reply_wait=1.5, aura_wait=3.0, poll=0.3):
    """`.gm visible on`, then `.gm off`; the row proves the first one took.

    Proof = the server's `system` reply to `.gm visible on` (a failure line fails the row) and
    the player no longer holding aura 16380 afterwards. A missing reply is not a failure by
    itself (the exact wording is the core's), a held 16380 always is.
    """
    since = agent.snapshot()
    guard.send("gm_visible_on", None)
    lines, _rows = agent.read_lines(
        since, timeout=reply_wait, kinds=("system",),
        stop=lambda t: bool(VISIBLE_REPLY.search(t)) or bool(VISIBLE_FAILED.search(t)))
    guard.send("gm_off", None)
    reply = next((t for t in lines if VISIBLE_FAILED.search(t)), "") or \
        next((t for t in lines if VISIBLE_REPLY.search(t)), "")
    failed = bool(reply and VISIBLE_FAILED.search(reply))
    gone = wait_until(lambda: GM_INVISIBLE_AURA not in _aura_ids(agent.own_row()),
                      timeout=aura_wait, poll=poll)
    ok = bool(gone) and not failed
    value = {"reply": reply or None, "aura_16380": not gone}
    evidence = "gm visible on -> %s; aura %d %s" % (
        repr(reply) if reply else "no reply line", GM_INVISIBLE_AURA,
        "absent" if gone else "STILL HELD (the player is hidden from the bots)")
    if failed:
        evidence += "; the reply reads as a refusal"
    return row("gm_visible", ok, value, evidence)


def check_level(agent, trial, guard, timeout=8.0):
    """Bring the player to the trial's level, up or down (`.levelup N` takes a signed count).

    A slot's character is kept between trials, so a trial after a level-45 one met a level-45 player
    ("character above trial level (45 > 40)": 16 reps of the first class sweep). The player never runs
    a trial above its level now. The final level is read back; a level that does not move stays a failure.
    """
    st = agent.status()
    if st.get("state") != "in_world":
        return row("player_level", False, st.get("state"), "not in world")
    want = int(trial["player"]["level"])
    have = int(st.get("level") or 0)
    lowered = ""
    if have != want:
        guard.send("levelup", guard.ctx.player_guid, want - have)
        wait_until(lambda: int(agent.status().get("level") or 0) == want, timeout=timeout, poll=0.5)
        if have > want:
            lowered = "lowered from %d; " % have
        have = int(agent.status().get("level") or 0)
    return row("player_level", have == want, have, "%slevel %d (target %d)" % (lowered, have, want))


def rpc_timeout(deadline, default=20.0):
    """The `timeout` kwarg of an RPC under a deadline: the default capped by the time left (at least 1 s), so
    one RPC never outlives the budget. {} without a finite deadline (the RPC keeps its own default)."""
    if deadline is None or deadline.budget == float("inf"):
        return {}
    return {"timeout": "%ds" % max(1, int(deadline.cap(default)))}


def worn_entries(agent, deadline=None):
    """({item entry}, error text): the entries the player wears, from the agent's `equipment` read."""
    out = agent.rpc("equipment", {}, **rpc_timeout(deadline))
    if isinstance(out, dict) and "_err" in out:
        return None, "no equipment read in this agent: %s" % str(out["_err"])[:120]
    rows = out.get("items") if isinstance(out, dict) else out
    if not isinstance(rows, list):
        return None, "equipment reply is not a list"
    return {int(i.get("item_entry") or 0) for i in rows if isinstance(i, dict) and int(i.get("item_entry") or 0)}, ""


def bagged_entries(agent, deadline=None):
    """({entry: copies}, error text): the items in the backpack and the bags (`inventory` with bags)."""
    out = agent.rpc("inventory", {"bags": True}, **rpc_timeout(deadline))
    if isinstance(out, dict) and "_err" in out:
        return None, "no inventory read in this agent: %s" % str(out["_err"])[:120]
    rows = out.get("items") if isinstance(out, dict) else out
    if not isinstance(rows, list):
        return None, "inventory reply is not a list"
    counts = {}
    for item in rows:
        if isinstance(item, dict) and int(item.get("item_entry") or 0):
            entry = int(item["item_entry"])
            counts[entry] = counts.get(entry, 0) + max(1, int(item.get("count") or 1))
    return counts, ""


GEAR_STEP_IDS = {"player_gear_carryover", "player_gear", "player_gear_strip"}
GEAR_SETUP_BUDGET_S = 90.0     # the whole gear setup of one rep: carry-over strip, proficiencies, grants, equips


def _no_deadline():
    return Deadline(float("inf"))


def _timeout_evidence(timed_out, deadline):
    if not timed_out:
        return ""
    return "; setup deadline (%.0f s) ran out in: %s" % (deadline.budget, ", ".join(timed_out))


def _wait_worn(agent, entries, wait, poll, deadline=None):
    """True once every entry shows in the `equipment` read (an equip is a packet: the server's reply, and so
    the read, can trail the call by a moment)."""
    def all_worn():
        worn, _err = worn_entries(agent, deadline)
        return worn is not None and set(entries) <= worn
    return bool(wait_until(all_worn, timeout=wait, poll=poll))


def _learn_proficiencies(agent, spells, guard, ctx, settle, wait, poll, deadline, timed_out):
    """Learn the kit's armor / weapon proficiencies the player lacks (a GM-levelled character never learned
    Plate Mail 750 or Mail 8737, and an item without its proficiency cannot be equipped). Only for the player
    itself, through the guarded `.learn`; a spell already known gets no line. Returns (learned, still missing).
    Every wait is capped by the deadline; a spent deadline stops the sends and notes the phase in timed_out."""
    try:
        have = agent.spell_ids(deadline)
    except Exception:  # noqa: BLE001 - an unreadable spell list: learn them all, verified below
        have = set()
    todo = [sp for sp in spells if sp not in have]
    initial = list(todo)
    for _attempt in (1, 2):
        if not todo:
            break
        if deadline.expired():
            timed_out.append("proficiency learn")
            break
        for sp in todo:
            if deadline.expired():
                timed_out.append("proficiency learn")
                break
            guard.send("learn", ctx.player_guid, sp)
        time.sleep(deadline.cap(settle))

        def known():
            try:
                return set(todo) <= agent.spell_ids(deadline)
            except Exception:  # noqa: BLE001
                return False
        wait_until(known, timeout=deadline.cap(wait), poll=poll)
        try:
            have = agent.spell_ids(deadline)
        except Exception:  # noqa: BLE001
            have = set()
        todo = [sp for sp in todo if sp not in have]
    learned = [sp for sp in initial if sp not in todo]
    return learned, todo


def check_player_gear(agent, trial, guard, ctx, kits=None, settle=1.0, wait=5.0, poll=0.25, deadline=None):
    """`player.gear: "tier"`: equip the player's gear kit (granting only what is missing), read it back.

    The player of a tank or DPS trial is a GM-levelled character with no items: its threat is far below a
    real tank's. The kit (trl/gear.py, data/gear_kits.json) is put on in this order: the armor and weapon
    proficiencies the kit needs are learned first (a GM-levelled warrior has no Plate Mail: 8 of 11 plate
    pieces stayed in the bags); an item already worn stays; one already in the bags (a kit left by an earlier
    repeat or trial) is equipped without a new grant; only an item that is neither is granted with the guarded
    self-only `.additem`. So a repeat never piles up duplicates. The equip waits for the grant to show in the
    bags, and the read-back waits for the equip to show on the body (an equip sent before the bag read showed
    the item, or a worn read right behind the equip, left one piece "unworn" at random); a piece still off
    and not refused gets its equip sent again, twice at most. A piece the server REFUSED (no proficiency,
    wrong slot) is not retried or waited for. A kit entry without item class data fails the check ("kit data
    lacks item class"), never a guess from the slot. All waits share one deadline (`deadline`, default
    unbounded here, GEAR_SETUP_BUDGET_S in run_setup, shared with the carry-over strip and both `_retry`
    passes); when it runs out the sends stop and the evidence names the phase. Verified from the `equipment`
    read: every kit item must be worn; the evidence carries the count and the kit's armor total (the agent
    has no stats read, so the figure is the sum of the worn items' armor from the kit data). A missing kit,
    an unreadable equipment or inventory list, or a missing item fails the check with the reason.
    """
    deadline = deadline or _no_deadline()
    timed_out = []
    prev_guard_deadline, guard.deadline = guard.deadline, deadline
    try:
        return _check_player_gear(agent, trial, guard, ctx, kits, settle, wait, poll, deadline, timed_out)
    finally:
        guard.deadline = prev_guard_deadline


def _check_player_gear(agent, trial, guard, ctx, kits, settle, wait, poll, deadline, timed_out):
    player = trial["player"]
    items, kit_level, why = gear_mod.pick_kit(player, kits)
    if not items:
        return row("player_gear", False, None, why)
    lacking = gear_mod.missing_class(items)
    if lacking:
        return row("player_gear", False, None, "kit data lacks item class: %s (rebuild data/gear_kits.json)"
                   % ", ".join("%s (%d)" % (i.get("name"), int(i["entry"])) for i in lacking[:5]))
    worn, err = worn_entries(agent, deadline)
    if worn is None:
        return row("player_gear", False, None, "cannot verify the gear: %s" % err)
    bags, err = bagged_entries(agent, deadline)
    if bags is None:
        return row("player_gear", False, None, "cannot read the bags: %s" % err)
    to_equip = [i for i in items if int(i["entry"]) not in worn]
    to_grant = [i for i in to_equip if bags.get(int(i["entry"]), 0) < 1]
    learned, unlearned = [], []
    if to_equip:
        learned, unlearned = _learn_proficiencies(agent, gear_mod.proficiency_spells(items), guard, ctx,
                                                  settle, wait, poll, deadline, timed_out)
    granted_now = []
    for item in to_grant:
        if deadline.expired():
            timed_out.append("grant")
            break
        guard.send("additem_self", ctx.player_guid, int(item["entry"]), 1)
        granted_now.append(int(item["entry"]))
    if granted_now:
        time.sleep(deadline.cap(settle))
        wait_until(lambda: (lambda b: b is not None and all(e in b for e in granted_now))(bagged_entries(agent, deadline)[0]),
                   timeout=deadline.cap(wait), poll=poll)
    errors = {}                 # entry -> the server's refusal: not retried, not waited for
    pending = to_equip
    for _attempt in range(3):
        live = [i for i in pending if int(i["entry"]) not in errors]
        if not live:
            break
        if deadline.expired():
            timed_out.append("equip")
            break
        for item in live:
            if deadline.expired():
                timed_out.append("equip")
                break
            out = agent.rpc("equip", {"item_entry": int(item["entry"])}, **rpc_timeout(deadline))
            if isinstance(out, dict) and "_err" in out:
                errors[int(item["entry"])] = "%s (%d): %s" % (item["name"], item["entry"], str(out["_err"])[:80])
        waiting = [int(i["entry"]) for i in live if int(i["entry"]) not in errors]
        if waiting and not _wait_worn(agent, waiting, deadline.cap(wait), poll, deadline):
            if deadline.expired():
                timed_out.append("equip read-back")
        worn, err = worn_entries(agent, deadline)
        if worn is None:
            return row("player_gear", False, None, "cannot verify the gear: %s" % err)
        pending = [i for i in pending if int(i["entry"]) not in worn]
        if deadline.expired():
            if any(int(i["entry"]) not in errors for i in pending) and "equip read-back" not in timed_out:
                timed_out.append("equip read-back")
            break
    failures = [errors[int(i["entry"])] for i in pending if int(i["entry"]) in errors]
    worn, err = worn_entries(agent, deadline)
    if worn is None:
        return row("player_gear", False, None, "cannot verify the gear: %s" % err)
    missing = [i for i in items if int(i["entry"]) not in worn]
    armor = sum(int(i.get("armor") or 0) for i in items if int(i["entry"]) in worn)
    equipped = len(items) - len(missing)
    role = gear_mod.role_of(player)
    evidence = "%s %s kit (level %d): %d/%d items worn (%d granted, %d equipped from the bags), kit armor %d" % (
        player.get("class"), role, kit_level, equipped, len(items), len(granted_now),
        len(to_equip) - len(to_grant), armor)
    if learned:
        evidence += "; learned proficiency spell(s) %s" % ", ".join(str(sp) for sp in learned)
    if unlearned:
        evidence += "; proficiency NOT learned: %s" % ", ".join(str(sp) for sp in unlearned)
    if missing:
        evidence += "; not worn: %s" % ", ".join("%s (%d)" % (i["name"], i["entry"]) for i in missing)
    if failures:
        evidence += "; equip errors: %s" % " | ".join(failures[:3])
    evidence += _timeout_evidence(list(dict.fromkeys(timed_out)), deadline)
    return row("player_gear", not missing, {"worn": equipped, "kit": len(items), "armor": armor,
                                            "granted": len(granted_now), "learned": learned}, evidence)


def strip_player_gear(agent, guard, ctx, kits=None, keep=None, row_id="player_gear_strip",
                      wait=5.0, poll=0.25, deadline=None):
    """Take kit items off the player before a trial's own gear step.

    A trial WITHOUT `player.gear` starts from a player that wears no kit item (keep is empty). A trial WITH
    `player.gear` passes its own kit as `keep`: the slot character is kept between trials, so a level-40
    kit left by the last trial was still on (or in the bags of) the level-50 trial's player and failed
    every rep. Only the FOREIGN kit entries are taken off: every entry of any kit that is not in `keep`,
    worn or in the bags, with the guarded self-only `.additem <id> -1` (the core takes equipped items away
    too), once per copy. The trial's own kit entries are never touched, and a player that carries none of
    the foreign entries gets no GM line at all. Slot characters are bench-owned (DECISIONS.md), so the kit
    entries on them are the bench's own. The read-back waits for the bags and the body to settle (a removal is
    a packet too), within the shared deadline, then the `equipment` and `inventory` reads must show no foreign
    entry left. Items that belong to no kit (a character's own starting gear) are not touched; the evidence
    counts what the player still wears. An agent without the reads cannot be checked: the row passes and
    says UNVERIFIED, like the off-hand read.
    """
    deadline = deadline or _no_deadline()
    timed_out = []
    prev_guard_deadline, guard.deadline = guard.deadline, deadline
    try:
        return _strip_player_gear(agent, guard, ctx, kits, keep, row_id, wait, poll, deadline, timed_out)
    finally:
        guard.deadline = prev_guard_deadline


def _strip_player_gear(agent, guard, ctx, kits, keep, row_id, wait, poll, deadline, timed_out):
    strip_ids = set(ctx.strip_ids) if ctx.strip_ids else gear_mod.all_entries(kits)
    keep = set(keep or ())
    targets = strip_ids - keep
    worn, err = worn_entries(agent, deadline)
    bags, err2 = bagged_entries(agent, deadline)
    if worn is None or bags is None:
        return row(row_id, True, "unreadable",
                   "UNVERIFIED: %s" % (err or err2))
    if keep and not (targets & (worn | set(bags))):
        return row(row_id, True, {"removed": 0, "left": 0, "other_worn": len(worn - strip_ids)},
                   "no kit item of another trial on the player")
    removed = []
    for entry in sorted(targets & worn):
        if deadline.expired():
            timed_out.append("strip")
            break
        guard.send("additem_self", ctx.player_guid, entry, -1)
        removed.append(entry)
    for entry in sorted(targets & set(bags)):
        for _ in range(bags[entry]):
            if deadline.expired():
                timed_out.append("strip")
                break
            guard.send("additem_self", ctx.player_guid, entry, -1)
            removed.append(entry)

    def reads():
        w, e1 = worn_entries(agent, deadline)
        b, e2 = bagged_entries(agent, deadline)
        return w, b, (e1 or e2)

    def clean():
        w, b, _e = reads()
        return w is not None and b is not None and not (targets & w) and not (targets & set(b))
    if removed and not clean():
        wait_until(clean, timeout=deadline.cap(wait), poll=poll)
        if deadline.expired():
            timed_out.append("strip read-back")
    worn, bags, err = reads()
    if worn is None or bags is None:
        return row(row_id, False, None, "cannot verify the strip: %s" % err)
    left = sorted((targets & worn) | (targets & set(bags)))
    evidence = "removed %d kit item(s); %d kit item(s) left; %d other item(s) worn" % (
        len(removed), len(left), len(worn - strip_ids))
    if left:
        evidence += " (kit entries left: %s)" % ", ".join(str(e) for e in left[:8])
    evidence += _timeout_evidence(list(dict.fromkeys(timed_out)), deadline)
    return row(row_id, not left, {"removed": len(removed), "left": len(left),
                                  "other_worn": len(worn - strip_ids)}, evidence)


def check_talents(agent, trial, guard, ctx, settle=1.0):
    tal = trial["player"].get("talents") or {}
    spells = [int(s) for s in tal.get("spells") or []]
    level = int(trial["player"].get("level") or 0)
    if not spells:
        if tal.get("tree") == "none" and level < 10:
            return row("player_talents", True, [], "no talents (tree none, level < 10)")
        return row("player_talents", False, [], "talents missing: tree %r needs spells" % tal.get("tree"))
    before = agent.snapshot()
    guard.send("reset_talents", ctx.player_guid)
    for sid in spells:
        guard.send("learn", ctx.player_guid, sid)
    time.sleep(settle)
    lines, _rows = agent.read_lines(before, timeout=1.5)
    have = agent.spell_ids()
    missing = [sid for sid in spells if sid not in have]
    if missing:
        # a single `.learn` can answer "Player not found!" right after the reset (the selection raced the
        # reset): learn only the missing ones again, once, with a fresh selection each
        before = agent.snapshot()
        for sid in missing:
            guard.send("learn", ctx.player_guid, sid)
        time.sleep(settle)
        retry_lines, _rows = agent.read_lines(before, timeout=1.5)
        lines = list(lines) + ["retry: %s" % l for l in retry_lines]
        have = agent.spell_ids()
        missing = [sid for sid in spells if sid not in have]
    if missing:
        return row("player_talents", False, missing,
                   "talents not learned: %s | server: %s" % (", ".join(str(x) for x in missing),
                                                             " / ".join(lines[-4:]) or "no reply"))
    return row("player_talents", True, spells, "spells RPC holds %s" % ", ".join(str(x) for x in spells))


def check_stance(agent, trial, guard, ctx, timeout=3.0, poll=0.2):
    stance = trial["player"].get("stance")
    if not stance:
        return row("player_stance", True, None, "no stance/form for this trial")
    stance = int(stance)
    try:
        guard.send("unaura", ctx.player_guid, "all")
        # a GM-levelled character never learned its later class spells: learn the stance
        # (and the threat spell the fight will cast) before proving it
        guard.send("learn", ctx.player_guid, stance)
        threat = THREAT_CAST.get(stance)
        if threat:
            guard.send("learn", ctx.player_guid, threat)
    except GmRefused as exc:
        return row("player_stance", False, stance, "unaura/learn refused: %s" % exc)
    detail = ""
    # live3 (run 045710 reps 2-4, reason 42 line_of_sight): `cast` with no target sends the
    # CLIENT'S SELECTION (internal/session/actions.go Cast: t == 0 -> s.target), and every guarded
    # GM line above restores the previous selection afterwards, which after repeat 1 is the dead
    # mob 17 yd away behind terrain. A self cast names the player itself.
    self_guid = norm_guid(ctx.player_guid) if ctx is not None else ""
    for _attempt in (1, 2):
        out = agent.cast(stance, target=self_guid or 0)
        if isinstance(out, dict) and out.get("_err"):
            detail = str(out["_err"])[:200]
        if wait_until(lambda: stance in _aura_ids(agent.own_row()), timeout=timeout, poll=poll):
            return row("player_stance", True, stance, "aura %d held" % stance)
    own = agent.own_row()
    cast = agent.rpc("cast_state") or {}
    try:
        selected = norm_guid((agent.status() or {}).get("target") or "")
    except Exception:  # noqa: BLE001 - evidence only
        selected = ""
    return row("player_stance", False, stance,
               "stance not held: aura %d missing after 2 casts (auras %s, cast_state %s, cast %s, selected %s)"
               % (stance, sorted(_aura_ids(own)), cast.get("last_failure"), detail, selected or "none"))


def read_offhand(agent):
    """The player's off-hand item: {"readable", "item_entry", "evidence"}.

    The agent has NO read of the worn gear: `inventory` lists the backpack and bags only
    (README `inventory`; Session.Inventory = Backpack), `equip` is an action. The read below
    asks for an `equipment` rpc (rows `{slot, item_entry}` like `inventory`, slots 0-18; the
    off-hand is slot 16); an agent without it answers an error and the off-hand is UNREADABLE,
    never "empty".
    """
    out = agent.rpc("equipment", {})
    if isinstance(out, dict) and "_err" in out:
        return {"readable": False, "item_entry": None, "slot": EQUIPMENT_OFFHAND_SLOT,
                "evidence": "no equipment read in this agent: %s" % str(out["_err"])[:120]}
    rows = out.get("items") if isinstance(out, dict) else out
    if not isinstance(rows, list):
        return {"readable": False, "item_entry": None, "slot": EQUIPMENT_OFFHAND_SLOT,
                "evidence": "equipment reply is not a list"}
    for item in rows:
        if isinstance(item, dict) and int(item.get("slot", -1)) == EQUIPMENT_OFFHAND_SLOT \
                and int(item.get("item_entry") or 0):
            return {"readable": True, "item_entry": int(item["item_entry"]), "slot": EQUIPMENT_OFFHAND_SLOT,
                    "evidence": "off-hand holds item %d" % int(item["item_entry"])}
    return {"readable": True, "item_entry": None, "slot": EQUIPMENT_OFFHAND_SLOT,
            "evidence": "off-hand slot is empty"}


def check_player_no_shield(offhand):
    """No item in the player's off-hand: a shield makes GetRoleFromStanceOrGear read tank.

    Fails when the off-hand holds anything (an Arms/Battle trial has no use for any off-hand
    item, and the item type is not in the tool's data). An UNREADABLE off-hand is NOT proof:
    the row passes but says UNVERIFIED, so a card cannot read it as a checked fact.
    """
    if not offhand.get("readable"):
        return row("player_no_shield", True, "unreadable",
                   "UNVERIFIED: %s" % offhand.get("evidence"))
    entry = offhand.get("item_entry")
    if entry:
        return row("player_no_shield", False, entry,
                   "off-hand holds item %d (a shield reads as tank at AiFactory.cpp GetRoleFromStanceOrGear)"
                   % entry)
    return row("player_no_shield", True, None, "off-hand slot is empty")


def read_bot_offhand(agent, name):
    """The off-hand (slot 16) of a hire, from its `.bot inv` listing (TBM:INV_EQ|slot|item|...)."""
    since = agent.tbm("inv %s" % name)
    want = name.strip().lower()

    def done(text):
        m = INV_END.search(text)
        return bool(m) and m.group(1).strip().lower() == want
    lines, _rows = agent.read_lines(since, timeout=6.0, stop=done)
    if not any(done(t) for t in lines):
        return {"readable": False, "item_entry": None, "slot": EQUIPMENT_OFFHAND_SLOT,
                "evidence": "no complete .bot inv %s listing" % name}
    span, inside = [], False
    for line in lines:
        m = INV_BEGIN.search(line)
        if m:
            inside = m.group(1).strip().lower() == want
            continue
        if INV_END.search(line):
            inside = False
            continue
        if inside:
            span.append(line)
    for line in span:
        m = INV_EQ.search(line)
        if m and int(m.group(1)) == EQUIPMENT_OFFHAND_SLOT and int(m.group(2)):
            return {"readable": True, "item_entry": int(m.group(2)), "slot": EQUIPMENT_OFFHAND_SLOT,
                    "evidence": "off-hand holds item %s" % m.group(2)}
    return {"readable": True, "item_entry": None, "slot": EQUIPMENT_OFFHAND_SLOT,
            "evidence": "off-hand slot is empty"}


def check_player_auras(agent, trial, guard, ctx, timeout=3.0):
    wanted = [int(x) for x in (trial["player"].get("auras") or [])]
    if not wanted:
        return row("player_auras", True, [], "no extra player auras")
    for spell in wanted:
        guard.send("aura", ctx.player_guid, spell)
    have = _aura_ids(agent.own_row())
    missing = [s for s in wanted if s not in have]
    ok = not missing
    return row("player_auras", ok, sorted(have), "auras %s%s" % (
        sorted(have), "" if ok else "; missing %s" % missing))


def check_forced_role(agent, trial, guard, ledger, timeout=8.0):
    role = trial["player"].get("forced_role")
    cmd = "role self %s" % role if role else "role self clear"
    since = agent.tbm(cmd)
    line, _rows = agent.wait_reply(since, r"Your role set to \w+", timeout=timeout)
    ledger.add({"kind": "role", "role": role or "clear", "reply": line or ""})
    if not line:
        return row("player_forced_role", False, role, "no 'Your role set to' reply to .bot %s" % cmd)
    return row("player_forced_role", True, role or "clear", line)


def party_guid(agent, name, deadline=None):
    """The guid of a party member by name, from the party RPC only (never chat)."""
    want = (name or "").strip().lower()
    if not want:
        return ""
    for member in (agent.members(deadline) if deadline is not None else agent.members()):
        if (member.get("name") or "").strip().lower() == want:
            return norm_guid(member.get("guid") or "")
    return ""


def _hire(agent, bot, ledger, timeout, previous, extra=None):
    """The one hire path (main bot and extra bots): (row, name).

    The identity comes from the server's `system` reply and the party list only.
    `extra` is the 1-based extra_bots index, recorded in the ledger so cleanup can
    replay the removal; the main hire's ledger row carries no such key.
    """
    if previous and party_guid(agent, previous):
        return row("bot_present", True, previous, "reused hire %s from the previous repeat" % previous), previous
    cmd = "hire %s %s level=%d" % (bot["class"], bot["role"], int(bot["level"]))
    since = agent.tbm(cmd)
    # the hire identity comes from the server's `system` reply and the party list only:
    # a whisper/chat line claiming a hire must have no effect
    line, _rows = agent.wait_reply(since, r"hired\s+[A-Za-z]{2,12}\s+for|" + HIRE_REFUSAL.pattern,
                                   timeout=timeout, kinds=("system",))
    if not line:
        return row("bot_present", False, None,
                   "no system reply to .bot %s in %.0f s" % (cmd, timeout)), None
    m = HIRED.search(line)
    if not m:
        return row("bot_present", False, None, "hire refused: %s" % line), None
    name = m.group(1)
    guid = ""
    if wait_until(lambda: party_guid(agent, name), timeout=timeout, poll=0.5):
        guid = party_guid(agent, name)
    entry = {"kind": "hire", "name": name, "class": bot["class"], "role": bot["role"],
             "level": int(bot["level"]), "guid": guid, "reply": line}
    if extra:
        entry["extra"] = int(extra)
    ledger.add(entry)
    if not guid:
        return row("bot_present", False, name,
                   "system reply named %s but it is not a party member (party has no guid): %s"
                   % (name, line)), None
    return row("bot_present", True, name, "%s (party guid %s)" % (line, guid)), name


def hire_bot(agent, trial, guard, ledger, timeout=30.0, previous=None):
    return _hire(agent, trial["bot"], ledger, timeout, previous)


def _apply_role(agent, spec, name, ledger, timeout=15.0):
    since = agent.tbm("role %s %s" % (name, spec))
    line, _rows = agent.wait_reply(since, r"Bot \S+ role set to|does not use that spec|has no AI yet",
                                   timeout=timeout)
    ledger.add({"kind": "bot_role", "bot": name, "spec": spec, "reply": line or ""})
    if not line or not re.search(r"role set to", line, re.I):
        return row("bot_role", False, spec, line or "no 'role set to' reply for .bot role %s %s" % (name, spec))
    return row("bot_role", True, spec, line)


def apply_spec(agent, trial, name, ledger, timeout=15.0):
    return _apply_role(agent, trial["bot"]["spec"], name, ledger, timeout)


TALENTS_APPLY_OK = re.compile(r"Apply spec", re.I)
# an apply the bot says failed: several premade specs match, none does (TB spells it "not fount"), the spec is
# invalid, or it needs a higher level
TALENTS_APPLY_FAIL = re.compile(r"Found multiple specs|not fou|spec is invalid|higher level", re.I)
TALENTS_APPLY_REPLY = TALENTS_APPLY_OK.pattern + "|" + TALENTS_APPLY_FAIL.pattern
TALENTS_HIRE_KINDS = ("whisper", "party", "chat")
TALENT_BUDGET_S = 60.0          # per hired bot: the talent apply, the read-back and the gear read, both `_retry` passes


def _from_hire(row, name, guid=""):
    """True when the event row was said by THIS hire: its party guid when the row carries one, else its name.
    A row with no sender, another hire's reply, the player's own line and system text are never the hire's."""
    if row.get("kind") not in TALENTS_HIRE_KINDS:
        return False
    row_guid = event_guid(row, "source")
    if row_guid and guid:
        return row_guid == norm_guid(guid)
    sender = event_name(row, "source").strip().lower()
    return bool(sender) and sender == str(name or "").strip().lower()


def _hire_texts(rows, name, guid=""):
    return [event_text(r) for r in rows if _from_hire(r, name, guid) and event_text(r)]


def apply_talents(agent, bot, name, ledger, timeout=12.0, deadline=None, guid=""):
    """Whisper `talents <spec>` to a hired bot: the bot takes the premade talent build of that name.

    `.bot role <bot> <spec>` rebuilds the strategies only, so a hire keeps the random talent build it was
    created with (a mage: arcane, fire or frost 1/3 each) and a fire trial could run on an arcane mage
    (TB ChangeTalentsAction: `talents <premade name>` applies the class's premade spec of that name).
    Returns the HIRE's own reply text ('' when it did not answer; another bot's reply never counts), or None
    when the bot names no spec the table knows (nothing is sent). The verdict is `check_bot_talents`'s.
    """
    spec = (bot or {}).get("spec")
    if not spec or talents_mod.marker_for((bot or {}).get("class"), spec) is None:
        return None
    deadline = deadline or _no_deadline()
    since = agent.snapshot(deadline)
    agent.chat("whisper", "talents %s" % talents_mod.premade_name(spec), to=name, deadline=deadline)

    def answered(rows):
        return any(TALENTS_APPLY_OK.search(t) or TALENTS_APPLY_FAIL.search(t) for t in _hire_texts(rows, name, guid))
    rows = agent.read_rows(since, timeout=deadline.cap(timeout), stop=answered, deadline=deadline)
    texts = _hire_texts(rows, name, guid)
    line = next((t for t in texts if TALENTS_APPLY_FAIL.search(t)), None) \
        or next((t for t in texts if TALENTS_APPLY_OK.search(t)), "")
    ledger.add({"kind": "bot_talents", "bot": name, "spec": spec, "reply": line or ""})
    return line or ""


def read_bot_talents(agent, name, timeout=12.0, deadline=None, guid="", timed_out=None):
    """(raw link or None, reply text, ignored): the `Link:` of the HIRE's own `talents` reply.

    Only an event row said by this hire counts (guid, else name; see _from_hire): another bot's reply, the
    player's own line and a party-fallback answer from someone else are ignored and counted in `ignored`."""
    deadline = deadline or _no_deadline()
    seen = {"ignored": 0}

    def hire_link(rows):
        return any(talents_mod.link_text(t) is not None for t in _hire_texts(rows, name, guid))

    def pick(rows):
        for r in rows:
            text = event_text(r)
            if talents_mod.link_text(text) is None:
                continue
            if _from_hire(r, name, guid):
                return text
            seen["ignored"] += 1
        return None

    timed_out = [] if timed_out is None else timed_out
    before = agent.snapshot(deadline)
    agent.chat("whisper", "talents", to=name, deadline=deadline)
    rows = agent.read_rows(before, timeout=deadline.cap(timeout), stop=hire_link, deadline=deadline)
    text = pick(rows)
    if text is None and deadline.expired():
        timed_out.append("talent read-back")
    elif text is None:
        agent.say_to_bot(name, "talents", deadline=deadline)
        rows = agent.read_rows(before, timeout=deadline.cap(timeout), stop=hire_link, deadline=deadline)
        seen["ignored"] = 0
        text = pick(rows)
        if text is None and deadline.expired():
            timed_out.append("party fallback")
    if text is None:
        return None, " / ".join(_hire_texts(rows, name, guid))[-300:], seen["ignored"]
    return talents_mod.link_text(text), text, seen["ignored"]


def check_bot_talents(agent, bot, name, ledger, timeout=12.0, deadline=None, guid=None):
    """`bot_talents`: send the spec's premade talents to the hire, then prove the build from the bot's own read.

    ALL of these must hold, else the rep is INVALID with the evidence (never a silent score):
      * the reply is the hire's own and carries a well-formed link (guid or name bound);
      * the spec's marker talent (trl/talents.py: Fire Vulnerability for a fire mage) is at rank 1 or more;
      * the spec's own tree holds at least SPEC_SHARE_MIN (60 %) of all spent points (a stale hybrid that has
        the marker but spends its points elsewhere is not the spec; exact premade links are not required,
        they differ by level);
      * the apply reply is not a failure: "Found multiple specs", "not found", an invalid or too-high spec
        fail the row whatever the build looks like; NO apply reply fails it only when the build also fails
        the checks above.
    A trial spec with no marker is INVALID too ("no talent marker for <class>/<spec>"), not a silent pass.
    The whole check (apply, read-back, gear read) runs under `deadline` (TALENT_BUDGET_S per bot by the
    caller). The hire's gear after the apply is recorded in the evidence (TB re-gears on a build change).
    None when the bot names no spec at all.
    """
    spec = (bot or {}).get("spec")
    klass = (bot or {}).get("class")
    if not spec:
        return None
    marker = talents_mod.marker_for(klass, spec)
    if marker is None:
        return row("bot_talents", False, spec,
                   "no talent marker for %s/%s (trl/talents.py): the build cannot be proven" % (klass, spec))
    deadline = deadline or _no_deadline()
    timed_out = []
    if guid is None:
        guid = party_guid(agent, name, deadline)
        if deadline.expired():
            return row("bot_talents", False, spec, "%s: no time left to read the build%s" % (
                name, _timeout_evidence(["party read"], deadline)))
    reply = apply_talents(agent, bot, name, ledger, timeout=timeout, deadline=deadline, guid=guid)
    if deadline.expired():
        return row("bot_talents", False, spec, "%s: no time left to read the build%s" % (
            name, _timeout_evidence(["talent apply"], deadline)))
    raw, text, ignored = read_bot_talents(agent, name, timeout=timeout, deadline=deadline, guid=guid,
                                          timed_out=timed_out)
    skipped = "; ignored %d link reply(ies) not from %s" % (ignored, name) if ignored else ""
    if raw is None:
        return row("bot_talents", False, spec,
                   "no talent link from %s in reply to `talents` (wanted %s, marker %s); apply reply: %s; read: %s%s%s"
                   % (name, spec, marker.name, reply or "none", text or "no reply from the hire", skipped,
                      _timeout_evidence(timed_out, deadline)))
    trees = talents_mod.parse_link(text)
    if trees is None:
        return row("bot_talents", False, spec, "malformed talent link %r from %s (wanted %s)%s"
                   % (raw[:40], name, spec, skipped))
    rank = talents_mod.marker_rank(trees, marker)
    own, total, share = talents_mod.spec_share(trees, marker)
    link = "-".join(trees).rstrip("-") or "0"
    build_ok = rank >= 1 and share >= talents_mod.SPEC_SHARE_MIN
    apply_failed = bool(reply and TALENTS_APPLY_FAIL.search(reply))
    apply_state = "failed" if apply_failed else "ok" if reply else "no reply"
    gear = ""
    if not deadline.expired():
        entries, complete = read_inventory(agent, name, timeout=deadline.cap(6.0), deadline=deadline)
        gear = "; gear after the apply: %s" % (
            "%d item(s) equipped or bagged" % len(entries) if complete else "unreadable (no complete `.bot inv`)")
        if not complete and deadline.expired():
            gear += _timeout_evidence(["gear read"], deadline)
    ok = build_ok and not apply_failed
    detail = "%s %s: marker %s rank %d/%d, %d of %d points in the %s tree (%.0f%%), link %s; apply %s%s" % (
        name, spec, marker.name, rank, len(marker.ranks), own, total, spec, share * 100, link,
        "FAILED: %s" % reply if apply_failed else "ok" if reply else "gave no reply", skipped)
    if not ok:
        why = []
        if rank < 1:
            why.append("lacks %s (rank 0)" % marker.name)
        if share < talents_mod.SPEC_SHARE_MIN:
            why.append("only %.0f%% of its points are in the %s tree (need %.0f%%)"
                       % (share * 100, spec, talents_mod.SPEC_SHARE_MIN * 100))
        if apply_failed:
            why.append("the apply failed")
        detail = "%s wanted %s but %s; %s" % (name, spec, " and ".join(why), detail)
    return row("bot_talents", ok, {"spec": spec, "marker": marker.name, "rank": rank, "link": link,
                                   "tree_share": round(share, 3), "apply": apply_state},
               detail + gear)


def check_bot_level(agent, trial, name):
    unit = agent.unit(name=name)
    if not unit:
        return row("bot_level", False, None, "%s not in view" % name)
    want = int(trial["bot"]["level"])
    have = int(unit.get("level") or 0)
    percent = 100.0 * (unit.get("health") or 0) / max(1, unit.get("max_health") or 1)
    return row("bot_level", have == want, have,
               "%s level %d (target %d), hp %.0f%%" % (name, have, want, percent))


def _strategy_lines(texts):
    return [t for t in texts if "strategies:" in t.lower()]


def _check_strategy(agent, spec, name, timeout=12.0):
    """The bot lists the wanted strategy in a `... Strategies:` line of ITS reply.

    The read starts at the log head taken right before the `co ?` (agent.snapshot), and
    the verdict reads only the `Strategies:` lines: the reply is interleaved with other
    system lines (inventory, role, greeting text that names the spec) and an older bot's
    Strategies line must not stand in for this one.
    """
    want = SPEC_STRATEGY.get(spec, spec)
    needle = want.lower()

    def has_want(text):
        return "strategies:" in text.lower() and needle in text.lower()

    before = agent.snapshot()
    agent.say_to_bot(name, "co ?")
    texts, rows = agent.read_lines(before, timeout=timeout, stop=has_want)
    if not _strategy_lines(texts):
        agent.chat("whisper", "co ?", to=name)
        more, more_rows = agent.read_lines(before, timeout=timeout, stop=has_want)
        texts, rows = more, more_rows
    strat = _strategy_lines(texts)
    joined = " / ".join(texts)
    if not strat:
        return row("bot_spec", False, spec,
                   "no 'Strategies:' reply from %s to co ? (%s)" % (name, joined[-300:] or "no chat line"))
    ok = any(has_want(t) for t in strat)
    return row("bot_spec", ok, spec,
               " / ".join(strat)[-400:] if ok
               else "%s (wanted strategy %r)" % (" / ".join(strat)[-300:], want))


def check_bot_spec(agent, trial, name, timeout=12.0):
    return _check_strategy(agent, trial["bot"]["spec"], name, timeout)


def _item_row(text, wanted):
    """(id, line) of the row whose item name equals `wanted` exactly, else None."""
    for rx in (ITEM_LINK_NAME, ITEM_WORD_NAME):
        for m in rx.finditer(text or ""):
            if " ".join(m.group(2).split()).lower() == wanted:
                return int(m.group(1))
    return None


def lookup_item(agent, guard, name, timeout=6.0):
    wanted = " ".join(str(name or "").split()).lower()
    before = agent.snapshot()
    guard.send("lookup_item", None, name)
    lines, _rows = agent.read_raw_lines(before, timeout=timeout,
                                        stop=lambda t: _item_row(t, wanted) is not None)
    for line in lines:
        item_id = _item_row(line, wanted)
        if item_id is not None:
            return item_id, line
    return None, " / ".join(lines[-3:])

INV_ITEM = re.compile(r"TBM:INV_ITEM\|(\d+)\|(\d+)\|(\d+)\|(\d+)")
INV_EQ = re.compile(r"TBM:INV_EQ\|(\d+)\|(\d+)")
INV_BEGIN = re.compile(r"TBM:INV_BEGIN\|([^|]+)\|")
INV_END = re.compile(r"TBM:INV_END\|([^|]+)\|")


def inventory_entries(lines, name=None):
    """entry -> count from a `.bot inv` stream, equipped items counted once.

    With `name`, only the span INV_BEGIN|name .. INV_END|name is read: the stream is
    interleaved with other system lines, and another hire's inventory in the same
    window must not count for this one. Without a BEGIN line the whole window is read.
    """
    want = (name or "").strip().lower()
    spans = bool(want) and any(INV_BEGIN.search(line) for line in lines)
    got = {}
    inside = not spans
    for line in lines:
        if spans:
            m = INV_BEGIN.search(line)
            if m:
                inside = m.group(1).strip().lower() == want
                continue
            m = INV_END.search(line)
            if m:
                if m.group(1).strip().lower() == want:
                    inside = False
                continue
            if not inside:
                continue
        m = INV_ITEM.search(line)
        if m:
            entry = int(m.group(3))
            got[entry] = got.get(entry, 0) + int(m.group(4))
            continue
        m = INV_EQ.search(line)
        if m:
            got.setdefault(int(m.group(2)), 1)
    return got


def read_inventory(agent, name, timeout=6.0, deadline=None):
    """One `.bot inv <name>` read: (entry -> count, complete).

    complete = the whole INV_BEGIN|name .. INV_END|name span arrived. A timed-out or
    partial read is NOT a listing that proves anything is missing.
    """
    since = agent.tbm("inv %s" % name, deadline=deadline) if deadline is not None else agent.tbm("inv %s" % name)
    if deadline is not None and deadline.expired():
        return {}, False
    want = name.strip().lower()

    def done(text):
        m = INV_END.search(text)
        return bool(m) and m.group(1).strip().lower() == want

    def begun(text):
        m = INV_BEGIN.search(text)
        return bool(m) and m.group(1).strip().lower() == want
    lines, _rows = (agent.read_lines(since, timeout=timeout, stop=done, deadline=deadline) if deadline is not None
                    else agent.read_lines(since, timeout=timeout, stop=done))
    complete = any(begun(t) for t in lines) and any(done(t) for t in lines)
    return inventory_entries(lines, name), complete


def _items_short(items, ids, got):
    """[(item, item_id, shortfall)] for what the listing proves is short (wanted - have)."""
    out = []
    for item, item_id in zip(items, ids):
        short = int(item.get("count", 1)) - got.get(item_id, 0)
        if short > 0:
            out.append((item, item_id, short))
    return out


MAX_GRANTS = 2


def check_items(agent, trial, guard, ctx, ledger, name, timeout=6.0, bot=None, settle=(0.5, 1.0, 1.5, 2.0),
                sleep=time.sleep):
    """Every wanted item is in `.bot inv <name>`.

    The `.additem` lands a beat AFTER the `.bot inv` that follows it (live1: the first
    listing lacked the stack, the next one carried it), and a reused hire may have lost
    the stack since the last repeat. So: read first and add only what is missing, then
    re-read on a short back-off; the stack is added once more only if the whole back-off
    came up empty.
    """
    items = (bot if bot is not None else trial["bot"]).get("items") or []
    if not items:
        return row("bot_items", True, [], "no items for this trial")
    ids = []
    evidence = []
    for item in items:
        item_id, line = lookup_item(agent, guard, item["name"], timeout=timeout)
        if not item_id:
            return row("bot_items", False, ids, "no exact .lookup item row for %r (%s)" % (item["name"], line))
        ids.append(item_id)
        evidence.append("lookup %s -> %d" % (item["name"], item_id))
    ctx.item_ids = frozenset(list(ctx.item_ids) + ids)
    hire = agent.unit(name=name)
    if not hire:
        return row("bot_items", False, ids, "%s not in view for .additem" % name)

    def unreadable(why):
        return row("bot_items", False, ids, "inventory unreadable: %s; nothing was granted" % why
                   if not grants else "inventory unreadable: %s (after %d .additem)" % (why, grants))

    # 1. a COMPLETE listing proves the deficit; a timed-out or partial read proves nothing
    grants = 0
    short = None
    got = {}
    for _attempt in range(2):
        got, complete = read_inventory(agent, name, timeout=timeout)
        if complete:
            short = _items_short(items, ids, got)
            break
    if short is None:
        return unreadable("no complete .bot inv %s listing" % name)
    # 2. grant only the measured shortfall, re-read on a back-off, grant again only if a
    #    complete listing still shows a shortfall (at most MAX_GRANTS grants in all)
    while short:
        if grants >= MAX_GRANTS:
            break
        for item, item_id, count in short:
            try:
                guard.send("additem", hire.get("guid"), item_id, count)
            except GmServerError as exc:
                # the guard sends nothing until the server shows the hire as the selection
                # (live2: an .additem sent on the old selection went to the GM itself)
                return row("bot_items", False, ids, "%s not given .additem: %s" % (name, exc))
        grants += 1
        short = None
        for wait in settle:
            sleep(wait)
            got, complete = read_inventory(agent, name, timeout=timeout)
            if not complete:
                continue
            short = _items_short(items, ids, got)
            if not short:
                break
        if short is None:
            return unreadable("no complete listing after the .additem")
    if short:
        return row("bot_items", False, ids,
                   "not in .bot inv %s: %s after %d .additem (entries %s)"
                   % (name, ", ".join("%s x%d short" % (item["name"], n) for item, _, n in short),
                      grants, sorted(got)[:40]))
    evidence.append(".bot inv %s carries every item%s" % (name, " (added %dx)" % grants if grants else
                                                           " (already carried)"))
    return row("bot_items", True, ids, "; ".join(evidence))


def check_mana(agent, trial, guard, ctx, name):
    pct = trial["bot"].get("mana_pct")
    if pct is None:
        return row("bot_mana", True, None, "not a mana trial")
    unit = agent.unit(name=name)
    if not unit:
        return row("bot_mana", False, None, "%s not in view" % name)
    maxp = int(unit.get("max_power") or unit.get("max_mana") or 0)
    if maxp <= 0:
        return row("bot_mana", False, None, "%s has no power pool" % name)
    value = int(maxp * float(pct) / 100)
    guard.send("modify_mana", unit.get("guid"), value, maxp)
    time.sleep(0.7)
    unit = agent.unit(name=name) or {}
    have = 100.0 * int(unit.get("power") or 0) / max(1, maxp)
    return row("bot_mana", abs(have - float(pct)) <= 3, round(have, 1),
               "power %.0f%% of %d (set %s%%)" % (have, maxp, pct))


# The built-in defaults the core uses when the key is not set in aiplayerbot.conf
# (PlayerbotAIConfig.cpp: GetIntDefault("AiPlayerbot.CriticalHealth", 20) / LowHealth 50 /
# MediumHealth 70; aiplayerbot.conf.dist.in:2674-2676 lists them commented out).
CONFIG_DEFAULTS = {"criticalhealth": 20, "lowhealth": 50, "mediumhealth": 70}
CONFIG_KEYS = (("critical", "criticalhealth"), ("low", "lowhealth"), ("medium", "mediumhealth"))
CONFIG_LINE = re.compile(r"TBM:CONFIG\|([^|]+)\|(-?\d+)\s*$")
CONFIG_END = re.compile(r"TBM:CONFIG_END\|(\d+)\|(\d+)")


def read_config(agent, timeout=10.0):
    """RUN.json thresholds from `.bot features all <key>`.

    That command lists the keys SET in the server's bot config file (Info.cpp
    SendConfigSearch over PlayerbotAIConfig::FindConfigEntries -> config.GetKeys), so a
    threshold left at its built-in default is NOT listed: the reply is
    `Config: 0 keys match` / `TBM:CONFIG_BEGIN|0` / `TBM:CONFIG_END|0|0`. That complete,
    empty answer is authoritative: the key is unset and the core default applies (live1
    read it as "unreadable" in every rep). A listed key wins; no CONFIG_END (or a capped
    listing without the key) stays unreadable. out["source"] records per label whether the
    value came from the file or from the default.
    """
    out = {"critical": None, "low": None, "medium": None, "ok": False, "raw": [], "source": {}}
    for label, key in CONFIG_KEYS:
        since = agent.tbm("features all %s" % key)
        lines, _rows = agent.read_lines(since, timeout=timeout,
                                        stop=lambda t: "TBM:CONFIG_END" in t)
        out["raw"].extend(lines)
        complete = False
        for line in lines:
            m = CONFIG_LINE.match(line.strip())
            if m and m.group(1).lower().split(".")[-1] == key:
                out[label] = int(m.group(2))
                out["source"][label] = "file"
            m = CONFIG_END.search(line)
            if m and int(m.group(1)) >= int(m.group(2)):
                complete = True  # every match was listed: a missing key is really unset
        if out[label] is None and complete:
            out[label] = CONFIG_DEFAULTS[key]
            out["source"][label] = "default"
    out["ok"] = all(out[k] is not None for k in ("critical", "low", "medium"))
    return out


def config_row(cfg):
    if not cfg.get("ok"):
        return row("config_read", False, None,
                   "thresholds unreadable; rules use defaults 20/50/70 (assumed)")
    src = cfg.get("source") or {}
    where = ", ".join("%s %s (%s)" % (k, cfg[k], "set in the config file" if src.get(k) == "file"
                                      else "unset in the config file: core default")
                      for k in ("critical", "low", "medium"))
    return row("config_read", True,
               {"critical": cfg["critical"], "low": cfg["low"], "medium": cfg["medium"]}, where)


def check_grouped(agent, name):
    party = agent.party()
    members = [m.get("name", "").lower() for m in party.get("members") or []]
    ok = bool(party.get("in_group")) and name.lower() in members
    return row("grouped", ok, len(members),
               "in_group=%s, members=%s" % (party.get("in_group"), ", ".join(members)))


def check_extras_grouped(agent, names):
    """Every extra hire is in the party list (and the player is in a group)."""
    party = agent.party()
    members = [m.get("name", "").lower() for m in party.get("members") or []]
    missing = [n for n in names if not n or n.lower() not in members]
    ok = bool(party.get("in_group")) and bool(names) and not missing
    return row("extra_bots_in_group", ok, len(members),
               "in_group=%s, members=%s%s" % (party.get("in_group"), ", ".join(members),
                                              "" if ok else "; not in the party: %s"
                                              % (", ".join(str(n) for n in missing) or "no extra hire")))


def check_pet(agent, trial, name):
    bot_class = (trial["bot"].get("class") or "").lower()
    if bot_class not in PET_CLASSES:
        return row("pet_present", True, None, "%s has no pet" % bot_class)
    hire = agent.unit(name=name) or {}
    pets = [u for u in agent.units(within=60)
            if str(u.get("guid", "")).lower().replace("0x", "").startswith("f140")]
    if not pets:
        return row("pet_present", False, None, "no pet-guid unit within 60 yd of %s" % name)
    pets.sort(key=lambda u: (u.get("distance") if u.get("distance", -1) >= 0 else 999))
    pet = pets[0]
    return row("pet_present", True, pet.get("guid"),
               "pet %s entry %s at %.1f yd (hire at %.1f yd)"
               % (pet.get("guid"), pet.get("entry"), pet.get("distance", -1), hire.get("distance", -1)))


def _extra_row(cid, ok, value, parts):
    return row(cid, ok, value, "; ".join(parts))


def check_extras_no_shield(agent, names):
    """Every extra hire has an empty off-hand (read from its own `.bot inv` listing)."""
    bad, parts = [], []
    for i, name in enumerate(names, start=1):
        if not name:
            bad.append("extra%d: not hired" % i)
            continue
        info = read_bot_offhand(agent, name)
        parts.append("extra%d %s: %s" % (i, name, info["evidence"]))
        if not info["readable"] or info["item_entry"]:
            bad.append("extra%d %s: %s" % (i, name, info["evidence"]))
    return row("extra_bots_no_shield", not bad, [n for n in names if n],
               "; ".join(bad or parts))


def run_extras(agent, trial, guard, ledger, ctx, previous=None, timeout=30.0):
    """Hire and set up every `extra_bots` bot: (rows, names, guids).

    Same rules as the main hire: the identity is the `system` reply plus the party
    list, never chat. Every hire is a ledger row (name + guid, `extra` index). The
    three rows (extra_bots_present / _role / _in_group) pass only when every extra
    bot passes. `names` / `guids` stay aligned with extra_bots ("" for a bot that
    was not hired); ctx.extra["extra_slots"] is the same index-aligned list of
    NAMED hires (None for a slot that never got a name), updated as each hire
    happens so a crash mid-setup still leaves the teardown its names and indices.
    ctx.extra["extra_names"] is the compacted copy (names only, for cleanup heals).
    A slot is reused on the next repeat only by its own index.
    """
    extras = trial.get("extra_bots") or []
    prev = list(previous or [])
    names, guids = [], []
    present_ok, present_ev = True, []
    main_guid = norm_guid(ctx.hire_guid) if ctx.hire_guid else ""
    ctx.extra.setdefault("extra_names", [])
    slots = ctx.extra["extra_slots"] = [None] * len(extras)
    for i, eb in enumerate(extras, start=1):
        prev_name = prev[i - 1] if i - 1 < len(prev) else None
        hire_row, name = _hire(agent, eb, ledger, timeout, prev_name, extra=i)
        named = name or (hire_row.get("value") if isinstance(hire_row.get("value"), str) else None)
        if named:
            slots[i - 1] = named
            if named not in ctx.extra["extra_names"]:
                ctx.extra["extra_names"].append(named)
        guid = party_guid(agent, name) if name else ""
        if name and (not guid or guid == main_guid or guid in guids):
            hire_row = row("bot_present", False, name,
                           "extra bot %s has no distinct party guid (%s)" % (name, guid or "none"))
            guid = ""
            name = None
        if not name:
            present_ok = False
            present_ev.append("extra%d: %s" % (i, hire_row["evidence"]))
            names.append("")
            guids.append("")
            continue
        names.append(name)
        guids.append(guid)
        ctx.extra_hire_guids = frozenset(list(ctx.extra_hire_guids) + [guid])
        present_ev.append("extra%d: %s" % (i, hire_row["evidence"]))
        bring_hire(agent, name)
        if eb.get("items"):
            item_row = _retry(lambda: check_items(agent, trial, guard, ctx, ledger, name, bot=eb))
            if not item_row.get("ok"):
                present_ok = False
                present_ev.append("extra%d items: %s" % (i, item_row["evidence"]))
    rows = [_extra_row("extra_bots_present", present_ok, [n for n in names if n], present_ev)]

    role_ok, role_ev = True, []
    for i, (eb, name) in enumerate(zip(extras, names), start=1):
        if not name:
            role_ok = False
            role_ev.append("extra%d: not hired" % i)
            continue
        talents_dl = Deadline(TALENT_BUDGET_S)
        talents_row = _retry(lambda: check_bot_talents(agent, eb, name, ledger, deadline=talents_dl,
                                                       guid=guids[i - 1] or ""), deadline=talents_dl)
        spec_row = _retry(lambda: _apply_role(agent, eb["spec"], name, ledger))
        strat_row = _retry(lambda: _check_strategy(agent, eb["spec"], name))
        gear = eb.get("gear")
        if gear:
            since = agent.tbm("autogear %s %s" % (name, gear))
            line, _rows = agent.wait_reply(
                since, r"Re-geared|autogear is disabled|refused:|pool bot|must be alive|usage:", timeout=20)
            ledger.add({"kind": "autogear", "bot": name, "gear": gear, "extra": i, "reply": line or ""})
            if not (line and "Re-geared" in line):
                role_ok = False
                role_ev.append("extra%d autogear %s: %s" % (i, gear, line or "no reply"))
                continue
            role_ev.append("extra%d %s: %s | %s%s; autogear %s: %s" % (
                i, name, spec_row["evidence"], strat_row["evidence"],
                " | talents: %s" % talents_row["evidence"] if talents_row is not None else "",
                gear, line))
            talents_ok = talents_row is None or talents_row.get("ok")
            if not spec_row.get("ok") or not strat_row.get("ok") or not talents_ok:
                role_ok = False
            continue
        if not spec_row.get("ok") or not strat_row.get("ok") or (talents_row is not None
                                                                 and not talents_row.get("ok")):
            role_ok = False
        role_ev.append("extra%d %s: %s | %s%s" % (
            i, name, spec_row["evidence"], strat_row["evidence"],
            " | talents: %s" % talents_row["evidence"] if talents_row is not None else ""))
    rows.append(_extra_row("extra_bots_role", role_ok, [eb["spec"] for eb in extras], role_ev))
    rows.append(_retry(lambda: check_extras_grouped(agent, names)))
    if "extra_bots_no_shield" in set(trial.get("setup_checks") or []):
        rows.append(check_extras_no_shield(agent, names))
    return rows, names, guids


def run_setup(agent, trial, guard, ledger, ctx, first=True, previous=None, previous_extras=None):
    """All checks, retried once each; returns setup.json's dict."""
    checks = []
    wanted = set(trial.get("setup_checks") or [])
    guids = {"player": norm_guid(ctx.player_guid), "bot": None, "pet": None, "mobs": [], "marked": None}
    result = {"checks": checks, "ok": False, "invalid_reason": "", "guids": guids, "bot_name": None}

    try:
        visible = _retry(lambda: check_gm_visible(agent, guard, ctx))
    except GmRefused as exc:
        checks.append(row("player_level", False, None, ".gm visible on / .gm off refused: %s" % exc))
        result["invalid_reason"] = "gm off failed: %s" % exc
        return result
    st = agent.status()
    if st.get("state") != "in_world":
        checks.append(row("player_level", False, st.get("state"), "not in world"))
        result["invalid_reason"] = "not in world"
        return result
    checks.append(visible)
    if not visible["ok"]:
        result["invalid_reason"] = visible["evidence"]
        return result

    gear_dl = None       # one deadline for the whole gear setup (carry-over strip + kit), started at its first step
    steps = [("player_level", lambda: check_level(agent, trial, guard))]
    if "player_talents" in wanted:
        steps.append(("player_talents", lambda: check_talents(agent, trial, guard, ctx)))
    if trial["player"].get("gear") == "tier":
        # a kit left by an earlier trial (another level or role) comes off before this trial's own kit goes on
        steps.append(("player_gear_carryover", lambda: strip_player_gear(
            agent, guard, ctx, keep=set(ctx.gear_ids), row_id="player_gear_carryover", deadline=gear_dl)))
        steps.append(("player_gear", lambda: check_player_gear(agent, trial, guard, ctx, deadline=gear_dl)))
    else:
        # no gear option: whatever kit an earlier gear trial left on this slot character comes off first
        steps.append(("player_gear_strip", lambda: strip_player_gear(agent, guard, ctx, deadline=gear_dl)))
    if trial["player"].get("stance") or {"player_stance", "player_auras"} & wanted:
        steps.append(("player_stance", lambda: check_stance(agent, trial, guard, ctx)))
    if trial["player"].get("auras"):
        steps.append(("player_auras", lambda: check_player_auras(agent, trial, guard, ctx)))
    steps.append(("player_forced_role", lambda: check_forced_role(agent, trial, guard, ledger)))

    for cid, fn in steps:
        if cid in GEAR_STEP_IDS:
            if gear_dl is None:
                gear_dl = Deadline(GEAR_SETUP_BUDGET_S)
            checks.append(_retry(fn, deadline=gear_dl))
        else:
            checks.append(_retry(fn))

    # the player's off-hand is recorded for every trial; the no-shield check is the Arms/Battle trials'
    offhand = read_offhand(agent)
    result["player_offhand"] = offhand
    if "player_no_shield" in wanted:
        checks.append(check_player_no_shield(offhand))

    bot_name = None
    bot_present = None
    if first or "bot_present" in wanted:
        bot_present, bot_name = hire_bot(agent, trial, guard, ledger, previous=previous)
        checks.append(bot_present)
        result["bot_name"] = bot_name
        if bot_name:
            ctx.hire_name = bot_name
            # the hire's identity is the party row, never a chat/unit guess
            guid = party_guid(agent, bot_name)
            guids["bot"] = guid
            ctx.hire_guid = guid
            if not guid:
                checks[-1] = row("bot_present", False, bot_name,
                                 "%s is not a party member; no hire identity" % bot_name)
                bot_name = None
                result["bot_name"] = None
            else:
                bring_hire(agent, bot_name)

    if bot_name:
        if "bot_level" in wanted:
            checks.append(_retry(lambda: check_bot_level(agent, trial, bot_name)))
        # the talents go on BEFORE the role: a talent change rebuilds the strategies from the new build, and
        # the role then sets the strategies of the spec on top of it
        talents_dl = Deadline(TALENT_BUDGET_S)      # one budget for this bot's talent check, both passes
        talents_row = _retry(lambda: check_bot_talents(agent, trial["bot"], bot_name, ledger, deadline=talents_dl,
                                                       guid=guids["bot"] or ""), deadline=talents_dl)
        spec_row = _retry(lambda: apply_spec(agent, trial, bot_name, ledger))
        if "bot_role" in wanted:
            checks.append(spec_row)
        if talents_row is not None:
            checks.append(talents_row)
        gear = trial["bot"].get("gear")
        if gear:
            since = agent.tbm("autogear %s %s" % (bot_name, gear))
            line, _rows = agent.wait_reply(
                since, r"Re-geared|autogear is disabled|refused:|pool bot|must be alive|usage:", timeout=20)
            ledger.add({"kind": "autogear", "bot": bot_name, "gear": gear, "reply": line or ""})
            if spec_row.get("ok") and line:
                spec_row["evidence"] = "%s; autogear %s: %s" % (spec_row["evidence"], gear, line)
        if "bot_spec" in wanted:
            checks.append(_retry(lambda: check_bot_spec(agent, trial, bot_name)))
        if "bot_items" in wanted:
            checks.append(_retry(lambda: check_items(agent, trial, guard, ctx, ledger, bot_name)))
        if "bot_mana" in wanted:
            checks.append(_retry(lambda: check_mana(agent, trial, guard, ctx, bot_name)))
        if "pet_present" in wanted:
            checks.append(_retry(lambda: check_pet(agent, trial, bot_name)))
        if "grouped" in wanted:
            checks.append(_retry(lambda: check_grouped(agent, bot_name)))
    if trial.get("extra_bots"):
        if bot_name:
            extra_rows, extra_names, extra_guids = run_extras(
                agent, trial, guard, ledger, ctx, previous=previous_extras)
            checks.extend(extra_rows)
            result["extra_names"] = list(ctx.extra.get("extra_names") or [])
            result["extra_slots"] = list(ctx.extra.get("extra_slots") or [])
            guids["extras"] = extra_guids
        else:
            for cid in ("extra_bots_present", "extra_bots_role", "extra_bots_in_group"):
                checks.append(row(cid, False, None, "main hire failed: extra bots not hired"))
            result["extra_names"] = []
            result["extra_slots"] = [None] * len(trial.get("extra_bots") or [])
            guids["extras"] = []
    if "config_read" in wanted:
        cfg = read_config(agent)
        checks.append(config_row(cfg))
        result["config"] = cfg

    failed = [c for c in checks if not c["ok"] and c["id"] != "config_read"]
    result["ok"] = not failed
    if failed:
        result["invalid_reason"] = failed[0]["evidence"] or ("%s failed" % failed[0]["id"])
    return result


def _events_rows(rep_dir):
    path = os.path.join(rep_dir, "events.jsonl")
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def check_player_visible(rep_dir, trial, window_s=VISIBLE_WINDOW_S):
    """REQUIRED in-fight check `player_visible`, from the rep's events.jsonl (None for a raid trial).

    Visible = within the first `window_s` s of the fight (t = 0 is fight_start) a hired bot (the hire or
    an extra_bots hire) cast a spell at the player. A hired HEALER must do so: a mob hitting the player proves nothing
    (live3: the hidden GM was hit by mobs in 6 of 9 reps and healed in none). For a bot that is not
    a healer, or when the player is not the trial's victim (victim extra1: it is a bystander), a mob
    attack on the player counts too (nothing obliges the bot to cast at a full-health player). An
    ooc_buffs / ooc_hurt trial looks at its whole window. Failing = the rep is INVALID with
    "player not visible to bots".
    """
    if (trial or {}).get("group") == "raid":
        return None
    sit = (trial or {}).get("situation") or {}
    if sit.get("kind") in ("ooc_buffs", "ooc_hurt"):
        window_s = max(float(window_s), float(sit.get("ooc_wait_s") or 0) + 1.0)
    try:
        rows = _events_rows(rep_dir)
    except (OSError, ValueError) as exc:
        return row("player_visible", False, None,
                   "%s: events.jsonl unreadable (%s)" % (REASON_NOT_VISIBLE, str(exc)[:80]))
    window = [r for r in rows if 0.0 <= float(r.get("t") or 0.0) <= float(window_s)]
    # any hired bot counts: the main hire ("bot") and extra_bots hires ("extra") skip a hidden GM alike (a
    # healer extra casting on the player proves the player is visible; the sweep's kick trial had 32 such casts)
    casts = [r for r in window if r.get("kind") in ("cast", "cast_start")
             and r.get("src_role") in ("bot", "extra") and r.get("dst_role") == "player"]
    hits = [r for r in window if r.get("kind") == "damage"
            and r.get("src_role") == "mob" and r.get("dst_role") == "player"]
    # a healer must answer the player only when the player is the one being hurt (victim player);
    # with an extra_bots victim the player is a bystander and a mob hitting it counts
    healer = (((trial or {}).get("bot") or {}).get("role") == "healer"
              and (sit.get("victim") or "player") == "player")
    ok = bool(casts) or (not healer and bool(hits))
    value = {"bot_casts_on_player": len(casts), "mob_hits_on_player": len(hits), "window_s": float(window_s)}
    evidence = "first %.0f s of the fight: bot casts on the player %d, mob hits on the player %d%s" % (
        window_s, len(casts), len(hits),
        "" if ok else " -> %s%s" % (REASON_NOT_VISIBLE,
                                    " (a healer must cast at the player; mob hits do not prove it)"
                                    if healer else ""))
    return row("player_visible", ok, value, evidence)
