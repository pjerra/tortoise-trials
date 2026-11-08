"""Adaptive difficulty: a ladder of easier fights for a trial whose reps end on the rescue cap.

A rep that the HP governor ends as "floor" (max_rescues_per_min GM rescues inside 60 s) is INVALID.
When every rep of a control ends that way the trial proves nothing, so the NEXT rep of that trial
runs one step easier (never harder again within the run):

  step 0  as written
  step 1  mobs.level_delta - 1
  step 2  mobs.level_delta - 2   (the mob target level never goes below player level - 1)
  step 3  as step 2, plus refill off and count 1

`apply(trial, step)` returns a deep copy of the trial with the step's mobs settings and a
`_difficulty` record; the original trial (and the scored trial file) is never touched. The ladder
only changes the NEXT rep: a rep cut by the cap stays INVALID.

`situation.adaptive: false` turns the ladder (and the extra reps) off. The default is on for the
kinds that summon mobs and off for ooc_hurt / ooc_buffs (no mobs) and cc_target (it needs two
humanoid mobs, so a step 3 count of 1 would break it; an explicit `adaptive: true` keeps its count).
"""

import copy

MAX_STEP = 3
MAX_EXTRA_REPS = 2
LEVEL_FLOOR_DELTA = -1          # the mob target is never below player level - 1
NO_LADDER_KINDS = {"ooc_hurt", "ooc_buffs", "cc_target"}


def base_delta(mobs):
    """The level_delta a fight actually uses at step 0.

    A missing or 0 level_delta has always meant +1 in situations.level_window (`int(x or 1)`);
    the ladder starts from what really ran, so it reads the value the same way.
    """
    try:
        return int((mobs or {}).get("level_delta") or 1)
    except (TypeError, ValueError):
        return 1


def enabled(trial):
    """True when the trial's reps may step down."""
    sit = (trial or {}).get("situation") or {}
    flag = sit.get("adaptive")
    if flag is not None:
        return bool(flag)
    mobs = sit.get("mobs") or {}
    if mobs.get("count") == 0:
        return False
    return (sit.get("kind") or "tank_damage") not in NO_LADDER_KINDS


def params(trial, step):
    """The effective mobs settings at `step`: level_delta, count, refill, refill_every_s."""
    sit = (trial or {}).get("situation") or {}
    mobs = sit.get("mobs") or {}
    step = max(0, min(MAX_STEP, int(step or 0)))
    base = base_delta(mobs)
    level_delta = base
    if step:
        level_delta = max(base - min(step, 2), min(base, LEVEL_FLOOR_DELTA))
    count = mobs.get("count")
    refill = mobs.get("refill", True)
    refill_every = mobs.get("refill_every_s")
    if step >= 3:
        if (sit.get("kind") or "") != "cc_target":
            count = 1
        refill = False
        refill_every = None
    return {"step": step, "level_delta": level_delta, "count": count, "refill": refill,
            "refill_every_s": refill_every}


def apply(trial, step):
    """A copy of the trial that runs at `step` (step 0 changes nothing but records the settings)."""
    eff = copy.deepcopy(trial)
    sit = eff.setdefault("situation", {})
    mobs = sit.setdefault("mobs", {})
    got = params(trial, step)
    mobs["level_delta"] = got["level_delta"]
    if got["count"] is not None:
        mobs["count"] = got["count"]
    mobs["refill"] = got["refill"]
    if got["refill_every_s"] is None:
        mobs.pop("refill_every_s", None)
    else:
        mobs["refill_every_s"] = got["refill_every_s"]
    eff["_difficulty"] = dict(got, adaptive=enabled(trial))
    return eff


def capped(outcome):
    """True when the situation ended on the rescue cap."""
    return str((outcome or {}).get("ended") or "") == "floor"


class Ladder:
    """The per-trial-run state: current step, valid reps so far, extra reps used."""

    def __init__(self, trial, planned):
        self.on = enabled(trial)
        self.planned = max(1, int(planned or 1))
        self.step = 0
        self.valid = 0
        self.capped = 0
        self.extra = 0

    def record(self, valid, was_capped):
        """One finished rep: count it, and step down after a cap rep (adaptive only)."""
        if valid:
            self.valid += 1
        if was_capped:
            self.capped += 1
        if self.on and was_capped:
            self.step = min(MAX_STEP, self.step + 1)

    def want_extra(self):
        """After the planned reps: another rep while fewer than planned-1 are valid (max 2 extra).

        Only a trial that lost a rep to the rescue cap gets extras: a setup failure or an error
        repeats the same way, so extra reps would only burn server time.
        """
        if not self.on or self.extra >= MAX_EXTRA_REPS or not self.capped:
            return False
        if self.valid >= self.planned - 1:
            return False
        self.extra += 1
        return True
