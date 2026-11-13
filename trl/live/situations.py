"""One driver per situation kind, the HP governor, summons and the arena probe.

Every GM line goes through the guard. A summon is ledgered the moment its guid
is seen and before anything attacks it; teardown reads that ledger.
"""

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from . import arena_lock
from . import ladder
from .agent import RpcError, UnitGone, event_guid, norm_guid, wait_until

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(TOOL_ROOT, "data")

THREAT_CAST = {71: 355, 2457: 7386, 9634: 6795, 5487: 6795}
SUMMON_CAP = 20
REFILL_CAP = 3
# GM rescues (a heal after the victim fell under the floor) the governor allows per rolling
# minute before it ends the repeat as "floor"; a trial may set situation.max_rescues_per_min
MAX_RESCUES_PER_MIN = 6
# a victim whose last fall would leave it at or under this many percent on the next sample is
# rescued before it reaches the floor (live3: floor_pct 12 sits under a 22-point single-sample fall)
DEATH_MARGIN_PCT = 3.0
GM_FALL_SAMPLES = 2         # the governor's damage line lands a sample or two later: that fall is not a mob's
MAX_DROP_CAP_PCT = 30.0
DROP_WINDOW_S = 20.0        # only falls this recent drive an early rescue
# ooc_hurt: the victim is set to a percent inside hp_band every this many seconds (scenario: 8-120)
HURT_EVERY_S = 20.0
STANCE_RECAST_GAP_S = 5.0
PREPULL_AURA_WAIT_S = 3.0  # the hire's aura row drops a tick or two after `.unaura`; then it must be gone
# ooc drink settle (bench-fix-1112): a low-mana hire starts drinking during setup, and the window's
# `.unaura all` strips that setup drink. A stripped drink can leave the AI asleep in the meal, so the
# window must not open on a stripped drink: when a Drink aura was on the hire, wait (bounded) for the
# healthy immediate re-drink and open the window on it. Each rep then tests "starts a drink".
DRINK_AURA_IDS = frozenset({430, 431, 432, 1133, 1135, 1137, 10250, 22734, 24355, 25696, 26261, 26402,
                            26473, 26475, 29007})  # keep in sync with PRI-LOWMANA-DRINK-UPTIME's aura list
DRINK_SETTLE_S = 10.0  # a healthy re-drink lands within ~2 s (rep2/4 shape); then open the window anyway
# -- arena lock (one exclusive file lock per arena, held for the whole repeat) --
ARENA_WAIT_S = 15.0        # wait between passes when every arena is locked or not quiet
ARENA_GIVE_UP_S = 600.0    # give up after 10 min with the existing "no quiet arena" outcome


def load_mobs(path=None):
    path = path or os.path.join(DATA, "mobs.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def bring_hire(agent, name, within=10.0, timeout=45.0):
    """Party-chat `summon` (the #1553 way), then the named order; a unit out of view is not near."""
    if not name:
        return False

    def near():
        row = agent.unit(name=name) or {}
        dist = row.get("distance", -1)
        return 0 <= dist <= within

    try:
        agent.chat("party", "summon")
        agent.say_to_bot(name, "summon")
    except Exception:  # noqa: BLE001
        return False
    if wait_until(near, timeout=max(5.0, timeout / 2.0), poll=1.0):
        return True
    try:
        agent.say_to_bot(name, "come")
    except Exception:  # noqa: BLE001
        return False
    return bool(wait_until(near, timeout=max(5.0, timeout / 2.0), poll=1.0))


def load_arenas(path=None):
    path = path or os.path.join(DATA, "arenas.json")
    try:
        with open(path) as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def extra_victim_index(victim):
    """1-3 for a victim named extra1..extra3, else 0."""
    m = re.fullmatch(r"extra([1-3])", str(victim or ""))
    return int(m.group(1)) if m else 0


def damage_step(hp, max_hp, band_low, band_top, to_pct=None):
    """The largest GM damage that keeps the victim above band_low; 0 when none is due."""
    if not max_hp:
        return 0
    percent = 100.0 * float(hp) / float(max_hp)
    if percent <= float(band_top):
        return 0
    if to_pct is None:
        middle = (float(band_low) + float(band_top)) / 2.0 * float(max_hp) / 100.0
    else:
        middle = float(to_pct) * float(max_hp) / 100.0
    cap = float(hp) - float(band_low) * float(max_hp) / 100.0
    return max(0, int(min(float(hp) - middle, cap)))


def pick_entry(mobs, role, window_low, window_high):
    """Mob entries of this role; ranges fully inside the window first, then the closest."""
    rows = list((mobs or {}).get(role) or [])
    mid = (float(window_low) + float(window_high)) / 2.0

    def rank(row):
        lo, hi = int(row.get("level_min", 0)), int(row.get("level_max", 0))
        inside = 0 if lo >= window_low and hi <= window_high else 1
        gap = max(0, window_low - hi, lo - window_high)
        return (inside, gap, abs((lo + hi) / 2.0 - mid))
    rows.sort(key=rank)
    return rows


def _guid_words(text):
    out = set()
    for word in re.findall(r"0x[0-9A-Fa-f]{1,16}|(?<![0-9A-Za-z])[0-9A-Fa-f]{16}(?![0-9A-Za-z])",
                           str(text or "")):
        out.add(norm_guid(word))
    return out


def _wall_now(offset=0.0):
    """An ISO wall time (the fallback when the caller has no steady clock to stamp with)."""
    import datetime
    return (datetime.datetime.now().astimezone()
            + datetime.timedelta(seconds=offset)).isoformat(timespec="milliseconds")


def summon_mob(agent, guard, ledger, entry, within=3.0, timeout=5.0, poll=0.5, foreign=None, log=None,
               arena=None, now_iso=None, sent=None):
    """Summon one entry and claim it by the reply's guid, or by a NEW unit.

    `within` is the claim radius that tells apart two or more fresh units: a nearby same-entry
    unit that was absent from the pre-summon list, never a distance match to any unit. When the
    reply names no guid and exactly ONE fresh unit of the entry appeared (bench-fix-1111: the
    reply carries none on this core, and 39 of 14 trials' summons stood 3.3-14.7 yd away), that
    single new unit is claimed at any distance inside the 30 yd scan: an unclaimed summon stops
    every refill for the rest of the repeat. Two or more fresh units keep the 3 yd rule and the
    miss, because there is no way to tell them apart.
    `foreign` (parallel runs) returns the guids the OTHER slots summoned: never claimed here.
    `now_iso` stamps the send (the caller's steady clock); `sent` receives {"wall": ...} right
    BEFORE the `.npc summon` line goes out, so a marker keyed on it precedes the command.
    An unclaimed summon may still stand in the arena (sweep-1b: five of them, in combat with the player,
    made the arena "not quiet" for the next 11 reps), so it is written to the ledger as
    `summon_unclaimed` with what was in view, and logged; the caller stops summoning.
    """
    known = {norm_guid(g) for g in ledger.summon_guids()}
    if foreign is not None:
        known |= {norm_guid(g) for g in (foreign() or ()) if g}
    # the pre-list must not read an rpc error as "no unit was there before"
    before = {norm_guid(u.get("guid")) for u in agent.units_must(entry=int(entry), within=30)
              if u.get("guid")}
    since = agent.snapshot()
    if sent is not None:
        sent["wall"] = now_iso() if now_iso is not None else _wall_now()
    guard.send("npc_summon", None, int(entry))
    deadline = time.time() + timeout
    seen = []
    while True:
        reply_guids = set()
        try:
            lines, _rows = agent.read_lines(since, timeout=0.5, kinds=("system",))
            for line in lines:
                reply_guids |= _guid_words(line)
        except Exception:  # noqa: BLE001 - the reply scan is only a hint
            pass
        units = [u for u in agent.units_must(entry=int(entry), within=30)
                 if u.get("guid") and not u.get("dead")]
        by_guid = {norm_guid(u.get("guid")): u for u in units}
        for guid in reply_guids:
            unit = by_guid.get(guid)
            if unit and guid not in known:
                ledger.add({"kind": "summon", "guid": guid, "entry": int(entry)})
                return guid
        fresh = []
        for unit in units:
            guid = norm_guid(unit.get("guid"))
            if not guid or guid in known or guid in before:
                continue
            fresh.append(unit)
            dist = unit.get("distance", -1)
            # an unknown distance (-1) is not proof this is the new summon
            if dist is None or dist < 0 or dist > within:
                continue
            ledger.add({"kind": "summon", "guid": guid, "entry": int(entry)})
            return guid
        seen = [(norm_guid(u.get("guid")), u.get("distance")) for u in fresh]
        if len(fresh) == 1:
            # exactly one new unit of the entry: it is this summon, wherever it walked to inside the
            # 30 yd scan (the reply names no guid on this core). Two or more stay a miss without a
            # 3 yd candidate: there is nothing to tell them apart by.
            guid = norm_guid(fresh[0].get("guid"))
            dist = fresh[0].get("distance", -1)
            if guid and dist is not None and dist >= 0:
                ledger.add({"kind": "summon", "guid": guid, "entry": int(entry)})
                if log:
                    log("summon entry %s: claimed the only new unit at %.1f yd" % (entry, float(dist)))
                return guid
        if time.time() >= deadline:
            ledger.add({"kind": "summon_unclaimed", "entry": int(entry),
                        "new_units": [{"guid": g, "distance": d} for g, d in seen],
                        "reply_guids": sorted(reply_guids),
                        # where it was summoned: `trials cleanup` kills a recorded unit only when it is still
                        # this entry and within 5 yd of this point
                        "arena": ({k: arena.get(k) for k in ("id", "map", "x", "y", "z")} if arena else None)})
            if log:
                log("summon entry %s: no unit claimed (new units in view: %s)" % (entry, seen or "none"))
            return None
        time.sleep(poll)


def kill_summon(agent, guard, ledger, guid):
    try:
        guard.kill(guid)
        return True
    except Exception:  # noqa: BLE001 - refused kills are recorded, the caller moves on
        return False


NOT_IN_VIEW = re.compile(r"\bis not in view\b", re.I)


def _gone_ok(absent_is_gone, guid):
    """absent_is_gone is a bool for every guid, or the collection of guids it holds for."""
    if isinstance(absent_is_gone, (set, frozenset, list, tuple)):
        return norm_guid(guid) in {norm_guid(g) for g in absent_is_gone if g}
    return bool(absent_is_gone)


def summon_states(agent, ledger, only=None, absent_is_gone=False, unknown_ok=False):
    """guid -> alive|dead|gone, from units_must first and unit_must for the missing.

    A guid absent from the 60 yd list is not dead: only unit_must may confirm it
    dead or gone. An rpc failure raises, so the caller records UNKNOWN, not zero.

    `only` limits the states to those guids (one repeat's own mobs; the slot ledger also
    holds every earlier repeat's). `absent_is_gone` is for a caller that stands where the
    summons were made: the agent answers "unit <guid> is not in view" for a temp summon
    whose corpse has despawned, and with the player in the arena that is gone, not UNKNOWN.
    Without it (the player may be anywhere) "not in view" stays an rpc failure. It may also be a
    collection of guids: "not in view" then counts as gone for those only (this repeat's own mobs,
    made where the player stands), and stays UNKNOWN for the rest. With `unknown_ok` such a guid is
    answered "unknown" instead of failing the whole call, so the others are still checked (and killed).
    """
    ids = [norm_guid(g) for g in ledger.summon_guids() if g]
    if only is not None:
        want = {norm_guid(g) for g in only if g}
        ids = [g for g in ids if g in want]
    if not ids:
        return {}
    rows = {norm_guid(u.get("guid")): u for u in agent.units_must(within=60) if u.get("guid")}
    states = {}
    for guid in ids:
        row = rows.get(guid)
        if row is not None:
            states[guid] = "dead" if row.get("dead") else "alive"
            continue
        try:
            one = agent.unit_must(guid=guid)
        except UnitGone:
            states[guid] = "gone"
            continue
        except RpcError as exc:
            if _gone_ok(absent_is_gone, guid) and NOT_IN_VIEW.search(str(exc)):
                states[guid] = "gone"
                continue
            if unknown_ok:
                states[guid] = "unknown"
                continue
            raise
        states[guid] = "gone" if not one else ("dead" if one.get("dead") else "alive")
    return states


def alive_summons(agent, ledger, only=None, absent_is_gone=False):
    """Ledger summons not confirmed dead/gone (UNKNOWN still counts as there)."""
    return [g for g, state in summon_states(agent, ledger, only=only, absent_is_gone=absent_is_gone).items()
            if state not in ("dead", "gone")]


@dataclass
class SitContext:
    player_guid: str = ""
    bot_name: str = ""
    hire_guid: str = ""
    guids: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    cc_marked_guid: str = ""
    extra_names: tuple = ()


@dataclass
class Governor:
    agent: object
    guard: object
    marks: object
    victim: str = "player"
    band: tuple = (40, 90)
    floor: int = 30
    victim_guid: str = ""
    top_up_to: object = None
    now: object = time.time
    max_rescues: int = MAX_RESCUES_PER_MIN
    top_up_gap: float = 4.0
    last_damage: float = 0.0
    rescues: list = field(default_factory=list)
    ended: str = ""
    own_hp: int = 0
    own_max: int = 0
    stopped: object = None
    log: object = None
    # live3: the floor can sit below the critical band (floor_pct 12), where one big hit between two
    # 1 Hz samples can pass the floor and the corpse line in a single step (run 050750 rep3: 47.4 ->
    # 25.1 in 0.93 s). max_drop is the biggest fall between two samples in the last DROP_WINDOW_S
    # seconds; a victim that the same fall would take to DEATH_MARGIN_PCT or less is rescued before it
    # reaches the floor, and a spike older than the window stops mattering. Only the fall the
    # governor's own `.damage` line makes is taken out (gm_fall_pending, for GM_FALL_SAMPLES samples,
    # and only that many points of it): a rescue is a RISE and skips nothing, so a hit right after a
    # rescue is recorded like any other.
    prev_pct: object = None
    max_drop: float = 0.0
    falls: list = field(default_factory=list)     # (time, fall) of the last DROP_WINDOW_S seconds
    gm_fall_pending: float = 0.0          # points the governor's own damage line is about to take
    gm_fall_samples: int = 0
    early_rescues: int = 0
    # ooc_hurt: nothing should damage the victim out of combat, so a rescue there is logged loudly
    ooc: bool = False

    def _log(self, line):
        if self.log:
            self.log(line)

    def tick(self, sample):
        if self.stopped is not None and self.stopped.is_set():
            # the sampler is stopping: no more lines go out
            return None
        sample = sample or {}
        victim = sample if "hp" in sample else (sample.get(self.victim) or {})
        hp = int(victim.get("hp") or 0)
        max_hp = int(victim.get("max_hp") or 0)
        if not max_hp:
            return None
        if hp <= 0 or victim.get("dead"):
            self.ended = "player_dead" if self.victim == "player" else "victim_dead"
            return self.ended
        percent = 100.0 * hp / max_hp
        now = self.now()
        floor_hp = int(float(self.floor) * max_hp / 100.0)
        # the fall since the last sample, less what the governor's own damage line took off
        if self.prev_pct is not None and float(self.prev_pct) > percent:
            fall = float(self.prev_pct) - percent
            if self.gm_fall_samples > 0 and self.gm_fall_pending > 0:
                if fall >= 0.5 * self.gm_fall_pending:       # the line has landed: take it out once
                    fall = max(0.0, fall - self.gm_fall_pending)
                    self.gm_fall_pending = 0.0
            if fall > 0.05:                 # float residue of a GM line is not a hit
                self.falls.append((now, min(MAX_DROP_CAP_PCT, fall)))
        if self.gm_fall_samples > 0:
            self.gm_fall_samples -= 1
            if self.gm_fall_samples == 0:
                self.gm_fall_pending = 0.0
        self.falls = [(t, f) for (t, f) in self.falls if now - t <= DROP_WINDOW_S]
        self.max_drop = max([f for (_t, f) in self.falls] or [0.0])
        self.prev_pct = percent
        early = (hp >= floor_hp and self.max_drop > 0.0
                 and percent - self.max_drop <= DEATH_MARGIN_PCT)
        if hp < floor_hp or early:
            if early:
                self.early_rescues += 1
            self._floor(hp, max_hp, percent, now)
            recent = [t for t in self.rescues if now - t <= 60.0]
            self.rescues = recent
            if len(recent) >= int(self.max_rescues):
                self.ended = "floor"
                return self.ended
            return None
        if percent > float(self.band[1]) and now - self.last_damage > float(self.top_up_gap) \
                and victim.get("in_combat", True):
            self._top_up(hp, max_hp)
        return None

    def _floor(self, hp, max_hp, percent, now):
        # Heal to the middle of the band, never to the floor itself: a heal to exactly the
        # floor leaves no headroom, so the next hit is another rescue (live1: three rescues
        # in three seconds ended a repeat at 16 s). The middle is above the floor, so the next
        # sample is not a rescue either.
        heal = self.rescue_hp(max_hp)
        try:
            self.guard.send("modify_hp", self.victim_guid, heal, max_hp)
        except Exception as exc:  # noqa: BLE001 - a refused heal must not kill the sampler
            # a normal-flow skip (the victim is a corpse, the lease ended): log only
            self._log("governor floor heal skipped: %s" % exc)
            return
        wall = self._wall()
        self.marks.add({"kind": "gm_heal", "hp": hp, "max_hp": max_hp, "pct": round(percent, 1),
                        "to": heal, "wall": wall})
        self.marks.excluded(wall, self._wall(offset=3), "floor")
        self.rescues.append(now)
        self.last_damage = now
        if self.ooc:
            self._log("!!! OOC RESCUE FIRED: %s fell to %.1f%% (%d/%d) in an out-of-combat window, "
                      "healed to %d; something is damaging it" % (self.victim, percent, hp, max_hp, heal))

    def rescue_hp(self, max_hp):
        middle = (float(self.band[0]) + float(self.band[1])) / 2.0
        target = max(middle, float(self.floor))
        return max(1, min(int(max_hp), int(target * max_hp / 100.0)))

    def _top_up(self, hp, max_hp):
        to_pct = self.top_up_to
        if self.victim == "player":
            dmg = damage_step(hp, max_hp, self.band[0], self.band[1], to_pct=to_pct)
            if dmg < 1:
                return
            try:
                self.guard.send("damage", self.victim_guid, dmg)
            except Exception as exc:  # noqa: BLE001
                self._log("governor damage skipped: %s" % exc)
                return
        else:
            if to_pct is None:
                middle = int((float(self.band[0]) + float(self.band[1])) / 2.0 * max_hp / 100.0)
            else:
                middle = int(float(to_pct) * max_hp / 100.0)
            if hp <= middle:
                return
            try:
                self.guard.send("modify_hp", self.victim_guid, middle, max_hp)
            except Exception as exc:  # noqa: BLE001
                self._log("governor bot heal skipped: %s" % exc)
                return
        self.last_damage = self.now()
        taken = (dmg if self.victim == "player" else hp - middle) * 100.0 / max(1, max_hp)
        self.gm_fall_pending = max(0.0, taken)
        self.gm_fall_samples = GM_FALL_SAMPLES

    def _wall(self, offset=0.0):
        stamp = getattr(self.marks, "now_iso", None)
        if stamp is not None:
            return stamp(offset)       # the rep's steady clock (see collect.Marks)
        import datetime
        return (datetime.datetime.now().astimezone()
                + datetime.timedelta(seconds=offset)).isoformat(timespec="milliseconds")


class _HeldArena:
    """One taken arena: the exclusive file lock plus the in-process claim.

    `abandon()` gives both back WITHOUT touching `situation` (a refused probe:
    the caller keeps looking). The context manager releases on every exit path,
    exceptions included; `release_arena` covers a Situation that never entered it."""
    def __init__(self, situation, arena=None, lock=None):
        self.situation = situation
        self.arena = arena
        self.lock = lock
        self.done = False

    def abandon(self):
        if self.done:
            return
        self.done = True
        if self.lock is not None:
            self.lock.release()
        if self.arena is not None:
            try:
                self.situation.arena_free(self.arena)
            except Exception:  # noqa: BLE001 - teardown must not fail
                pass

    def __enter__(self):
        return self.situation.current_arena if self.arena is None else self.arena

    def __exit__(self, *exc):
        held = getattr(self.situation, "_arena_held", None)
        if held is self:
            self.situation._arena_held = None
            self.situation.current_arena = None
        self.abandon()
        return False


class Situation:
    def __init__(self, agent, trial, guard, ledger, marks, collector, ctx, arenas=None, mobs=None,
                 log=None, slot_index=0, abort=None, on_arena=None, arena_claim=None, arena_free=None,
                 foreign_summons=None, server=None, arena_state_dir=None):
        self.agent = agent
        self.trial = trial
        self.guard = guard
        self.ledger = ledger
        self.marks = marks
        self.collector = collector
        self.ctx = ctx
        self.arenas = arenas if arenas is not None else load_arenas()
        self.mobs = mobs if mobs is not None else load_mobs()
        self.log = log or (lambda line: None)
        self.on_arena = on_arena or (lambda arena: None)
        # parallel slots: an arena another slot holds is skipped, a refused one is given back
        self.arena_claim = arena_claim or (lambda arena: True)
        self.arena_free = arena_free or (lambda arena: None)
        self.foreign_summons = foreign_summons
        self.sit = trial.get("situation") or {}
        self.victim = self.sit.get("victim") or "player"
        self.band = tuple(self.sit.get("hp_band") or (40, 90))
        self.floor = int(self.sit.get("floor_pct") or 30)
        self.max_rescues = int(self.sit.get("max_rescues_per_min") or MAX_RESCUES_PER_MIN)
        self.slot_index = int(slot_index or 0)
        self.abort = abort or (lambda: "")
        self.extra_victim = extra_victim_index(self.victim)
        self.server = server
        self.arena_state_dir = arena_state_dir
        self._arena_held = None        # the held arena span, released on every exit path
        self.current_arena = None
        self.prepull_unaura = [int(x) for x in (self.sit.get("prepull_unaura") or [])]
        self.summoned = []
        self.mob_source = "summon"
        self.last_summon_wall = ""      # the `.npc summon` send wall of the last claimed summon
        self.fight_start = None
        self.ended = ""
        self.drain_next = None

    def player_pulls(self):
        """The player pulls and fights unless the victim is the hired bot (the old bot-victim flow)."""
        return self.victim != "bot"

    def victim_target_guid(self):
        """The guid the governor heals/damages: the player, the main hire, or extra_bots[n]."""
        if self.victim == "player":
            return self.ctx.player_guid
        if self.extra_victim:
            extras = list((self.ctx.guids or {}).get("extras") or [])
            return norm_guid(extras[self.extra_victim - 1]) if self.extra_victim - 1 < len(extras) else ""
        return self.ctx.hire_guid

    # -- arena -------------------------------------------------------------

    def eligible_arenas(self):
        level = int(self.trial["player"].get("level") or 1)
        out = []
        for arena in self.arenas:
            lo, hi = int(arena.get("level_min", 1)), int(arena.get("level_max", 60))
            if lo <= level <= hi:
                out.append(arena)
        return out

    def arena_lock_for(self, arena):
        """The exclusive file lock for one arena (None when this situation names no server)."""
        if not self.server:
            return None
        return arena_lock.ArenaLock(self.server, arena.get("id"),
                                    base_dir=self.arena_state_dir)

    def release_arena(self):
        """Give the held arena back: the file lock and the in-process claim."""
        held, self._arena_held = getattr(self, "_arena_held", None), None
        arena = getattr(self, "current_arena", None)
        self.current_arena = None
        if held is not None:
            held.__exit__(None, None, None)
        if arena is not None:
            try:
                self.arena_free(arena)
            except Exception:  # noqa: BLE001 - teardown must not fail
                pass

    def held_arena(self):
        """Context manager: the repeat's whole arena span. The file lock and the
        in-process claim are held until the repeat's teardown is done, on every
        exit path including exceptions."""
        return _HeldArena(self)

    def _arena_once(self):
        """One pass over the rotated candidates: skip locked arenas WITHOUT probing
        them, take the first unlocked arena that is also quiet, and hold the lock."""
        rows = self.eligible_arenas()
        if rows:
            k = self.slot_index % len(rows)
            rows = rows[k:] + rows[:k]
        for arena in rows:
            if not self.arena_claim(arena):
                self.log("arena %s is held by another slot, next" % arena.get("id"))
                continue
            lock = self.arena_lock_for(arena)
            if lock is not None and not lock.acquire():
                self.arena_free(arena)
                self.log("arena %s locked by another slot, next" % arena.get("id"))
                continue
            held = _HeldArena(self, arena, lock)
            try:
                self.guard.send("go_xyz", None, int(arena["x"]), int(arena["y"]), int(arena["z"]), int(arena.get("map", 0)))
                time.sleep(2.5)
                reply = self.quiet_ok()
            except Exception:
                held.abandon()
                raise
            if reply:
                self.log("arena %s quiet" % arena.get("id"))
                self.current_arena = arena
                self._arena_held = held
                self.on_arena(arena)
                if self.ctx.bot_name:
                    bring_hire(self.agent, self.ctx.bot_name)
                for extra_name in self.ctx.extra_names:
                    bring_hire(self.agent, extra_name)
                return arena
            held.abandon()
            self.log("arena %s not quiet, next" % arena.get("id"))
        return None

    def arena(self):
        """One pass over the rotated candidates: skip locked arenas WITHOUT probing
        them, take the first unlocked arena that is also quiet, and hold the lock
        until the repeat's teardown is done (`with situation.held_arena()`,
        every exit path)."""
        return self._arena_once()

    def acquire_arena(self):
        """The quiet arena for this slot, waiting when every arena is locked or
        not quiet: retry every 15 s, give up after 10 min with None (the caller
        reports the existing "no quiet arena" outcome)."""
        start = time.time()
        while True:
            arena = self._arena_once()
            if arena is not None:
                return arena
            if time.time() - start >= ARENA_GIVE_UP_S:
                return None
            time.sleep(ARENA_WAIT_S)

    def quiet_ok(self):
        try:
            out = self.guard.tt("quiet 40 w=2")
        except Exception:  # noqa: BLE001
            return False
        for reply in (out or {}).get("replies") or []:
            if isinstance(reply, dict):
                if reply.get("quiet") is True and reply.get("final", True):
                    return True
            elif isinstance(reply, str) and '"quiet":true' in reply.replace(" ", ""):
                return True
        return False

    # -- summoning ---------------------------------------------------------

    def level_window(self):
        level = int(self.trial["player"].get("level") or 1)
        difficulty = self.trial.get("_difficulty")
        if difficulty:
            delta = int(difficulty["level_delta"])       # the ladder step's own value (0 is a real 0)
        else:
            delta = ladder.base_delta(self.sit.get("mobs"))
        target = level + delta
        return max(1, target - 1), min(60, target + 1)

    def summon(self, role, count, near_player=True):
        if getattr(self, "summon_blocked", False):
            return []
        lo, hi = self.level_window()
        entries = pick_entry(self.mobs, role, lo, hi)
        if not entries and role != "humanoid":
            entries = pick_entry(self.mobs, "melee", lo, hi)
        made = []
        tries = 0
        while len(made) < count and tries < min(5, max(1, len(entries)) * 2):
            row = entries[tries % len(entries)] if entries else None
            tries += 1
            if not row:
                break
            sent = {}
            guid = summon_mob(self.agent, self.guard, self.ledger, row["entry"],
                              foreign=self.foreign_summons, log=self.log,
                              arena=getattr(self, "current_arena", None),
                              now_iso=self._wall, sent=sent)
            if guid:
                self.last_summon_wall = sent.get("wall")
            if not guid:
                # an unclaimed summon may stand in the arena: another entry, or another refill, would only
                # add orphans (sweep-1b: 5 from one refill). Stop summoning for this repeat.
                self.summon_blocked = True
                break
            unit = wait_until(lambda: self.agent.unit(guid=guid), timeout=3.0, poll=0.3) or {}
            level = int(unit.get("level") or 0)
            if not unit.get("hostile") or not (lo <= level <= hi):
                kill_summon(self.agent, self.guard, self.ledger, guid)
                continue
            made.append(guid)
            self.summoned.append(guid)
            self.ctx.guids["mobs"] = [norm_guid(g) for g in self.summoned]
            self.ledger.add({"kind": "mob_ready", "guid": guid, "entry": int(row["entry"]),
                             "level": level, "hostile": True})
        return made

    def world_pull(self):
        """Fallback when `.npc summon` is refused: the nearest hostile in the level window."""
        lo, hi = self.level_window()
        # never another slot's summon (parallel runs), nor one this ledger already holds
        taken = {norm_guid(g) for g in (self.foreign_summons() or ()) if g} if self.foreign_summons else set()
        taken |= {norm_guid(g) for g in self.ledger.summon_guids() if g}
        candidates = [u for u in self.agent.units(within=70)
                      if u.get("hostile") and not u.get("dead") and lo <= int(u.get("level") or 0) <= hi
                      and (u.get("distance", 999) >= 0) and norm_guid(u.get("guid")) not in taken]
        if not candidates:
            return None
        candidates.sort(key=lambda u: u.get("distance", 999))
        guid = norm_guid(candidates[0]["guid"])
        self.ledger.add({"kind": "world_mob", "guid": guid, "entry": candidates[0].get("entry"),
                         "level": candidates[0].get("level")})
        self.summoned.append(guid)
        self.mob_source = "world"
        return guid

    # -- pulling -----------------------------------------------------------

    def target(self, guid):
        # moving the shared selection is a new generation: the guard must not restore
        # a captured previous target over it
        with self.agent.action_lock:
            self.agent.selection_gen += 1
            self.agent.rpc("target", {"guid": guid})

    def attack(self, guid):
        # the same lock the guard takes around select+send: the selection is shared
        with self.agent.action_lock:
            self.target(guid)
            self.agent.rpc("attack", {})

    def cc_targets(self):
        """cc_target: every summon except the marked (crowd-controlled) one."""
        marked = self.ctx.guids.get("marked")
        return [g for g in self.summoned if not marked or norm_guid(g) != norm_guid(marked)]

    def engage_marked(self, timeout=5.0):
        """cc_target: taunt the marked mob into combat before the fight counts.

        The CC chooser only sees mobs in combat with the group (CcTargetValue reads the group's
        attackers, which need InCombat), and a sheeped mob never fights on its own. Taunt (355,
        already .learned by the stance setup) deals no damage and is only ever sent here, never an
        attack on the marked guid. The player's last selection stays an unmarked mob: threat()
        casts on the selection and must never Taunt the sheep.
        """
        marked = self.ctx.guids.get("marked")
        if not marked:
            return
        self.walk_near(marked, 4.0)
        with self.agent.action_lock:
            self.target(marked)
            self.agent.cast(355, target=norm_guid(marked))
        if not wait_until(lambda: (self.agent.unit(guid=marked) or {}).get("in_combat"),
                          timeout=timeout, poll=0.5):
            with self.agent.action_lock:
                self.target(marked)
                self.agent.cast(355, target=norm_guid(marked))
            wait_until(lambda: (self.agent.unit(guid=marked) or {}).get("in_combat"),
                       timeout=timeout, poll=0.5)
        # the player's last selection is an unmarked mob: threat() casts on the selection
        # and must never Taunt the sheep
        unmarked = self.cc_targets()
        if unmarked:
            self.target(unmarked[0])

    def engage_cc_targets(self, timeout=10.0):
        """cc_target: the player attacks every UNMARKED summon, one after the other (the marked mob is
        never touched: damaging it would break the crowd control the trial watches), then mobs_ready is
        taken again and also needs every one of them in combat. Before this only summoned[0] was
        attacked, so a third mob could stay idle and the check could not tell."""
        targets = self.cc_targets()
        if not targets:
            return
        self.walk_near(targets[0], 25.0)
        for guid in targets:
            self.attack(guid)
        wait_until(lambda: all((self.agent.unit(guid=g) or {}).get("in_combat") for g in targets),
                   timeout=timeout, poll=0.5)
        lo, hi = self.level_window()
        row = mobs_ready_row(self.agent, self.ledger, (lo, hi), self.summoned,
                             engaged=targets, marked=self.ctx.guids.get("marked"))
        checks = self.ctx.checks
        for i, check in enumerate(checks):
            if check.get("id") == "mobs_ready":
                checks[i] = row
                break
        else:
            checks.append(row)

    def run_cc_pull(self):
        """cc_target pull order: mark_and_cc, engage_marked, then the unmarked attacks."""
        kind = self.sit.get("kind")
        if kind != "cc_target" or not self.summoned or len(self.summoned) < 2:
            return
        marked = self.summoned[1]
        mark_row = self.mark_and_cc(marked)
        self.ctx.checks.append(mark_row)
        self.ctx.guids["marked"] = marked
        self.engage_marked()
        self.engage_cc_targets()

    CC_GAP_TAIL_S = 3.0     # a refilled mob is summoned and pulled; it needs a moment to be the target

    def track_cc_gap(self, gap_start):
        """cc_target: an `excluded` span (why cc_gap) while no UNMARKED summon is alive, until the next one
        has been up for CC_GAP_TAIL_S. With only the sheep left it is the only possible target, and the mage
        nukes it by design (PossibleAttackTargetsValue.cpp:97-107): that is not a CC-respect failure.
        Returns the gap's start (an ISO wall time) while it is open, else None."""
        marked = self.ctx.guids.get("marked")
        try:
            alive = self.alive_now()
        except RpcError as exc:
            self.log("cc gap check skipped: alive count unknown (%s)" % exc)
            return gap_start
        unmarked = [g for g in alive if not marked or norm_guid(g) != norm_guid(marked)]
        if not unmarked and gap_start is None:
            return self._wall()
        if unmarked and gap_start is not None:
            self.marks.excluded(gap_start, self._wall(offset=self.CC_GAP_TAIL_S), "cc_gap")
            return None
        return gap_start

    def _wall(self, offset=0.0):
        stamp = getattr(self.marks, "now_iso", None)
        if stamp is not None:
            return stamp(offset)       # the rep's steady clock (see collect.Marks)
        import datetime
        return (datetime.datetime.now().astimezone()
                + datetime.timedelta(seconds=offset)).isoformat(timespec="milliseconds")

    def pull(self):
        for guid in self.summoned:
            self.walk_near(guid, 5.0)
            self.attack(guid)

    def walk_near(self, guid, want=25.0):
        unit = self.agent.unit(guid=guid) or {}
        st = self.agent.status()
        dx = float(unit.get("x", 0)) - float(st.get("x", 0))
        dy = float(unit.get("y", 0)) - float(st.get("y", 0))
        dist = math.hypot(dx, dy)
        if dist <= want or dist <= 0:
            return True
        k = max(0.0, dist - want) / dist
        self.agent.rpc("walk_to", {"x": float(st.get("x", 0)) + dx * k,
                                   "y": float(st.get("y", 0)) + dy * k,
                                   "z": unit.get("z", st.get("z", 0))}, timeout="40s")
        return True

    def threat(self):
        stance = self.trial["player"].get("stance")
        spell = THREAT_CAST.get(int(stance)) if stance else None
        if spell:
            # cast(target=0) uses the shared selection: same lock as select+send
            with self.agent.action_lock:
                self.agent.cast(spell, target=0)

    # -- drivers -----------------------------------------------------------

    def abort_reason(self):
        """A lost session or lease ends the repeat at once (checked every second)."""
        reason = self.abort() or ""
        if not reason:
            reason = str(getattr(self.collector, "lost", "") or "")
        return reason

    def run(self):
        kind = self.sit.get("kind") or "tank_damage"
        if kind == "ooc_buffs":
            return self.run_ooc()
        if kind == "ooc_hurt":
            return self.run_ooc_hurt()
        arena = self.acquire_arena()
        if not arena:
            return {"ok": False, "ended": "no arena", "invalid_reason": "no quiet arena"}
        mobs = self.sit.get("mobs") or {}
        role = mobs.get("role") or "melee"
        count = int(mobs.get("count") or 1)
        self.drain_next = None
        if self.prepull_unaura:
            failed = self.clear_prepull_auras()
            if failed:
                return {"ok": False, "ended": "prepull", "invalid_reason": failed}
        if self.sit.get("reset_hire_cooldowns"):
            self.reset_hire_cooldowns()
        made = self.summon(role, count)
        if not made and getattr(self, "summon_blocked", False):
            # a summon that went unclaimed may stand in the arena: fighting it as a world mob would leave a
            # non-ledger mob that nothing ever kills. The repeat is invalid; the ledger row names the unit.
            return {"ok": False, "ended": "no mobs", "invalid_reason": "summon unclaimed"}
        if not made:
            guid = self.world_pull()
            if not guid:
                return {"ok": False, "ended": "no mobs", "invalid_reason": "no mobs (summon refused and no world mob)"}
            made = [guid]
        self.ctx.guids["mobs"] = [norm_guid(g) for g in self.summoned]
        lo, hi = self.level_window()
        self.ctx.checks.append(mobs_ready_row(self.agent, self.ledger, (lo, hi), self.summoned))
        if self.victim == "bot" and self.ctx.bot_name:
            self.bring_hire_near()

        if kind == "cc_target" and len(self.summoned) >= 2:
            self.run_cc_pull()
        elif kind == "cc_target" and len(self.summoned) == 1:
            self.ctx.checks.append({"id": "cc_mark_set", "ok": False, "value": None,
                                    "evidence": "cc_target wants two humanoid mobs, got 1"})

        if kind == "debuff":
            debuff = (self.sit.get("debuff") or {}).get("spell_id")
            if debuff:
                self.apply_debuff(int(debuff))

        wait = float(self.sit.get("pull_after_s") or self.sit.get("ooc_wait_s") or 0)
        if wait:
            time.sleep(wait)

        if kind == "cc_target" and self.summoned:
            self.engage_cc_targets()
        elif self.player_pulls():
            self.pull()
        if kind == "pack":
            for guid in self.summoned[1:]:
                self.attack(guid)

        governor = Governor(self.agent, self.guard, self.marks, victim=self.victim,
                            band=self.band, floor=self.floor,
                            max_rescues=self.max_rescues,
                            victim_guid=self.victim_target_guid(),
                            top_up_to=self.sit.get("top_up_to_pct"),
                            top_up_gap=float(self.sit.get("damage_interval_s") or 4.0),
                            stopped=getattr(self.collector, "_stop", None), log=self.log)
        self.collector.tick_hooks.append(governor.tick)
        # an extra_bots victim leaves the player fighting ungoverned: a floor-only governor
        # (band top above 100: never a top-up) keeps the player from dying
        guard_gov = None
        if self.extra_victim:
            guard_gov = Governor(self.agent, self.guard, self.marks, victim="player",
                                 band=(float(self.band[0]), 101.0), floor=self.floor,
                                 max_rescues=self.max_rescues, victim_guid=self.ctx.player_guid,
                                 stopped=getattr(self.collector, "_stop", None), log=self.log)
            self.collector.tick_hooks.append(guard_gov.tick)

        deadline = None
        duration = float(self.trial.get("duration_s") or 60)
        refill_every = float(mobs.get("refill_every_s") or 0)
        next_refill = time.time() + refill_every if refill_every else 0.0
        last_threat = 0.0
        debuff_spell = (self.sit.get("debuff") or {}).get("spell_id") if kind == "debuff" else None
        debuff_gone_at = None
        ended = ""
        invalid = ""
        gap_start = None          # cc_target: since when no unmarked summon has been alive
        try:
            entered = time.time()
            while True:
                reason = self.abort_reason()
                if reason:
                    ended = "disconnected" if "disconnect" in reason.lower() else reason
                    invalid = reason
                    break
                now = time.time()
                if not self.fight_start and self.fighting(self.summoned):
                    self.fight_start = now
                    self.marks.add({"kind": "fight_start", "mobs": [norm_guid(g) for g in self.summoned]})
                    deadline = now + duration
                if not self.fight_start and now - entered > 60.0:
                    ended = "no_fight"
                    break
                if governor.ended:
                    ended = governor.ended
                    break
                if guard_gov is not None and guard_gov.ended:
                    ended = guard_gov.ended
                    break
                if deadline and now >= deadline:
                    break
                if kind == "cc_target" and self.fight_start:
                    gap_start = self.track_cc_gap(gap_start)
                if mobs.get("refill", True) and now - getattr(self, "_last_refill", 0) > 3:
                    try:
                        alive = len(self.alive_now())
                    except RpcError as exc:
                        # an unreadable unit list is not "0 alive": skip this tick
                        self.log("refill skipped: alive count unknown (%s)" % exc)
                        alive = None
                    if alive is not None and alive < count and len(self.summoned) < SUMMON_CAP:
                        self._last_refill = now
                        added = self.summon(role, 1)
                        if added:
                            self.add_refill_mark(added[0])
                            if self.player_pulls():
                                self.attack(added[0])
                if next_refill and now >= next_refill:
                    next_refill = now + refill_every
                    try:
                        alive = len(self.alive_now())
                    except RpcError as exc:
                        self.log("refill_every skipped: alive count unknown (%s)" % exc)
                        alive = None
                    if alive is not None and alive < REFILL_CAP:
                        added = self.summon((self.sit.get("mobs") or {}).get("role") or "melee", 1)
                        if added:
                            self.add_refill_mark(added[0], every=refill_every)
                            if self.player_pulls():
                                self.attack(added[0])
                if self.player_pulls() and now - last_threat > 6.0 and self.summoned:
                    last_threat = now
                    self.threat()
                if debuff_spell:
                    sample = self.collector.last_sample or {}
                    victim_auras = (sample.get(self.victim) or {}).get("auras") or []
                    if debuff_spell in victim_auras:
                        debuff_gone_at = None
                    elif debuff_gone_at is None:
                        debuff_gone_at = now + 10.0
                    elif now >= debuff_gone_at:
                        self.apply_debuff(int(debuff_spell))
                        debuff_gone_at = None
                if self.sit.get("energy_drain"):
                    self.drain_tick(now)
                time.sleep(1.0)
        finally:
            self.collector.tick_hooks.remove(governor.tick)
            if guard_gov is not None:
                self.collector.tick_hooks.remove(guard_gov.tick)
            if gap_start is not None:
                self.marks.excluded(gap_start, self._wall(), "cc_gap")
            self.marks.add({"kind": "fight_end", "ended": ended or "duration"})
        out = {"ok": not invalid, "ended": ended or "duration", "mob_source": self.mob_source,
               "fight_start": self.fight_start}
        if invalid:
            out["invalid_reason"] = invalid
        return out

    def alive_now(self):
        """This repeat's own summons that are still up.

        Not the slot ledger's whole history: a leftover of an earlier repeat (live2: a mob a
        misdirected `.modify hp` had revived) counted as alive for ever, so nothing was refilled
        and the fight ended with the first mob after 20 s. The player stands in the arena
        for the fight, so a despawned corpse ("not in view") is gone.
        """
        return alive_summons(self.agent, self.ledger, only=self.summoned, absent_is_gone=True)

    def clear_prepull_auras(self):
        """`prepull_unaura`: strip the listed auras off the hire before the pull.

        The hire is reused across repeats, so a form it cast in an earlier repeat is still up when
        the next one starts (bench-fix-1111: reps 2-3 of priest-shadow-form-prepull had Shadowform
        from the rep before, and the per-rep value was a coin flip). `.unaura <id>` on the hire makes
        every repeat test the out-of-combat cast. A refused line, a missing hire identity or an aura
        still up after a fresh read must not score the rep: it returns the reason and the caller ends
        the repeat INVALID before the summon (the old code logged the refusal and carried on, so the
        prepull rule scored a form the hire never lost).
        """
        if not self.ctx.hire_guid:
            why = "prepull unaura: no hire identity"
            self.marks.add({"kind": "prepull_unaura_failed", "reason": why})
            return why
        for spell in self.prepull_unaura:
            try:
                self.guard.send("unaura", self.ctx.hire_guid, int(spell))
            except Exception as exc:  # noqa: BLE001 - a refused line makes the rep INVALID
                self.log("prepull unaura %s refused: %s" % (spell, exc))
                why = "prepull unaura %s refused: %s" % (spell, str(exc)[:120])
                self.marks.add({"kind": "prepull_unaura_failed", "reason": why})
                return why
            self.marks.add({"kind": "prepull_unaura", "spell": int(spell)})
        wanted = {int(spell) for spell in self.prepull_unaura}

        def gone():
            have = self.hire_aura_ids()
            return have is not None and not (wanted & have)

        if not wait_until(gone, timeout=PREPULL_AURA_WAIT_S, poll=0.3):
            have = self.hire_aura_ids()
            if have is None:
                why = "prepull unaura: no fresh aura sample of the hire"
            else:
                why = "prepull unaura left aura(s) %s on the hire" % ", ".join(
                    str(s) for s in sorted(wanted & have))
            self.log(why)
            self.marks.add({"kind": "prepull_unaura_failed", "reason": why})
            return why
        return ""

    def hire_aura_ids(self):
        """The hire's aura ids from a fresh unit read (None when the agent cannot answer).

        A missing, null or partly malformed `auras` list is not an empty list: `gone()` reads an
        empty set as every aura absent, so the prepull rule scored a form the hire never lost.
        Only a list in which every entry parses counts as a sample.
        """
        try:
            unit = self.agent.unit(guid=self.ctx.hire_guid)
        except Exception:  # noqa: BLE001 - an unreadable unit is unknown, not absent
            return None
        if not isinstance(unit, dict):
            return None
        auras = unit.get("auras")
        if not isinstance(auras, list):
            return None
        out = set()
        for aura in auras:
            try:
                out.add(int(aura["spell_id"]))
            except (TypeError, ValueError, KeyError):
                return None
        return out

    def add_refill_mark(self, guid, **extra):
        """The `refill` marker, stamped with the `.npc summon` SEND wall (bench-fix-1111).

        A mark added after `summon()` returns carries the claim time, and the claim loop waits up
        to 5 s (plus 3 s for the unit row): rep3 of dps-mage-threat-cap stamped its refill 5.6 s
        after the summon and all 6 counted hits fell in that gap. With the send wall the marker
        precedes the new mob, so `not_within_s_after_mark` covers it.
        """
        row = {"kind": "refill", "guid": guid}
        if self.last_summon_wall:
            row["wall"] = self.last_summon_wall
        row.update(extra)
        return self.marks.add(row)

    def reset_hire_cooldowns(self):
        """`reset_hire_cooldowns`: clear the hire's cooldowns before the first summon.

        Item-use cooldowns (Thistle Tea's 5-min cooldown) live on the reused hire. A refused
        line is logged and the rep carries the `energy_drain_refused` mark; a rule with zero
        triggers then scores INCONCLUSIVE, not FAIL.
        """
        try:
            self.guard.send("cooldown", self.ctx.hire_guid)
            ok, evidence = True, "cooldown sent to the hire"
        except Exception as exc:  # noqa: BLE001 - a refused line is logged, not fatal
            ok, evidence = False, "cooldown refused: %s" % exc
            self.marks.add({"kind": "energy_drain_refused", "evidence": str(exc)[:120]})
        self.ctx.checks.append({"id": "hire_cooldowns_reset", "ok": bool(ok),
                                "value": None, "evidence": evidence})
        self.marks.add({"kind": "cooldowns_reset"})

    def drain_tick(self, now):
        """One `energy_drain` tick: empty the rogue hire's bar on schedule after fight_start."""
        cfg = self.sit.get("energy_drain")
        if not cfg or not self.fight_start:
            return
        if self.drain_next is None:
            self.drain_next = float(self.fight_start) + float(cfg.get("first_at_s") or 0)
        if float(now) < float(self.drain_next):
            return
        self.drain_next = float(now) + float(cfg.get("every_s") or 0)
        sample = (getattr(self.collector, "last_sample", None) or {}).get("bot") or {}
        try:
            max_power = int(sample.get("max_power") or 100)
        except (TypeError, ValueError):
            max_power = 100
        # the marker is stamped BEFORE the line goes out: the bot can answer within milliseconds
        # (rep4 of dps-rogue-thistle-tea drank 64 ms before a mark stamped after the send)
        wall = self._wall()
        try:
            self.guard.send("modify_energy", self.ctx.hire_guid, int(cfg.get("to") or 0), max_power)
        except Exception as exc:  # noqa: BLE001 - a refused line is logged, not fatal
            self.marks.add({"kind": "energy_drain_refused", "evidence": str(exc)[:120]})
            return
        self.marks.add({"kind": "energy_drain", "to": int(cfg.get("to") or 0), "wall": wall})

    def ooc_drink_settled(self):
        """Wait (bounded) for the re-drink after `.unaura all` stripped a setup drink.

        bench-fix-1112: `.unaura all` at fight_start strips a drink the hire started during setup,
        and the strip can leave the AI asleep in the meal (nightly rep1/rep3: first drink at 49.8).
        The caller saw a Drink aura on the hire before the strip. The window opens on the healthy
        immediate re-drink (rep2/rep4: back within ~2 s), or after DRINK_SETTLE_S with a log line
        and an `ooc_drink_settle_timeout` mark (the asleep bot then scores its FAIL, which the
        bot-side meal-sleep fix addresses).
        """
        self.log("ooc drink settle: stripped a setup drink, waiting for the re-drink")
        deadline = time.time() + DRINK_SETTLE_S
        while time.time() < deadline:
            if self.abort_reason():
                return
            have = self.hire_aura_ids()
            if have is not None and (have & DRINK_AURA_IDS):
                self.log("ooc drink settle: the hire re-drank, opening the window")
                return
            time.sleep(0.5)
        self.log("ooc drink settle: no re-drink in %.0f s, opening the window anyway" % DRINK_SETTLE_S)
        self.marks.add({"kind": "ooc_drink_settle_timeout"})

    def run_ooc(self):
        wait = float(self.sit.get("ooc_wait_s") or 60)
        have = self.hire_aura_ids() if self.ctx.hire_guid else set()
        if have is None and self.ctx.hire_guid:
            # one retry: a transient unit-read failure must not read as "no drink"
            have = self.hire_aura_ids()
        stripped = bool(have is not None and (have & DRINK_AURA_IDS))
        unknown = bool(self.ctx.hire_guid and have is None)
        for target in (self.ctx.player_guid, self.ctx.hire_guid):
            if target:
                try:
                    self.guard.send("unaura", target, "all")
                except Exception:  # noqa: BLE001 - a refused cleanup must not stop the window
                    pass
        if unknown:
            self.log("ooc drink settle: hire aura read unknown, waiting out a possible drink")
        if stripped or unknown:
            self.ooc_drink_settled()
        self.marks.add({"kind": "fight_start", "mobs": [], "ooc": True})
        deadline = time.time() + wait
        invalid = ""
        while time.time() < deadline:
            invalid = self.abort_reason()
            if invalid:
                break
            time.sleep(1.0)
        self.marks.add({"kind": "fight_end", "ended": "ooc" if not invalid else "disconnected"})
        if invalid:
            return {"ok": False, "ended": "disconnected", "invalid_reason": invalid}
        return {"ok": True, "ended": "ooc"}

    # -- ooc_hurt ----------------------------------------------------------

    def sample_key(self):
        """The collector sample key of the victim: player, bot, or extraN."""
        if self.extra_victim:
            return "extra%d" % self.extra_victim
        return "bot" if self.victim == "bot" else "player"

    def hurt_target_pct(self):
        low, high = float(self.band[0]), float(self.band[1])
        return (low + high) / 2.0

    def hurt_tick(self, sample):
        """One hurt attempt: ("set", pct) | ("in_band", pct) | ("skip", why) | ("wait", why).

        "wait" = no usable sample yet (retry next second, do not spend the tick); "skip" = the tick is
        spent and logged (a unit is in combat). The victim is set with the governor's own GM line
        (`.modify hp`, the guard's modify_hp) to the middle of hp_band.
        """
        sample = sample or {}
        victim = sample.get(self.sample_key()) or {}
        hire = sample.get("bot") or {}
        if not victim or not hire:
            return "wait", "no sample for the %s" % ("victim" if not victim else "hire")
        for who, row in (("victim", victim), ("hire", hire)):
            if row.get("in_combat"):
                return "skip", "%s in combat" % who
        max_hp = int(victim.get("max_hp") or 0)
        hp = int(victim.get("hp") or 0)
        if not max_hp or hp <= 0:
            return "wait", "victim hp unreadable"
        pct = self.hurt_target_pct()
        if float(self.band[0]) <= 100.0 * hp / max_hp <= float(self.band[1]):
            return "in_band", round(100.0 * hp / max_hp, 1)
        target = max(1, min(max_hp, int(pct * max_hp / 100.0)))
        self.guard.send("modify_hp", self.victim_target_guid(), target, max_hp)
        return "set", round(100.0 * target / max_hp, 1)

    def keep_stance(self, sample, state):
        """Out of combat nothing re-casts the stance (threat() only runs in a fight): do it here."""
        stance = self.trial["player"].get("stance")
        if not stance:
            return False
        have = ((sample or {}).get("player") or {}).get("auras")
        if have is None or int(stance) in have:
            return False
        now = time.time()
        if now - state.get("last", 0.0) < STANCE_RECAST_GAP_S:
            return False
        state["last"] = now
        try:
            with self.agent.action_lock:
                self.agent.cast(int(stance), target=norm_guid(self.ctx.player_guid) or 0)
        except Exception as exc:  # noqa: BLE001 - the stance uptime rule still scores it
            self.log("stance %s re-cast failed: %s" % (stance, exc))
            return False
        self.log("stance %s was down out of combat: re-cast" % stance)
        return True

    def run_ooc_hurt(self):
        """No mobs: the player and the hire stand in the quiet arena out of combat and the victim is
        set to a percent inside hp_band at the window start and every hurt_every_s seconds after.

        A tick is skipped (and logged) while the victim or the hire reads in combat in the
        collector's last sample. The floor governor stays on, floor-only, and logs loudly if it
        ever fires (out of combat nothing should damage the victim). No `unaura all`: the stance
        set up by the setup checks must stay up.
        """
        arena = self.acquire_arena()
        if not arena:
            return {"ok": False, "ended": "no arena", "invalid_reason": "no quiet arena"}
        wait = float(self.sit.get("ooc_wait_s") or 60)
        every = float(self.sit.get("hurt_every_s") or HURT_EVERY_S)
        governor = Governor(self.agent, self.guard, self.marks, victim=self.sample_key(),
                            band=(float(self.band[0]), 101.0), floor=self.floor,
                            max_rescues=self.max_rescues, victim_guid=self.victim_target_guid(),
                            stopped=getattr(self.collector, "_stop", None), log=self.log, ooc=True)
        self.collector.tick_hooks.append(governor.tick)
        self.marks.add({"kind": "fight_start", "mobs": [], "ooc": True})
        start = time.time()
        deadline = start + wait
        next_hurt = start
        invalid = ""
        ended = "ooc"
        stance_state = {}
        try:
            while time.time() < deadline:
                invalid = self.abort_reason()
                if invalid:
                    break
                if governor.ended:
                    ended = governor.ended
                    break
                sample = self.collector.last_sample or {}
                self.keep_stance(sample, stance_state)
                now = time.time()
                if now >= next_hurt:
                    try:
                        what, detail = self.hurt_tick(sample)
                    except Exception as exc:  # noqa: BLE001 - a refused line spends the tick
                        what, detail = "skip", "hurt line refused: %s" % exc
                    if what == "set":
                        self.marks.add({"kind": "hurt", "hp": detail})
                        next_hurt = now + every
                    elif what == "wait":
                        self.log("hurt tick waits: %s" % detail)
                    else:
                        self.log("hurt tick skipped: %s" % (detail if what == "skip"
                                                           else "victim already at %s%%, inside the band" % detail))
                        next_hurt = now + every
                time.sleep(1.0)
        finally:
            self.release_arena()      # the lock is held until the repeat's teardown is done
            if governor.tick in self.collector.tick_hooks:
                self.collector.tick_hooks.remove(governor.tick)
            self.marks.add({"kind": "fight_end", "ended": ended if not invalid else "disconnected"})
        if invalid:
            return {"ok": False, "ended": "disconnected", "invalid_reason": invalid}
        return {"ok": True, "ended": ended}

    def fighting(self, mobs):
        ids = {norm_guid(g) for g in mobs}
        for unit in self.agent.units(within=40):
            if norm_guid(unit.get("guid")) in ids and unit.get("in_combat") and not unit.get("dead"):
                return True
        return False

    def bring_hire_near(self):
        agent = self.agent
        wait_until(lambda: (agent.unit(name=self.ctx.bot_name) or {}).get("distance", 999) <= 10,
                   timeout=30.0, poll=1.0)

    def mark_and_cc(self, guid):
        """Put the moon on `guid` and tell the bot to crowd-control it.

        Two facts from the sweeps: the agent's `mark` answers {"icon": "moon", "guid": ...} (this code read
        a "mark" key and saw None on every rep), and `.bot cc moon <bot>` acts on the player's SELECTED
        target ("TBM:ACTION_ERR|cc moon|no-target|Your target is dead ... Select a live enemy first" on
        every rep after the first, when the selection was the previous rep's corpse). So the marked mob is
        selected first, under the same lock as the mark.
        """
        # mark reads and moves the shared raid marks: same lock as the GM selection
        with self.agent.action_lock:
            row = self.agent.rpc("mark", {"guid": guid, "icon": "moon"})
            self.target(guid)
        echo = None
        if isinstance(row, dict):
            echo = row.get("icon") or row.get("mark")
        intent = "cc moon %s" % self.ctx.bot_name
        since = self.agent.tbm(intent)
        # the build acks `TBM:ACTION_ACK|cc moon|bot:<name>|<count>|<name>` (the intent field is the command
        # WITHOUT its argument) and refuses with `TBM:ACTION_ERR|cc moon|<why>|<text>`
        line, _rows = self.agent.wait_reply(since, r"TBM:ACTION_(?:ACK|ERR)\|cc moon[ |]", timeout=10)
        ack = re.search(r"TBM:ACTION_ACK\|cc moon(?: " + re.escape(self.ctx.bot_name) + r")?\|[^|]*\|(\d+)\|",
                        line or "")
        self.ledger.add({"kind": "cc_mark", "guid": guid, "mark": echo or "", "bot": self.ctx.bot_name,
                         "reply": line or ""})
        err = ""
        if isinstance(row, dict) and row.get("_err"):
            err = "; mark: %s" % str(row["_err"])[:120]
        return {"id": "cc_mark_set", "ok": bool(echo) and bool(ack) and int(ack.group(1)) >= 1,
                "value": echo or line,
                "evidence": "mark %s; .bot %s -> %s%s" % (echo, intent, line or "no TBM:ACTION_ACK", err)}

    def apply_debuff(self, spell_id):
        target = self.victim_target_guid()
        try:
            self.guard.send("aura", target, int(spell_id))
        except Exception as exc:  # noqa: BLE001
            self.log("debuff refused: %s" % exc)

def mobs_ready_row(agent, ledger, window, guids, engaged=None, marked=None):
    """`engaged`: guids that must also be in combat (cc_target: every unmarked summon)."""
    lo, hi = window
    alive = []
    for guid in guids:
        unit = agent.unit(guid=guid) or {}
        if unit and unit.get("hostile") and not unit.get("dead") and lo <= int(unit.get("level") or 0) <= hi:
            alive.append(guid)
    ok = bool(alive)
    evidence = "%d/%d summons hostile, alive, level %d-%d" % (len(alive), len(guids), lo, hi)
    if engaged:
        fighting = [g for g in engaged if (agent.unit(guid=g) or {}).get("in_combat")]
        ok = ok and len(fighting) == len(engaged)
        evidence += "; %d/%d unmarked summons in combat" % (len(fighting), len(engaged))
    if marked:
        in_combat = bool((agent.unit(guid=marked) or {}).get("in_combat"))
        ok = ok and in_combat
        evidence += "; marked %s in combat: %s" % (marked, "yes" if in_combat else "no")
    return {"id": "mobs_ready", "ok": ok, "value": [norm_guid(g) for g in alive], "evidence": evidence}

