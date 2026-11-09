"""The ONLY path for GM text. A closed template set, each kind bound to a target class.

`GmGuard.send(kind, target, *args)` renders a template; `send_text` parses the
text against the same closed set, so a raw `.npc add`, `.reset all`, `.respawn`
or `.bot run stop` has no way through. Selection-based lines target the guid and
confirm `status.target == guid` before the line goes out.

The gm RPC is called here and nowhere else (tests/test_live.py greps for it).
"""

import re
import time
from dataclasses import dataclass, field

from .agent import RpcError, norm_guid

TEMPLATES = {
    # live3: the core hides a GM at login (GM visible state 0 -> SetGMVisible(false)) and `.gm off`
    # does not unhide, so the bots skip the player (VISIBILITY_OFF); `.gm visible on` does
    "gm_visible_on": ".gm visible on",
    "gm_off": ".gm off",
    "levelup": ".levelup {0}",
    "reset_talents": ".reset talents",
    "learn": ".learn {0}",
    "unaura": ".unaura {0}",
    "aura": ".aura {0}",
    "damage": ".damage {0}",
    "modify_hp": ".modify hp {0} {1}",
    "modify_mana": ".modify mana {0} {1}",
    "npc_summon": ".npc summon {0}",
    "go_xyz": ".go xyz {0} {1} {2} {3}",
    "additem": ".additem {0} {1}",
    "additem_self": ".additem {0} {1}",      # the player's own gear kit (trl/gear.py); see send(), never classified
    "cooldown": ".cooldown",
    "die": ".die",
    "lookup_item": ".lookup item {0}",
}

CLASSIFY = [
    (re.compile(r"\.gm visible on"), "gm_visible_on"),
    (re.compile(r"\.gm off"), "gm_off"),
    (re.compile(r"\.levelup (-?\d{1,2})"), "levelup"),
    (re.compile(r"\.reset talents"), "reset_talents"),
    (re.compile(r"\.learn (\d{1,6})"), "learn"),
    (re.compile(r"\.unaura (all|\d{1,6})"), "unaura"),
    (re.compile(r"\.aura (\d{1,6})"), "aura"),
    (re.compile(r"\.damage (\d{1,6})"), "damage"),
    (re.compile(r"\.modify hp (\d{1,6}) (\d{1,6})"), "modify_hp"),
    (re.compile(r"\.modify mana (\d{1,6}) (\d{1,6})"), "modify_mana"),
    (re.compile(r"\.npc summon (\d{1,6})"), "npc_summon"),
    (re.compile(r"\.go xyz (-?\d{1,6}) (-?\d{1,6}) (-?\d{1,6})(?: (\d{1,2}))?"), "go_xyz"),
    (re.compile(r"\.additem (\d{1,6}) (\d{1,2})"), "additem"),
    (re.compile(r"\.cooldown"), "cooldown"),
    (re.compile(r"\.die"), "die"),
    (re.compile(r"\.lookup item ([A-Za-z0-9 ':.\-]{1,60})"), "lookup_item"),
]

# kinds that first target the guid and confirm status.target (the core reads these
# from the selection, even `.learn` and `.cooldown`; `.levelup` and `.reset talents`
# act on the selected unit, so they are self-only selections too)
SELECTION = {"damage", "unaura", "aura", "modify_hp", "modify_mana", "additem", "additem_self", "learn", "cooldown",
             "die", "levelup", "reset_talents"}
# kinds allowed on each target class; "none" means no selection at all
TARGETS = {
    "gm_visible_on": ("none", "self"),
    "gm_off": ("none", "self"),
    "levelup": ("self",),
    "reset_talents": ("self",),
    "learn": ("self",),
    "unaura": ("self", "hire"),
    "aura": ("self", "hire"),
    "damage": ("self",),
    "modify_hp": ("self", "hire"),
    "modify_mana": ("hire",),
    "npc_summon": ("none",),
    "go_xyz": ("none", "self"),
    "additem": ("hire",),
    "additem_self": ("self",),
    "cooldown": ("self",),
    "die": ("summon",),
    "lookup_item": ("none",),
}

# How long the server gets to show a selection before the GM line is given up on, and how often the
# player's own object is re-read meanwhile (each read is one rpc, ~50 ms). The restore of the previous
# target is best effort, so it waits less.
SELECT_WAIT = 4.0
RESTORE_WAIT = 1.5
SELECT_POLL = 0.1

TT_OK = re.compile(r"\A(quiet 40 w=2|q unit 0x[0-9A-Fa-f]{1,16}(?: au=[0-9A-Fa-f]{1,16})?)\Z")


class GmRefused(Exception):
    """The line would leave the closed safety set: exit 4, never sent."""


class GmServerError(Exception):
    """The server rejected a line, the selection did not stick, or the unit is not visible: exit 5."""


class GmAborted(Exception):
    """The session went away (a lost lease or a disconnect): nothing more goes out."""


def _tracked(fn):
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except GmRefused:
            self.refused = True
            raise
        except GmServerError:
            self.server_error = True
            raise
    wrapper.__name__ = fn.__name__
    return wrapper


@dataclass
class GmContext:
    player_guid: str = ""
    band_low: int = 40
    floor_pct: int = 30
    hire_name: str = ""
    arena_coords: frozenset = frozenset()
    mob_entries: frozenset = frozenset()
    learn_ids: frozenset = frozenset()
    aura_ids: frozenset = frozenset()
    item_ids: frozenset = frozenset()
    gear_ids: frozenset = frozenset()      # the player's gear kit entries (`.additem` on the player itself)
    trial_id: str = ""
    hire_guid: str = ""
    # the extra_bots hires' party guids; with hire_guid they are the whole hire set
    extra_hire_guids: frozenset = frozenset()
    extra: dict = field(default_factory=dict)


class GmGuard:
    def __init__(self, agent, ledger, ctx=None, dry_run=False, on_gm=None, abort=None):
        self.agent = agent
        self.ledger = ledger
        self.ctx = ctx or GmContext()
        self.dry_run = dry_run
        self.refused = False
        self.server_error = False
        self.on_gm = on_gm or (lambda *a: None)
        # session.abort_reason: once set (lease lost, disconnected) no GM line goes out
        self.abort = abort or (lambda: "")
        # the selection wait is timed by these (tests inject fakes)
        self.clock = time.monotonic
        self.sleep = time.sleep
        self.select_wait = SELECT_WAIT
        self.restore_wait = RESTORE_WAIT
        self.select_poll = SELECT_POLL

    def _check_abort(self):
        reason = str(self.abort() or "")
        if reason:
            raise GmAborted("guard aborted: %s" % reason)

    # -- public -----------------------------------------------------------

    def refuse(self, message):
        self.refused = True
        raise GmRefused(message)

    @_tracked
    def send(self, kind, target=None, *args):
        if kind == "gm_kill":
            return self.kill(args[0] if args else target)
        if kind not in TEMPLATES:
            raise GmRefused("kind %r is not in the closed template set" % (kind,))
        text = TEMPLATES[kind].format(*args)
        return self.send_text(text, target=target, kind_hint=kind)

    @_tracked
    def kill(self, guid):
        self._check_abort()
        guid = norm_guid(guid)
        if not guid:
            raise GmRefused("gm_kill needs a guid")
        if guid not in [norm_guid(g) for g in self.ledger.summon_guids()]:
            raise GmRefused("gm_kill %s: not a ledger summon" % guid)
        if self.dry_run:
            self.ledger.add({"kind": "gm_kill", "guid": guid, "dry_run": True, "trial": self.ctx.trial_id})
            return guid
        out = self.agent.rpc("gm_kill", {"guid": guid})
        if isinstance(out, dict) and "_err" in out:
            # the agent refuses gm_kill while the player attacks or is in combat;
            # `.die` on a confirmed selection works there (the overview's line list)
            self.send("die", guid)
            return guid
        self.ledger.add({"kind": "gm_kill", "guid": guid, "trial": self.ctx.trial_id})
        self.on_gm("gm_kill", "gm_kill", guid)
        return guid

    @_tracked
    def tt(self, cmd):
        self._check_abort()
        cmd = str(cmd or "").strip()
        if not TT_OK.fullmatch(cmd):
            raise GmRefused("tt %r is not a read-only toolkit line" % cmd)
        if self.dry_run:
            return {"replies": []}
        out = self.agent.rpc("tt", {"cmd": cmd})
        if isinstance(out, dict) and "_err" in out:
            raise GmServerError("tt %r: %s" % (cmd, out["_err"]))
        self.ledger.add({"kind": "tt", "cmd": cmd, "trial": self.ctx.trial_id})
        return out

    # -- closed set -------------------------------------------------------

    @_tracked
    def send_text(self, text, target=None, kind_hint=None):
        self._check_abort()
        text = " ".join(str(text or "").split())
        kind, groups = None, ()
        for rx, name in CLASSIFY:
            m = rx.fullmatch(text)
            if m:
                kind, groups = name, m.groups()
                break
        if kind is None:
            raise GmRefused("GM text not in the closed template set: %r" % text)
        # `.additem` on the player's own kit is a different kind from `.additem` on a hire: only send() with the
        # kind name reaches it, so a raw ".additem N 1" line still means the hire kind (hire target only)
        if kind == "additem" and kind_hint == "additem_self":
            kind = "additem_self"
        prev = ""
        gen = 0
        self._validate(kind, groups, target)
        # one lock per agent, held for capture+select+confirm+send+restore: the sampler
        # (governor) and the situation loop both move the selection, so the whole
        # sequence is atomic. The generation counter catches a mover that skipped the
        # lock between the capture and the restore.
        with self.agent.action_lock:
            if kind in SELECTION:
                prev = norm_guid(self.agent.rpc("status", {}, timeout="10s").get("target") or "")
                gen = int(self.agent.selection_gen)
                self._select(target)
            if self.dry_run:
                self.ledger.add({"kind": "gm", "line": text, "kind_detail": kind,
                                 "target": target, "dry_run": True, "trial": self.ctx.trial_id})
                self.on_gm(kind, text, target)
                return text
            out = self.agent.rpc("gm", {"text": text}, timeout="10s")
            if isinstance(out, dict) and "_err" in out:
                raise GmServerError("server refused %r: %s" % (text, out["_err"]))
            self.ledger.add({"kind": "gm", "line": text, "kind_detail": kind,
                             "target": norm_guid(target) if target else None, "trial": self.ctx.trial_id})
            if kind in SELECTION and prev and prev != norm_guid(target):
                # the GM line moved the selection: put the fight's current target back,
                # unless someone else moved the selection after we captured it
                if int(self.agent.selection_gen) == gen:
                    self.agent.rpc("target", {"guid": prev}, timeout="10s")
                    seen = norm_guid(self.agent.rpc("status", {}, timeout="10s").get("target") or "")
                    if seen != prev:
                        self.ledger.add({"kind": "retarget", "wanted": prev, "seen": seen,
                                         "trial": self.ctx.trial_id})
                    else:
                        # no select may stay in flight when the lock is let go: the next line
                        # would read the field of the one before it (live2)
                        # best effort: the line is sent; the NEXT guarded line waits for its own
                        # confirmation whatever this one saw
                        ok, why = self._await_server_selection(prev, self.restore_wait)
                        if not ok:
                            self.ledger.add({"kind": "retarget", "wanted": prev, "seen": why,
                                             "server": True, "trial": self.ctx.trial_id})
                else:
                    self.ledger.add({"kind": "retarget_skipped", "wanted": prev,
                                     "generation": int(self.agent.selection_gen),
                                     "captured": gen, "trial": self.ctx.trial_id})
        self.on_gm(kind, text, target)
        return text

    # -- validation -------------------------------------------------------

    def _validate(self, kind, groups, target=None):
        role = self._target_role(target)
        allowed = TARGETS[kind]
        if role not in allowed:
            raise GmRefused("%s on a %s target; allowed: %s" % (kind, role, ", ".join(allowed)))
        if kind in SELECTION and role == "none":
            raise GmRefused("%s is selection-based: give the target guid" % kind)
        if kind == "levelup":
            n = int(groups[0])
            # a negative count lowers the level (the core's .levelup takes a signed count and stops at 1):
            # a slot character that an earlier trial levelled above this trial's level is brought back down
            if n == 0 or not -59 <= n <= 60:
                raise GmRefused(".levelup %d out of range" % n)
        elif kind == "learn":
            spell = int(groups[0])
            if spell not in self.ctx.learn_ids:
                raise GmRefused(".learn %d: not a trial talent/data spell" % spell)
        elif kind == "aura":
            spell = int(groups[0])
            if spell <= 0:
                raise GmRefused(".aura needs a spell id")
            if spell not in self.ctx.aura_ids:
                raise GmRefused(".aura %d: not an aura the trial file lists" % spell)
        elif kind in ("unaura",):
            value = groups[0]
            if value != "all" and int(value) <= 0:
                raise GmRefused(".unaura needs all or a spell id")
        elif kind == "damage":
            dmg = int(groups[0])
            st = self.agent.status()
            hp, maxhp = int(st.get("health", 0)), int(st.get("max_health", 1) or 1)
            floor = int(hp - self.ctx.band_low * maxhp / 100)
            if dmg < 1 or dmg > floor:
                raise GmRefused(".damage %d would cross band_low %d%% (hp %d/%d, max %d)"
                                % (dmg, self.ctx.band_low, hp, maxhp, floor))
        elif kind == "modify_hp":
            hp, maxhp = int(groups[0]), int(groups[1])
            row = self._row(target)
            own = int(row.get("max_health") or 0)
            if not own:
                raise GmRefused(".modify hp %s: the unit's max health is unknown" % norm_guid(target))
            if maxhp != own:
                raise GmRefused(".modify hp max %d is not the unit's own max %d" % (maxhp, own))
            if hp < 1 or hp > maxhp:
                raise GmRefused(".modify hp %d %d out of range" % (hp, maxhp))
        elif kind == "modify_mana":
            mana, maxmana = int(groups[0]), int(groups[1])
            row = self._row(target)
            own = int(row.get("max_power") or 0) or int(row.get("max_mana") or 0)
            if not own:
                raise GmRefused(".modify mana %s: the unit's max mana is unknown" % norm_guid(target))
            if maxmana != own:
                raise GmRefused(".modify mana max %d is not the unit's own max %d" % (maxmana, own))
            if mana < 0 or mana > maxmana:
                raise GmRefused(".modify mana %d %d out of range" % (mana, maxmana))
        elif kind == "npc_summon":
            entry = int(groups[0])
            if entry not in self.ctx.mob_entries:
                raise GmRefused(".npc summon %d: entry not in data/mobs.json" % entry)
        elif kind == "go_xyz":
            x, y, z = int(groups[0]), int(groups[1]), int(groups[2])
            aw = int(groups[3] or 0) if len(groups) > 3 and groups[3] else 0
            if not self._arena_ok(x, y, z, aw):
                raise GmRefused(".go xyz %d %d %d: not an arena coordinate" % (x, y, z))
        elif kind == "additem_self":
            item, count = int(groups[0]), int(groups[1])
            if item not in self.ctx.gear_ids:
                raise GmRefused(".additem %d: not an entry of this trial's gear kit" % item)
            if count != 1:
                raise GmRefused(".additem count %d: a kit item is granted once" % count)
        elif kind == "additem":
            item, count = int(groups[0]), int(groups[1])
            if item not in self.ctx.item_ids:
                raise GmRefused(".additem %d: not a trial bot item" % item)
            if not 1 <= count <= 20:
                raise GmRefused(".additem count %d out of range" % count)

    def _arena_ok(self, x, y, z, want_map):
        """Arena coords are (map, x, y[, z]); x/y within 1 yd, z within 2 when known."""
        for coord in self.ctx.arena_coords:
            values = list(coord) + [None, None, None, None]
            amap, ax, ay, az = values[0], values[1], values[2], values[3]
            if int(amap or 0) != int(want_map):
                continue
            if abs(int(ax) - x) > 1 or abs(int(ay) - y) > 1:
                continue
            if az is not None and abs(int(round(float(az))) - z) > 2:
                continue
            return True
        return False

    def _target_role(self, target):
        if not target:
            return "none"
        tg = norm_guid(target)
        if not tg:
            return "other"
        if tg == norm_guid(self.ctx.player_guid):
            return "self"
        hire = self._hire_guid()
        if hire and tg == hire:
            return "hire"
        if tg in self._extra_hire_guids():
            return "hire"
        if tg in [norm_guid(g) for g in self.ledger.summon_guids()]:
            return "summon"
        return "other"

    def _hire_guid(self):
        """The hire identity is the party/ledger guid set at setup; never a name lookup."""
        return norm_guid(self.ctx.hire_guid) if self.ctx.hire_guid else ""

    def _extra_hire_guids(self):
        """The extra_bots hires: party guids set at setup, never a name lookup."""
        return {norm_guid(g) for g in (self.ctx.extra_hire_guids or ()) if norm_guid(g)}

    def _row(self, target):
        row = self.agent.unit(guid=target)
        if not row:
            raise GmServerError("target %s is not in view" % norm_guid(target))
        return row

    def _select(self, target):
        tg = norm_guid(target)
        if not tg:
            raise GmRefused("selection needs a guid")
        if self.dry_run:
            return
        if not norm_guid(self.ctx.player_guid):
            raise GmServerError("selection not confirmable: no player guid; no line was sent")
        self.agent.rpc("target", {"guid": tg}, timeout="10s")
        st = self.agent.rpc("status", {}, timeout="10s")
        seen = norm_guid(st.get("target") or "")
        if seen != tg:
            raise GmServerError("selection did not stick: status.target %s, wanted %s" % (seen, tg))
        # status.target is what the CLIENT sent. The core applies a selection on the map thread and
        # runs a GM chat line on the world thread, so the line can run on the selection from before
        # (live2: `.additem` and `.modify hp` landed on the GM itself or on the fought mob). Only the
        # target field of the player's own object is the server's word, and without it no line goes
        # out: this fails closed.
        ok, why = self._await_server_selection(tg, self.select_wait)
        if not ok:
            self.ledger.add({"kind": "selection_unconfirmed", "wanted": tg, "reason": why,
                             "trial": self.ctx.trial_id})
            raise GmServerError("selection not confirmed by the server: %s (wanted %s, waited %.1f s); "
                                "no line was sent" % (why, tg, self.select_wait))

    def _await_server_selection(self, tg, wait):
        """Poll the player's own object until its target field shows `tg`: (True, "") or (False, why).

        A unit read that fails (rpc error, empty row) is retried until the deadline like a wrong
        value; there is no "cannot tell, carry on". Holds for combat too: attacking writes the
        field only when the attack starts or stops, and a select written after it shows until then.
        """
        own = norm_guid(self.ctx.player_guid)
        if not own:
            return False, "no player guid to read the server's selection from"
        deadline = self.clock() + float(wait)
        why = "the player's own unit row was never readable"
        while True:
            row = self.agent.unit(guid=own)
            if isinstance(row, dict) and row:
                shown = norm_guid(row.get("target") or "")
                if shown == tg:
                    return True, ""
                why = "its target field shows %s" % (shown or "nothing")
            if self.clock() >= deadline:
                return False, why
            self.sleep(self.select_poll)
